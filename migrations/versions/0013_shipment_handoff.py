"""Add hash-bound shipment handoff and freight evidence records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_shipment_handoff"
down_revision = "0012_document_consistency"
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_unique_constraint(
        "uq_quote_approvals_org_id_approval",
        "quote_approvals",
        ["org_id", "approval_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "shipment_handoffs",
        sa.Column("shipment_handoff_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("quotation_id", sa.String(128), nullable=False),
        sa.Column("quote_revision_id", sa.String(128), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=False),
        sa.Column("approved_content_hash", sa.String(64), nullable=False),
        sa.Column("document_set_id", sa.String(128)),
        sa.Column("selected_incoterm", sa.String(32), nullable=False),
        sa.Column("named_place", sa.String(128), nullable=False),
        sa.Column(
            "responsibility_split",
            jsonb,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("freight_forwarder_name", sa.String(255)),
        sa.Column("freight_forwarder_quote_ref", sa.String(512)),
        sa.Column("carrier_name", sa.String(255)),
        sa.Column("insurance_scope", sa.String(2000)),
        sa.Column("insurance_expires_at", sa.DateTime(timezone=True)),
        sa.Column("booking_reference", sa.String(255)),
        sa.Column("eta", sa.DateTime(timezone=True)),
        sa.Column(
            "required_documents",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default=sa.text("'draft'")
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
            "org_id", "shipment_handoff_id", name="uq_shipment_handoffs_org_id"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'ready_for_booking', 'booked', 'in_transit', 'completed', 'cancelled', 'blocked')",
            name="ck_shipment_handoffs_status",
        ),
        sa.CheckConstraint(
            "length(approved_content_hash) = 64",
            name="ck_shipment_handoffs_approved_hash_length",
        ),
        sa.CheckConstraint(
            "length(trim(selected_incoterm)) > 0",
            name="ck_shipment_handoffs_incoterm_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(named_place)) > 0",
            name="ck_shipment_handoffs_named_place_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "quotation_id", "quote_revision_id"],
            [
                f"{SCHEMA}.quote_revisions.org_id",
                f"{SCHEMA}.quote_revisions.quotation_id",
                f"{SCHEMA}.quote_revisions.revision_id",
            ],
            name="fk_shipment_handoffs_quote_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "approval_id"],
            [
                f"{SCHEMA}.quote_approvals.org_id",
                f"{SCHEMA}.quote_approvals.approval_id",
            ],
            name="fk_shipment_handoffs_quote_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "document_set_id"],
            [
                f"{SCHEMA}.document_sets.org_id",
                f"{SCHEMA}.document_sets.document_set_id",
            ],
            name="fk_shipment_handoffs_document_set",
            ondelete="RESTRICT",
        ),
        sa.Index("ix_shipment_handoffs_org_created_at", "org_id", "created_at"),
        sa.Index("ix_shipment_handoffs_org_status", "org_id", "status"),
        schema=SCHEMA,
    )

    op.create_table(
        "shipment_handoff_evidence",
        sa.Column("evidence_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("shipment_handoff_id", sa.String(128), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_evidence_id", sa.String(128)),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("check_key", sa.String(128)),
        sa.Column("evidence_ref", sa.String(1024), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'verified'"),
        ),
        sa.Column("notes", sa.String(4000)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "shipment_handoff_id",
            "revision_no",
            name="uq_shipment_handoff_evidence_revision",
        ),
        sa.UniqueConstraint(
            "org_id",
            "shipment_handoff_id",
            "evidence_id",
            name="uq_shipment_handoff_evidence_pointer",
        ),
        sa.CheckConstraint(
            "revision_no > 0", name="ck_shipment_handoff_evidence_revision_positive"
        ),
        sa.CheckConstraint(
            "evidence_type IN ('freight_quote', 'insurance', 'packing_check', 'label_check', 'required_document', 'booking', 'bill_of_lading', 'air_waybill', 'export_document', 'import_document', 'other')",
            name="ck_shipment_handoff_evidence_type",
        ),
        sa.CheckConstraint(
            "status IN ('verified', 'pending', 'rejected', 'void')",
            name="ck_shipment_handoff_evidence_status",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_shipment_handoff_evidence_hash_length",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_shipment_handoff_evidence_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id"],
            [
                f"{SCHEMA}.shipment_handoffs.org_id",
                f"{SCHEMA}.shipment_handoffs.shipment_handoff_id",
            ],
            name="fk_shipment_handoff_evidence_aggregate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id", "parent_evidence_id"],
            [
                f"{SCHEMA}.shipment_handoff_evidence.org_id",
                f"{SCHEMA}.shipment_handoff_evidence.shipment_handoff_id",
                f"{SCHEMA}.shipment_handoff_evidence.evidence_id",
            ],
            name="fk_shipment_handoff_evidence_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_shipment_handoff_evidence_idempotency",
            ondelete="RESTRICT",
        ),
        sa.Index("ix_shipment_handoff_evidence_org_created_at", "org_id", "created_at"),
        sa.Index(
            "ix_shipment_handoff_evidence_aggregate_key",
            "org_id",
            "shipment_handoff_id",
            "evidence_type",
            "check_key",
            "revision_no",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "shipment_handoff_gate_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("shipment_handoff_id", sa.String(128), nullable=False),
        sa.Column("target_status", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("gate_hash", sa.String(64), nullable=False),
        sa.Column(
            "overridden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reason", sa.String(4000)),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_record_id", sa.String(128), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "org_id",
            "shipment_handoff_id",
            "target_status",
            "decision_id",
            name="uq_shipment_handoff_gate_decision_pointer",
        ),
        sa.CheckConstraint(
            "target_status IN ('ready_for_booking', 'booked')",
            name="ck_shipment_handoff_gate_target",
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_shipment_handoff_gate_decision",
        ),
        sa.CheckConstraint(
            "actor_role IN ('reviewer', 'admin')",
            name="ck_shipment_handoff_gate_actor_role",
        ),
        sa.CheckConstraint(
            "length(gate_hash) = 64", name="ck_shipment_handoff_gate_hash_length"
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_shipment_handoff_gate_request_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "shipment_handoff_id"],
            [
                f"{SCHEMA}.shipment_handoffs.org_id",
                f"{SCHEMA}.shipment_handoffs.shipment_handoff_id",
            ],
            name="fk_shipment_handoff_gate_decisions_aggregate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["idempotency_record_id"],
            [f"{SCHEMA}.idempotency_records.record_id"],
            name="fk_shipment_handoff_gate_decisions_idempotency",
            ondelete="RESTRICT",
        ),
        sa.Index("ix_shipment_handoff_gate_decisions_org_time", "org_id", "decided_at"),
        schema=SCHEMA,
    )

    for table in ("shipment_handoff_evidence", "shipment_handoff_gate_decisions"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER immutable_{table} "
                f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
                "FOR EACH ROW EXECUTE FUNCTION "
                f"{SCHEMA}.reject_immutable_row_mutation()"
            )
        )


def downgrade() -> None:
    for table in ("shipment_handoff_evidence", "shipment_handoff_gate_decisions"):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS immutable_{table} ON {SCHEMA}.{table}")
        )
    op.drop_table("shipment_handoff_gate_decisions", schema=SCHEMA)
    op.drop_table("shipment_handoff_evidence", schema=SCHEMA)
    op.drop_index(
        "ix_shipment_handoffs_org_status",
        table_name="shipment_handoffs",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_shipment_handoffs_org_created_at",
        table_name="shipment_handoffs",
        schema=SCHEMA,
    )
    op.drop_table("shipment_handoffs", schema=SCHEMA)
    op.drop_constraint(
        "uq_quote_approvals_org_id_approval",
        "quote_approvals",
        schema=SCHEMA,
        type_="unique",
    )
