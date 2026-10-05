"""Add durable workflow run lifecycle and append-only events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_run_lifecycle"
down_revision = "0003_match_decisions"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("rfq_id", sa.String(128), nullable=False),
        sa.Column("rfq_revision_id", sa.String(128), nullable=False),
        sa.Column("graph_version", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("wait_reason", sa.String(32)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("event_seq", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("event_seq >= 0", name="ck_runs_event_seq_nonnegative"),
        sa.UniqueConstraint("org_id", "run_id", name="uq_runs_org_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_runs_status_updated_at", "runs", ["status", "updated_at"], schema=SCHEMA
    )
    op.create_table(
        "run_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("node_name", sa.String(64)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("actor_id", sa.String(128)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{SCHEMA}.runs.run_id"],
            name="fk_run_events_run",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("run_id", "event_seq", name="uq_run_events_sequence"),
        sa.UniqueConstraint("run_id", "event_key", name="uq_run_events_key"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_run_events_run_seq", "run_events", ["run_id", "event_seq"], schema=SCHEMA
    )
    op.execute(
        sa.text(
            f"CREATE TRIGGER immutable_run_events "
            f"BEFORE UPDATE OR DELETE ON {SCHEMA}.run_events "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_immutable_row_mutation()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(f"DROP TRIGGER IF EXISTS immutable_run_events ON {SCHEMA}.run_events")
    )
    op.drop_index("ix_run_events_run_seq", table_name="run_events", schema=SCHEMA)
    op.drop_table("run_events", schema=SCHEMA)
    op.drop_index("ix_runs_status_updated_at", table_name="runs", schema=SCHEMA)
    op.drop_table("runs", schema=SCHEMA)
