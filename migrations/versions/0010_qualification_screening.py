"""Add immutable customer qualification and screening evidence."""

from alembic import op
import sqlalchemy as sa

revision = "0010_qualification_screening"
down_revision = "0009_inquiry_fanout"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.create_table(
        "qualification_checks",
        sa.Column("check_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("customer_id", sa.String(128)),
        sa.Column("inquiry_id", sa.String(128)),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("reference", sa.String(1024), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("evidence_attachment_ref", sa.String(512)),
        sa.Column("notes", sa.String(4000)),
        sa.Column("reviewer_id", sa.String(128)),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "customer_id IS NOT NULL OR inquiry_id IS NOT NULL",
            name="ck_qualification_checks_target",
        ),
        sa.CheckConstraint(
            "result IN ('clear', 'potential_match', 'blocked', 'unverified')",
            name="ck_qualification_checks_result",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_qualification_checks_evidence_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_qualification_checks_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "inquiry_id"],
            [
                f"{SCHEMA}.inquiry_cases.org_id",
                f"{SCHEMA}.inquiry_cases.inquiry_id",
            ],
            name="fk_qualification_checks_inquiry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_qualification_checks_idempotency",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "org_id", "check_id", name="uq_qualification_checks_org_id"
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_qualification_checks_idempotency",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_qualification_checks_idempotency_record",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qualification_checks_org_checked_at",
        "qualification_checks",
        ["org_id", "checked_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qualification_checks_org_customer",
        "qualification_checks",
        ["org_id", "customer_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qualification_checks_org_inquiry",
        "qualification_checks",
        ["org_id", "inquiry_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "qualification_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("check_id", sa.String(128), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("notes", sa.String(4000)),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result IN ('clear', 'potential_match', 'blocked', 'unverified')",
            name="ck_qualification_decisions_result",
        ),
        sa.CheckConstraint(
            "length(evidence_hash) = 64",
            name="ck_qualification_decisions_evidence_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_qualification_decisions_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "check_id"],
            [
                f"{SCHEMA}.qualification_checks.org_id",
                f"{SCHEMA}.qualification_checks.check_id",
            ],
            name="fk_qualification_decisions_check",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_qualification_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "org_id", "check_id", name="uq_qualification_decisions_check"
        ),
        sa.UniqueConstraint(
            "org_id",
            "actor_id",
            "idempotency_key",
            name="uq_qualification_decisions_idempotency",
        ),
        sa.UniqueConstraint(
            "idempotency_record_id",
            name="uq_qualification_decisions_idempotency_record",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_qualification_decisions_org_decided_at",
        "qualification_decisions",
        ["org_id", "decided_at"],
        schema=SCHEMA,
    )
    for table in ("qualification_checks", "qualification_decisions"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER immutable_{table} "
                f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
                "FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.reject_immutable_row_mutation()"
            )
        )


def downgrade() -> None:
    for table in ("qualification_decisions", "qualification_checks"):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS immutable_{table} ON {SCHEMA}.{table}")
        )
    op.drop_index(
        "ix_qualification_decisions_org_decided_at",
        table_name="qualification_decisions",
        schema=SCHEMA,
    )
    op.drop_table("qualification_decisions", schema=SCHEMA)
    op.drop_index(
        "ix_qualification_checks_org_inquiry",
        table_name="qualification_checks",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_qualification_checks_org_customer",
        table_name="qualification_checks",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_qualification_checks_org_checked_at",
        table_name="qualification_checks",
        schema=SCHEMA,
    )
    op.drop_table("qualification_checks", schema=SCHEMA)
