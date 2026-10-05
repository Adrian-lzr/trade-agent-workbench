from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
from pypdf import PdfReader
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.contracts import (
    CanonicalUnit,
    FactOrigin,
    FieldFact,
    ProductSnapshot,
    QuoteLineSnapshot,
    QuoteRevision,
    RFQItem,
    RFQPlan,
    RFQRevision,
    SourceEvidence,
    SpecificationFact,
)
from trade_agent.db.approvals import (
    ApprovalConflictError,
    ApprovalHashMismatchError,
    decide_quote_revision,
)
from trade_agent.db.clarifications import (
    ClarificationCommand,
    apply_persisted_clarification,
)
from trade_agent.db.decisions import (
    MatchDecisionCommand,
    MatchDecisionRejectedError,
    record_match_decision,
)
from trade_agent.db.exports import (
    ExportNotApprovedError,
    ExportPendingError,
    create_quote_artifact,
    get_quote_artifact,
)
from trade_agent.db.models import (
    AuditEventRecord,
    MatchDecisionRecord,
    QuotationAggregate,
    QuoteApprovalRecord,
    QuoteArtifactRecord,
    QuoteRevisionRecord,
    RFQAggregate,
    RFQRevisionRecord,
)
from trade_agent.db.repository import seed_synthetic_catalog
from trade_agent.db.revisions import (
    IdempotencyConflictError,
    RevisionConflictError,
    create_quote_revision,
    create_rfq_revision,
)
from trade_agent.drafting import (
    ManualProductSelection,
    QuoteDraftRequest,
    build_quote_draft,
)
from trade_agent.workflow.quote import build_quote_draft_from_match_decisions

DATABASE_URL = os.environ.get("TRADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.integration
CREATED_AT = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _QuoteRevisionOverrides:
    quotation_id: str = "quote_revision_integration_01"
    quote_item_id: str = "quote-item-001"


@pytest.fixture
def migrated_test_database():
    if not DATABASE_URL:
        pytest.skip("Set TRADE_TEST_DATABASE_URL to a disposable PostgreSQL *_test DB")
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TRADE_TEST_DATABASE_URL must use PostgreSQL")
    if not url.database or not url.database.endswith("_test"):
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


def _evidence(field: str) -> SourceEvidence:
    return SourceEvidence(
        field=field,
        source_document_id="doc_demo_01",
        location="body:line:2",
        quote="5000 pcs stainless bolts M8 x 30 A2-70",
    )


def _fact(field: str, value: str | None, raw: str | None = None) -> FieldFact:
    return FieldFact(
        field_name=field,
        origin=FactOrigin.EXTRACTED,
        raw_value=raw,
        normalized_value=value,
        evidence=(_evidence(field),),
    )


def _rfq_revision(
    revision_no: int,
    parent_revision_id: str | None,
    revision_id: str,
    quantity: str,
    *,
    rfq_id: str = "rfq_revision_integration_01",
) -> RFQRevision:
    quantity_value = Decimal(quantity)
    item = RFQItem(
        rfq_item_id="item_001",
        description_raw=f"stainless bolts M8 x 30 A2-70, {quantity} pcs",
        quantity=quantity_value,
        unit_raw="pcs",
        unit_canonical=CanonicalUnit.PIECE,
        specifications=(SpecificationFact(name="material_grade", value="A2-70"),),
        facts=(
            _fact("quantity", format(quantity_value.normalize(), "f"), quantity),
            _fact("unit", "piece", "pcs"),
            _fact("specifications.material_grade", "A2-70", "A2-70"),
        ),
        evidence=(_evidence("quantity"),),
    )
    plan_facts = (
        _fact("requested_currency", "USD"),
        _fact("customer_id", None),
        _fact("trade_term", None),
        _fact("named_place", None),
        _fact("requested_delivery_date", None),
    )
    return RFQRevision(
        org_id="org_demo_01",
        rfq_id=rfq_id,
        revision_id=revision_id,
        revision_no=revision_no,
        parent_revision_id=parent_revision_id,
        plan=RFQPlan(rfq_revision_id=revision_id, items=(item,), facts=plan_facts),
        created_at=CREATED_AT,
        created_by="sales_demo_01",
    )


def _quote_revision(
    revision_no: int,
    parent_revision_id: str | None,
    revision_id: str,
    body: str,
    *,
    overrides: _QuoteRevisionOverrides | None = None,
) -> QuoteRevision:
    overrides = overrides or _QuoteRevisionOverrides()
    line = QuoteLineSnapshot(
        quote_item_id=overrides.quote_item_id,
        rfq_item_id="item_001",
        product=ProductSnapshot(
            sku="BOLT-M8-30-A2",
            catalog_version="SYNTH-CAT-1",
            name="Hex bolt M8 x 30 A2-70",
            specifications=(SpecificationFact(name="material_grade", value="A2-70"),),
        ),
        quantity=Decimal("5000"),
        unit=CanonicalUnit.PIECE,
        price_list_version="SYNTH-USD-2026-01",
        price_list_source="Synthetic demo data; no commercial value",
        unit_price=Decimal("0.08"),
        price_unit=CanonicalUnit.PIECE,
        line_amount=Decimal("400.00"),
    )
    return QuoteRevision(
        org_id="org_demo_01",
        quotation_id=overrides.quotation_id,
        revision_id=revision_id,
        revision_no=revision_no,
        parent_revision_id=parent_revision_id,
        rfq_revision_id="rfq_revision_01",
        catalog_version="SYNTH-CAT-1",
        price_list_version="SYNTH-USD-2026-01",
        items=(line,),
        trade_term="FOB",
        named_place="Shanghai",
        response_body=body,
        template_version="quote-v1",
        created_at=CREATED_AT,
        created_by="sales_demo_01",
    )


def _assert_immutable_mutation_rejected(
    session: Session, statement: str, row_id: str
) -> None:
    with pytest.raises(DBAPIError, match="immutable"), session.begin_nested():
        session.execute(text(statement), {"row_id": row_id})


def test_rfq_revision_write_is_idempotent_append_only_and_version_checked(
    migrated_test_database,
) -> None:
    first = _rfq_revision(1, None, "rfqr_001", "5000")
    second = _rfq_revision(2, first.revision_id, "rfqr_002", "6000")
    stale = _rfq_revision(3, first.revision_id, "rfqr_003", "7000")

    with Session(migrated_test_database) as session, session.begin():
        result_v1 = create_rfq_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="rfq-create-v1",
        )
        replay = create_rfq_revision(
            session,
            _rfq_revision(1, None, "rfqr_001_retry", "5000"),
            expected_revision_id=None,
            idempotency_key="rfq-create-v1",
        )
        result_v2 = create_rfq_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="rfq-create-v2",
            reason="Corrected quantity",
        )

        assert result_v1.revision_id == replay.revision_id
        assert replay.idempotent_replay
        assert result_v2.revision_no == 2
        assert not result_v2.idempotent_replay
        with pytest.raises(RevisionConflictError, match="current revision"):
            create_rfq_revision(
                session,
                stale,
                expected_revision_id=first.revision_id,
                idempotency_key="rfq-stale-v3",
            )

        altered_request = _rfq_revision(1, None, "rfqr_changed", "5500")
        with pytest.raises(IdempotencyConflictError, match="different request"):
            create_rfq_revision(
                session,
                altered_request,
                expected_revision_id=None,
                idempotency_key="rfq-create-v1",
            )

        aggregate = session.get(RFQAggregate, first.rfq_id)
        revision_count = session.scalar(
            select(func.count()).select_from(RFQRevisionRecord)
        )
        audit_count = session.scalar(select(func.count()).select_from(AuditEventRecord))
        assert aggregate is not None
        assert aggregate.current_revision_id == second.revision_id
        assert revision_count == 2
        assert audit_count == 2


def test_idempotency_key_cannot_be_reused_across_endpoints(
    migrated_test_database,
) -> None:
    rfq_revision = _rfq_revision(1, None, "rfqr_cross_endpoint_001", "5000")
    quote_revision = _quote_revision(1, None, "quote_cross_endpoint_001", "Draft")

    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            rfq_revision,
            expected_revision_id=None,
            idempotency_key="shared-endpoint-key",
        )
        with pytest.raises(IdempotencyConflictError, match="different endpoint"):
            create_quote_revision(
                session,
                quote_revision,
                expected_revision_id=None,
                idempotency_key="shared-endpoint-key",
            )
        assert session.get(QuotationAggregate, quote_revision.quotation_id) is None


def test_quote_revision_graph_action_key_replays_after_request_retry(
    migrated_test_database,
) -> None:
    overrides = _QuoteRevisionOverrides(quotation_id="quote_action_idempotency_01")
    first = _quote_revision(1, None, "quote_action_001", "Draft", overrides=overrides)
    retry = _quote_revision(1, None, "quote_action_retry", "Draft", overrides=overrides)
    action = {
        "run_id": "run-quote-action-01",
        "action_type": "quote_revision.create",
        "business_version": first.rfq_revision_id,
    }

    with Session(migrated_test_database) as session, session.begin():
        stored = create_quote_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="request-1",
            **action,
        )
        replay = create_quote_revision(
            session,
            retry,
            expected_revision_id=None,
            idempotency_key="request-after-worker-retry",
            **action,
        )
        assert replay.idempotent_replay
        assert replay.revision_id == stored.revision_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(QuoteRevisionRecord)
                .where(
                    QuoteRevisionRecord.run_id == action["run_id"],
                    QuoteRevisionRecord.action_type == action["action_type"],
                    QuoteRevisionRecord.business_version == action["business_version"],
                )
            )
            == 1
        )


def test_concurrent_rfq_writers_only_advance_expected_revision_once(
    migrated_test_database,
) -> None:
    rfq_id = "rfq_revision_concurrent_01"
    first = _rfq_revision(1, None, "rfqr_concurrent_001", "5000", rfq_id=rfq_id)
    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="rfq-concurrent-v1",
        )

    barrier = Barrier(2)

    def append(revision_id: str, quantity: str) -> tuple[str, str | None]:
        revision = _rfq_revision(
            2,
            first.revision_id,
            revision_id,
            quantity,
            rfq_id=rfq_id,
        )
        barrier.wait(timeout=10)
        try:
            with Session(migrated_test_database) as session, session.begin():
                result = create_rfq_revision(
                    session,
                    revision,
                    expected_revision_id=first.revision_id,
                    idempotency_key=f"{revision_id}-key",
                )
            return "created", result.revision_id
        except RevisionConflictError:
            return "conflict", None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(append, "rfqr_concurrent_a", "6000"),
            executor.submit(append, "rfqr_concurrent_b", "7000"),
        )
        outcomes = tuple(future.result(timeout=15) for future in futures)

    assert sorted(result for result, _ in outcomes) == ["conflict", "created"]
    winning_revision_id = next(
        revision_id for result, revision_id in outcomes if result == "created"
    )
    with Session(migrated_test_database) as session:
        aggregate = session.get(RFQAggregate, rfq_id)
        revision_count = session.scalar(
            select(func.count())
            .select_from(RFQRevisionRecord)
            .where(RFQRevisionRecord.rfq_id == rfq_id)
        )
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(
                AuditEventRecord.aggregate_type == "rfq",
                AuditEventRecord.aggregate_id == rfq_id,
            )
        )

    assert aggregate is not None
    assert aggregate.current_revision_id == winning_revision_id
    assert revision_count == 2
    assert audit_count == 2


def test_quote_revision_replay_preserves_hash_and_new_body_creates_new_hash(
    migrated_test_database,
) -> None:
    first = _quote_revision(1, None, "quote_rev_001", "Please review draft v1.")
    second = _quote_revision(
        2, first.revision_id, "quote_rev_002", "Please review draft v2."
    )

    with Session(migrated_test_database) as session, session.begin():
        result_v1 = create_quote_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="quote-create-v1",
        )
        replay = create_quote_revision(
            session,
            _quote_revision(
                1,
                None,
                "quote_rev_001_retry",
                "Please review draft v1.",
                overrides=_QuoteRevisionOverrides(quote_item_id="quote-item-retry"),
            ),
            expected_revision_id=None,
            idempotency_key="quote-create-v1",
        )
        result_v2 = create_quote_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="quote-create-v2",
        )

        assert result_v1.content_hash == replay.content_hash == first.content_hash
        assert replay.revision_id == first.revision_id
        assert replay.idempotent_replay
        assert result_v2.content_hash == second.content_hash
        assert result_v2.content_hash != result_v1.content_hash
        aggregate = session.get(QuotationAggregate, first.quotation_id)
        stored = session.get(QuoteRevisionRecord, second.revision_id)
        assert aggregate is not None
        assert aggregate.current_revision_id == second.revision_id
        assert stored is not None
        assert stored.content_hash == second.content_hash


def test_database_rejects_revision_update_and_delete(migrated_test_database) -> None:
    rfq_revision = _rfq_revision(1, None, "rfqr_immutable_001", "5000")
    quote_revision = _quote_revision(1, None, "quote_immutable_001", "Draft")
    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            rfq_revision,
            expected_revision_id=None,
            idempotency_key="rfq-immutable-v1",
        )
        create_quote_revision(
            session,
            quote_revision,
            expected_revision_id=None,
            idempotency_key="quote-immutable-v1",
        )
        audit_id = session.scalar(select(AuditEventRecord.event_id).limit(1))
        assert audit_id is not None

        immutable_mutations = (
            (
                "UPDATE trade_agent.rfq_revisions SET created_by = created_by "
                "WHERE revision_id = :row_id",
                "DELETE FROM trade_agent.rfq_revisions WHERE revision_id = :row_id",
                rfq_revision.revision_id,
            ),
            (
                "UPDATE trade_agent.quote_revisions SET created_by = created_by "
                "WHERE revision_id = :row_id",
                "DELETE FROM trade_agent.quote_revisions WHERE revision_id = :row_id",
                quote_revision.revision_id,
            ),
            (
                "UPDATE trade_agent.audit_events SET actor_id = actor_id "
                "WHERE event_id = :row_id",
                "DELETE FROM trade_agent.audit_events WHERE event_id = :row_id",
                audit_id,
            ),
        )
        for update, delete, row_id in immutable_mutations:
            _assert_immutable_mutation_rejected(session, update, row_id)
            _assert_immutable_mutation_rejected(session, delete, row_id)


def test_current_pointer_revision_number_is_database_enforced(
    migrated_test_database,
) -> None:
    first = _rfq_revision(1, None, "rfqr_pointer_001", "5000")
    second = _rfq_revision(2, first.revision_id, "rfqr_pointer_002", "6000")
    other = _rfq_revision(
        1,
        None,
        "rfqr_pointer_other_001",
        "5000",
        rfq_id="rfq_pointer_other_01",
    )
    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="rfq-pointer-v1",
        )
        create_rfq_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="rfq-pointer-v2",
        )
        create_rfq_revision(
            session,
            other,
            expected_revision_id=None,
            idempotency_key="rfq-pointer-other-v1",
        )
        session.execute(text("SET CONSTRAINTS fk_rfqs_current_revision IMMEDIATE"))
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.rfqs "
                    "SET current_revision_no = 1 WHERE rfq_id = :rfq_id"
                ),
                {"rfq_id": first.rfq_id},
            )
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.rfqs "
                    "SET current_revision_id = :revision_id, current_revision_no = 1 "
                    "WHERE rfq_id = :rfq_id"
                ),
                {"rfq_id": first.rfq_id, "revision_id": other.revision_id},
            )


def test_quote_current_pointer_rejects_wrong_number_and_other_aggregate(
    migrated_test_database,
) -> None:
    first = _quote_revision(1, None, "quote_pointer_001", "Draft v1")
    second = _quote_revision(2, first.revision_id, "quote_pointer_002", "Draft v2")
    other = _quote_revision(
        1,
        None,
        "quote_pointer_other_001",
        "Other draft",
        overrides=_QuoteRevisionOverrides(quotation_id="quote_pointer_other_01"),
    )

    with Session(migrated_test_database) as session, session.begin():
        create_quote_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="quote-pointer-v1",
        )
        create_quote_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="quote-pointer-v2",
        )
        create_quote_revision(
            session,
            other,
            expected_revision_id=None,
            idempotency_key="quote-pointer-other-v1",
        )

        session.execute(
            text("SET CONSTRAINTS fk_quotations_current_revision IMMEDIATE")
        )
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.quotations "
                    "SET current_revision_no = 1 WHERE quotation_id = :quotation_id"
                ),
                {"quotation_id": first.quotation_id},
            )
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.quotations "
                    "SET current_revision_id = :revision_id, current_revision_no = 1 "
                    "WHERE quotation_id = :quotation_id"
                ),
                {
                    "quotation_id": first.quotation_id,
                    "revision_id": other.revision_id,
                },
            )


def test_approval_binds_hash_and_old_page_cannot_approve(
    migrated_test_database,
) -> None:
    quotation_id = "approval_contract_quote"
    overrides = _QuoteRevisionOverrides(quotation_id=quotation_id)
    first = _quote_revision(
        1, None, "approval_quote_v1", "Review v1", overrides=overrides
    )
    second = _quote_revision(
        2,
        first.revision_id,
        "approval_quote_v2",
        "Review v2",
        overrides=overrides,
    )

    with Session(migrated_test_database) as session, session.begin():
        create_quote_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="approval-quote-v1",
        )
        decision = decide_quote_revision(
            session,
            org_id=first.org_id,
            quotation_id=quotation_id,
            revision_id=first.revision_id,
            content_hash=first.content_hash,
            decision="approved",
            actor_id="reviewer-1",
            idempotency_key="approval-v1",
        )
        replay = decide_quote_revision(
            session,
            org_id=first.org_id,
            quotation_id=quotation_id,
            revision_id=first.revision_id,
            content_hash=first.content_hash,
            decision="approved",
            actor_id="reviewer-1",
            idempotency_key="approval-v1",
        )
        assert decision.approval_id == replay.approval_id
        assert replay.idempotent_replay

        with pytest.raises(ApprovalHashMismatchError):
            decide_quote_revision(
                session,
                org_id=first.org_id,
                quotation_id=quotation_id,
                revision_id=first.revision_id,
                content_hash="0" * 64,
                decision="approved",
                actor_id="reviewer-2",
                idempotency_key="approval-stale-hash",
            )

        create_quote_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="approval-quote-v2",
        )
        with pytest.raises(ApprovalConflictError):
            decide_quote_revision(
                session,
                org_id=first.org_id,
                quotation_id=quotation_id,
                revision_id=first.revision_id,
                content_hash=first.content_hash,
                decision="approved",
                actor_id="reviewer-3",
                idempotency_key="approval-old-page",
            )

        approvals = session.scalars(
            select(QuoteApprovalRecord).where(
                QuoteApprovalRecord.quotation_id == quotation_id
            )
        ).all()
        assert len(approvals) == 1
        assert approvals[0].content_hash == first.content_hash


def test_concurrent_reviewers_only_one_final_decision_wins(
    migrated_test_database,
) -> None:
    quotation_id = "approval_concurrent_quote"
    quote = _quote_revision(
        1,
        None,
        "approval_concurrent_v1",
        "Review concurrently",
        overrides=_QuoteRevisionOverrides(quotation_id=quotation_id),
    )
    with Session(migrated_test_database) as session, session.begin():
        create_quote_revision(
            session,
            quote,
            expected_revision_id=None,
            idempotency_key="approval-concurrent-quote",
        )

    barrier = Barrier(2)

    def decide(actor_id: str) -> str:
        barrier.wait(timeout=10)
        try:
            with Session(migrated_test_database) as session, session.begin():
                decide_quote_revision(
                    session,
                    org_id=quote.org_id,
                    quotation_id=quotation_id,
                    revision_id=quote.revision_id,
                    content_hash=quote.content_hash,
                    decision="approved",
                    actor_id=actor_id,
                    idempotency_key=f"approval-{actor_id}",
                )
            return "approved"
        except ApprovalConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(
            future.result(timeout=15)
            for future in (
                executor.submit(decide, "reviewer-a"),
                executor.submit(decide, "reviewer-b"),
            )
        )

    assert sorted(outcomes) == ["approved", "conflict"]
    with Session(migrated_test_database) as session:
        approvals = session.scalars(
            select(QuoteApprovalRecord).where(
                QuoteApprovalRecord.quotation_id == quotation_id
            )
        ).all()
    assert len(approvals) == 1


def test_export_requires_approval_is_idempotent_and_is_private_pdf(
    migrated_test_database,
) -> None:
    quotation_id = "export_contract_quote"
    quote = _quote_revision(
        1,
        None,
        "export_quote_v1",
        "Draft",
        overrides=_QuoteRevisionOverrides(quotation_id=quotation_id),
    )

    with Session(migrated_test_database) as session, session.begin():
        create_quote_revision(
            session,
            quote,
            expected_revision_id=None,
            idempotency_key="export-quote-v1",
        )
        with pytest.raises(ExportNotApprovedError):
            create_quote_artifact(
                session,
                org_id=quote.org_id,
                quotation_id=quotation_id,
                revision_id=quote.revision_id,
                artifact_type="proforma_invoice",
                actor_id="sales-1",
                idempotency_key="export-before-approval",
            )
        decide_quote_revision(
            session,
            org_id=quote.org_id,
            quotation_id=quotation_id,
            revision_id=quote.revision_id,
            content_hash=quote.content_hash,
            decision="approved",
            actor_id="reviewer-1",
            idempotency_key="export-approval",
        )
        artifact = create_quote_artifact(
            session,
            org_id=quote.org_id,
            quotation_id=quotation_id,
            revision_id=quote.revision_id,
            artifact_type="proforma_invoice",
            actor_id="sales-1",
            idempotency_key="export-file",
            requested_content_hash=quote.content_hash,
        )
        replay = create_quote_artifact(
            session,
            org_id=quote.org_id,
            quotation_id=quotation_id,
            revision_id=quote.revision_id,
            artifact_type="proforma_invoice",
            actor_id="sales-1",
            idempotency_key="export-file",
            requested_content_hash=quote.content_hash,
        )
        assert artifact.artifact_id == replay.artifact_id
        assert replay.idempotent_replay
        assert artifact.amount_total == Decimal("400.00")
        stored = get_quote_artifact(
            session, org_id=quote.org_id, artifact_id=artifact.artifact_id
        )
        assert stored.content_bytes is not None
        assert stored.content_bytes.startswith(b"%PDF-")
        assert stored.content_sha256 == artifact.content_sha256
        assert stored.snapshot["amount_total"] == "400.00"
        pdf_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(BytesIO(stored.content_bytes)).pages
        )
        assert "Quotation: export_contract_quote" in pdf_text
        assert "Revision: export_quote_v1" in pdf_text
        assert "BOLT-M8-30-A2" in pdf_text
        assert "5000 piece" in pdf_text
        assert "0.0800 USD" in pdf_text
        assert "TOTAL: 400.00 USD" in pdf_text

        session.execute(
            QuoteArtifactRecord.__table__.update()
            .where(QuoteArtifactRecord.artifact_id == artifact.artifact_id)
            .values(status="failed", content_bytes=None)
        )
        session.expire_all()
        retry = create_quote_artifact(
            session,
            org_id=quote.org_id,
            quotation_id=quotation_id,
            revision_id=quote.revision_id,
            artifact_type="proforma_invoice",
            actor_id="sales-1",
            idempotency_key="export-file-retry",
            requested_content_hash=quote.content_hash,
        )
        assert retry.artifact_id == artifact.artifact_id
        assert not retry.idempotent_replay

        session.execute(
            QuoteArtifactRecord.__table__.update()
            .where(QuoteArtifactRecord.artifact_id == artifact.artifact_id)
            .values(status="pending", content_bytes=None)
        )
        session.expire_all()
        with pytest.raises(ExportPendingError):
            get_quote_artifact(
                session, org_id=quote.org_id, artifact_id=artifact.artifact_id
            )


def test_confirmed_rfq_selection_builds_and_persists_a_quote_draft(
    migrated_test_database,
) -> None:
    rfq_revision = _rfq_revision(1, None, "rfqr_draft_001", "5000")
    result = build_quote_draft(
        QuoteDraftRequest(
            rfq_revision=rfq_revision,
            catalog=build_synthetic_catalog(),
            selected_products=(ManualProductSelection("item_001", "BOLT-M8-30-A2"),),
            quotation_id="quote_from_draft_001",
            revision_id="quote_from_draft_rev_001",
            revision_no=1,
            parent_revision_id=None,
            created_by="sales_demo_01",
            created_at=CREATED_AT,
            as_of=date(2026, 9, 29),
            valid_until=date(2026, 10, 29),
            template_version="quote-v1",
        )
    )

    assert result.is_ready
    assert result.quote_revision is not None
    assert result.quote_revision.items[0].line_amount == Decimal("400.00")

    with Session(migrated_test_database) as session, session.begin():
        stored = create_quote_revision(
            session,
            result.quote_revision,
            expected_revision_id=None,
            idempotency_key="quote-from-draft-v1",
        )

        assert stored.revision_id == "quote_from_draft_rev_001"
        aggregate = session.get(QuotationAggregate, "quote_from_draft_001")
        record = session.get(QuoteRevisionRecord, "quote_from_draft_rev_001")
        assert aggregate is not None
        assert aggregate.current_revision_id == result.quote_revision.revision_id
        assert record is not None
        assert record.content_hash == result.quote_revision.content_hash


def test_saved_match_decision_drives_m1_quote_draft_and_persistence(
    migrated_test_database,
) -> None:
    revision = _rfq_revision(1, None, "rfqr_decision_quote_001", "5000")
    catalog = build_synthetic_catalog()

    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, catalog)
        create_rfq_revision(
            session,
            revision,
            expected_revision_id=None,
            idempotency_key="rfq-decision-quote-001",
        )
        record_match_decision(
            session,
            _match_decision_command(
                revision,
                decision_id="match-decision-quote-001",
                idempotency_key="match-decision-quote-key-001",
            ),
        )
        result = build_quote_draft_from_match_decisions(
            session,
            QuoteDraftRequest(
                rfq_revision=revision,
                catalog=catalog,
                selected_products=(),
                quotation_id="quote_from_saved_match_001",
                revision_id="quote_from_saved_match_rev_001",
                revision_no=1,
                parent_revision_id=None,
                created_by="sales_demo_01",
                created_at=CREATED_AT,
                as_of=date(2026, 9, 29),
                valid_until=date(2026, 10, 29),
                template_version="quote-v1",
            ),
        )

        assert result.is_ready
        assert result.quote_revision is not None
        assert result.quote_revision.items[0].product.sku == "BOLT-M8-30-A2"
        assert result.quote_revision.items[0].line_amount == Decimal("400.00")
        stored = create_quote_revision(
            session,
            result.quote_revision,
            expected_revision_id=None,
            idempotency_key="quote-from-saved-match-001",
        )
        assert stored.revision_id == result.quote_revision.revision_id


def _match_decision_command(
    revision: RFQRevision,
    *,
    decision_id: str,
    idempotency_key: str,
    selected_sku: str = "BOLT-M8-30-A2",
    item_id: str = "item_001",
) -> MatchDecisionCommand:
    return MatchDecisionCommand(
        decision_id=decision_id,
        org_id=revision.org_id,
        rfq_id=revision.rfq_id,
        rfq_revision_id=revision.revision_id,
        rfq_item_id=item_id,
        catalog_version="SYNTH-CAT-1",
        selected_sku=selected_sku,
        actor_id="sales_demo_01",
        idempotency_key=idempotency_key,
        reason="Salesperson confirmed the reviewed material and dimensions.",
        created_at=CREATED_AT,
    )


def _incomplete_rfq_revision(
    revision_id: str,
    *,
    rfq_id: str,
) -> RFQRevision:
    complete = _rfq_revision(1, None, revision_id, "5000", rfq_id=rfq_id)
    source_item = complete.plan.items[0]
    item = RFQItem.model_validate({
        **source_item.model_dump(mode="python"),
        "quantity": None,
        "unit_raw": "boxes",
        "unit_canonical": None,
        "missing_fields": ("quantity", "unit"),
        "facts": (
            _fact("quantity", None),
            _fact("unit", None),
            source_item.facts[-1],
        ),
    })
    plan = RFQPlan.model_validate({
        **complete.plan.model_dump(mode="python"),
        "items": (item,),
    })
    return RFQRevision.model_validate({
        **complete.model_dump(mode="python"),
        "plan": plan,
    })


def test_match_decision_is_current_version_checked_audited_and_idempotent(
    migrated_test_database,
) -> None:
    revision = _rfq_revision(1, None, "rfqr_match_001", "5000")
    command = _match_decision_command(
        revision,
        decision_id="match_decision_001",
        idempotency_key="match-choice-001",
    )
    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        create_rfq_revision(
            session,
            revision,
            expected_revision_id=None,
            idempotency_key="rfq-match-001",
        )
        stored = record_match_decision(session, command)
        replay = record_match_decision(
            session,
            command.model_copy(update={"decision_id": "ignored-replay-id"}),
        )
        decision = session.get(MatchDecisionRecord, stored.decision_id)
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditEventRecord)
            .where(AuditEventRecord.action == "match_decision_recorded")
        )

        assert stored.decision_id == "match_decision_001"
        assert stored.idempotent_replay is False
        assert replay.decision_id == stored.decision_id
        assert replay.idempotent_replay is True
        assert decision is not None
        assert decision.rfq_revision_id == revision.revision_id
        assert decision.selected_sku == "BOLT-M8-30-A2"
        assert decision.actor_id == "sales_demo_01"
        assert audit_count == 1
        with pytest.raises(IdempotencyConflictError, match="different request"):
            record_match_decision(
                session,
                command.model_copy(update={"selected_sku": "BOLT-M8-30-A4"}),
            )
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.match_decisions SET reason = reason "
                    "WHERE decision_id = :row_id"
                ),
                {"row_id": stored.decision_id},
            )
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "DELETE FROM trade_agent.match_decisions "
                    "WHERE decision_id = :row_id"
                ),
                {"row_id": stored.decision_id},
            )


def test_match_decision_rejects_spec_conflict_and_unclarified_item(
    migrated_test_database,
) -> None:
    complete = _rfq_revision(1, None, "rfqr_match_conflict", "5000")
    incomplete = _incomplete_rfq_revision(
        "rfqr_match_incomplete",
        rfq_id="rfq_match_incomplete",
    )
    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        create_rfq_revision(
            session,
            complete,
            expected_revision_id=None,
            idempotency_key="rfq-match-conflict",
        )
        create_rfq_revision(
            session,
            incomplete,
            expected_revision_id=None,
            idempotency_key="rfq-match-incomplete",
        )

        with pytest.raises(MatchDecisionRejectedError, match="specification fields"):
            record_match_decision(
                session,
                _match_decision_command(
                    complete,
                    decision_id="match-conflict-001",
                    idempotency_key="match-conflict-001",
                    selected_sku="BOLT-M8-30-A4",
                ),
            )
        with pytest.raises(MatchDecisionRejectedError, match="must be clarified"):
            record_match_decision(
                session,
                _match_decision_command(
                    incomplete,
                    decision_id="match-incomplete-001",
                    idempotency_key="match-incomplete-001",
                ),
            )

        count = session.scalar(select(func.count()).select_from(MatchDecisionRecord))
        assert count == 0


def test_match_decision_rejects_stale_rfq_revision(migrated_test_database) -> None:
    first = _rfq_revision(1, None, "rfqr_match_stale_001", "5000")
    second = _rfq_revision(2, first.revision_id, "rfqr_match_stale_002", "6000")
    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, build_synthetic_catalog())
        create_rfq_revision(
            session,
            first,
            expected_revision_id=None,
            idempotency_key="rfq-match-stale-v1",
        )
        create_rfq_revision(
            session,
            second,
            expected_revision_id=first.revision_id,
            idempotency_key="rfq-match-stale-v2",
        )

        with pytest.raises(RevisionConflictError, match="no longer current"):
            record_match_decision(
                session,
                _match_decision_command(
                    first,
                    decision_id="match-stale-001",
                    idempotency_key="match-stale-001",
                ),
            )

        assert (
            session.scalar(select(func.count()).select_from(MatchDecisionRecord)) == 0
        )


def _clarification_command(
    revision: RFQRevision,
    *,
    answers: dict[str, dict[str, object]],
    idempotency_key: str,
    revision_id: str,
) -> ClarificationCommand:
    return ClarificationCommand(
        org_id=revision.org_id,
        rfq_id=revision.rfq_id,
        expected_revision_id=revision.revision_id,
        revision_id=revision_id,
        actor_id="sales_demo_02",
        idempotency_key=idempotency_key,
        reason="Buyer confirmed the order quantity.",
        answers=answers,
        created_at=CREATED_AT,
    )


def test_persisted_clarification_is_versioned_audited_partial_and_idempotent(
    migrated_test_database,
) -> None:
    initial = _incomplete_rfq_revision(
        "rfqr_clarification_001",
        rfq_id="rfq_clarification_001",
    )
    command = _clarification_command(
        initial,
        answers={"item_001": {"quantity": "5"}},
        idempotency_key="clarification-quantity-001",
        revision_id="rfqr_clarification_002",
    )

    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            initial,
            expected_revision_id=None,
            idempotency_key="rfq-clarification-001",
        )
        result = apply_persisted_clarification(session, command)
        replay = apply_persisted_clarification(
            session,
            command.model_copy(
                update={
                    "revision_id": "ignored-clarification-retry",
                    "created_at": datetime(2026, 9, 29, 8, 0, tzinfo=UTC),
                }
            ),
        )

        assert result.revision.revision_id == "rfqr_clarification_002"
        assert result.revision.parent_revision_id == initial.revision_id
        assert result.revision.revision_no == 2
        assert result.idempotent_replay is False
        assert replay.revision.revision_id == result.revision.revision_id
        assert replay.idempotent_replay is True
        item = result.revision.plan.items[0]
        assert item.quantity == Decimal("5")
        assert item.unit_canonical is None
        assert item.unit_raw == "boxes"
        assert item.missing_fields == ("unit",)
        assert item.description_raw == initial.plan.items[0].description_raw
        quantity_fact = next(
            fact for fact in item.facts if fact.field_name == "quantity"
        )
        assert quantity_fact.confirmed_by == "sales_demo_02"
        assert quantity_fact.confirmed_at == CREATED_AT
        assert quantity_fact.evidence == initial.plan.items[0].facts[0].evidence

        aggregate = session.get(RFQAggregate, initial.rfq_id)
        revision_count = session.scalar(
            select(func.count()).select_from(RFQRevisionRecord)
        )
        events = session.scalars(
            select(AuditEventRecord).where(
                AuditEventRecord.aggregate_type == "rfq",
                AuditEventRecord.aggregate_id == initial.rfq_id,
            )
        ).all()
        assert aggregate is not None
        assert aggregate.current_revision_id == result.revision.revision_id
        assert revision_count == 2
        assert {event.action for event in events} == {"created", "revision_created"}

        with pytest.raises(IdempotencyConflictError, match="different request"):
            apply_persisted_clarification(
                session,
                command.model_copy(update={"answers": {"item_001": {"quantity": "6"}}}),
            )


def test_persisted_clarification_rejects_a_stale_expected_revision(
    migrated_test_database,
) -> None:
    initial = _rfq_revision(
        1,
        None,
        "rfqr_clarification_stale_001",
        "5000",
        rfq_id="rfq_clarification_stale_001",
    )
    current = _rfq_revision(
        2,
        initial.revision_id,
        "rfqr_clarification_stale_002",
        "6000",
        rfq_id=initial.rfq_id,
    )
    command = _clarification_command(
        initial,
        answers={"item_001": {"quantity": "7000"}},
        idempotency_key="clarification-stale-001",
        revision_id="rfqr_clarification_stale_003",
    )

    with Session(migrated_test_database) as session, session.begin():
        create_rfq_revision(
            session,
            initial,
            expected_revision_id=None,
            idempotency_key="rfq-clarification-stale-v1",
        )
        create_rfq_revision(
            session,
            current,
            expected_revision_id=initial.revision_id,
            idempotency_key="rfq-clarification-stale-v2",
        )

        with pytest.raises(RevisionConflictError, match="no longer current"):
            apply_persisted_clarification(session, command)

        revision_count = session.scalar(
            select(func.count()).select_from(RFQRevisionRecord)
        )
        assert revision_count == 2
