"""Persist audited, idempotent RFQ product-selection decisions."""

from alembic import op
import sqlalchemy as sa

revision = "0003_match_decisions"
down_revision = "0002_rfq_quote_revisions"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "match_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("rfq_id", sa.String(128), nullable=False),
        sa.Column("rfq_revision_id", sa.String(128), nullable=False),
        sa.Column("rfq_item_id", sa.String(128), nullable=False),
        sa.Column("catalog_version", sa.String(64), nullable=False),
        sa.Column("selected_sku", sa.String(80), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_match_decisions_reason"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "rfq_id", "rfq_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_match_decisions_rfq_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version", "selected_sku"],
            [f"{SCHEMA}.products.catalog_version", f"{SCHEMA}.products.sku"],
            name="fk_match_decisions_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_match_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id", name="uq_match_decisions_idempotency"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_match_decisions_rfq_item_time",
        "match_decisions",
        ["org_id", "rfq_id", "rfq_revision_id", "rfq_item_id", "created_at"],
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            f"CREATE TRIGGER immutable_match_decisions "
            f"BEFORE UPDATE OR DELETE ON {SCHEMA}.match_decisions "
            "FOR EACH ROW EXECUTE FUNCTION "
            f"{SCHEMA}.reject_immutable_row_mutation()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS immutable_match_decisions "
            f"ON {SCHEMA}.match_decisions"
        )
    )
    op.drop_index(
        "ix_match_decisions_rfq_item_time",
        table_name="match_decisions",
        schema=SCHEMA,
    )
    op.drop_table("match_decisions", schema=SCHEMA)
