"""Add append-only RFQ/quote revisions, idempotency and audit records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_rfq_quote_revisions"
down_revision = "0001_trade_catalog"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "rfqs",
        sa.Column("rfq_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("current_revision_id", sa.String(128)),
        sa.Column("current_revision_no", sa.Integer()),
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
            "(current_revision_id IS NULL) = (current_revision_no IS NULL)",
            name="ck_rfqs_current_revision_pair",
        ),
        sa.UniqueConstraint("org_id", "rfq_id", name="uq_rfqs_org_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_rfqs_org_created_at", "rfqs", ["org_id", "created_at"], schema=SCHEMA
    )
    op.create_table(
        "rfq_revisions",
        sa.Column("revision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("rfq_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(128)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint("revision_no > 0", name="ck_rfq_revisions_number_positive"),
        sa.ForeignKeyConstraint(
            ["org_id", "rfq_id"],
            [f"{SCHEMA}.rfqs.org_id", f"{SCHEMA}.rfqs.rfq_id"],
            name="fk_rfq_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "rfq_id", "parent_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_rfq_revisions_parent",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("rfq_id", "revision_no", name="uq_rfq_revisions_number"),
        sa.UniqueConstraint(
            "org_id", "rfq_id", "revision_id", name="uq_rfq_revisions_pointer"
        ),
        sa.UniqueConstraint(
            "org_id",
            "rfq_id",
            "revision_id",
            "revision_no",
            name="uq_rfq_revisions_pointer_no",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_rfq_revisions_org_created_at",
        "rfq_revisions",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_rfqs_current_revision",
        "rfqs",
        "rfq_revisions",
        ["org_id", "rfq_id", "current_revision_id", "current_revision_no"],
        ["org_id", "rfq_id", "revision_id", "revision_no"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "quotations",
        sa.Column("quotation_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("current_revision_id", sa.String(128)),
        sa.Column("current_revision_no", sa.Integer()),
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
            "(current_revision_id IS NULL) = (current_revision_no IS NULL)",
            name="ck_quotations_current_revision_pair",
        ),
        sa.UniqueConstraint("org_id", "quotation_id", name="uq_quotations_org_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_quotations_org_created_at",
        "quotations",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "quote_revisions",
        sa.Column("revision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("quotation_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(128)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "revision_no > 0", name="ck_quote_revisions_number_positive"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_quote_revisions_hash_length"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "quotation_id"],
            [f"{SCHEMA}.quotations.org_id", f"{SCHEMA}.quotations.quotation_id"],
            name="fk_quote_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "quotation_id", "parent_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_quote_revisions_parent",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "quotation_id", "revision_no", name="uq_quote_revisions_number"
        ),
        sa.UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            name="uq_quote_revisions_pointer",
        ),
        sa.UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            "revision_no",
            name="uq_quote_revisions_pointer_no",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_quote_revisions_org_created_at",
        "quote_revisions",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_quotations_current_revision",
        "quotations",
        "quote_revisions",
        [
            "org_id",
            "quotation_id",
            "current_revision_id",
            "current_revision_no",
        ],
        ["org_id", "quotation_id", "revision_id", "revision_no"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "idempotency_records",
        sa.Column("record_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_aggregate_type", sa.String(16), nullable=False),
        sa.Column("result_aggregate_id", sa.String(128), nullable=False),
        sa.Column("result_revision_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_idempotency_hash_length"
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_idempotency_org_created_at",
        "idempotency_records",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(16), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("prior_revision_id", sa.String(128)),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "aggregate_type IN ('rfq', 'quotation')",
            name="ck_audit_events_aggregate_type",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_audit_events_idempotency",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id", name="uq_audit_events_idempotency"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_org_aggregate_time",
        "audit_events",
        ["org_id", "aggregate_type", "aggregate_id", "occurred_at"],
        schema=SCHEMA,
    )

    op.execute(
        sa.text(
            """
            CREATE FUNCTION trade_agent.reject_immutable_row_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'rows in %.% are immutable',
                    TG_TABLE_SCHEMA, TG_TABLE_NAME;
            END;
            $$
            """
        )
    )
    for table in ("rfq_revisions", "quote_revisions", "audit_events"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER immutable_{table} "
                f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
                "FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.reject_immutable_row_mutation()"
            )
        )


def downgrade() -> None:
    for table in ("rfq_revisions", "quote_revisions", "audit_events"):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS immutable_{table} ON {SCHEMA}.{table}")
        )
    op.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_immutable_row_mutation()")
    )
    op.drop_table("audit_events", schema=SCHEMA)
    op.drop_index(
        "ix_idempotency_org_created_at", table_name="idempotency_records", schema=SCHEMA
    )
    op.drop_table("idempotency_records", schema=SCHEMA)
    op.drop_constraint(
        "fk_quotations_current_revision",
        "quotations",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_index(
        "ix_quote_revisions_org_created_at", table_name="quote_revisions", schema=SCHEMA
    )
    op.drop_table("quote_revisions", schema=SCHEMA)
    op.drop_index(
        "ix_quotations_org_created_at", table_name="quotations", schema=SCHEMA
    )
    op.drop_table("quotations", schema=SCHEMA)
    op.drop_constraint(
        "fk_rfqs_current_revision", "rfqs", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_index(
        "ix_rfq_revisions_org_created_at", table_name="rfq_revisions", schema=SCHEMA
    )
    op.drop_table("rfq_revisions", schema=SCHEMA)
    op.drop_index("ix_rfqs_org_created_at", table_name="rfqs", schema=SCHEMA)
    op.drop_table("rfqs", schema=SCHEMA)
