"""PostgreSQL vertical-slice tests for qualification and screening evidence."""

from datetime import UTC, datetime
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from trade_agent.api import create_app
from trade_agent.contracts import QualificationCheck, QualificationResult
from trade_agent.db.models import RFQAggregate, RFQRevisionRecord
from trade_agent.db.qualification import (
    QualificationAuthorizationError,
    QualificationGateError,
    QualificationHashMismatchError,
    QualificationIdempotencyConflictError,
    QualificationNotFoundError,
    create_qualification_check,
    decide_qualification_check,
    ensure_quote_qualification_clear,
    get_qualification_check,
)

DATABASE_URL = os.environ.get("TRADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.integration


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


@pytest.fixture
def factory(migrated_test_database):
    return sessionmaker(migrated_test_database, expire_on_commit=False)


def _check(
    *,
    check_id: str = "check-a",
    actor_id: str = "sales-1",
    result: QualificationResult = QualificationResult.UNVERIFIED,
    evidence_hash: str = "a" * 64,
    customer_id: str = "customer-a",
) -> QualificationCheck:
    return QualificationCheck(
        org_id="org-a",
        check_id=check_id,
        customer_id=customer_id,
        source="ita_consolidated_screening_list",
        reference="https://www.trade.gov/consolidated-screening-list",
        checked_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        result=result,
        evidence_hash=evidence_hash,
        evidence_attachment_ref="s3://evidence/check-a.json",
        notes="Screened against the current source snapshot.",
        created_at=datetime(2026, 10, 1, 8, 1, tzinfo=UTC),
        created_by=actor_id,
    )


def test_create_is_idempotent_and_clear_requires_reviewer(factory) -> None:
    with factory() as session, session.begin():
        with pytest.raises(QualificationAuthorizationError):
            create_qualification_check(
                session,
                _check(result=QualificationResult.CLEAR),
                actor_role="sales",
                idempotency_key="clear-sales",
            )

        first = create_qualification_check(
            session,
            _check(),
            actor_role="sales",
            idempotency_key="check-1",
        )
        replay = create_qualification_check(
            session,
            _check(check_id="different-id"),
            actor_role="sales",
            idempotency_key="check-1",
        )
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert replay.check_id == "check-a"

        with pytest.raises(QualificationIdempotencyConflictError):
            create_qualification_check(
                session,
                _check(check_id="check-b", evidence_hash="b" * 64),
                actor_role="sales",
                idempotency_key="check-1",
            )


def test_decision_requires_exact_hash_and_is_org_scoped(factory) -> None:
    with factory() as session, session.begin():
        create_qualification_check(
            session,
            _check(),
            actor_role="sales",
            idempotency_key="check-1",
        )
        with pytest.raises(QualificationAuthorizationError):
            decide_qualification_check(
                session,
                org_id="org-a",
                check_id="check-a",
                evidence_hash="a" * 64,
                result=QualificationResult.CLEAR,
                actor_id="sales-1",
                actor_role="sales",
                idempotency_key="decision-sales",
            )
        with pytest.raises(QualificationHashMismatchError):
            decide_qualification_check(
                session,
                org_id="org-a",
                check_id="check-a",
                evidence_hash="b" * 64,
                result=QualificationResult.CLEAR,
                actor_id="reviewer-1",
                actor_role="reviewer",
                idempotency_key="decision-bad-hash",
            )
        decision = decide_qualification_check(
            session,
            org_id="org-a",
            check_id="check-a",
            evidence_hash="a" * 64,
            result=QualificationResult.CLEAR,
            actor_id="reviewer-1",
            actor_role="reviewer",
            idempotency_key="decision-1",
        )
        replay = decide_qualification_check(
            session,
            org_id="org-a",
            check_id="check-a",
            evidence_hash="a" * 64,
            result=QualificationResult.CLEAR,
            actor_id="reviewer-1",
            actor_role="reviewer",
            idempotency_key="decision-1",
        )
        assert decision.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert replay.decision_id == decision.decision_id

        assert (
            get_qualification_check(session, org_id="other-org", check_id="check-a")
            is None
        )
        with pytest.raises(QualificationNotFoundError):
            decide_qualification_check(
                session,
                org_id="other-org",
                check_id="check-a",
                evidence_hash="a" * 64,
                result=QualificationResult.CLEAR,
                actor_id="reviewer-2",
                actor_role="reviewer",
                idempotency_key="decision-other-org",
            )


def test_api_exposes_effective_result_and_enforces_roles(factory) -> None:
    client = TestClient(create_app(factory))
    headers = {
        "X-Org-Id": "org-a",
        "X-Actor-Id": "sales-1",
        "X-Role": "sales",
        "Idempotency-Key": "check-api-1",
    }
    body = {
        "customer_id": "customer-a",
        "source": "ita_consolidated_screening_list",
        "reference": "https://www.trade.gov/consolidated-screening-list",
        "checked_at": "2026-10-01T08:00:00Z",
        "result": "potential_match",
        "evidence_hash": "a" * 64,
        "evidence_attachment_ref": "s3://evidence/check-api.json",
        "notes": "Potential hit requires human confirmation.",
    }
    created = client.post("/api/v1/qualification-checks", headers=headers, json=body)
    assert created.status_code == 201
    check_id = created.json()["check_id"]
    forbidden_clear = client.post(
        "/api/v1/qualification-checks",
        headers={**headers, "Idempotency-Key": "check-api-clear"},
        json={**body, "result": "clear"},
    )
    assert forbidden_clear.status_code == 403

    decision = client.post(
        f"/api/v1/qualification-checks/{check_id}/decision",
        headers={
            "X-Org-Id": "org-a",
            "X-Actor-Id": "reviewer-1",
            "X-Role": "reviewer",
            "Idempotency-Key": "decision-api-1",
        },
        json={
            "evidence_hash": "a" * 64,
            "result": "clear",
            "notes": "Reviewed against the source snapshot.",
        },
    )
    assert decision.status_code == 201
    assert decision.json()["reviewer_id"] == "reviewer-1"

    detail = client.get(
        f"/api/v1/qualification-checks/{check_id}",
        headers={k: v for k, v in headers.items() if k != "Idempotency-Key"},
    )
    assert detail.status_code == 200
    assert detail.json()["effective_result"] == "clear"
    assert detail.json()["decision"]["reviewer_id"] == "reviewer-1"

    hidden = client.get(
        f"/api/v1/qualification-checks/{check_id}",
        headers={
            "X-Org-Id": "other-org",
            "X-Actor-Id": "sales-2",
            "X-Role": "sales",
        },
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "qualification_not_found"


def test_quote_gate_blocks_unresolved_screening_until_clear(factory) -> None:
    created_at = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    with factory() as session, session.begin():
        aggregate = RFQAggregate(
            rfq_id="rfq-qualification-gate",
            org_id="org-a",
            current_revision_id=None,
            current_revision_no=None,
            created_at=created_at,
            updated_at=created_at,
        )
        revision = RFQRevisionRecord(
            revision_id="rfq-qualification-gate-rev-1",
            org_id="org-a",
            rfq_id="rfq-qualification-gate",
            revision_no=1,
            parent_revision_id=None,
            payload={"plan": {"customer_id": "customer-a"}},
            created_at=created_at,
            created_by="sales-1",
        )
        session.add_all([aggregate, revision])
        session.flush()
        aggregate.current_revision_id = revision.revision_id
        aggregate.current_revision_no = revision.revision_no
        create_qualification_check(
            session,
            _check(result=QualificationResult.POTENTIAL_MATCH),
            actor_role="sales",
            idempotency_key="gate-check",
        )
        with pytest.raises(QualificationGateError, match="blocked"):
            ensure_quote_qualification_clear(
                session,
                org_id="org-a",
                rfq_revision_id=revision.revision_id,
            )
        decide_qualification_check(
            session,
            org_id="org-a",
            check_id="check-a",
            evidence_hash="a" * 64,
            result=QualificationResult.CLEAR,
            actor_id="reviewer-1",
            actor_role="reviewer",
            idempotency_key="gate-decision",
        )
        ensure_quote_qualification_clear(
            session,
            org_id="org-a",
            rfq_revision_id=revision.revision_id,
        )


def test_postgres_trigger_rejects_mutation(factory) -> None:
    with factory() as session, session.begin():
        create_qualification_check(
            session,
            _check(),
            actor_role="sales",
            idempotency_key="check-immutable",
        )

    with factory() as session:
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE trade_agent.qualification_checks "
                    "SET notes = 'mutated' WHERE check_id = 'check-a'"
                )
            )
        session.rollback()
        assert (
            session.scalar(
                text(
                    "SELECT notes FROM trade_agent.qualification_checks "
                    "WHERE check_id = 'check-a'"
                )
            )
            == "Screened against the current source snapshot."
        )
