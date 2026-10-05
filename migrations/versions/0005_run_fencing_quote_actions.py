"""Add worker fencing and stable quote action keys."""

from alembic import op
import sqlalchemy as sa

revision = "0005_run_fencing_quote_actions"
down_revision = "0004_run_lifecycle"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("lease_generation", sa.Integer(), server_default="0", nullable=False),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_runs_lease_generation_nonnegative",
        "runs",
        "lease_generation >= 0",
        schema=SCHEMA,
    )
    op.add_column(
        "quote_revisions",
        sa.Column("run_id", sa.String(128), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "quote_revisions",
        sa.Column("action_type", sa.String(64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "quote_revisions",
        sa.Column("business_version", sa.String(128), nullable=True),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        "uq_quote_revisions_run_action_version",
        "quote_revisions",
        ["run_id", "action_type", "business_version"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_quote_revisions_run_action_version",
        "quote_revisions",
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_column("quote_revisions", "business_version", schema=SCHEMA)
    op.drop_column("quote_revisions", "action_type", schema=SCHEMA)
    op.drop_column("quote_revisions", "run_id", schema=SCHEMA)
    op.drop_constraint(
        "ck_runs_lease_generation_nonnegative",
        "runs",
        schema=SCHEMA,
        type_="check",
    )
    op.drop_column("runs", "lease_generation", schema=SCHEMA)
