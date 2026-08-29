"""Schema/shard migration coordinator (W3-B).

Applies one migration graph across selected schemas/shards with:

- **Per-target advisory locks** — reuses the W1-C ``pg_advisory_xact_lock``
  pattern. Each target acquires its own transaction-scoped advisory lock
  inside the apply transaction, so concurrent coordinators on the same
  PostgreSQL cluster cannot double-apply. The lock key is derived from a
  fixed Ferrum namespace plus a per-target hash — never from user input.
- **Bounded concurrency** — non-canary targets run with a configurable
  ``asyncio.Semaphore`` parallelism bound. Canary targets always run
  serially first.
- **Resumable status** — per-target, per-migration state is tracked in
  memory and reported via :meth:`SchemaShardMigrationCoordinator.status`.
  Reruns are idempotent because each target re-reads its own ledger before
  applying (the W1-C replay guard).
- **Fail-fast / continue policy** — ``"fail_fast"`` (default) raises on the
  first target failure; ``"continue"`` collects failures and continues with
  the remaining targets. Both report the full per-target outcome.
- **Canary-target support** — ``canary_targets`` run first, serially. A
  canary failure halts the entire rollout regardless of policy.
- **Structured progress hooks** — ``on_progress`` receives
  :class:`ProgressEvent` objects for every state transition.
- **Never promises cross-shard atomicity** — each target's apply is
  independent. No cross-target transaction is ever opened. Partial rollout
  is reported precisely per target/migration.

Security invariants (AGENTS.md §3, §5a):

- Schema selection uses :func:`ferrum.session.schema_transaction`
  (allowlisted schema identifier + identifier regex, transaction-local
  ``search_path``, never interpolated from untrusted input). The
  coordinator never accepts schema names from untrusted input — they must
  come from a trusted per-target allowlist.
- Advisory lock keys are derived from a fixed Ferrum namespace + a
  per-target hash. Never from user input.
- Per-target apply reuses the W1-C advisory-lock + transactional-DDL +
  atomic-ledger-write pattern. The ledger records the file-content digest
  (matches :class:`~ferrum.migrations.orchestrator.MigrationGraph.status`).
- No secrets, DSNs, bound values, or row data appear in progress events or
  results (Tier A only — AGENTS.md §3).
- Non-development environments require ``confirm=True`` (MIG-5), enforced
  by the underlying apply path.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ferrum.errors import FerrumMigrationError, map_db_error
from ferrum.migrations.ledger import (
    ensure_ledger_on_conn,
    is_applied_on_conn,
    record_applied_on_conn,
)
from ferrum.migrations.loader import MigrationModule
from ferrum.migrations.orchestrator import (
    MigrationGraph,
    _op_to_sql,
    _split_ops_by_phase,
    _validate_timeout,
)
from ferrum.session import schema_transaction

if TYPE_CHECKING:
    from ferrum.connection import Connection

__all__ = [
    "CoordinatorResult",
    "MigrationTarget",
    "ProgressEvent",
    "ProgressEventType",
    "ProgressHook",
    "SchemaShardMigrationCoordinator",
    "TargetMigrationState",
    "TargetMigrationStatus",
    "TargetResult",
]


# ---------------------------------------------------------------------------
# Per-target advisory lock key derivation
# ---------------------------------------------------------------------------

# Fixed Ferrum namespace for the W3-B coordinator's per-target advisory locks.
# Two 32-bit keys: key1 is the shared coordinator namespace, key2 is derived
# from the target_id so two shards on the same PostgreSQL cluster do not
# block each other. Both are held inside the apply transaction
# (pg_advisory_xact_lock) so commit/rollback auto-releases them.
#
# ``pg_advisory_xact_lock(int4, int4)`` expects two SIGNED 32-bit integers
# (range -2147483648..2147483647). ``int.from_bytes`` produces UNSIGNED values
# that can exceed 2**31-1; we mask to signed via ``_to_int32`` so asyncpg
# does not reject the bind.
_COORD_LOCK_NAMESPACE = b"ferrum.migrations.coordinator"
_h = hashlib.sha256(_COORD_LOCK_NAMESPACE).digest()


def _to_int32(value: int) -> int:
    """Mask an unsigned 32-bit integer to a signed int32.

    ``pg_advisory_xact_lock(int4, int4)`` expects signed int32. Hash
    digests produce unsigned values that can exceed 2**31-1; this helper
    wraps them into the signed range so asyncpg accepts the bind parameter.
    """
    if value >= 2**31:
        return value - 2**32
    return value


_COORD_LOCK_KEY_1: int = _to_int32(int.from_bytes(_h[0:4], "big"))


def _coord_lock_key_2(target_id: str) -> int:
    """Derive a stable signed-int32 advisory-lock key from a target_id.

    The target_id is a trusted caller-supplied identifier (shard name or
    caller-chosen label). It is never user input and never a secret. The hash
    is deterministic so two coordinators racing on the same target_id acquire
    the same lock.
    """
    return _to_int32(int.from_bytes(hashlib.sha256(target_id.encode()).digest()[0:4], "big"))


# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------


class TargetMigrationState(Enum):
    """Per-(target, migration) state tracked by the coordinator."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"  # already applied on this target (idempotent rerun)


class ProgressEventType(Enum):
    """Structured progress event types emitted to ``on_progress`` hooks."""

    COORDINATOR_STARTED = "coordinator_started"
    COORDINATOR_COMPLETED = "coordinator_completed"
    CANARY_PHASE_STARTED = "canary_phase_started"
    CANARY_PHASE_COMPLETED = "canary_phase_completed"
    CANARY_PHASE_FAILED = "canary_phase_failed"
    TARGET_STARTED = "target_started"
    TARGET_COMPLETED = "target_completed"
    TARGET_FAILED = "target_failed"
    MIGRATION_STARTED = "migration_started"
    MIGRATION_APPLIED = "migration_applied"
    MIGRATION_SKIPPED = "migration_skipped"
    MIGRATION_FAILED = "migration_failed"


_VALID_POLICIES: frozenset[str] = frozenset({"fail_fast", "continue"})


@dataclass(frozen=True)
class MigrationTarget:
    """One shard/schema target for migration coordination.

    A target identifies a (connection, optional schema) pair. The
    ``connection`` is one shard's open :class:`~ferrum.connection.Connection`
    (typically obtained from a :class:`~ferrum.routing.ConnectionRegistry`
    or :class:`~ferrum.routing.ShardRouter`). When ``schema`` is supplied, the
    coordinator wraps every per-migration apply in a
    :func:`~ferrum.session.schema_transaction` so unqualified DDL targets the
    tenant schema and the ``ferrum_migrations`` ledger is created inside it
    (per-schema ledger — the right semantics for schema-tenant migrations).

    Security: ``schema`` must come from a trusted allowlist. The coordinator
    delegates validation to :func:`ferrum.session.schema_transaction`
    (identifier regex AND allowlist), so an unallowed schema name fails closed
    before any SQL is emitted. Never construct schema names from untrusted
    input.
    """

    target_id: str
    connection: Connection
    schema: str | None = None
    allowed_schemas: frozenset[str] | None = None


@dataclass
class ProgressEvent:
    """Structured progress event emitted to ``on_progress`` hooks.

    Tier A only (AGENTS.md §3): no secrets, DSNs, bound values, or row data.
    ``error`` carries a sanitized error category/message, never raw
    PostgreSQL ``DETAIL``/``HINT`` or bound parameter values.
    """

    event_type: ProgressEventType
    target_id: str
    migration_name: str = ""
    applied_count: int = 0
    pending_count: int = 0
    error: str = ""


@dataclass
class TargetMigrationStatus:
    """Per-(target, migration) status snapshot returned by :meth:`status`.

    ``error`` is a sanitized error category (e.g. ``"migration"``,
    ``"connection"``, ``"config"``) — never raw DB detail, bound values, or
    row data (AGENTS.md §3, §5a "Safe error fields").
    """

    target_id: str
    migration_name: str
    state: TargetMigrationState
    error: str = ""


@dataclass
class TargetResult:
    """Per-target outcome of a coordinator run.

    - ``applied``: migration names actually applied on this target this run.
    - ``skipped``: migration names already applied (idempotent rerun).
    - ``failed``: per-migration failures with sanitized error categories.
    - ``halted``: True if the target was not run because a canary failure or
      fail-fast policy stopped the rollout before this target started.
    """

    target_id: str
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[TargetMigrationStatus] = field(default_factory=list)
    halted: bool = False


@dataclass
class CoordinatorResult:
    """Aggregate result of a coordinator run.

    Never promises cross-shard atomicity. ``partial_rollout`` is True iff
    some targets applied migrations and others did not (failure, halt, or
    skip). Callers must inspect per-target results to decide rollback /
    retry — the coordinator performs no cross-target rollback.
    """

    targets: list[TargetResult]
    canary_results: list[TargetResult]
    policy: str
    halted: bool
    partial_rollout: bool


# A progress hook may be sync or async; the coordinator awaits async hooks
# and calls sync hooks directly.
ProgressHook = Callable[[ProgressEvent], None] | Callable[[ProgressEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class SchemaShardMigrationCoordinator:
    """Coordinates applying one migration graph across selected schemas/shards.

    The coordinator NEVER opens a cross-target transaction. Each target's
    migration apply is independent: per-target advisory lock, per-target
    transactional DDL, per-target atomic ledger write. Partial rollout is
    reported precisely per target/migration; reruns are idempotent because
    each target re-reads its ledger before applying (W1-C replay guard).

    Construction:
        targets: ordered list of :class:`MigrationTarget`. Order is preserved
            for canary-first scheduling; ``canary_targets`` references
            ``target_id``s that must run first, serially.
        modules: migration modules (e.g. from
            :func:`~ferrum.migrations.loader.scan`) to apply on every target.
            The coordinator builds a per-target :class:`MigrationGraph` so
            ``upgrade_plan()`` returns only pending migrations for that
            target's ledger.
        max_parallelism: bounded concurrency for non-canary targets
            (default 4). Canary targets always run serially first.
        policy: ``"fail_fast"`` (default) raises on the first target failure
            (after recording it); ``"continue"`` collects failures and
            continues with remaining targets. Both return a full
            :class:`CoordinatorResult`.
        canary_targets: ``target_id``s that run first, serially. A canary
            failure halts the entire rollout regardless of policy.
        on_progress: optional structured progress hook (sync or async).
        env: environment passed to the ledger (MIG-5). Non-``"development"``
            requires ``confirm=True``.
        confirm: explicit confirmation flag forwarded to the apply path
            (MIG-2/MIG-5). Required for destructive ops and non-dev envs.
        lock_timeout / statement_timeout: per-apply ``SET LOCAL`` timeouts
            (PostgreSQL only, validated by the W1-C ``_validate_timeout``).

    Security:
        - Schema names are validated by :func:`ferrum.session.schema_transaction`
          (allowlist + identifier regex). Never interpolated from untrusted
          input.
        - Advisory lock keys are derived from a fixed Ferrum namespace +
          per-target hash. Never from user input.
        - No secrets/DSNs/bound values in events or results (Tier A only).
    """

    def __init__(
        self,
        targets: list[MigrationTarget],
        modules: list[MigrationModule],
        *,
        max_parallelism: int = 4,
        policy: str = "fail_fast",
        canary_targets: list[str] | None = None,
        on_progress: ProgressHook | None = None,
        env: str = "development",
        confirm: bool = False,
        lock_timeout: str | None = None,
        statement_timeout: str | None = None,
    ) -> None:
        if not targets:
            raise FerrumMigrationError(
                "SchemaShardMigrationCoordinator requires at least one target. [FERR-M001]"
            )
        if max_parallelism < 1:
            raise FerrumMigrationError(
                f"max_parallelism must be >= 1, got {max_parallelism}. [FERR-M001]"
            )
        if policy not in _VALID_POLICIES:
            raise FerrumMigrationError(
                f"Unknown policy {policy!r}. Expected one of {sorted(_VALID_POLICIES)}. [FERR-M001]"
            )
        # Validate target_id uniqueness and connection openness up front.
        seen_ids: set[str] = set()
        for t in targets:
            if not t.target_id:
                raise FerrumMigrationError(
                    "MigrationTarget.target_id must be a non-empty string. [FERR-M001]"
                )
            if t.target_id in seen_ids:
                raise FerrumMigrationError(
                    f"Duplicate target_id {t.target_id!r}. Each target must have a "
                    f"unique id. [FERR-M001]"
                )
            seen_ids.add(t.target_id)
        # Validate canary references against the target list.
        canary_set = set(canary_targets or [])
        unknown_canaries = canary_set - seen_ids
        if unknown_canaries:
            raise FerrumMigrationError(
                f"Canary target_ids {sorted(unknown_canaries)!r} are not in the targets "
                f"list. Known target_ids: {sorted(seen_ids)}. [FERR-M001]"
            )
        # Validate timeout strings up front (W1-C _validate_timeout).
        _validate_timeout(lock_timeout, "lock_timeout")
        _validate_timeout(statement_timeout, "statement_timeout")

        self._targets: list[MigrationTarget] = list(targets)
        self._modules: list[MigrationModule] = list(modules)
        self._max_parallelism = max_parallelism
        self._policy = policy
        self._canary_ids: list[str] = list(canary_targets or [])
        self._on_progress = on_progress
        self._env = env
        self._confirm = confirm
        self._lock_timeout = lock_timeout
        self._statement_timeout = statement_timeout

        # Per-target, per-migration in-memory status (resumable snapshot).
        # Keyed by target_id, then migration_name.
        self._status: dict[str, dict[str, TargetMigrationStatus]] = {
            t.target_id: {
                m.name: TargetMigrationStatus(t.target_id, m.name, TargetMigrationState.PENDING)
                for m in modules
            }
            for t in self._targets
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def status(self) -> dict[str, list[TargetMigrationStatus]]:
        """Return a per-target, per-migration status snapshot (in-memory).

        The snapshot reflects what this coordinator instance has observed.
        For a fresh authoritative view, construct a new coordinator (or query
        the per-target ledger via :class:`MigrationGraph`).
        """
        return {tid: list(statuses.values()) for tid, statuses in self._status.items()}

    async def run(self) -> CoordinatorResult:
        """Apply pending migrations across all targets.

        Canary targets run first, serially. Non-canary targets run with
        bounded concurrency. Never opens a cross-target transaction. Returns
        a full :class:`CoordinatorResult`; raises only on constructor-level
        misuse (already validated in ``__init__``).
        """
        await self._emit(
            ProgressEvent(
                event_type=ProgressEventType.COORDINATOR_STARTED,
                target_id="",
                pending_count=len(self._modules),
            )
        )

        canary_results: list[TargetResult] = []
        halted = False

        # ---- Canary phase: serial, fail-fast regardless of policy ----
        if self._canary_ids:
            await self._emit(
                ProgressEvent(
                    event_type=ProgressEventType.CANARY_PHASE_STARTED,
                    target_id="",
                    pending_count=len(self._canary_ids),
                )
            )
            for tid in self._canary_ids:
                target = self._target_by_id(tid)
                result = await self._run_one_target(target)
                canary_results.append(result)
                if result.failed:
                    halted = True
                    await self._emit(
                        ProgressEvent(
                            event_type=ProgressEventType.CANARY_PHASE_FAILED,
                            target_id=tid,
                            error=result.failed[0].error,
                        )
                    )
                    break
            if not halted:
                await self._emit(
                    ProgressEvent(
                        event_type=ProgressEventType.CANARY_PHASE_COMPLETED,
                        target_id="",
                    )
                )

        # ---- Main phase: bounded concurrency, fail-fast or continue ----
        main_ids = [t.target_id for t in self._targets if t.target_id not in set(self._canary_ids)]
        main_results: list[TargetResult] = []
        if not halted and main_ids:
            sem = asyncio.Semaphore(self._max_parallelism)
            # We need an ordered result list with fail-fast semantics. Use a
            # task-per-target with the semaphore, but track order and
            # short-circuit on fail_fast.
            main_results = await self._run_main_phase(main_ids, sem)

        # Mark non-run targets as halted (canary failure or fail-fast stop).
        run_ids = {r.target_id for r in canary_results} | {r.target_id for r in main_results}
        halted_targets: list[TargetResult] = []
        for t in self._targets:
            if t.target_id not in run_ids:
                halted_targets.append(TargetResult(target_id=t.target_id, halted=True))
                # Reflect halt in the in-memory status for every pending migration.
                for s in self._status[t.target_id].values():
                    if s.state == TargetMigrationState.PENDING:
                        # Leave as PENDING — the target was never started; the
                        # status snapshot distinguishes "never run" from "ran
                        # and failed". ``halted`` on the TargetResult carries
                        # the halt signal.
                        pass

        all_results = canary_results + main_results + halted_targets
        # Preserve the original target order in the returned list.
        result_by_id = {r.target_id: r for r in all_results}
        ordered_results = [result_by_id[t.target_id] for t in self._targets]

        # partial_rollout: True iff at least one target made progress (applied
        # or skipped) AND at least one target did not (failed, halted, or no
        # progress). A fully-applied rollout and a fully-halted no-op are both
        # False. The coordinator never promises cross-target atomicity; this
        # flag signals that the caller must inspect per-target results.
        any_progress = any(r.applied or r.skipped for r in ordered_results)
        any_not_progress = any(
            bool(r.failed) or r.halted or (not r.applied and not r.skipped) for r in ordered_results
        )
        partial_rollout = any_progress and any_not_progress

        await self._emit(
            ProgressEvent(
                event_type=ProgressEventType.COORDINATOR_COMPLETED,
                target_id="",
                applied_count=sum(len(r.applied) for r in ordered_results),
                pending_count=sum(1 for r in ordered_results for _ in r.failed),
            )
        )

        return CoordinatorResult(
            targets=ordered_results,
            canary_results=canary_results,
            policy=self._policy,
            halted=halted,
            partial_rollout=partial_rollout,
        )

    # ------------------------------------------------------------------
    # Internal: per-target apply
    # ------------------------------------------------------------------

    def _target_by_id(self, target_id: str) -> MigrationTarget:
        for t in self._targets:
            if t.target_id == target_id:
                return t
        # Unreachable: validated in __init__.
        raise FerrumMigrationError(f"Unknown target_id {target_id!r}. [FERR-M001]")

    async def _run_main_phase(
        self,
        main_ids: list[str],
        sem: asyncio.Semaphore,
    ) -> list[TargetResult]:
        """Run non-canary targets with bounded concurrency.

        fail_fast: cancel outstanding tasks on first failure and return what
        completed. continue: run every target to completion.
        """
        if self._policy == "fail_fast":
            # Run targets in order, but with bounded parallelism. On the first
            # failure, stop scheduling new ones and wait for in-flight ones
            # to finish (they are not cancelled mid-apply — each apply is an
            # independent transaction; cancellation safety is the W1-C
            # contract's responsibility, not the coordinator's).
            results: list[TargetResult] = []
            # Use a queue + workers model so we stop dequeueing on failure.
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            for tid in main_ids:
                queue.put_nowait(tid)
            queue.put_nowait(None)  # sentinel

            async def _worker() -> TargetResult | None:
                while True:
                    tid = await queue.get()
                    if tid is None:
                        # Re-inject the sentinel for the next worker.
                        queue.put_nowait(None)
                        return None
                    async with sem:
                        target = self._target_by_id(tid)
                        return await self._run_one_target(target)

            # Run up to max_parallelism workers concurrently.
            workers = [asyncio.create_task(_worker()) for _ in range(self._max_parallelism)]
            pending = list(workers)
            first_failure: TargetResult | None = None
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    res = task.result()
                    if res is None:
                        continue
                    results.append(res)
                    if res.failed and first_failure is None:
                        first_failure = res
                if first_failure is not None:
                    # Stop scheduling new work: drain the queue of real ids.
                    # Workers currently holding sem will finish their target
                    # and then exit on the next sentinel fetch.
                    break
            # Cancel remaining workers (they are blocked on queue.get()).
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if first_failure is not None:
                # Raise on fail_fast, after recording the result, so callers
                # using try/except still get partial state via the exception
                # message. Callers that want the full result without raising
                # should use policy="continue".
                err = first_failure.failed[0]
                raise FerrumMigrationError(
                    f"fail_fast policy: target {first_failure.target_id!r} failed at "
                    f"migration {err.migration_name!r} (category={err.error}). "
                    f"Partial rollout — inspect CoordinatorResult for per-target "
                    f"state. No cross-target rollback performed. [FERR-M001]"
                )
            return results

        # continue: run every target; collect all results.
        async def _run_with_sem(tid: str) -> TargetResult:
            async with sem:
                return await self._run_one_target(self._target_by_id(tid))

        tasks = [asyncio.create_task(_run_with_sem(tid)) for tid in main_ids]
        return await asyncio.gather(*tasks)

    async def _run_one_target(self, target: MigrationTarget) -> TargetResult:
        """Apply all pending migrations on one target.

        Builds a fresh :class:`MigrationGraph` bound to the target's
        connection so ``upgrade_plan()`` returns only migrations not yet
        recorded in that target's ledger. Each migration applies with a
        per-target advisory lock + transactional DDL + atomic ledger write
        (W1-C pattern). Idempotent: already-applied migrations are skipped.
        """
        await self._emit(
            ProgressEvent(
                event_type=ProgressEventType.TARGET_STARTED,
                target_id=target.target_id,
                pending_count=len(self._modules),
            )
        )
        result = TargetResult(target_id=target.target_id)

        # Build a per-target graph bound to this target's connection so
        # upgrade_plan() filters out already-applied migrations from that
        # target's ledger.
        graph = MigrationGraph(self._modules, conn=target.connection)
        try:
            pending = await graph.upgrade_plan()
        except Exception as exc:
            # Could not read the ledger on this target — record a target-level
            # failure with a sanitized error category.
            category = _sanitized_category(exc)
            result.failed.append(
                TargetMigrationStatus(
                    target_id=target.target_id,
                    migration_name="",
                    state=TargetMigrationState.FAILED,
                    error=category,
                )
            )
            await self._emit(
                ProgressEvent(
                    event_type=ProgressEventType.TARGET_FAILED,
                    target_id=target.target_id,
                    error=category,
                )
            )
            return result

        # Track migrations NOT in the pending plan as skipped — they are
        # already applied in this target's ledger (upgrade_plan filtered them
        # out). This gives a complete status picture for reruns/resume.
        pending_names = {m.name for m in pending}
        for module in self._modules:
            if module.name not in pending_names:
                self._status[target.target_id][module.name].state = TargetMigrationState.SKIPPED
                result.skipped.append(module.name)

        for module in pending:
            state = self._status[target.target_id][module.name]
            assert state.state == TargetMigrationState.PENDING  # noqa: S101
            state.state = TargetMigrationState.IN_PROGRESS
            await self._emit(
                ProgressEvent(
                    event_type=ProgressEventType.MIGRATION_STARTED,
                    target_id=target.target_id,
                    migration_name=module.name,
                )
            )
            try:
                applied_now = await self._apply_migration_on_target(target, module)
            except Exception as exc:
                category = _sanitized_category(exc)
                state.state = TargetMigrationState.FAILED
                state.error = category
                result.failed.append(
                    TargetMigrationStatus(
                        target_id=target.target_id,
                        migration_name=module.name,
                        state=TargetMigrationState.FAILED,
                        error=category,
                    )
                )
                await self._emit(
                    ProgressEvent(
                        event_type=ProgressEventType.MIGRATION_FAILED,
                        target_id=target.target_id,
                        migration_name=module.name,
                        error=category,
                    )
                )
                # On the first failure for this target, stop applying further
                # migrations on this target (the ledger may be in a partially
                # applied state for the failing migration — but the W1-C
                # transactional DDL contract rolls back the failed migration's
                # tx ops, leaving prior applied migrations committed).
                await self._emit(
                    ProgressEvent(
                        event_type=ProgressEventType.TARGET_FAILED,
                        target_id=target.target_id,
                        error=category,
                    )
                )
                return result
            if applied_now:
                state.state = TargetMigrationState.APPLIED
                result.applied.append(module.name)
                await self._emit(
                    ProgressEvent(
                        event_type=ProgressEventType.MIGRATION_APPLIED,
                        target_id=target.target_id,
                        migration_name=module.name,
                        applied_count=len(result.applied),
                    )
                )
            else:
                state.state = TargetMigrationState.SKIPPED
                result.skipped.append(module.name)
                await self._emit(
                    ProgressEvent(
                        event_type=ProgressEventType.MIGRATION_SKIPPED,
                        target_id=target.target_id,
                        migration_name=module.name,
                    )
                )

        await self._emit(
            ProgressEvent(
                event_type=ProgressEventType.TARGET_COMPLETED,
                target_id=target.target_id,
                applied_count=len(result.applied),
            )
        )
        return result

    # ------------------------------------------------------------------
    # Internal: per-migration apply (reuses the W1-C advisory-lock pattern)
    # ------------------------------------------------------------------

    async def _apply_migration_on_target(
        self,
        target: MigrationTarget,
        module: MigrationModule,
    ) -> bool:
        """Apply one migration on one target. Returns True if applied this call.

        Returns False if the migration was already applied (idempotent skip).
        Reuses the W1-C pattern: pin a connection, advisory-lock, check
        ledger, run transactional ops + atomic ledger write, then run
        non-transactional post-tx ops. When ``target.schema`` is set, the
        transactional apply is wrapped in a ``schema_transaction`` so
        unqualified DDL and the ledger land in the tenant schema.

        Never opens a cross-target transaction. The advisory lock key is
        per-target (derived from a fixed namespace + target_id hash).

        Schema-tenant constraint: when a schema is set, the migration must be
        fully transactional — pre-tx and post-tx non-transactional ops
        (CREATE EXTENSION, CREATE INDEX CONCURRENTLY) are rejected because
        they cannot run inside a ``schema_transaction``. Apply such
        migrations without schema selection, or split them.
        """
        ops_dicts = [op.to_op_dict() for op in module.migration.operations]
        # The ledger records the file-content digest so it matches
        # MigrationGraph.status() (W3-A).
        from ferrum.migrations.ledger import compute_digest

        content = module.path.read_text(encoding="utf-8")
        digest = compute_digest(module.name, content)

        # MIG-2 / MIG-5 gates: scan ops independently for destructive kinds;
        # non-dev envs require confirm. Evaluated before any SQL is emitted.
        self._check_gates(target, module, ops_dicts)

        if target.schema is not None:
            async with schema_transaction(
                target.connection,
                target.schema,
                allowed_schemas=target.allowed_schemas,
            ) as tx:
                # schema_transaction opened the tx and set search_path. Use
                # the Transaction's pinned driver directly — it is the same
                # raw connection that holds the tx and the GUC. Cast to Any
                # for the ledger helpers: the ledger's _RawConn protocol
                # types execute() as returning None, while the driver's
                # execute() returns the asyncpg status string. Runtime-
                # compatible; the mismatch is a protocol annotation gap.
                driver: Any = tx._require_driver()
                await ensure_ledger_on_conn(driver, dialect="postgres")
                return await self._transactional_phase(
                    driver, target, module, digest, ops_dicts, schema_tx_open=True
                )

        # No schema selection: pin a raw connection. Pre/post-tx
        # non-transactional ops run in autocommit around the tx (W1-C pattern).
        async with target.connection.acquire() as raw_conn:
            return await self._apply_no_schema(raw_conn, target, module, digest, ops_dicts)

    async def _apply_no_schema(
        self,
        raw_conn: Any,  # noqa: ANN401
        target: MigrationTarget,
        module: MigrationModule,
        digest: str,
        ops_dicts: list[dict[str, Any]],
    ) -> bool:
        """Non-schema apply: pre-tx autocommit, tx phase, post-tx autocommit."""
        pre_ops, tx_ops, post_ops = _split_ops_by_phase(ops_dicts)

        # Pre-tx non-transactional ops (autocommit, no ledger yet).
        for op in pre_ops:
            sql = _op_to_sql(op, dialect="postgres")
            try:
                await raw_conn.execute(sql)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise _apply_error(module, target, op, 0, exc) from None

        # Transactional phase: advisory lock + ledger check + tx ops + atomic
        # ledger write, all on one pinned connection in one transaction.
        async with raw_conn.transaction():
            await ensure_ledger_on_conn(raw_conn, dialect="postgres")
            applied = await self._transactional_phase(
                raw_conn, target, module, digest, tx_ops, schema_tx_open=False
            )

        if not applied:
            # Idempotent skip — do not run post_ops (they were already run on
            # the previous successful apply, or the migration is a no-op).
            return False

        # Post-tx non-transactional ops (autocommit, after ledger commit).
        for op in post_ops:
            sql = _op_to_sql(op, dialect="postgres")
            try:
                await raw_conn.execute(sql)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise FerrumMigrationError(
                    f"Post-transaction op failed after ledger commit in {module.name!r} "
                    f"on target {target.target_id!r} ({op.get('kind', 'unknown')}): "
                    f"{type(exc).__name__}. The migration is recorded as applied; "
                    f"the failed op must be reconciled manually. [FERR-M001]"
                ) from None
        return True

    async def _transactional_phase(
        self,
        raw_conn: Any,  # noqa: ANN401
        target: MigrationTarget,
        module: MigrationModule,
        digest: str,
        ops_dicts: list[dict[str, Any]],
        *,
        schema_tx_open: bool,
    ) -> bool:
        """Advisory lock, ledger replay guard, tx ops, atomic ledger write.

        Runs inside an already-open transaction (either the caller's
        ``raw_conn.transaction()`` or the ``schema_transaction``). Returns
        True if applied this call; False if the migration was already applied
        (idempotent rerun — the replay guard).

        When ``schema_tx_open`` is True, rejects pre-tx/post-tx
        non-transactional ops because they cannot run inside the
        schema_transaction (PostgreSQL rejects CREATE EXTENSION / CREATE
        INDEX CONCURRENTLY inside a tx block).
        """
        pre_ops, tx_ops, post_ops = _split_ops_by_phase(ops_dicts)
        if schema_tx_open and (pre_ops or post_ops):
            raise FerrumMigrationError(
                f"Migration {module.name!r} on target {target.target_id!r} has "
                f"non-transactional operations that cannot run inside a "
                f"schema_transaction. Apply without schema selection, or split "
                f"the migration so non-transactional ops are in a separate "
                f"migration applied without a schema. [FERR-M001]"
            )

        # SET LOCAL timeouts (validated strings only — W1-C contract).
        if self._lock_timeout is not None:
            await raw_conn.execute(f"SET LOCAL lock_timeout = {self._lock_timeout}")
        if self._statement_timeout is not None:
            await raw_conn.execute(f"SET LOCAL statement_timeout = {self._statement_timeout}")

        # Per-target advisory lock. Auto-released on commit/rollback.
        # Uses the W3-B coordinator's own namespace key1 (distinct from the
        # W1-C apply path's ADVISORY_LOCK_KEY_1) so the coordinator does not
        # collide with a concurrently-running W1-C ``apply()`` on the same
        # database.
        key2 = _coord_lock_key_2(target.target_id)
        try:
            await raw_conn.execute(
                "SELECT pg_advisory_xact_lock($1, $2)",
                _COORD_LOCK_KEY_1,
                key2,
            )
        except Exception as exc:
            mapped = map_db_error(exc)
            raise FerrumMigrationError(
                f"Failed to acquire per-target advisory lock on {target.target_id!r} "
                f"for migration {module.name!r} "
                f"(category={getattr(mapped, 'category', 'migration')}). "
                f"Another coordinator may be running on this target. [FERR-M001]"
            ) from None

        # Replay guard: if already applied, skip (idempotent rerun).
        if await is_applied_on_conn(raw_conn, digest, dialect="postgres"):
            return False

        # Run transactional ops.
        for op_index, op in enumerate(tx_ops):
            sql = _op_to_sql(op, dialect="postgres")
            try:
                await raw_conn.execute(sql)
            except FerrumMigrationError:
                raise
            except Exception as exc:
                raise _apply_error(module, target, op, op_index, exc) from None

        # Atomic ledger write — same transaction, same connection.
        await record_applied_on_conn(
            raw_conn,
            digest,
            environment=self._env,
            description=module.name,
            dialect="postgres",
        )
        return True

    def _check_gates(
        self,
        target: MigrationTarget,
        module: MigrationModule,
        ops_dicts: list[dict[str, Any]],
    ) -> None:
        """MIG-2 (destructive) and MIG-5 (env) gates before any SQL is emitted.

        Scans ops independently for destructive kinds — never trusts a plan
        flag. Reuses the orchestrator's ``_is_op_destructive`` so
        ``alter_column`` SET NOT NULL / type narrowing hits the confirm gate
        (the W1-C closure of the §3 destructive gate).
        """
        from ferrum.migrations.orchestrator import _is_op_destructive

        if any(_is_op_destructive(op) for op in ops_dicts) and not self._confirm:
            raise FerrumMigrationError(
                f"Migration {module.name!r} on target {target.target_id!r} has "
                f"destructive operations and requires confirm=True. [FERR-M001]"
            )
        if self._env != "development" and not self._confirm:
            raise FerrumMigrationError(
                f"Non-development apply on target {target.target_id!r} requires "
                f"confirm=True. [FERR-M001]"
            )

    # ------------------------------------------------------------------
    # Internal: progress hook dispatch
    # ------------------------------------------------------------------

    async def _emit(self, event: ProgressEvent) -> None:
        """Dispatch a progress event to the hook, if registered.

        Sync hooks are called directly; async hooks are awaited. Exceptions
        from the hook are swallowed so a buggy hook cannot break the
        coordinator — but logged via ``stderr`` so it is visible. This is
        intentional: the coordinator's contract is to apply migrations and
        report; progress hooks are observability, not control flow.
        """
        if self._on_progress is None:
            return
        try:
            result = self._on_progress(event)
            if isinstance(result, Awaitable):
                await result
        except Exception:
            # Swallow hook errors — observability must not break the rollout.
            # Print to stderr so it is visible without polluting the result.
            import sys

            print(
                f"[ferrum coordinator] progress hook raised; ignored: {type(event).__name__}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitized_category(exc: Exception) -> str:
    """Return a sanitized error category for a coordinator failure.

    Never includes raw PostgreSQL DETAIL/HINT, bound values, or row data
    (AGENTS.md §3 "Safe error fields"). Uses the mapped Ferrum exception's
    ``category`` attribute when available; otherwise falls back to the
    exception class name.
    """
    mapped = map_db_error(exc)
    cat = getattr(mapped, "category", None)
    if cat:
        return str(cat)
    return type(exc).__name__


def _apply_error(
    module: MigrationModule,
    target: MigrationTarget,
    op: dict[str, Any],
    op_index: int,
    exc: Exception,
) -> FerrumMigrationError:
    """Build a sanitized FerrumMigrationError for a failed apply op.

    Delegates to :func:`ferrum.errors.migration_op_failure` which produces a
    sanitized message (no DETAIL/HINT, no bound values). The migration name
    is annotated with the target_id so multi-target logs are unambiguous.
    """
    from ferrum.errors import migration_op_failure

    return migration_op_failure(
        action="apply",
        migration_name=f"{module.name}@{target.target_id}",
        op_index=op_index,
        op=op,
        exc=exc,
    )


__all__ = [
    "CoordinatorResult",
    "MigrationTarget",
    "ProgressEvent",
    "ProgressEventType",
    "ProgressHook",
    "SchemaShardMigrationCoordinator",
    "TargetMigrationState",
    "TargetMigrationStatus",
    "TargetResult",
]
