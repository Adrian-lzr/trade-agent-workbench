from decimal import Decimal

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.db.models import (
    Base,
    IdempotencyRecord,
    PriceListItem,
    QuoteRevisionRecord,
    RFQRevisionRecord,
    RunEventRecord,
    RunRecord,
)
from trade_agent.db.repository import seed_synthetic_catalog


class RecordingSession:
    def __init__(self) -> None:
        self.statements = []

    def execute(self, statement: object) -> None:
        self.statements.append(statement)


def test_schema_compiles_for_postgresql_with_exact_money_storage() -> None:
    ddl = str(
        CreateTable(PriceListItem.__table__).compile(dialect=postgresql.dialect())
    )

    assert "CREATE TABLE trade_agent.price_list_items" in ddl
    assert "NUMERIC(12, 4)" in ddl
    assert "minimum_quantity > 0" in ddl
    assert "fk_price_items_product" in ddl
    assert "fk_price_items_list_catalog" in ddl
    assert {table.name for table in Base.metadata.sorted_tables} == {
        "audit_events",
        "catalog_versions",
        "document_set_revisions",
        "document_sets",
        "idempotency_records",
        "inquiry_cases",
        "inquiry_reply_revisions",
        "inquiry_translation_reviews",
        "qualification_checks",
        "qualification_decisions",
        "match_decisions",
        "price_lists",
        "price_list_items",
        "products",
        "quote_approvals",
        "quote_artifacts",
        "quote_revisions",
        "quotations",
        "rfq_revisions",
        "rfqs",
        "run_events",
        "runs",
        "shipment_handoff_evidence",
        "shipment_handoff_gate_decisions",
        "shipment_handoffs",
    }


def test_revision_schema_has_json_snapshots_immutable_unique_revision_keys() -> None:
    rfq_ddl = str(
        CreateTable(RFQRevisionRecord.__table__).compile(dialect=postgresql.dialect())
    )
    quote_ddl = str(
        CreateTable(QuoteRevisionRecord.__table__).compile(dialect=postgresql.dialect())
    )
    idempotency_ddl = str(
        CreateTable(IdempotencyRecord.__table__).compile(dialect=postgresql.dialect())
    )

    assert "JSONB NOT NULL" in rfq_ddl
    assert "uq_rfq_revisions_number" in rfq_ddl
    assert "uq_quote_revisions_number" in quote_ddl
    assert "content_hash VARCHAR(64) NOT NULL" in quote_ddl
    assert "uq_idempotency_scope" in idempotency_ddl
    assert "length(request_hash) = 64" in idempotency_ddl
    idempotency_scope = next(
        constraint
        for constraint in IdempotencyRecord.__table__.constraints
        if constraint.name == "uq_idempotency_scope"
    )
    assert tuple(column.name for column in idempotency_scope.columns) == (
        "org_id",
        "actor_id",
        "idempotency_key",
    )


def test_run_schema_has_lease_state_and_idempotent_event_keys() -> None:
    run_ddl = str(
        CreateTable(RunRecord.__table__).compile(dialect=postgresql.dialect())
    )
    event_ddl = str(
        CreateTable(RunEventRecord.__table__).compile(dialect=postgresql.dialect())
    )

    assert (
        "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled')"
        in run_ddl
    )
    assert "lease_expires_at TIMESTAMP WITH TIME ZONE" in run_ddl
    assert "uq_run_events_sequence" in event_ddl
    assert "uq_run_events_key" in event_ddl


def test_repository_uses_immutable_postgresql_inserts() -> None:
    session = RecordingSession()

    seed_synthetic_catalog(session, build_synthetic_catalog())

    statements = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
    ]
    assert len(statements) == 4
    assert all("ON CONFLICT" in statement for statement in statements)
    assert all("DO NOTHING" in statement for statement in statements)
    assert all("DO UPDATE" not in statement for statement in statements)
    assert "catalog_versions" in statements[0]
    assert "price_lists" in statements[1]
    assert "products" in statements[2]
    assert "price_list_items" in statements[3]
    assert (
        Decimal("0.080")
        in session.statements[3].compile(dialect=postgresql.dialect()).params.values()
    )
