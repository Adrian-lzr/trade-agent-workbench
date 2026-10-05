"""Add immutable quote approvals and private export artifacts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_quote_approvals_artifacts"
down_revision = "0005_run_fencing_quote_actions"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "quote_approvals",
        sa.Column("approval_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("quotation_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "quotation_id", "revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_quote_approvals_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_quote_approvals_decision",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_quote_approvals_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_quote_approvals_request_hash_length",
        ),
        sa.UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            name="uq_quote_approvals_revision",
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_quote_approvals_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_quote_approvals_org_quote",
        "quote_approvals",
        ["org_id", "quotation_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "quote_artifacts",
        sa.Column("artifact_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("quotation_id", sa.String(128), nullable=False),
        sa.Column("revision_id", sa.String(128), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("content_bytes", sa.LargeBinary()),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            [f"{SCHEMA}.quote_approvals.approval_id"],
            name="fk_quote_artifacts_approval",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed')",
            name="ck_quote_artifacts_status",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_quote_artifacts_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_quote_artifacts_request_hash_length",
        ),
        sa.CheckConstraint(
            "amount_total >= 0",
            name="ck_quote_artifacts_amount_nonnegative",
        ),
        sa.UniqueConstraint(
            "org_id",
            "quotation_id",
            "revision_id",
            "artifact_type",
            name="uq_quote_artifacts_revision_type",
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_quote_artifacts_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_quote_artifacts_org_created_at",
        "quote_artifacts",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )

    op.execute(
        sa.text(
            f"CREATE TRIGGER immutable_quote_approvals "
            f"BEFORE UPDATE OR DELETE ON {SCHEMA}.quote_approvals "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_immutable_row_mutation()"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f"DROP TRIGGER IF EXISTS immutable_quote_approvals "
            f"ON {SCHEMA}.quote_approvals"
        )
    )
    op.drop_index(
        "ix_quote_artifacts_org_created_at",
        table_name="quote_artifacts",
        schema=SCHEMA,
    )
    op.drop_table("quote_artifacts", schema=SCHEMA)
    op.drop_index(
        "ix_quote_approvals_org_quote",
        table_name="quote_approvals",
        schema=SCHEMA,
    )
    op.drop_table("quote_approvals", schema=SCHEMA)
