"""PostgreSQL vertical-slice tests for inquiry SLA and translation review."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from threading import Barrier

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trade_agent.api import create_app
from trade_agent.contracts import (
    InquiryAttachment,
    InquiryCase,
    InquiryReplyRevision,
    TranslationReviewDecision,
)
from trade_agent.db.inquiries import (
    InquiryAuthorizationError,
    InquiryHashMismatchError,
    InquiryIdempotencyConflictError,
    InquiryRFQNotFoundError,
    InquirySelfReviewError,
    append_inquiry_reply_revision,
    create_inquiry_case,
    list_inquiry_queue,
    review_inquiry_translation,
)
from trade_agent.db.models import RFQAggregate, RFQRevisionRecord

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


def _seed_rfq(
    factory: sessionmaker[Session], *, org_id: str = "org-a", rfq_id: str = "rfq-a"
) -> None:
    created_at = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    with factory() as session, session.begin():
        aggregate = RFQAggregate(
            rfq_id=rfq_id,
            org_id=org_id,
            current_revision_id=None,
            current_revision_no=None,
            created_at=created_at,
            updated_at=created_at,
        )
        revision = RFQRevisionRecord(
            revision_id=f"{rfq_id}-rev-1",
            org_id=org_id,
            rfq_id=rfq_id,
            revision_no=1,
            parent_revision_id=None,
            payload={},
            created_at=created_at,
            created_by="sales-1",
        )
        session.add_all([aggregate, revision])
        session.flush()
        aggregate.current_revision_id = revision.revision_id
        aggregate.current_revision_no = revision.revision_no


def _case(
    *,
    org_id: str = "org-a",
    inquiry_id: str = "inquiry-a",
    rfq_id: str = "rfq-a",
    now: datetime | None = None,
    response_due_at: datetime | None = None,
) -> InquiryCase:
    received_at = now or datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    return InquiryCase(
        org_id=org_id,
        inquiry_id=inquiry_id,
        rfq_id=rfq_id,
        source_channel="email",
        customer_role="distributor",
        original_language=" EN_US ",
        received_at=received_at,
        response_due_at=response_due_at or received_at + timedelta(hours=4),
        owner_id="sales-1",
        created_at=received_at,
        created_by="sales-1",
    )


def _reply(
    *,
    actor_id: str = "sales-1",
    inquiry_id: str = "inquiry-a",
    revision_id: str = "reply-a-1",
    source_content: str = "Thank you for your inquiry.",
) -> InquiryReplyRevision:
    return InquiryReplyRevision(
        org_id="org-a",
        inquiry_id=inquiry_id,
        rfq_id="rfq-a",
        reply_revision_id=revision_id,
        revision_no=1,
        rfq_revision_id="rfq-a-rev-1",
        source_language="en-us",
        target_language="zh-cn",
        source_content=source_content,
        translated_content="感谢您的询盘。",
        created_at=datetime(2026, 10, 1, 9, 1, tzinfo=UTC),
        created_by=actor_id,
    )


def _headers(
    *, org_id: str = "org-a", actor_id: str = "sales-1", role: str = "sales"
) -> dict[str, str]:
    return {
        "X-Org-Id": org_id,
        "X-Actor-Id": actor_id,
        "X-Role": role,
    }


def _attachment(attachment_id: str = "attachment-1") -> InquiryAttachment:
    return InquiryAttachment(
        attachment_id=attachment_id,
        filename="specification.pdf",
        media_type="application/pdf",
        storage_ref=f"private://{attachment_id}",
        content_sha256="a" * 64,
        size_bytes=128,
    )


def test_create_requires_same_org_rfq_and_is_idempotent(factory) -> None:
    _seed_rfq(factory)
    case = _case()
    with factory() as session, session.begin():
        first = create_inquiry_case(session, case, idempotency_key="create-1")
        replay = create_inquiry_case(session, case, idempotency_key="create-1")
        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True

    with factory() as session, session.begin(), pytest.raises(InquiryRFQNotFoundError):
        create_inquiry_case(
            session,
            _case(org_id="other-org", inquiry_id="inquiry-other"),
            idempotency_key="create-other",
        )


def test_concurrent_inquiry_first_write_replays_and_rejects_different_request(
    factory,
) -> None:
    _seed_rfq(factory)
    barrier = Barrier(2)

    def create() -> bool:
        with factory() as session, session.begin():
            barrier.wait(timeout=10)
            result = create_inquiry_case(
                session, _case(), idempotency_key="inquiry-concurrent-create"
            )
            return result.idempotent_replay

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay_flags = tuple(
            future.result(timeout=15)
            for future in (executor.submit(create), executor.submit(create))
        )

    assert sorted(replay_flags) == [False, True]
    with factory() as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM trade_agent.inquiry_cases "
                    "WHERE org_id = 'org-a' AND inquiry_id = 'inquiry-a'"
                )
            )
            == 1
        )
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM trade_agent.idempotency_records "
                    "WHERE org_id = 'org-a' AND actor_id = 'sales-1' "
                    "AND idempotency_key = 'inquiry-concurrent-create'"
                )
            )
            == 1
        )

    with (
        factory() as session,
        session.begin(),
        pytest.raises(InquiryIdempotencyConflictError, match="different request"),
    ):
        create_inquiry_case(
            session,
            _case(inquiry_id="inquiry-a", now=datetime(2026, 10, 1, 9, 2, tzinfo=UTC)),
            idempotency_key="inquiry-concurrent-create",
        )


def test_reply_is_append_only_and_hash_conflicts_are_detected(factory) -> None:
    _seed_rfq(factory)
    with factory() as session, session.begin():
        create_inquiry_case(session, _case(), idempotency_key="create-1")
        revision = _reply()
        first = append_inquiry_reply_revision(
            session,
            revision,
            expected_content_hash=None,
            idempotency_key="reply-1",
        )
        replay = append_inquiry_reply_revision(
            session,
            revision,
            expected_content_hash=None,
            idempotency_key="reply-1",
        )
        assert first.content_hash == revision.content_hash
        assert replay.idempotent_replay is True
        with pytest.raises(InquiryHashMismatchError):
            append_inquiry_reply_revision(
                session,
                _reply(revision_id="reply-a-2"),
                expected_content_hash="0" * 64,
                idempotency_key="reply-2",
            )


def test_translation_review_enforces_role_author_and_idempotency(factory) -> None:
    _seed_rfq(factory)
    with factory() as session, session.begin():
        create_inquiry_case(session, _case(), idempotency_key="create-1")
        revision = _reply()
        append_inquiry_reply_revision(
            session,
            revision,
            expected_content_hash=None,
            idempotency_key="reply-1",
        )
        with pytest.raises(InquiryAuthorizationError):
            review_inquiry_translation(
                session,
                org_id="org-a",
                inquiry_id="inquiry-a",
                reply_revision_id="reply-a-1",
                content_hash=revision.content_hash,
                decision=TranslationReviewDecision.APPROVED,
                actor_id="reviewer-1",
                actor_role="sales",
                idempotency_key="review-sales",
            )
        with pytest.raises(InquirySelfReviewError):
            review_inquiry_translation(
                session,
                org_id="org-a",
                inquiry_id="inquiry-a",
                reply_revision_id="reply-a-1",
                content_hash=revision.content_hash,
                decision=TranslationReviewDecision.APPROVED,
                actor_id="sales-1",
                actor_role="reviewer",
                idempotency_key="review-self",
            )
        result = review_inquiry_translation(
            session,
            org_id="org-a",
            inquiry_id="inquiry-a",
            reply_revision_id="reply-a-1",
            content_hash=revision.content_hash,
            decision=TranslationReviewDecision.APPROVED,
            actor_id="reviewer-1",
            actor_role="reviewer",
            idempotency_key="review-1",
        )
        replay = review_inquiry_translation(
            session,
            org_id="org-a",
            inquiry_id="inquiry-a",
            reply_revision_id="reply-a-1",
            content_hash=revision.content_hash,
            decision=TranslationReviewDecision.APPROVED,
            actor_id="reviewer-1",
            actor_role="reviewer",
            idempotency_key="review-1",
        )
        assert result.idempotent_replay is False
        assert replay.idempotent_replay is True


def test_queue_calculates_overdue_at_exact_due_boundary(factory) -> None:
    _seed_rfq(factory)
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with factory() as session, session.begin():
        create_inquiry_case(
            session,
            _case(response_due_at=now),
            idempotency_key="create-due",
        )
        create_inquiry_case(
            session,
            _case(
                inquiry_id="inquiry-future", response_due_at=now + timedelta(seconds=1)
            ),
            idempotency_key="create-future",
        )
        queue = list_inquiry_queue(session, org_id="org-a", now=now)
        assert [
            (item.record.inquiry_id, item.queue_state, item.is_overdue)
            for item in queue
        ] == [
            ("inquiry-a", "overdue", True),
            ("inquiry-future", "open", False),
        ]


def test_api_routes_enforce_org_scope_and_translate_review_flow(factory) -> None:
    _seed_rfq(factory)
    with factory() as session, session.begin():
        create_inquiry_case(session, _case(), idempotency_key="create-1")

    client = TestClient(create_app(factory))
    queue = client.get("/api/v1/inquiries/queue", headers=_headers())
    assert queue.status_code == 200
    assert queue.json()["items"][0]["inquiry_id"] == "inquiry-a"

    hidden = client.get(
        "/api/v1/inquiries/inquiry-a", headers=_headers(org_id="other-org")
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "inquiry_not_found"

    body = {
        "rfq_revision_id": "rfq-a-rev-1",
        "source_language": "en_US",
        "target_language": "zh-CN",
        "source_content": "Thank you for your inquiry.",
        "translated_content": "感谢您的询盘。",
    }
    reply = client.post(
        "/api/v1/inquiries/inquiry-a/reply-revisions",
        headers={**_headers(), "Idempotency-Key": "reply-api-1"},
        json=body,
    )
    assert reply.status_code == 201
    reply_replay = client.post(
        "/api/v1/inquiries/inquiry-a/reply-revisions",
        headers={**_headers(), "Idempotency-Key": "reply-api-1"},
        json=body,
    )
    assert reply_replay.status_code == 200
    content_hash = reply.json()["content_hash"]
    reply_revision_id = reply.json()["reply_revision_id"]

    review_path = (
        f"/api/v1/inquiries/inquiry-a/reply-revisions/{reply_revision_id}"
        "/translation-review"
    )
    review = client.post(
        review_path,
        headers={
            **_headers(actor_id="reviewer-1", role="reviewer"),
            "Idempotency-Key": "review-api-1",
        },
        json={"content_hash": content_hash, "decision": "approved"},
    )
    assert review.status_code == 201
    assert review.json()["decision"] == "approved"
    review_replay = client.post(
        review_path,
        headers={
            **_headers(actor_id="reviewer-1", role="reviewer"),
            "Idempotency-Key": "review-api-1",
        },
        json={"content_hash": content_hash, "decision": "approved"},
    )
    assert review_replay.status_code == 200

    detail = client.get("/api/v1/inquiries/inquiry-a", headers=_headers())
    assert detail.status_code == 200
    assert detail.json()["translation_review_state"] == "approved"

    self_review = client.post(
        review_path,
        headers={
            **_headers(actor_id="sales-1", role="reviewer"),
            "Idempotency-Key": "review-self-api",
        },
        json={"content_hash": content_hash, "decision": "approved"},
    )
    assert self_review.status_code == 409
    assert self_review.json()["code"] == "self_review_forbidden"


def test_api_create_inquiry_and_reply_preserve_attachment_references(factory) -> None:
    _seed_rfq(factory)
    client = TestClient(create_app(factory))
    case_body = {
        "inquiry_id": "inquiry-api-created",
        "rfq_id": "rfq-a",
        "source_channel": "email",
        "customer_role": "end_customer",
        "original_language": "en-US",
        "received_at": "2026-10-01T09:00:00Z",
        "response_due_at": "2099-01-01T00:00:00Z",
        "attachments": [
            {
                "attachment_id": "attachment-1",
                "filename": "specification.pdf",
                "media_type": "application/pdf",
                "storage_ref": "private://attachment-1",
                "content_sha256": "a" * 64,
                "size_bytes": 128,
            }
        ],
    }
    created = client.post(
        "/api/v1/inquiries",
        headers={**_headers(), "Idempotency-Key": "create-api"},
        json=case_body,
    )
    assert created.status_code == 201
    replay = client.post(
        "/api/v1/inquiries",
        headers={**_headers(), "Idempotency-Key": "create-api"},
        json=case_body,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    reply = client.post(
        "/api/v1/inquiries/inquiry-api-created/reply-revisions",
        headers={**_headers(), "Idempotency-Key": "reply-api-attachments"},
        json={
            "rfq_revision_id": "rfq-a-rev-1",
            "source_language": "en-US",
            "target_language": "zh-CN",
            "source_content": "Please find the quote attached.",
            "translated_content": "请查收附件中的报价。",
            "attachments": [
                {
                    "attachment_id": "quote-1",
                    "filename": "quote.pdf",
                    "media_type": "application/pdf",
                    "storage_ref": "private://quote-1",
                    "content_sha256": "b" * 64,
                    "size_bytes": 256,
                }
            ],
        },
    )
    assert reply.status_code == 201
    detail = client.get("/api/v1/inquiries/inquiry-api-created", headers=_headers())
    assert detail.status_code == 200
    assert detail.json()["case"]["attachments"][0]["attachment_id"] == "attachment-1"
    assert (
        detail.json()["current_reply"]["attachments"][0]["attachment_id"] == "quote-1"
    )
    assert detail.json()["case"]["queue_state"] == "open"


def test_postgres_immutable_trigger_rejects_reply_update(factory) -> None:
    _seed_rfq(factory)
    with factory() as session, session.begin():
        create_inquiry_case(session, _case(), idempotency_key="create-1")
        append_inquiry_reply_revision(
            session,
            _reply(),
            expected_content_hash=None,
            idempotency_key="reply-1",
        )

    with factory() as session:
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    "UPDATE trade_agent.inquiry_reply_revisions "
                    "SET source_content = 'mutated' "
                    "WHERE reply_revision_id = 'reply-a-1'"
                )
            )
        session.rollback()
        assert (
            session.scalar(
                text(
                    "SELECT source_content FROM trade_agent.inquiry_reply_revisions "
                    "WHERE reply_revision_id = 'reply-a-1'"
                )
            )
            == "Thank you for your inquiry."
        )
