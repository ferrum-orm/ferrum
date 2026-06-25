"""Initial ticket-analyzer compatibility schema (manual migration)."""

from __future__ import annotations

from ferrum.migrations import Migration
from ferrum.migrations import operations as ops

ECHO_FUNCTION = """CREATE OR REPLACE FUNCTION ta_compat_echo(p_msg text)
RETURNS TABLE(result text) LANGUAGE sql AS $$
  SELECT p_msg;
$$;
"""


class Migration(Migration):
    dependencies: list[str] = []

    operations = [
        ops.CreateExtension("vector"),
        ops.CreateTable(
            "ta_compat_teams",
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("name", "TEXT", not_null=True, default="''"),
            ],
        ),
        ops.CreateTable(
            "ta_compat_tickets",
            [
                ops.Column("id", "BIGINT", not_null=True),
                ops.Column("first_seen_at", "TIMESTAMPTZ", not_null=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("helpshift_id", "TEXT", not_null=True, default="''"),
                ops.Column("summary", "TEXT", not_null=True, default="''"),
                ops.Column("summary_embedding", "vector(8)"),
            ],
            composite_pk_columns=["id", "first_seen_at"],
        ),
        ops.AddIndex(
            "ta_compat_tickets",
            "idx_ta_compat_tickets_summary_embedding_hnsw",
            ["summary_embedding"],
            using="hnsw",
            opclasses=["vector_cosine_ops"],
        ),
        ops.CreateTable(
            "ta_compat_issues",
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("dedup_key", "TEXT", not_null=True),
                ops.Column("title", "TEXT", not_null=True, default="''"),
                ops.Column("summary", "TEXT", not_null=True, default="''"),
            ],
        ),
        ops.AddIndex(
            "ta_compat_issues",
            "idx_ta_compat_issues_team_dedup",
            ["team_id", "dedup_key"],
            unique=True,
        ),
        ops.CreateTable(
            "ta_compat_alerts",
            [
                ops.Column("id", "UUID", not_null=True, primary_key=True),
                ops.Column("team_id", "UUID", not_null=True),
                ops.Column("title", "TEXT", not_null=True, default="''"),
                ops.Column("ticket_ids", "UUID[]", not_null=True),
                ops.Column("slack_delivery", "JSONB", not_null=True),
            ],
        ),
        ops.EnableRLS("ta_compat_issues", force=True),
        ops.CreatePolicy(
            "team_isolation",
            "ta_compat_issues",
            "team_id::text = current_setting('app.team_id', true)",
        ),
        ops.CreatePolicy(
            "platform_admin_bypass",
            "ta_compat_issues",
            "current_setting('app.platform_admin', true) = 'true'",
        ),
        ops.CreateFunction("ta_compat_echo", ECHO_FUNCTION),
    ]
