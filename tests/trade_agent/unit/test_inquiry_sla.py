from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.routing import APIRoute
import pytest

from trade_agent.api.app import create_app
from trade_agent.contracts import InquiryAttachment, InquiryCase, InquiryReplyRevision
from trade_agent.db.inquiries import effective_inquiry_queue_state


def _case(**overrides: object) -> InquiryCase:
    values: dict[str, object] = {
        "org_id": "org-1",
        "inquiry_id": "inq-1",
        "rfq_id": "rfq-1",
        "source_channel": "email",
        "original_language": " EN ",
        "received_at": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "response_due_at": datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        "created_at": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "created_by": "sales-1",
    }
    values.update(overrides)
    return InquiryCase(**values)


def test_inquiry_contract_normalizes_language_and_rejects_naive_or_reversed_sla() -> (
    None
):
    assert _case().original_language == "en"
    with pytest.raises(ValueError, match="timezone"):
        _case(received_at=datetime(2026, 10, 1, 8, 0))
    with pytest.raises(ValueError, match="response_due_at"):
        _case(response_due_at=datetime(2026, 10, 1, 7, 59, tzinfo=UTC))


def test_reply_contract_requires_translation_for_different_languages() -> None:
    base = {
        "org_id": "org-1",
        "inquiry_id": "inq-1",
        "rfq_id": "rfq-1",
        "reply_revision_id": "reply-1",
        "revision_no": 1,
        "rfq_revision_id": "rfqr-1",
        "source_language": " EN ",
        "target_language": "zh",
        "source_content": "Please confirm the quantity.",
        "created_at": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "created_by": "sales-1",
    }
    with pytest.raises(ValueError, match="translated_content"):
        InquiryReplyRevision(**base)
    reply = InquiryReplyRevision(**base, translated_content="请确认数量。")
    assert reply.source_language == "en"
    assert reply.requires_translation is True
    assert len(reply.content_hash) == 64


def test_reply_content_hash_binds_customer_visible_attachments() -> None:
    base = {
        "org_id": "org-1",
        "inquiry_id": "inq-1",
        "rfq_id": "rfq-1",
        "reply_revision_id": "reply-1",
        "revision_no": 1,
        "rfq_revision_id": "rfqr-1",
        "source_language": "en",
        "target_language": "en",
        "source_content": "Please see the attached specification.",
        "created_at": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "created_by": "sales-1",
    }
    attachment = InquiryAttachment(
        attachment_id="att-1",
        filename="spec.pdf",
        media_type="application/pdf",
        storage_ref="private://att-1",
        content_sha256="a" * 64,
        size_bytes=42,
    )
    without_attachment = InquiryReplyRevision(**base)
    with_attachment = InquiryReplyRevision(**base, attachments=(attachment,))
    assert without_attachment.content_hash != with_attachment.content_hash


def test_attachment_storage_ref_must_use_private_namespace() -> None:
    with pytest.raises(ValueError, match="storage_ref"):
        InquiryAttachment(
            attachment_id="att-1",
            filename="spec.pdf",
            media_type="application/pdf",
            storage_ref="https://example.test/spec.pdf",
            content_sha256="a" * 64,
        )


def test_queue_state_derives_overdue_at_exact_due_boundary_without_mutating_record() -> (
    None
):
    due = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    record = SimpleNamespace(queue_state="open", response_due_at=due)
    assert effective_inquiry_queue_state(record, now=due) == ("overdue", True)
    assert record.queue_state == "open"
    assert effective_inquiry_queue_state(
        record, now=due - timedelta(microseconds=1)
    ) == ("open", False)


def test_inquiry_api_exposes_sla_queue_detail_reply_and_review_routes() -> None:
    app = create_app(session_factory=None)
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/v1/inquiries/queue" in paths
    assert "/api/v1/inquiries/{inquiry_id}" in paths
    assert "/api/v1/inquiries/{inquiry_id}/reply-revisions" in paths
    assert (
        "/api/v1/inquiries/{inquiry_id}/reply-revisions/"
        "{reply_revision_id}/translation-review"
    ) in paths
