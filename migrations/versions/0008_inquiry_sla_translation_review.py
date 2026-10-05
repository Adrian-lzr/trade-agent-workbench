"""Add inquiry response SLA, append-only replies, and translation review."""

from alembic import op
import sqlalchemy as sa

revision = "0008_inquiry_sla"
down_revision = "0007_analytics_views"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "inquiry_cases",
        sa.Column("inquiry_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("rfq_id", sa.String(128), nullable=False),
        sa.Column("source_channel", sa.String(64), nullable=False),
        sa.Column("customer_role", sa.String(32), nullable=False),
        sa.Column("original_language", sa.String(16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.String(128)),
        sa.Column(
            "queue_state",
            sa.String(16),
            nullable=False,
            server_default="open",
        ),
        sa.Column("current_reply_revision_id", sa.String(128)),
        sa.Column("current_reply_revision_no", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "queue_state IN ('open', 'overdue', 'responded', 'nurture', 'closed')",
            name="ck_inquiry_cases_queue_state",
        ),
        sa.CheckConstraint(
            "response_due_at >= received_at",
            name="ck_inquiry_cases_sla_range",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "rfq_id"],
            [f"{SCHEMA}.rfqs.org_id", f"{SCHEMA}.rfqs.rfq_id"],
            name="fk_inquiry_cases_rfq",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(current_reply_revision_id IS NULL) = (current_reply_revision_no IS NULL)",
            name="ck_inquiry_cases_current_reply_pair",
        ),
        sa.UniqueConstraint("org_id", "inquiry_id", name="uq_inquiry_cases_org_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_inquiry_cases_org_due",
        "inquiry_cases",
        ["org_id", "response_due_at", "queue_state"],
        schema=SCHEMA,
    )

    op.create_table(
        "inquiry_reply_revisions",
        sa.Column("reply_revision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("inquiry_id", sa.String(128), nullable=False),
        sa.Column("rfq_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_reply_revision_id", sa.String(128)),
        sa.Column("rfq_revision_id", sa.String(128), nullable=False),
        sa.Column("source_language", sa.String(16), nullable=False),
        sa.Column("target_language", sa.String(16), nullable=False),
        sa.Column("source_content", sa.String(20000), nullable=False),
        sa.Column("translated_content", sa.String(20000)),
        sa.Column(
            "template_version",
            sa.String(64),
            nullable=False,
            server_default="reply-v1",
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "revision_no > 0", name="ck_inquiry_reply_revisions_number_positive"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_inquiry_reply_revisions_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "inquiry_id"],
            [
                f"{SCHEMA}.inquiry_cases.org_id",
                f"{SCHEMA}.inquiry_cases.inquiry_id",
            ],
            name="fk_inquiry_reply_revisions_inquiry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "rfq_id", "rfq_revision_id"],
            [
                f"{SCHEMA}.rfq_revisions.org_id",
                f"{SCHEMA}.rfq_revisions.rfq_id",
                f"{SCHEMA}.rfq_revisions.revision_id",
            ],
            name="fk_inquiry_reply_revisions_rfq_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "inquiry_id", "parent_reply_revision_id"],
            [
                f"{SCHEMA}.inquiry_reply_revisions.org_id",
                f"{SCHEMA}.inquiry_reply_revisions.inquiry_id",
                f"{SCHEMA}.inquiry_reply_revisions.reply_revision_id",
            ],
            name="fk_inquiry_reply_revisions_parent",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "inquiry_id", "revision_no", name="uq_inquiry_reply_revisions_number"
        ),
        sa.UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            name="uq_inquiry_reply_revisions_pointer",
        ),
        sa.UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            "revision_no",
            name="uq_inquiry_reply_revisions_pointer_no",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_inquiry_reply_revisions_org_created_at",
        "inquiry_reply_revisions",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "inquiry_translation_reviews",
        sa.Column("review_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("inquiry_id", sa.String(128), nullable=False),
        sa.Column("reply_revision_id", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(2000)),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_inquiry_translation_reviews_decision",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_inquiry_translation_reviews_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_inquiry_translation_reviews_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "inquiry_id", "reply_revision_id"],
            [
                f"{SCHEMA}.inquiry_reply_revisions.org_id",
                f"{SCHEMA}.inquiry_reply_revisions.inquiry_id",
                f"{SCHEMA}.inquiry_reply_revisions.reply_revision_id",
            ],
            name="fk_inquiry_translation_reviews_reply",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_inquiry_translation_reviews_idempotency",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "org_id",
            "inquiry_id",
            "reply_revision_id",
            name="uq_inquiry_translation_reviews_revision",
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_inquiry_translation_reviews_idempotency",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_inquiry_translation_reviews_org_reviewed_at",
        "inquiry_translation_reviews",
        ["org_id", "reviewed_at"],
        schema=SCHEMA,
    )

    op.create_foreign_key(
        "fk_inquiry_cases_current_reply",
        "inquiry_cases",
        "inquiry_reply_revisions",
        [
            "org_id",
            "inquiry_id",
            "current_reply_revision_id",
            "current_reply_revision_no",
        ],
        ["org_id", "inquiry_id", "reply_revision_id", "revision_no"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )
    for table in ("inquiry_reply_revisions", "inquiry_translation_reviews"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER immutable_{table} "
                f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
                "FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.reject_immutable_row_mutation()"
            )
        )


def downgrade() -> None:
    for table in ("inquiry_reply_revisions", "inquiry_translation_reviews"):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS immutable_{table} ON {SCHEMA}.{table}")
        )
    op.drop_constraint(
        "fk_inquiry_cases_current_reply",
        "inquiry_cases",
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_index(
        "ix_inquiry_translation_reviews_org_reviewed_at",
        table_name="inquiry_translation_reviews",
        schema=SCHEMA,
    )
    op.drop_table("inquiry_translation_reviews", schema=SCHEMA)
    op.drop_index(
        "ix_inquiry_reply_revisions_org_created_at",
        table_name="inquiry_reply_revisions",
        schema=SCHEMA,
    )
    op.drop_table("inquiry_reply_revisions", schema=SCHEMA)
    op.drop_index("ix_inquiry_cases_org_due", table_name="inquiry_cases", schema=SCHEMA)
    op.drop_table("inquiry_cases", schema=SCHEMA)
