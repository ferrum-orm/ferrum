"""Ferrum migration subsystem.

Submodule responsibilities:
- ``orchestrator``: dry-run, classification, apply sequencing.
- ``ledger``: migration history table access (append-only).
- ``tokens``: confirmation-token emit/validate (no secrets stored).
- ``gates``: destructive + non-dev confirmation guards.

Security invariants (MIG-1 through MIG-8):
- Dry-run is mandatory before apply.
- Destructive actions (column drop, table drop, type narrowing, NOT NULL on
  populated column) require explicit confirmation.
- Non-development applies require environment confirmation.
- Confirmation tokens are never emitted to argv or public logs.
- Token replay after apply is rejected.
"""
