"""Add immutable commercial-invoice and packing-list consistency snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_document_consistency"
down_revision = "0011_inquiry_attachments"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "document_sets",
        sa.Column("document_set_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("quotation_id", sa.String(128), nullable=False),
        sa.Column("quote_revision_id", sa.String(128), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=False),
        sa.Column("approved_content_hash", sa.String(64), nullable=False),
        sa.Column("current_revision_id", sa.String(128)),
        sa.Column("current_revision_no", sa.Integer()),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.UniqueConstraint(
            "org_id", "document_set_id", name="uq_document_sets_org_id"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'issued', 'void')",
            name="ck_document_sets_status",
        ),
        sa.CheckConstraint(
            "length(approved_content_hash) = 64",
            name="ck_document_sets_approved_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "quotation_id", "quote_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_document_sets_quote_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            [f"{SCHEMA}.quote_approvals.approval_id"],
            name="fk_document_sets_quote_approval",
            ondelete="RESTRICT",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_document_sets_org_created_at",
        "document_sets",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "document_set_revisions",
        sa.Column("revision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("document_set_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(128)),
        sa.Column("document_type", sa.String(32), nullable=False),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "document_set_id", "revision_no", name="uq_document_set_revisions_number"
        ),
        sa.UniqueConstraint(
            "org_id",
            "document_set_id",
            "revision_id",
            name="uq_document_set_revisions_pointer",
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_document_set_revisions_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "document_set_id"],
            [
                f"{SCHEMA}.document_sets.org_id",
                f"{SCHEMA}.document_sets.document_set_id",
            ],
            name="fk_document_set_revisions_aggregate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "document_set_id", "parent_revision_id"],
            [
                f"{SCHEMA}.document_set_revisions.org_id",
                f"{SCHEMA}.document_set_revisions.document_set_id",
                f"{SCHEMA}.document_set_revisions.revision_id",
            ],
            name="fk_document_set_revisions_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_document_set_revisions_idempotency",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "document_type IN ('commercial_invoice', 'packing_list')",
            name="ck_document_set_revisions_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'issued', 'void')",
            name="ck_document_set_revisions_status",
        ),
        sa.CheckConstraint(
            "revision_no > 0", name="ck_document_set_revisions_number_positive"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_document_set_revisions_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_document_set_revisions_request_hash_length",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_document_set_revisions_org_created_at",
        "document_set_revisions",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )
    for table in ("document_sets", "document_set_revisions"):
        if table == "document_sets":
            # The aggregate status and current pointer are intentionally mutable;
            # only the append-only snapshots need the immutable-row trigger.
            continue
        op.execute(
            sa.text(
                f"CREATE TRIGGER immutable_{table} "
                f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
                "FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.reject_immutable_row_mutation()"
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS immutable_document_set_revisions "
            f"ON {SCHEMA}.document_set_revisions"
        )
    )
    op.drop_index(
        "ix_document_set_revisions_org_created_at",
        table_name="document_set_revisions",
        schema=SCHEMA,
    )
    op.drop_table("document_set_revisions", schema=SCHEMA)
    op.drop_index(
        "ix_document_sets_org_created_at",
        table_name="document_sets",
        schema=SCHEMA,
    )
    op.drop_table("document_sets", schema=SCHEMA)
