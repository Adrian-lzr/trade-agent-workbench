"""PostgreSQL tests for hash-bound shipment handoff gates."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
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
    ShipmentEvidence,
    ShipmentEvidenceType,
    ShipmentHandoff,
    ShipmentHandoffStatus,
    SpecificationFact,
)
from trade_agent.db.approvals import decide_quote_revision
from trade_agent.db.documents import (
    append_document_revision,
    create_document_set,
    ready_document_set,
)
from trade_agent.db.models import ShipmentHandoffGateDecisionRecord
from trade_agent.db.qualification import create_qualification_check
from trade_agent.db.repository import seed_synthetic_catalog
from trade_agent.db.revisions import create_quote_revision
from trade_agent.db.shipments import (
    ShipmentConflictError,
    ShipmentGateError,
    ShipmentHashMismatchError,
    append_shipment_evidence,
    create_shipment_handoff,
    evaluate_shipment_gate,
    get_shipment_handoff,
    transition_shipment_handoff,
)

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
        quote_item_id="quote-line-ship-1",
        rfq_item_id="rfq-line-ship-1",
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
        org_id="org-shipment",
        quotation_id="quote-shipment",
        revision_id="quote-shipment-rev-1",
        revision_no=1,
        parent_revision_id=None,
        rfq_revision_id="rfq-shipment-rev-1",
        catalog_version="SYNTH-CAT-1",
        price_list_version="SYNTH-USD-2026-01",
        items=(line,),
        trade_term="FOB",
        named_place="Shanghai",
        response_body="quote",
        template_version="quote-v1",
        created_at=CREATED_AT,
        created_by="sales-shipment",
    )


def _invoice() -> CommercialInvoiceDocument:
    return CommercialInvoiceDocument(
        buyer="Buyer Shipment",
        consignee="Buyer Shipment",
        document_reference="CI-SHIP-001",
        currency=Currency.USD,
        incoterm="FOB",
        named_place="Shanghai",
        lines=(
            CommercialInvoiceLine(
                quote_item_id="quote-line-ship-1",
                sku="BOLT-M8-30-A2",
                description="Hex bolt M8 x 30 A2-70",
                quantity=Decimal("5000"),
                uom=CanonicalUnit.PIECE,
                unit_price=Decimal("0.08"),
                amount=Decimal("400.00"),
            ),
        ),
    )


def _packing() -> PackingListDocument:
    return PackingListDocument(
        buyer="Buyer Shipment",
        consignee="Buyer Shipment",
        document_reference="CI-SHIP-001",
        incoterm="FOB",
        named_place="Shanghai",
        packages=(
            PackingListPackage(
                package_id="PKG-SHIP-1",
                quote_item_id="quote-line-ship-1",
                marks="BUYER SHIPMENT / 1 OF 1",
                quantity=Decimal("5000"),
                uom=CanonicalUnit.PIECE,
                net_weight=Decimal("100"),
                gross_weight=Decimal("110"),
                weight_uom="kg",
            ),
        ),
    )


def _prepare(factory):
    quote = _quote()
    with factory() as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        session.execute(
            text(
                "INSERT INTO trade_agent.rfqs "
                "(rfq_id, org_id, current_revision_id, current_revision_no, created_at, updated_at) "
                "VALUES (:rfq, :org, :rev, 1, :at, :at)"
            ),
            {
                "rfq": "rfq-shipment",
                "org": quote.org_id,
                "rev": quote.rfq_revision_id,
                "at": CREATED_AT,
            },
        )
        session.execute(
            text(
                "INSERT INTO trade_agent.rfq_revisions "
                "(revision_id, org_id, rfq_id, revision_no, parent_revision_id, payload, created_at, created_by) "
                "VALUES (:rev, :org, :rfq, 1, NULL, CAST(:payload AS jsonb), :at, :actor)"
            ),
            {
                "rev": quote.rfq_revision_id,
                "org": quote.org_id,
                "rfq": "rfq-shipment",
                "payload": '{"plan":{"customer_id":"customer-shipment"}}',
                "at": CREATED_AT,
                "actor": "sales-shipment",
            },
        )
        create_qualification_check(
            session,
            QualificationCheck(
                org_id=quote.org_id,
                check_id="qualification-shipment",
                customer_id="customer-shipment",
                source="test",
                reference="https://example.test/screening",
                checked_at=CREATED_AT,
                result=QualificationResult.CLEAR,
                evidence_hash="a" * 64,
                created_at=CREATED_AT,
                created_by="reviewer-shipment",
            ),
            actor_role="reviewer",
            idempotency_key="qualification-shipment",
        )
        create_quote_revision(
            session, quote, expected_revision_id=None, idempotency_key="quote-shipment"
        )
        approval = decide_quote_revision(
            session,
            org_id=quote.org_id,
            quotation_id=quote.quotation_id,
            revision_id=quote.revision_id,
            content_hash=quote.content_hash,
            decision="approved",
            actor_id="reviewer-shipment",
            idempotency_key="approve-shipment",
        )
        create_document_set(
            session,
            org_id=quote.org_id,
            document_set_id="docs-shipment",
            quotation_id=quote.quotation_id,
            quote_revision_id=quote.revision_id,
            approved_content_hash=quote.content_hash,
            actor_id="sales-shipment",
            idempotency_key="docs-shipment-create",
        )
        ci = append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-shipment",
            document_type="commercial_invoice",
            document=_invoice(),
            actor_id="sales-shipment",
            idempotency_key="docs-shipment-ci",
        )
        append_document_revision(
            session,
            org_id=quote.org_id,
            document_set_id="docs-shipment",
            document_type="packing_list",
            document=_packing(),
            actor_id="sales-shipment",
            idempotency_key="docs-shipment-pl",
            expected_revision_id=ci.revision_id,
        )
        ready_document_set(
            session, org_id=quote.org_id, document_set_id="docs-shipment"
        )
    return quote, approval


def _handoff(quote: QuoteRevision) -> ShipmentHandoff:
    return ShipmentHandoff(
        shipment_handoff_id="shipment-1",
        org_id=quote.org_id,
        quotation_id=quote.quotation_id,
        quote_revision_id=quote.revision_id,
        approved_content_hash=quote.content_hash,
        selected_incoterm="FOB",
        named_place="Shanghai",
        responsibility_split={"freight": "buyer", "insurance": "buyer"},
        freight_forwarder_name="Forwarder A",
        freight_forwarder_quote_ref="forwarder://quote-1",
        carrier_name="Carrier A",
        insurance_scope="all risks from origin to destination",
        insurance_expires_at=CREATED_AT + timedelta(days=30),
        document_set_id="docs-shipment",
        required_documents=("commercial_invoice", "packing_list"),
    )


def _evidence(
    evidence_id: str,
    evidence_type: ShipmentEvidenceType,
    *,
    check_key: str | None = None,
    parent_evidence_id: str | None = None,
    expires_at: datetime | None = None,
) -> ShipmentEvidence:
    return ShipmentEvidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        check_key=check_key,
        evidence_ref="private://shipment/" + evidence_id,
        content_hash=(evidence_id.encode().hex() * 64)[:64],
        parent_evidence_id=parent_evidence_id,
        expires_at=expires_at,
    )


def _append_required_evidence(
    session, quote: QuoteRevision, *, expires_at: datetime | None = None
) -> None:
    handoff_id = "shipment-1"
    rows = [
        _evidence("ev-freight", ShipmentEvidenceType.FREIGHT_QUOTE),
        _evidence("ev-insurance", ShipmentEvidenceType.INSURANCE),
        _evidence("ev-packing", ShipmentEvidenceType.PACKING_CHECK),
        _evidence("ev-label", ShipmentEvidenceType.LABEL_CHECK),
        _evidence(
            "ev-ci",
            ShipmentEvidenceType.REQUIRED_DOCUMENT,
            check_key="commercial_invoice",
        ),
        _evidence(
            "ev-pl", ShipmentEvidenceType.REQUIRED_DOCUMENT, check_key="packing_list"
        ),
    ]
    if expires_at is not None:
        rows = [row.model_copy(update={"expires_at": expires_at}) for row in rows]
    for index, row in enumerate(rows):
        append_shipment_evidence(
            session,
            org_id=quote.org_id,
            shipment_handoff_id=handoff_id,
            evidence=row,
            actor_id="sales-shipment",
            idempotency_key="evidence-" + str(index),
            expected_evidence_id=None if index == 0 else rows[index - 1].evidence_id,
        )


def test_shipment_gate_is_fail_closed_then_supports_booking_and_transit(factory):
    quote, _approval = _prepare(factory)
    with factory() as session, session.begin():
        create_shipment_handoff(
            session,
            _handoff(quote),
            actor_id="sales-shipment",
            idempotency_key="shipment-create",
        )
        blocked = evaluate_shipment_gate(
            session, org_id=quote.org_id, shipment_handoff_id="shipment-1"
        )
        assert not blocked.valid
        assert "freight_quote_evidence_required" in blocked.issues
        _append_required_evidence(session, quote)
        ready = evaluate_shipment_gate(
            session, org_id=quote.org_id, shipment_handoff_id="shipment-1"
        )
        assert ready.valid
        transition_shipment_handoff(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            target_status=ShipmentHandoffStatus.READY_FOR_BOOKING.value,
            actor_id="reviewer-shipment",
            actor_role="reviewer",
            idempotency_key="shipment-ready",
        )
        append_shipment_evidence(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            evidence=_evidence("ev-booking", ShipmentEvidenceType.BOOKING),
            actor_id="sales-shipment",
            idempotency_key="evidence-booking",
            expected_evidence_id="ev-pl",
        )
        booked = transition_shipment_handoff(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            target_status=ShipmentHandoffStatus.BOOKED.value,
            actor_id="reviewer-shipment",
            actor_role="reviewer",
            idempotency_key="shipment-booked",
            booking_reference="BOOK-001",
            eta=CREATED_AT + timedelta(days=12),
        )
        assert booked.status == ShipmentHandoffStatus.BOOKED.value
        with pytest.raises(ShipmentGateError, match="bill_of_lading_or_air_waybill"):
            transition_shipment_handoff(
                session,
                org_id=quote.org_id,
                shipment_handoff_id="shipment-1",
                target_status=ShipmentHandoffStatus.IN_TRANSIT.value,
                actor_id="sales-shipment",
                actor_role="sales",
                idempotency_key="shipment-transit-blocked",
            )
        append_shipment_evidence(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            evidence=_evidence("ev-bl", ShipmentEvidenceType.BILL_OF_LADING),
            actor_id="sales-shipment",
            idempotency_key="evidence-bl",
            expected_evidence_id="ev-booking",
        )
        transit = transition_shipment_handoff(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            target_status=ShipmentHandoffStatus.IN_TRANSIT.value,
            actor_id="sales-shipment",
            actor_role="sales",
            idempotency_key="shipment-transit",
        )
        assert transit.status == ShipmentHandoffStatus.IN_TRANSIT.value


def test_shipment_gate_override_is_reasoned_and_expired_evidence_blocks(factory):
    quote, _approval = _prepare(factory)
    with factory() as session, session.begin():
        create_shipment_handoff(
            session,
            _handoff(quote),
            actor_id="sales-shipment",
            idempotency_key="shipment-override-create",
        )
        overridden = transition_shipment_handoff(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            target_status=ShipmentHandoffStatus.READY_FOR_BOOKING.value,
            actor_id="reviewer-shipment",
            actor_role="reviewer",
            idempotency_key="shipment-override",
            override=True,
            reason="Forwarder confirmation is pending but approved by the reviewer.",
        )
        assert overridden.status == ShipmentHandoffStatus.READY_FOR_BOOKING.value
        decision = session.scalar(
            select(ShipmentHandoffGateDecisionRecord).where(
                ShipmentHandoffGateDecisionRecord.decision_id == overridden.decision_id
            )
        )
        assert decision is not None
        assert decision.overridden is True
        assert decision.reason and "Forwarder" in decision.reason


def test_shipment_expired_evidence_blocks_ready_gate(factory):
    quote, _approval = _prepare(factory)
    with factory() as session, session.begin():
        create_shipment_handoff(
            session,
            _handoff(quote),
            actor_id="sales-shipment",
            idempotency_key="shipment-expired-create",
        )
        _append_required_evidence(
            session, quote, expires_at=CREATED_AT - timedelta(seconds=1)
        )
        gate = evaluate_shipment_gate(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            now=CREATED_AT,
        )
        assert not gate.valid
        assert "freight_quote_evidence_expired" in gate.issues
        assert "required_document_missing:commercial_invoice" in gate.issues


def test_shipment_booking_requires_carrier_and_eta(factory):
    quote, _approval = _prepare(factory)
    with factory() as session, session.begin():
        handoff = _handoff(quote).model_copy(
            update={"carrier_name": None, "booking_reference": "BOOK-001"}
        )
        create_shipment_handoff(
            session,
            handoff,
            actor_id="sales-shipment",
            idempotency_key="shipment-booking-fields-create",
        )
        _append_required_evidence(session, quote)
        gate = evaluate_shipment_gate(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            target_status=ShipmentHandoffStatus.BOOKED.value,
            now=CREATED_AT,
        )
        assert not gate.valid
        assert "carrier_name_required" in gate.issues
        assert "eta_required" in gate.issues


def test_shipment_hash_org_idempotency_and_immutable_evidence(factory):
    quote, _approval = _prepare(factory)
    with factory() as session, session.begin():
        with pytest.raises(ShipmentHashMismatchError):
            create_shipment_handoff(
                session,
                _handoff(quote).model_copy(update={"approved_content_hash": "b" * 64}),
                actor_id="sales-shipment",
                idempotency_key="shipment-bad-hash",
            )
        first = create_shipment_handoff(
            session,
            _handoff(quote),
            actor_id="sales-shipment",
            idempotency_key="shipment-replay",
        )
        replay = create_shipment_handoff(
            session,
            _handoff(quote),
            actor_id="sales-shipment",
            idempotency_key="shipment-replay",
        )
        assert not first.idempotent_replay and replay.idempotent_replay
        assert (
            get_shipment_handoff(
                session, org_id="other-org", shipment_handoff_id="shipment-1"
            )
            is None
        )
        evidence = append_shipment_evidence(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            evidence=_evidence("ev-one", ShipmentEvidenceType.FREIGHT_QUOTE),
            actor_id="sales-shipment",
            idempotency_key="evidence-replay",
        )
        replay_evidence = append_shipment_evidence(
            session,
            org_id=quote.org_id,
            shipment_handoff_id="shipment-1",
            evidence=_evidence("ev-one", ShipmentEvidenceType.FREIGHT_QUOTE),
            actor_id="sales-shipment",
            idempotency_key="evidence-replay",
        )
        assert replay_evidence.idempotent_replay
        with pytest.raises(ShipmentConflictError):
            append_shipment_evidence(
                session,
                org_id=quote.org_id,
                shipment_handoff_id="shipment-1",
                evidence=_evidence("ev-two", ShipmentEvidenceType.FREIGHT_QUOTE),
                actor_id="sales-shipment",
                idempotency_key="evidence-replay",
            )
        with pytest.raises(Exception, match="immutable"):
            session.execute(
                text(
                    "UPDATE trade_agent.shipment_handoff_evidence "
                    "SET status = 'void' WHERE evidence_id = :id"
                ),
                {"id": evidence.evidence_id},
            )


def test_shipment_api_enforces_org_and_reviewer_gate(factory):
    quote, _approval = _prepare(factory)
    client = TestClient(create_app(factory))
    headers = {
        "X-Org-Id": quote.org_id,
        "X-Actor-Id": "sales-shipment",
        "X-Role": "sales",
        "Idempotency-Key": "api-shipment-create",
    }
    created = client.post(
        "/api/v1/shipment-handoffs",
        headers=headers,
        json={
            "shipment_handoff_id": "shipment-api",
            "quotation_id": quote.quotation_id,
            "quote_revision_id": quote.revision_id,
            "approved_content_hash": quote.content_hash,
            "selected_incoterm": "FOB",
            "named_place": "Shanghai",
            "responsibility_split": {"freight": "buyer"},
            "freight_forwarder_quote_ref": "forwarder://api-1",
            "carrier_name": "Carrier API",
            "insurance_scope": "all risks",
            "insurance_expires_at": (CREATED_AT + timedelta(days=30)).isoformat(),
            "document_set_id": "docs-shipment",
            "required_documents": ["commercial_invoice", "packing_list"],
        },
    )
    assert created.status_code == 201
    hidden = client.get(
        "/api/v1/shipment-handoffs/shipment-api",
        headers={
            "X-Org-Id": "other-org",
            "X-Actor-Id": "sales-other",
            "X-Role": "sales",
        },
    )
    assert hidden.status_code == 404
    forbidden = client.post(
        "/api/v1/shipment-handoffs/shipment-api/transitions",
        headers={
            "X-Org-Id": quote.org_id,
            "X-Actor-Id": "sales-shipment",
            "X-Role": "sales",
            "Idempotency-Key": "api-shipment-ready",
        },
        json={"target_status": "ready_for_booking"},
    )
    assert forbidden.status_code == 403
