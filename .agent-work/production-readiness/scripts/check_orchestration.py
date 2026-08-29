from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

FULL_MIRRORS = (
    (".cursor/rules/project.md", ".claude/rules/project.md"),
    (
        ".cursor/rules/production-readiness-lifecycle.mdc",
        ".claude/rules/production-readiness-lifecycle.md",
    ),
    (
        ".cursor/rules/production-readiness-state.mdc",
        ".claude/rules/production-readiness-state.md",
    ),
    (".cursor/rules/architecture.md", ".claude/rules/architecture.md"),
    (
        ".cursor/skills/ferrum-readiness-orchestration/SKILL.md",
        ".claude/skills/ferrum-readiness-orchestration/SKILL.md",
    ),
    (
        ".cursor/skills/ferrum-readiness-execution/SKILL.md",
        ".claude/skills/ferrum-readiness-execution/SKILL.md",
    ),
    (
        ".cursor/skills/ferrum-readiness-verification/SKILL.md",
        ".claude/skills/ferrum-readiness-verification/SKILL.md",
    ),
    (
        ".cursor/skills/ferrum-readiness-loop/SKILL.md",
        ".claude/skills/ferrum-readiness-loop/SKILL.md",
    ),
    (
        ".cursor/commands/production-readiness-next.md",
        ".claude/commands/production-readiness-next.md",
    ),
    (
        ".cursor/commands/production-readiness-verify.md",
        ".claude/commands/production-readiness-verify.md",
    ),
    (
        ".cursor/commands/production-readiness-loop.md",
        ".claude/commands/production-readiness-loop.md",
    ),
    (
        ".cursor/commands/architecture-impact.md",
        ".claude/commands/architecture-impact.md",
    ),
    (".cursor/commands/design-feature.md", ".claude/commands/design-feature.md"),
    (
        ".cursor/commands/implement-feature.md",
        ".claude/commands/implement-feature.md",
    ),
    (".cursor/commands/review-code.md", ".claude/commands/review-code.md"),
    (
        ".cursor/goals/ferrum-production-readiness.md",
        ".claude/goals/ferrum-production-readiness.md",
    ),
    (
        ".cursor/loops/production-readiness.md",
        ".claude/loops/production-readiness.md",
    ),
)

AGENT_NAMES = (
    "production-readiness-coordinator",
    "production-readiness-executor",
    "production-readiness-verifier",
)

DOMAIN_AGENT_NAMES = (
    "chief-architect",
    "security-engineer",
    "code-reviewer",
    "product-manager",
    "product-designer",
    "test-engineer",
    "rust-core-engineer",
    "python-orm-engineer",
)

FORBIDDEN_CONTEXT = (
    ".claude/docs/PRODUCT_REQUIREMENTS.md",
    ".claude/docs/ARCHITECTURE.md",
    ".claude/docs/SECURITY.md",
    ".claude/docs/PRODUCT_DESIGN.md",
    ".claude/docs/DATA_MODELING.md",
    ".claude/docs/QUERY_ENGINE.md",
    ".claude/docs/MIGRATIONS.md",
    "PRODUCT_DESIGN.md",
    "PROJECT_STRUCTURE.md",
    "Authoritative docs: `.claude/docs/`",
    "Authoritative docs live in `.claude/docs/`",
    "undecided ADR",
    "open ADR",
    "ADR-001..006",
)

TASK_SECTIONS = ("## Specify", "## Plan", "## Tasks", "## Implement")

PROTOCOL_PHRASES = (
    "Specify → Plan → Tasks → Implement",
    "Load",
    "Execute",
    "Validate executor output",
    "Verify independently",
    "Update state",
    "SecurityEngineer",
    "ChiefArchitect",
    "shared paths",
    "reviews/<task-id>/<run-id>-<authority>.md",
)

HARD_STOP_FILES = (
    ".cursor/skills/ferrum-readiness-loop/SKILL.md",
    ".cursor/skills/ferrum-readiness-verification/SKILL.md",
    ".cursor/agents/production-readiness-verifier.md",
    ".cursor/commands/production-readiness-next.md",
    ".cursor/commands/production-readiness-verify.md",
    ".cursor/commands/production-readiness-loop.md",
)

HARD_STOP_PHRASES = (
    "SQL compilation",
    "migration apply",
    "errors/redaction",
    "auth/secrets",
    "RLS/admin GUCs",
    "schema selection",
)

SECURITY_SURFACES = (
    "sql_compilation",
    "migration_apply",
    "errors_redaction",
    "auth_secrets",
    "rls_admin_gucs",
    "schema_selection",
)

ALLOWED_MODELS = {
    "inherit",
    "claude-opus-5-thinking-medium",
    "claude-sonnet-5-thinking-high",
    "composer-2.5-fast",
    "cursor-grok-4.5-medium",
    "cursor-grok-4.6-high",
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "gemini-3.6-flash-medium",
    "gemini-3.7-flash-high",
    "gpt-5.6-luna-medium",
    "gpt-5.6-sol-medium",
}

AUTHORITY_METADATA = {
    "security_review": ("SecurityEngineer", "security-engineer"),
    "architecture_review": ("ChiefArchitect", "chief-architect"),
    "product_review": ("ProductManager", "product-manager"),
    "code_review": ("CodeReviewer", "code-reviewer"),
}

LEGACY_ARTIFACT_SCHEMAS = {
    ".agent-work/production-readiness/reviews/orchestration-bootstrap/"
    "20260821T075533Z-security-engineer.md": "review-v0",
    ".agent-work/production-readiness/reviews/orchestration-bootstrap/"
    "20260821T075533Z-chief-architect.md": "review-v0",
    ".agent-work/production-readiness/reviews/orchestration-bootstrap/"
    "20260821T075533Z-code-reviewer.md": "review-v0",
    ".agent-work/production-readiness/reviews/orchestration-bootstrap/"
    "20260821T080251Z-security-engineer.md": "review-v0",
    ".agent-work/production-readiness/verification/orchestration-bootstrap/"
    "20260821T073100Z.md": "verification-v0",
    ".agent-work/production-readiness/verification/orchestration-bootstrap/"
    "20260821T074435Z.md": "verification-v0",
    ".agent-work/production-readiness/verification/orchestration-bootstrap/"
    "20260821T080251Z.md": "verification-v1-missing-base-revision",
}


def _normalized(text: str) -> str:
    return text.replace(".claude/", ".cursor/")


def _agent_body(text: str) -> str:
    parts = text.split("---", 2)
    if len(parts) != 3:
        return text
    return _normalized(parts[2].strip())


def _frontmatter(text: str) -> str:
    parts = text.split("---", 2)
    return parts[1] if len(parts) == 3 else ""


def _yaml_list(text: str, key: str) -> tuple[str, ...]:
    match = re.search(
        rf"^{re.escape(key)}:\n(?P<paths>(?:[ \t]+- .+\n)+)",
        text,
        re.MULTILINE,
    )
    if match is None:
        return ()
    return tuple(
        re.sub(r"^[ \t]*-[ \t]+", "", line).strip().rstrip("/")
        for line in match.group("paths").splitlines()
    )


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def _path_covers(grant: str, target: str) -> bool:
    return target == grant or target.startswith(f"{grant}/")


def _lease_ids(text: str) -> set[str]:
    match = re.search(
        r"^shared_path_leases:\n(?P<leases>(?:  .+\n(?:    .+\n)*)+)",
        text,
        re.MULTILINE,
    )
    if match is None:
        return set()
    return {
        line.strip().removesuffix(":")
        for line in match.group("leases").splitlines()
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":")
    }


def _lease_records(text: str) -> dict[str, dict[str, str | tuple[str, ...]]]:
    records: dict[str, dict[str, str | tuple[str, ...]]] = {}
    lines = text.splitlines()
    try:
        start = lines.index("shared_path_leases:") + 1
    except ValueError:
        return records
    current_id: str | None = None
    paths: list[str] = []
    in_paths = False
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        if re.match(r"^  \S[^:]*:$", line):
            if current_id is not None:
                records[current_id]["paths"] = tuple(paths)
            current_id = line.strip().removesuffix(":")
            records[current_id] = {}
            paths = []
            in_paths = False
            continue
        if current_id is None:
            continue
        if line == "    paths:":
            in_paths = True
            continue
        if in_paths and line.startswith("      - "):
            paths.append(line.removeprefix("      - ").rstrip("/"))
            continue
        match = re.match(r"^    ([a-z_]+): (.+)$", line)
        if match is not None:
            in_paths = False
            records[current_id][match.group(1)] = match.group(2).strip()
    if current_id is not None:
        records[current_id]["paths"] = tuple(paths)
    return records


def _supersession_records(text: str) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    current_path: str | None = None
    in_supersessions = False
    for line in text.splitlines():
        if line == "supersessions:":
            in_supersessions = True
            continue
        if not in_supersessions:
            continue
        if line and not line.startswith(" "):
            break
        record_match = re.match(r"^  (.+):$", line)
        if record_match is not None:
            current_path = record_match.group(1)
            records[current_path] = {}
            continue
        field_match = re.match(r"^    ([a-z_]+): (.+)$", line)
        if current_path is not None and field_match is not None:
            records[current_path][field_match.group(1)] = field_match.group(2).strip()
    return records


def _scalar(text: str, key: str, *, indent: int = 0) -> str | None:
    match = re.search(
        rf"^{' ' * indent}{re.escape(key)}: (.+)$",
        text,
        re.MULTILINE,
    )
    return match.group(1).strip() if match is not None else None


def _flat_mapping(text: str, key: str) -> dict[str, str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"{key}:") + 1
    except ValueError:
        return {}
    result: dict[str, str] = {}
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([^:]+): (.+)$", line)
        if match is not None:
            result[match.group(1)] = match.group(2).strip()
    return result


def _duplicate_top_level_keys(frontmatter: str) -> set[str]:
    keys: list[str] = []
    for line in frontmatter.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if match is not None:
            keys.append(match.group(1))
    return {key for key in keys if keys.count(key) > 1}


def _is_utc_timestamp(value: str | None) -> bool:
    if value is None or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_internal_assumptions(errors: list[str]) -> None:
    adversarial = "---\ntask_id: expected\ndecision: changes_required\n---\ndecision: approved\n"
    metadata = _frontmatter(adversarial)
    if _scalar(metadata, "decision") != "changes_required":
        errors.append("frontmatter parser accepted body approval text")
    if not _paths_overlap(".github/workflows", ".github/workflows/nightly.yml"):
        errors.append("path overlap parser missed nested shared path")
    if _paths_overlap("AGENTS.md", "mise.toml"):
        errors.append("path overlap parser conflated disjoint paths")
    if not _path_covers(".github/workflows", ".github/workflows/nightly.yml"):
        errors.append("lease coverage parser missed nested owned path")
    if _path_covers(".github/workflows/nightly.yml", ".github/workflows/ci.yml"):
        errors.append("lease coverage parser widened a file grant")
    if _duplicate_top_level_keys("decision: approved\ndecision: blocked") != {"decision"}:
        errors.append("duplicate metadata parser missed conflicting decision")
    if not _is_utc_timestamp("2026-08-21T08:00:00Z"):
        errors.append("UTC timestamp parser rejected canonical timestamp")
    if _is_utc_timestamp("2026-08-21T08:00:00+03:00"):
        errors.append("UTC timestamp parser accepted non-canonical timezone")


def main() -> int:
    errors: list[str] = []
    ownership: dict[str, tuple[str, ...]] = {}
    task_frontmatters: dict[str, str] = {}
    workstream_states: dict[str, str] = {}
    _validate_internal_assumptions(errors)

    for cursor_path, claude_path in FULL_MIRRORS:
        cursor_file = ROOT / cursor_path
        claude_file = ROOT / claude_path
        if not cursor_file.is_file() or not claude_file.is_file():
            errors.append(f"missing mirror: {cursor_path} or {claude_path}")
            continue
        cursor_text = _normalized(cursor_file.read_text())
        claude_text = _normalized(claude_file.read_text())
        if cursor_text != claude_text:
            errors.append(f"mirror drift: {cursor_path} != {claude_path}")

    for name in AGENT_NAMES:
        cursor_file = ROOT / ".cursor" / "agents" / f"{name}.md"
        claude_file = ROOT / ".claude" / "agents" / f"{name}.md"
        if not cursor_file.is_file() or not claude_file.is_file():
            errors.append(f"missing agent mirror: {name}")
            continue
        if _agent_body(cursor_file.read_text()) != _agent_body(claude_file.read_text()):
            errors.append(f"agent body drift: {name}")
        for agent_file in (cursor_file, claude_file):
            metadata = _frontmatter(agent_file.read_text())
            if not re.search(r"^name: \S+", metadata, re.MULTILINE):
                errors.append(f"agent missing name metadata: {agent_file.relative_to(ROOT)}")
            if not re.search(r"^description: (?:\S|>-)", metadata, re.MULTILINE):
                errors.append(f"agent missing description metadata: {agent_file.relative_to(ROOT)}")
            model = _scalar(metadata, "model")
            if model not in ALLOWED_MODELS:
                errors.append(
                    f"agent has unsupported model metadata: {agent_file.relative_to(ROOT)}:{model}"
                )

    for name in DOMAIN_AGENT_NAMES:
        for platform in (".cursor", ".claude"):
            agent_file = ROOT / platform / "agents" / f"{name}.md"
            if not agent_file.is_file():
                errors.append(f"missing domain agent: {platform}/{name}")
                continue
            agent_text = agent_file.read_text()
            metadata = _frontmatter(agent_text)
            if not re.search(r"^name: \S+", metadata, re.MULTILINE):
                errors.append(f"agent missing name metadata: {platform}/agents/{name}")
            if not re.search(r"^description: (?:\S|>-)", metadata, re.MULTILINE):
                errors.append(f"agent missing description metadata: {platform}/agents/{name}")
            model = _scalar(metadata, "model")
            if model not in ALLOWED_MODELS:
                errors.append(
                    f"agent has unsupported model metadata: {platform}/agents/{name}:{model}"
                )
            for stale_text in FORBIDDEN_CONTEXT:
                if stale_text.casefold() in agent_text.casefold():
                    errors.append(f"stale context in {platform}/agents/{name}: {stale_text}")

    for platform in (".cursor", ".claude"):
        security_agent = (ROOT / platform / "agents" / "security-engineer.md").read_text()
        for phrase in HARD_STOP_PHRASES:
            if phrase.casefold() not in security_agent.casefold():
                errors.append(f"{platform} security agent missing trigger: {phrase}")

    protocol = (ROOT / ".agent-work/production-readiness/PROTOCOL.md").read_text()
    for phrase in PROTOCOL_PHRASES:
        if phrase.casefold() not in protocol.casefold():
            errors.append(f"protocol missing phrase: {phrase}")

    for relative_path in HARD_STOP_FILES:
        text = (ROOT / relative_path).read_text()
        for phrase in HARD_STOP_PHRASES:
            if phrase.casefold() not in text.casefold():
                errors.append(f"{relative_path} missing hard stop: {phrase}")

    index_text = (ROOT / ".agent-work/production-readiness/state/index.yaml").read_text()
    shared_paths = _yaml_list(index_text, "shared_paths")
    allowed_statuses = set(_yaml_list(index_text, "allowed_statuses"))
    aggregate_statuses = _flat_mapping(index_text, "workstreams")
    lease_records = _lease_records(index_text)
    lease_ids = set(lease_records)
    if lease_ids != _lease_ids(index_text):
        errors.append("shared lease parser disagreement")
    for lease_id, lease in lease_records.items():
        if lease.get("lease_id") != lease_id:
            errors.append(f"lease id mismatch: {lease_id}")
        for key in (
            "workstream",
            "run_id",
            "holder",
            "acquired_at",
            "expires_at",
            "paths",
        ):
            if not lease.get(key):
                errors.append(f"lease missing {key}: {lease_id}")
        try:
            acquired_at = datetime.fromisoformat(
                str(lease.get("acquired_at", "")).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(lease.get("expires_at", "")).replace("Z", "+00:00")
            )
            if expires_at <= acquired_at:
                errors.append(f"lease expiry is not after acquisition: {lease_id}")
            if expires_at <= datetime.now(UTC):
                errors.append(f"lease is expired: {lease_id}")
        except ValueError:
            errors.append(f"lease timestamp is invalid: {lease_id}")
    lease_id_list = sorted(lease_records)
    for index, left_id in enumerate(lease_id_list):
        for right_id in lease_id_list[index + 1 :]:
            left_paths = lease_records[left_id].get("paths", ())
            right_paths = lease_records[right_id].get("paths", ())
            if not isinstance(left_paths, tuple) or not isinstance(right_paths, tuple):
                continue
            for left_path in left_paths:
                for right_path in right_paths:
                    if _paths_overlap(left_path, right_path):
                        errors.append(
                            f"shared lease overlap: {left_id}:{left_path} "
                            f"with {right_id}:{right_path}"
                        )
    tasks_dir = ROOT / ".agent-work/production-readiness/tasks"
    for task_file in sorted(tasks_dir.glob("*.md")):
        if task_file.name == "TEMPLATE.md":
            continue
        task_text = task_file.read_text()
        task_frontmatter = _frontmatter(task_text)
        for section in TASK_SECTIONS:
            if section not in task_text:
                errors.append(f"{task_file.relative_to(ROOT)} missing {section}")
        task_match = re.search(r"^task_id: (\S+)$", task_text, re.MULTILINE)
        if task_match is None:
            errors.append(f"{task_file.relative_to(ROOT)} missing task_id")
            continue
        task_id = task_match.group(1)
        ownership[task_id] = _yaml_list(task_text, "owned_paths")
        task_frontmatters[task_id] = task_frontmatter
        state_file = ROOT / ".agent-work/production-readiness/state/workstreams" / f"{task_id}.yaml"
        if not state_file.is_file():
            errors.append(f"missing workstream state: {state_file.relative_to(ROOT)}")
            continue
        state_text = state_file.read_text()
        workstream_states[task_id] = state_text
        status = _scalar(state_text, "status")
        task_status = _scalar(task_frontmatter, "status")
        aggregate_status = aggregate_statuses.get(task_id)
        if status not in allowed_statuses:
            errors.append(f"workstream uses unsupported status: {task_id}:{status}")
        if task_status != status or aggregate_status != status:
            errors.append(
                f"status drift: {task_id}:task={task_status},"
                f"state={status},aggregate={aggregate_status}"
            )
        for identity_key in ("owner", "run_id", "shared_path_lease"):
            task_value = _scalar(task_frontmatter, identity_key) or "null"
            state_value = _scalar(state_text, identity_key) or "null"
            if task_value != state_value:
                errors.append(f"assignment drift: {task_id}:{identity_key}")
        if status == "ready" and _scalar(state_text, "owned_paths_checked") != "true":
            errors.append(f"ready task has unchecked ownership: {task_id}")
        if status in ("ready", "in_progress", "validating", "awaiting_verification"):
            if _scalar(task_frontmatter, "security_triage_complete") != "true":
                errors.append(f"active task lacks completed security triage: {task_id}")
            if _scalar(state_text, "security_triage_complete") != "true":
                errors.append(f"active state lacks completed security triage: {task_id}")
        task_surface_values = {
            surface: _scalar(task_frontmatter, surface, indent=2) for surface in SECURITY_SURFACES
        }
        state_surface_values = {
            surface: _scalar(state_text, surface, indent=2) for surface in SECURITY_SURFACES
        }
        for surface in SECURITY_SURFACES:
            if task_surface_values[surface] not in ("true", "false"):
                errors.append(f"task has invalid security surface: {task_id}:{surface}")
            if state_surface_values[surface] not in ("true", "false"):
                errors.append(f"state has invalid security surface: {task_id}:{surface}")
            if task_surface_values[surface] != state_surface_values[surface]:
                errors.append(f"security surface drift: {task_id}:{surface}")
        task_sensitive = "true" in task_surface_values.values()
        if task_sensitive and _scalar(state_text, "security_review") != "required":
            errors.append(f"security-sensitive task lacks required review: {task_id}")
        if not task_sensitive:
            task_justification = _scalar(
                task_frontmatter,
                "security_review_justification",
            )
            state_justification = _scalar(state_text, "security_review_justification")
            if task_justification in (None, "null", "replace-me"):
                errors.append(f"task lacks security-review justification: {task_id}")
            if state_justification in (None, "null"):
                errors.append(f"state lacks security-review justification: {task_id}")
        active = any(
            status == active_status
            for active_status in ("in_progress", "validating", "awaiting_verification")
        )
        touches_shared = any(
            _paths_overlap(owned_path, shared_path)
            for owned_path in ownership[task_id]
            for shared_path in shared_paths
        )
        lease_match = re.search(r"^shared_path_lease: (.+)$", state_text, re.MULTILINE)
        lease_id = lease_match.group(1).strip() if lease_match is not None else "null"
        if active and touches_shared:
            if lease_id == "null":
                errors.append(f"active shared-path task lacks lease: {task_id}")
            elif lease_id not in lease_ids:
                errors.append(f"active shared-path task references unknown lease: {task_id}")
            else:
                lease = lease_records[lease_id]
                if lease.get("workstream") != task_id:
                    errors.append(f"active task references another workstream lease: {task_id}")
                if lease.get("run_id") != _scalar(state_text, "run_id"):
                    errors.append(f"active task run does not match lease: {task_id}")
                if lease.get("holder") != _scalar(state_text, "owner"):
                    errors.append(f"active task owner does not match lease holder: {task_id}")
                lease_paths = lease.get("paths", ())
                if isinstance(lease_paths, tuple):
                    required_paths = tuple(
                        owned_path
                        for owned_path in ownership[task_id]
                        if any(
                            _paths_overlap(owned_path, shared_path) for shared_path in shared_paths
                        )
                    )
                    for required_path in required_paths:
                        if not any(
                            _path_covers(lease_path, required_path) for lease_path in lease_paths
                        ):
                            errors.append(
                                f"active lease does not cover owned shared path: "
                                f"{task_id}:{required_path}"
                            )
        if status == "ready" and touches_shared:
            next_action = _scalar(state_text, "next_action") or ""
            if "acquire" not in next_action.casefold() or "lease" not in next_action.casefold():
                errors.append(f"ready shared-path task lacks lease instruction: {task_id}")
        for review_name in (
            "security_review",
            "architecture_review",
            "product_review",
            "code_review",
        ):
            task_required = f"{review_name}: true" in task_text.split("---", 2)[1]
            state_required = f"{review_name}: required" in state_text
            if task_required != state_required:
                errors.append(f"review flag drift: {task_id}:{review_name}")
            if status in ("verified", "complete") and state_required:
                review_path = _scalar(state_text, review_name, indent=2)
                if review_path in (None, "null"):
                    errors.append(f"completed task lacks review artifact: {task_id}:{review_name}")
                    continue
                authority, authority_slug = AUTHORITY_METADATA[review_name]
                run_id = _scalar(state_text, "run_id")
                expected_path = (
                    f".agent-work/production-readiness/reviews/{task_id}/"
                    f"{run_id}-{authority_slug}.md"
                )
                review_file = ROOT / review_path
                if not review_file.is_file():
                    errors.append(f"completed task review is missing: {task_id}:{review_name}")
                    continue
                review_metadata = _frontmatter(review_file.read_text())
                review_run = _scalar(review_metadata, "run_id")
                same_run = review_path == expected_path and review_run == run_id
                prior_run_ok = (
                    review_run is not None
                    and re.fullmatch(r"\d{8}T\d{6}Z", review_run) is not None
                    and run_id is not None
                    and review_run <= run_id
                )
                valid_review = (
                    _scalar(review_metadata, "task_id") == task_id
                    and _scalar(review_metadata, "authority") == authority
                    and _scalar(review_metadata, "decision") == "approved"
                    and (same_run or prior_run_ok)
                )
                if not valid_review:
                    errors.append(f"completed task has unapproved review: {task_id}:{review_name}")
        if status in ("verified", "complete"):
            verification_path = _scalar(state_text, "latest_verification", indent=2)
            if verification_path in (None, "null"):
                errors.append(f"completed task lacks independent verification: {task_id}")
            else:
                run_id = _scalar(state_text, "run_id")
                expected_verification_path = (
                    f".agent-work/production-readiness/verification/{task_id}/{run_id}.md"
                )
                if verification_path != expected_verification_path:
                    errors.append(f"completed task verification path mismatch: {task_id}")
                    continue
                verification_file = ROOT / verification_path
                if not verification_file.is_file():
                    errors.append(f"completed task verification is missing: {task_id}")
                    continue
                verification_metadata = _frontmatter(verification_file.read_text())
                valid_verification = (
                    _scalar(verification_metadata, "task_id") == task_id
                    and _scalar(verification_metadata, "run_id") == run_id
                    and _scalar(verification_metadata, "decision") == "verified"
                )
                if not valid_verification:
                    errors.append(f"completed task verification is not passing: {task_id}")

    leased_workstreams: set[str] = set()
    for lease_id, lease in lease_records.items():
        workstream = str(lease.get("workstream", ""))
        if workstream in leased_workstreams:
            errors.append(f"workstream has multiple live leases: {workstream}")
        leased_workstreams.add(workstream)
        if workstream not in ownership or workstream not in workstream_states:
            errors.append(f"live lease references unknown workstream: {lease_id}")
            continue
        state_text = workstream_states[workstream]
        task_frontmatter = task_frontmatters[workstream]
        if _scalar(state_text, "shared_path_lease") != lease_id:
            errors.append(f"live lease is not referenced by workstream state: {lease_id}")
        if _scalar(task_frontmatter, "shared_path_lease") != lease_id:
            errors.append(f"live lease is not referenced by task contract: {lease_id}")
        if lease.get("run_id") != _scalar(state_text, "run_id"):
            errors.append(f"live lease run does not match workstream: {lease_id}")
        if lease.get("holder") != _scalar(state_text, "owner"):
            errors.append(f"live lease holder does not match workstream owner: {lease_id}")
        if lease.get("holder") != _scalar(task_frontmatter, "owner"):
            errors.append(f"live lease holder does not match task owner: {lease_id}")
        lease_paths = lease.get("paths", ())
        if not isinstance(lease_paths, tuple):
            errors.append(f"live lease paths are malformed: {lease_id}")
            continue
        required_paths = tuple(
            owned_path
            for owned_path in ownership[workstream]
            if any(_paths_overlap(owned_path, shared_path) for shared_path in shared_paths)
        )
        for required_path in required_paths:
            if not any(_path_covers(lease_path, required_path) for lease_path in lease_paths):
                errors.append(
                    f"live lease does not cover owned shared path: {lease_id}:{required_path}"
                )
        for lease_path in lease_paths:
            if not any(_path_covers(lease_path, path) for path in required_paths):
                errors.append(
                    f"live lease path is outside workstream ownership: {lease_id}:{lease_path}"
                )

    # Verified/complete/reverted/blocked workstreams have released exclusive
    # ownership. Later waves must reuse queryset.py, errors.py, etc.
    terminal_ownership = frozenset({"verified", "complete", "reverted", "blocked"})
    task_ids = sorted(ownership)
    for index, left_id in enumerate(task_ids):
        left_status = _scalar(workstream_states.get(left_id, ""), "status")
        if left_status in terminal_ownership:
            continue
        for right_id in task_ids[index + 1 :]:
            right_status = _scalar(workstream_states.get(right_id, ""), "status")
            if right_status in terminal_ownership:
                continue
            for left_path in ownership[left_id]:
                for right_path in ownership[right_id]:
                    if _paths_overlap(left_path, right_path):
                        errors.append(
                            f"ownership overlap: {left_id}:{left_path} with {right_id}:{right_path}"
                        )

    evidence_root = ROOT / ".agent-work/production-readiness"
    for category in ("logs", "verification", "reviews"):
        for evidence_file in (evidence_root / category).glob("*/*.md"):
            if evidence_file.name == "TEMPLATE.md":
                continue
            relative_path = evidence_file.relative_to(ROOT).as_posix()
            evidence_text = evidence_file.read_text()
            metadata = _frontmatter(evidence_text)
            artifact_task_id = evidence_file.parent.name
            run_prefix = evidence_file.stem.split("-", 1)[0]
            if re.fullmatch(r"\d{8}T\d{6}Z", run_prefix) is None:
                errors.append(f"invalid run-id filename: {relative_path}")
                continue
            if _scalar(metadata, "run_id") != run_prefix:
                errors.append(f"run-id metadata mismatch: {relative_path}")
            if _scalar(metadata, "task_id") != artifact_task_id:
                errors.append(f"artifact task-id/path mismatch: {relative_path}")
            duplicate_keys = _duplicate_top_level_keys(metadata)
            if duplicate_keys:
                errors.append(
                    f"duplicate artifact metadata keys: {relative_path}:"
                    f"{','.join(sorted(duplicate_keys))}"
                )
            if category == "logs":
                continue

            legacy_schema = LEGACY_ARTIFACT_SCHEMAS.get(relative_path)
            base_revision = _scalar(metadata, "base_revision")
            allows_missing_base = legacy_schema == "verification-v1-missing-base-revision"
            valid_base_revision = (
                base_revision is not None
                and re.fullmatch(r"[0-9a-f]{40}", base_revision) is not None
            )
            if not valid_base_revision and not (allows_missing_base and base_revision is None):
                errors.append(f"artifact has invalid base revision: {relative_path}")
            state_file = evidence_root / "state" / "workstreams" / f"{artifact_task_id}.yaml"
            if valid_base_revision and state_file.is_file():
                expected_base = _scalar(state_file.read_text(), "base_revision")
                if base_revision != expected_base:
                    errors.append(f"artifact has stale base revision: {relative_path}")

            if category == "reviews":
                authority_slug = evidence_file.stem[len(run_prefix) + 1 :]
                authority_by_slug = {
                    slug: authority for authority, slug in AUTHORITY_METADATA.values()
                }
                expected_authority = authority_by_slug.get(authority_slug)
                if expected_authority is None:
                    errors.append(f"review filename has unknown authority: {relative_path}")
                elif _scalar(metadata, "authority") != expected_authority:
                    errors.append(f"review authority/path mismatch: {relative_path}")
                if _scalar(metadata, "decision") not in (
                    "approved",
                    "changes_required",
                    "blocked",
                    "pending",
                ):
                    errors.append(f"review has invalid decision: {relative_path}")
                reviewer = _scalar(metadata, "reviewer")
                if (
                    reviewer is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]+", reviewer) is None
                ):
                    errors.append(f"review has invalid reviewer identity: {relative_path}")
                if not _is_utc_timestamp(_scalar(metadata, "reviewed_at")):
                    errors.append(f"review has invalid reviewed_at: {relative_path}")
                if not _yaml_list(metadata, "scope"):
                    errors.append(f"review has empty scope: {relative_path}")
                if legacy_schema is None:
                    for section in (
                        "## Authority",
                        "## Claims reviewed",
                        "## Evidence",
                        "## Findings",
                        "## Decision",
                    ):
                        if section not in evidence_text:
                            errors.append(f"review lacks section {section}: {relative_path}")
            else:
                if _scalar(metadata, "decision") not in (
                    "verified",
                    "changes_required",
                    "blocked",
                ):
                    errors.append(f"verification has invalid decision: {relative_path}")
                verifier = _scalar(metadata, "verifier")
                if (
                    verifier is None
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]+", verifier) is None
                ):
                    errors.append(f"verification has invalid verifier identity: {relative_path}")
                if legacy_schema is None:
                    if not _is_utc_timestamp(_scalar(metadata, "verified_at")):
                        errors.append(f"verification has invalid verified_at: {relative_path}")
                    executor_run = _scalar(metadata, "executor_run")
                    if executor_run is None or re.fullmatch(r"\d{8}T\d{6}Z", executor_run) is None:
                        errors.append(f"verification has invalid executor_run: {relative_path}")
                    for section in (
                        "## Claims under test",
                        "## Fresh evidence",
                        "## Adversarial checks",
                        "## Named authority verdicts",
                        "## Decision",
                    ):
                        if section not in evidence_text:
                            errors.append(f"verification lacks section {section}: {relative_path}")

    supersessions_file = evidence_root / "state" / "evidence-supersessions.yaml"
    if not supersessions_file.is_file():
        errors.append("missing evidence supersession manifest")
        supersessions: dict[str, dict[str, str]] = {}
    else:
        supersessions = _supersession_records(supersessions_file.read_text())
    for superseded_path, record in supersessions.items():
        correction_path = record.get("required_correction")
        schema = record.get("schema")
        if LEGACY_ARTIFACT_SCHEMAS.get(superseded_path) != schema:
            errors.append(f"supersession schema mismatch: {superseded_path}")
        if not (ROOT / superseded_path).is_file():
            errors.append(f"superseded artifact is missing: {superseded_path}")
        if correction_path is None or not (ROOT / correction_path).is_file():
            errors.append(f"required correction is missing: {superseded_path}")
        elif correction_path in LEGACY_ARTIFACT_SCHEMAS:
            errors.append(f"required correction cannot be legacy: {correction_path}")
        if not record.get("reason"):
            errors.append(f"supersession lacks reason: {superseded_path}")
    for legacy_path, schema in LEGACY_ARTIFACT_SCHEMAS.items():
        if schema == "verification-v1-missing-base-revision" and legacy_path not in supersessions:
            errors.append(f"missing required supersession record: {legacy_path}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    review_template = ROOT / ".agent-work/production-readiness/reviews/TEMPLATE.md"
    if not review_template.is_file():
        print("ERROR: missing named authority review template")
        return 1

    verification_template = ROOT / ".agent-work/production-readiness/verification/TEMPLATE.md"
    verification_template_text = (
        verification_template.read_text() if verification_template.is_file() else ""
    )
    for key in (
        "task_id:",
        "run_id:",
        "verifier:",
        "verified_at:",
        "base_revision:",
        "decision:",
        "executor_run:",
    ):
        if key not in _frontmatter(verification_template_text):
            print(f"ERROR: verification template missing {key}")
            return 1

    lease_template = ROOT / ".agent-work/production-readiness/state/LEASE_TEMPLATE.yaml"
    lease_text = lease_template.read_text() if lease_template.is_file() else ""
    for key in ("lease_id:", "workstream:", "run_id:", "paths:", "expires_at:"):
        if key not in lease_text:
            print(f"ERROR: lease template missing {key}")
            return 1

    print(
        f"orchestration OK: {len(FULL_MIRRORS)} mirrors, "
        f"{len(AGENT_NAMES) + len(DOMAIN_AGENT_NAMES)} agents, "
        f"{len(list(tasks_dir.glob('w*.md')))} task contracts, "
        "no ownership overlaps"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
