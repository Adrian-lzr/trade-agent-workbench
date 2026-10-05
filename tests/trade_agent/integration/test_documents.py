"""PostgreSQL tests for approved-quote document consistency."""

from datetime import UTC, datetime
from decimal import Decimal
import os
from pathlib import Path
from threading import Barrier, Thread

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from trade_agent.api import create_app
from trade_agent.catalog import build_synthetic_catalog
from trade_agent.contracts import (
    CanonicalUnit,
    CommercialInvoiceDocument,
    CommercialInvoiceLine,
    Currency,
    PackingListDocument,
    PackingListPackage,
    ProductSnapshot,
    QualificationCheck,
    QualificationResult,
    QuoteLineSnapshot,
    QuoteRevision,
    SpecificationFact,
)
from trade_agent.db.approvals import decide_quote_revision
from trade_agent.db.documents import (
    DocumentHashMismatchError,
    DocumentIdempotencyConflictError,
    append_document_revision,
    create_document_set,
    issue_document_set,
    validate_document_set,
)
from trade_agent.db.models import (
    IdempotencyRecord,
    QualificationCheckRecord,
    QuoteApprovalRecord,
    RFQAggregate,
    RFQRevisionRecord,
)
from trade_agent.db.qualification import (
    QualificationGateError,
    create_qualification_check,
)
from trade_agent.db.repository import seed_synthetic_catalog
from trade_agent.db.revisions import create_quote_revision

DATABASE_URL = os.environ.get("TRADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.integration
CREATED_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
def migrated_test_database():
    if not DATABASE_URL:
        pytest.skip("Set TRADE_TEST_DATABASE_URL to a disposable PostgreSQL *_test DB")
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql" or not url.database:
        pytest.fail("TRADE_TEST_DATABASE_URL must use PostgreSQL")
    if not url.database.endswith("_test"):
        pytest.fail("Refusing to reset schema unless database name ends in _test")
    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS trade_agent CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("DATABASE_URL", DATABASE_URL)
            command.upgrade(config, "head")
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS trade_agent CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


@pytest.fixture
def factory(migrated_test_database):
    return sessionmaker(migrated_test_database, expire_on_commit=False)


def _quote() -> QuoteRevision:
    line = QuoteLineSnapshot(
        quote_item_id="quote-line-1",
        rfq_item_id="rfq-line-1",
        product=ProductSnapshot(
            sku="BOLT-M8-30-A2",
            catalog_version="SYNTH-CAT-1",
            name="Hex bolt M8 x 30 A2-70",
            specifications=(SpecificationFact(name="grade", value="A2-70"),),
        ),
        quantity=Decimal("5000"),
        unit=CanonicalUnit.PIECE,
        price_list_version="SYNTH-USD-2026-01",
        price_list_source="synthetic",
        unit_price=Decimal("0.08"),
        price_unit=CanonicalUnit.PIECE,
        line_amount=Decimal("400.00"),
    )
    return QuoteRevision(
        org_id="org-docs",
        quotation_id="quote-docs",
        revision_id="quote-docs-rev-1",
        revision_no=1,
        parent_revision_id=None,
        rfq_revision_id="rfq-docs-rev-1",
        catalog_version="SYNTH-CAT-1",
        price_list_version="SYNTH-USD-2026-01",
        items=(line,),
        trade_term="FOB",
        named_place="Shanghai",
        response_body="quote",
        template_version="quote-v1",
        created_at=CREATED_AT,
        created_by="sales-docs",
    )


def _invoice(
    *, quantity: str = "5000", description: str = "Hex bolt M8 x 30 A2-70"
) -> CommercialInvoiceDocument:
    amount = (Decimal(quantity) * Decimal("0.08")).quantize(Decimal("0.01"))
    return CommercialInvoiceDocument(
        buyer="Buyer A",
        consignee="Buyer A",
        document_reference="CI-001",
        currency=Currency.USD,
        incoterm="FOB",
        named_place="Shanghai",
        lines=(
            CommercialInvoiceLine(
                quote_item_id="quote-line-1",
                sku="BOLT-M8-30-A2",
                description=description,
                quantity=Decimal(quantity),
                uom=CanonicalUnit.PIECE,
                unit_price=Decimal("0.08"),
                amount=amount,
            ),
        ),
    )


def _packing(
    *, quantity: str = "5000", weight_uom: str = "kg", reference: str = "CI-001"
) -> PackingListDocument:
    return PackingListDocument(
        buyer="Buyer A",
        consignee="Buyer A",
        document_reference=reference,
        incoterm="FOB",
        named_place="Shanghai",
        packages=(
            PackingListPackage(
                package_id="PKG-1",
                quote_item_id="quote-line-1",
                marks="BUYER A / 1 OF 1",
                quantity=Decimal(quantity),
                uom=CanonicalUnit.PIECE,
                net_weight=Decimal("100"),
                gross_weight=Decimal("110"),
                weight_uom=weight_uom,
            ),
        ),
    )


def _prepare(factory):
    quote = _quote()
    with factory() as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        session.add(
            RFQAggregate(
                rfq_id="rfq-docs",
                org_id="org-docs",
                current_revision_id="rfq-docs-rev-1",
                current_revision_no=1,
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )
        )
        session.add(
            RFQRevisionRecord(
                revision_id="rfq-docs-rev-1",
                org_id="org-docs",
                rfq_id="rfq-docs",
                revision_no=1,
                parent_revision_id=None,
                payload={"plan": {"customer_id": "customer-docs"}},
                created_at=CREATED_AT,
                created_by="sales-docs",
            )
        )
        session.flush()
        create_qualification_check(
            session,
            QualificationCheck(
                org_id="org-docs",
                check_id="qualification-docs",
                customer_id="customer-docs",
                source="test",
                reference="https://example.test/screening",
                checked_at=CREATED_AT,
                result=QualificationResult.CLEAR,
                evidence_hash="a" * 64,
                created_at=CREATED_AT,
                created_by="reviewer-docs",
            ),
            actor_role="reviewer",
            idempotency_key="qualification-docs",
        )
        create_quote_revision(
            session, quote, expected_revision_id=None, idempotency_key="quote-docs"
        )
        approval = decide_quote_revision(
            session,
            org_id=quote.org_id,
            quotation_id=quote.quotation_id,
            revision_id=quote.revision_id,
            content_hash=quote.content_hash,
            decision="approved",
            actor_id="reviewer-docs",
            idempotency_key="approve-docs",
        )
    return quote, approval


def _prepare_without_qualification(factory):
    quote = _quote()
    with factory() as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        session.add(
            RFQAggregate(
                rfq_id="rfq-docs",
                org_id="org-docs",
                current_revision_id="rfq-docs-rev-1",
                current_revision_no=1,
                created_at=CREATED_AT,
                updated_at=CREATED_AT,
            )
        )
        session.add(
            RFQRevisionRecord(
                revision_id="rfq-docs-rev-1",
                org_id="org-docs",
                rfq_id="rfq-docs",
                revision_no=1,
                parent_revision_id=None,
                payload={"plan": {"customer_id": "customer-docs"}},
                created_at=CREATED_AT,
                created_by="sales-docs",
            )
        )
        create_quote_revision(
            session, quote, expected_revision_id=None, idempotency_key="quote-docs"
        )
        session.add(
            QuoteApprovalRecord(
                approval_id="approval-docs-direct",
                org_id=quote.org_id,
                quotation_id=quote.quotation_id,
                revision_id=quote.revision_id,
                content_hash=quote.content_hash,
                decision="approved",
                actor_id="reviewer-docs",
                idempotency_key="approval-docs-direct",
                request_hash="c" * 64,
                reason=None,
                decided_at=CREATED_AT,
            )
        )
        session.add(
            QualificationCheckRecord(
                check_id="qualification-other-org",
                org_id="other-org",
                customer_id="customer-docs",
                inquiry_id=None,
                source="test",
                reference="https://example.test/screening",
                checked_at=CREATED_AT,
                result="clear",
                evidence_hash="d" * 64,
                evidence_attachment_ref=None,
                notes=None,
                reviewer_id=None,
                actor_id="reviewer-other",
                idempotency_key="qualification-other-org",
                request_hash="e" * 64,
                idempotency_record_id="idempotency-other-org",
                created_at=CREATED_AT,
                created_by="reviewer-other",
            )
        )
        session.add(
            IdempotencyRecord(
                record_id="idempotency-other-org",
                org_id="other-org",
                actor_id="reviewer-other",
                endpoint="qualification.checks.create",
                idempotency_key="qualification-other-org",
                request_hash="e" * 64,
                result_aggregate_type="qualification",
                result_aggregate_id="customer-docs",
                result_revision_id="qualification-other-org",
                created_at=CREATED_AT,
            )
        )
    return quote, quote.content_hash


def test_documents_validate_hash_bind_and_issue_immutably(factory):
    quote, approval = _prepare(factory)
    with factory() as session, session.begin():
        with pytest.raises(DocumentHashMismatchError):
            create_document_set(
                session,
                org_id=quote.org_id,
                document_set_id="docs-hash-bad",
                quotation_id=quote.quotation_id,
                quote_revision_id=quote.revision_id,
                approved_content_hash="a" * 64,
                actor_id="sales-docs",
                idempotency_key="docs-hash-bad",
            )
        created = create_document_set(
            session,
            org_id=quote.org_id,
            document_set_id="docs-1",
            quotation_id=quote.quotation_id,
            quote_revision_id=quote.revision_id,
            approved_content_hash=approval.content_hash,
            actor_id="sales-docs",
            idempotency_key="docs-create-1",
        )
        assert created.status == "draft"
        ci = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-1",
            document_type="commercial_invoice",
            document=_invoice(),
            actor_id="sales-docs",
            idempotency_key="docs-ci-1",
        )
        pl = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-1",
            document_type="packing_list",
            document=_packing(),
            actor_id="sales-docs",
            idempotency_key="docs-pl-1",
            expected_revision_id=ci.revision_id,
        )
        checked = validate_document_set(
            session, org_id=quote.org_id, document_set_id="docs-1"
        )
        assert checked.valid and checked.ready
        issued = issue_document_set(
            session, org_id=quote.org_id, document_set_id="docs-1"
        )
        assert issued.status == "issued"
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(
                text(
                    "UPDATE trade_agent.document_set_revisions "
                    "SET status = 'void' WHERE revision_id = :revision_id"
                ),
                {"revision_id": pl.revision_id},
            )


def test_document_validation_reports_field_mismatch_and_idempotency(factory):
    quote, approval = _prepare(factory)
    with factory() as session, session.begin():
        create_document_set(
            session,
            org_id=quote.org_id,
            document_set_id="docs-2",
            quotation_id=quote.quotation_id,
            quote_revision_id=quote.revision_id,
            approved_content_hash=approval.content_hash,
            actor_id="sales-docs",
            idempotency_key="docs-create-2",
        )
        first = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-2",
            document_type="commercial_invoice",
            document=_invoice(description="wrong description"),
            actor_id="sales-docs",
            idempotency_key="docs-ci-2",
        )
        replay = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-2",
            document_type="commercial_invoice",
            document=_invoice(description="wrong description"),
            actor_id="sales-docs",
            idempotency_key="docs-ci-2",
        )
        assert replay.idempotent_replay and replay.revision_id == first.revision_id
        with pytest.raises(DocumentIdempotencyConflictError):
            append_document_revision(
                session,
                org_id=quote.org_id,
                document_set_id="docs-2",
                document_type="commercial_invoice",
                document=_invoice(quantity="5001"),
                actor_id="sales-docs",
                idempotency_key="docs-ci-2",
            )
        append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-2",
            document_type="packing_list",
            document=_packing(reference="PL-2"),
            actor_id="sales-docs",
            idempotency_key="docs-pl-2",
            expected_revision_id=first.revision_id,
        )
        result = validate_document_set(
            session, org_id=quote.org_id, document_set_id="docs-2"
        )
        codes = {issue.code for issue in result.issues}
        assert "description_mismatch" in codes
        assert "cross_document_mismatch" in codes


def test_documents_fail_closed_when_screening_becomes_unresolved(factory):
    quote, approval = _prepare(factory)
    with factory() as session, session.begin():
        create_qualification_check(
            session,
            QualificationCheck(
                org_id=quote.org_id,
                check_id="qualification-docs-hit",
                customer_id="customer-docs",
                source="test",
                reference="https://example.test/screening",
                checked_at=CREATED_AT,
                result=QualificationResult.POTENTIAL_MATCH,
                evidence_hash="b" * 64,
                created_at=CREATED_AT,
                created_by="sales-docs",
            ),
            actor_role="sales",
            idempotency_key="qualification-docs-hit",
        )
        with pytest.raises(QualificationGateError):
            create_document_set(
                session,
                org_id=quote.org_id,
                document_set_id="docs-screening-blocked",
                quotation_id=quote.quotation_id,
                quote_revision_id=quote.revision_id,
                approved_content_hash=approval.content_hash,
                actor_id="sales-docs",
                idempotency_key="docs-screening-blocked",
            )


def test_documents_do_not_use_clear_screening_from_another_org(factory):
    quote, content_hash = _prepare_without_qualification(factory)
    with factory() as session, session.begin(), pytest.raises(QualificationGateError):
        create_document_set(
            session,
            org_id=quote.org_id,
            document_set_id="docs-other-org-screening",
            quotation_id=quote.quotation_id,
            quote_revision_id=quote.revision_id,
            approved_content_hash=content_hash,
            actor_id="sales-docs",
            idempotency_key="docs-other-org-screening",
        )


def test_partial_shipment_requires_cross_document_exact_match(factory):
    quote, approval = _prepare(factory)
    with factory() as session, session.begin():
        create_document_set(
            session,
            org_id=quote.org_id,
            document_set_id="docs-partial",
            quotation_id=quote.quotation_id,
            quote_revision_id=quote.revision_id,
            approved_content_hash=approval.content_hash,
            actor_id="sales-docs",
            idempotency_key="docs-partial-create",
        )
        ci = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-partial",
            document_type="commercial_invoice",
            document=_invoice(quantity="4000"),
            actor_id="sales-docs",
            idempotency_key="docs-partial-ci",
        )
        append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-partial",
            document_type="packing_list",
            document=_packing(quantity="4000"),
            actor_id="sales-docs",
            idempotency_key="docs-partial-pl",
            expected_revision_id=ci.revision_id,
        )
        result = validate_document_set(
            session, org_id=quote.org_id, document_set_id="docs-partial"
        )
        assert result.valid


def test_document_set_same_key_concurrent_first_write_replays(factory):
    quote, approval = _prepare(factory)
    barrier = Barrier(2)
    outcomes: list[object] = []

    def writer() -> None:
        try:
            with factory() as session, session.begin():
                barrier.wait()
                outcomes.append(
                    create_document_set(
                        session,
                        org_id=quote.org_id,
                        document_set_id="docs-concurrent",
                        quotation_id=quote.quotation_id,
                        quote_revision_id=quote.revision_id,
                        approved_content_hash=approval.content_hash,
                        actor_id="sales-docs",
                        idempotency_key="docs-concurrent-key",
                    )
                )
        except Exception as error:  # noqa: BLE001  # pragma: no cover - thread relay
            outcomes.append(error)

    threads = [Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == 2
    assert all(not isinstance(item, BaseException) for item in outcomes)
    assert sorted(
        item.idempotent_replay
        for item in outcomes
        if not isinstance(item, BaseException)
    ) == [False, True]


def test_documents_api_enforces_org_role_and_uniform_errors(factory):
    quote, approval = _prepare(factory)
    client = TestClient(create_app(factory))
    headers = {
        "X-Org-Id": quote.org_id,
        "X-Actor-Id": "sales-docs",
        "X-Role": "sales",
        "Idempotency-Key": "api-docs-1",
    }
    created = client.post(
        "/api/v1/document-sets",
        headers=headers,
        json={
            "document_set_id": "api-docs",
            "quotation_id": quote.quotation_id,
            "quote_revision_id": quote.revision_id,
            "approved_content_hash": approval.content_hash,
        },
    )
    assert created.status_code == 201
    body = {
        "document_type": "commercial_invoice",
        "buyer": "Buyer A",
        "consignee": "Buyer A",
        "document_reference": "CI-API",
        "currency": "USD",
        "incoterm": "FOB",
        "named_place": "Shanghai",
        "lines": [
            {
                "quote_item_id": "quote-line-1",
                "sku": "BOLT-M8-30-A2",
                "description": "Hex bolt M8 x 30 A2-70",
                "quantity": "5000",
                "uom": "piece",
                "unit_price": "0.08",
                "amount": "400.00",
            }
        ],
    }
    appended = client.post(
        "/api/v1/document-sets/api-docs/revisions",
        headers={**headers, "Idempotency-Key": "api-docs-ci"},
        json=body,
    )
    assert appended.status_code == 201
    hidden = client.get(
        "/api/v1/document-sets/api-docs",
        headers={
            "X-Org-Id": "other-org",
            "X-Actor-Id": "sales-other",
            "X-Role": "sales",
        },
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "document_set_not_found"
    forbidden = client.post(
        "/api/v1/document-sets/api-docs/issue",
        headers={
            "X-Org-Id": quote.org_id,
            "X-Actor-Id": "sales-docs",
            "X-Role": "sales",
        },
    )
    assert forbidden.status_code == 403
