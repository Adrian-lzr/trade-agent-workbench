"""Allow multiple inquiry threads to reference one RFQ aggregate."""

from alembic import op
import sqlalchemy as sa

revision = "0009_inquiry_fanout"
down_revision = "0008_inquiry_sla"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    # 0008 was released with this constraint, while fresh 0008 installs may
    # already omit it; make the forward repair safe in both cases.
    op.execute(
        sa.text(
            f"ALTER TABLE {SCHEMA}.inquiry_cases "
            "DROP CONSTRAINT IF EXISTS uq_inquiry_cases_rfq_org"
        )
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_inquiry_cases_rfq_org",
        "inquiry_cases",
        ["org_id", "rfq_id"],
        schema=SCHEMA,
    )
