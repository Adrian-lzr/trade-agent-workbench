"""Transactional inquiry SLA, reply revision, and translation review persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select, text

from trade_agent.contracts import TranslationReviewDecision
from trade_agent.db.models import (
    IdempotencyRecord,
    InquiryCaseRecord,
    InquiryReplyRevisionRecord,
    InquiryTranslationReviewRecord,
    RFQAggregate,
    RFQRevisionRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.contracts import InquiryCase, InquiryReplyRevision


INQUIRY_IDEMPOTENCY_TYPE = "inquiry"
_INQUIRY_CREATE_ENDPOINT = "inquiries.create"
_REPLY_CREATE_ENDPOINT = "inquiries.replies.create"
_TRANSLATION_REVIEW_ENDPOINT = "inquiries.translation_reviews.decide"
_SHA256_HEX_LENGTH = 64


class InquiryConflictError(Exception):
    """Raised when an inquiry operation conflicts with current state."""


class InquiryNotFoundError(Exception):
    """Raised when an inquiry or reply is not visible in the requested org."""


class InquiryRFQNotFoundError(InquiryNotFoundError):
    """Raised when an inquiry references an RFQ outside the requested org."""


class InquiryAuthorizationError(Exception):
    """Raised when the actor role is not allowed to review a translation."""


class InquiryHashMismatchError(InquiryConflictError):
    """Raised when a caller's expected content hash is stale."""


class InquiryIdempotencyConflictError(InquiryConflictError):
    """Raised when an idempotency key is reused with another request."""


class InquirySelfReviewError(InquiryConflictError):
    """Raised when a reply author tries to review their own translation."""


class TranslationNotRequiredError(InquiryConflictError):
    """Raised when a same-language reply is submitted for translation review."""


@dataclass(frozen=True, slots=True)
class InquiryWriteResult:
    inquiry_id: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ReplyWriteResult:
    inquiry_id: str
    reply_revision_id: str
    revision_no: int
    content_hash: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class TranslationReviewResult:
    review_id: str
    inquiry_id: str
    reply_revision_id: str
    content_hash: str
    decision: TranslationReviewDecision
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class InquiryQueueItem:
    record: InquiryCaseRecord
    queue_state: str
    is_overdue: bool


def create_inquiry_case(
    session: Session,
    case: InquiryCase,
    *,
    idempotency_key: str,
) -> InquiryWriteResult:
    """Persist one SLA-bearing inquiry case with scoped idempotency."""
    _validate_key(idempotency_key)
    request_payload = case.model_dump(mode="json")
    request_payload.pop("created_at", None)
    request_hash = _request_hash(request_payload)
    with session.begin_nested():
        _lock_idempotency_scope(
            session,
            org_id=case.org_id,
            actor_id=case.created_by,
            idempotency_key=idempotency_key,
        )
        existing = _find_idempotency(
            session,
            org_id=case.org_id,
            actor_id=case.created_by,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _validate_idempotency(
                existing,
                endpoint=_INQUIRY_CREATE_ENDPOINT,
                request_hash=request_hash,
                aggregate_id=case.inquiry_id,
            )
            prior = session.scalar(
                select(InquiryCaseRecord).where(
                    InquiryCaseRecord.org_id == case.org_id,
                    InquiryCaseRecord.inquiry_id == case.inquiry_id,
                )
            )
            if prior is None:
                raise InquiryConflictError(
                    "inquiry idempotency record references a missing case"
                )
            return InquiryWriteResult(prior.inquiry_id, idempotent_replay=True)

        rfq = session.scalar(
            select(RFQAggregate).where(
                RFQAggregate.org_id == case.org_id,
                RFQAggregate.rfq_id == case.rfq_id,
            )
        )
        if rfq is None:
            raise InquiryRFQNotFoundError("RFQ was not found in this organization")

        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=case.org_id,
                actor_id=case.created_by,
                endpoint=_INQUIRY_CREATE_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=INQUIRY_IDEMPOTENCY_TYPE,
                result_aggregate_id=case.inquiry_id,
                result_revision_id=case.inquiry_id,
                created_at=case.created_at,
            )
        )
        session.add(
            InquiryCaseRecord(
                inquiry_id=case.inquiry_id,
                org_id=case.org_id,
                rfq_id=case.rfq_id,
                source_channel=case.source_channel,
                customer_role=case.customer_role.value,
                original_language=case.original_language,
                received_at=case.received_at,
                response_due_at=case.response_due_at,
                owner_id=case.owner_id,
                queue_state=case.queue_state.value,
                attachments=case.model_dump(mode="json")["attachments"],
                current_reply_revision_id=None,
                current_reply_revision_no=None,
                created_at=case.created_at,
                updated_at=case.created_at,
                created_by=case.created_by,
            )
        )
        session.flush()
    return InquiryWriteResult(case.inquiry_id, idempotent_replay=False)


def append_inquiry_reply_revision(  # noqa: C901
    session: Session,
    revision: InquiryReplyRevision,
    *,
    expected_content_hash: str | None,
    idempotency_key: str,
) -> ReplyWriteResult:
    """Append a reply revision after checking the current hash and org scope."""
    _validate_key(idempotency_key)
    if expected_content_hash is not None:
        _validate_hash(expected_content_hash)
    payload = revision.model_dump(mode="json", exclude={"content_hash"})
    request_hash = _request_hash({
        "expected_content_hash": expected_content_hash,
        "revision": {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "created_at",
                "parent_reply_revision_id",
                "reply_revision_id",
                "revision_no",
            }
        },
    })
    with session.begin_nested():
        existing = _find_idempotency(
            session,
            org_id=revision.org_id,
            actor_id=revision.created_by,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _validate_idempotency(
                existing,
                endpoint=_REPLY_CREATE_ENDPOINT,
                request_hash=request_hash,
                aggregate_id=revision.inquiry_id,
            )
            prior = session.scalar(
                select(InquiryReplyRevisionRecord).where(
                    InquiryReplyRevisionRecord.org_id == revision.org_id,
                    InquiryReplyRevisionRecord.inquiry_id == revision.inquiry_id,
                    InquiryReplyRevisionRecord.reply_revision_id
                    == existing.result_revision_id,
                )
            )
            if prior is None:
                raise InquiryConflictError(
                    "reply idempotency record references a missing revision"
                )
            return ReplyWriteResult(
                inquiry_id=prior.inquiry_id,
                reply_revision_id=prior.reply_revision_id,
                revision_no=prior.revision_no,
                content_hash=prior.content_hash,
                idempotent_replay=True,
            )

        case = session.scalar(
            select(InquiryCaseRecord)
            .where(
                InquiryCaseRecord.org_id == revision.org_id,
                InquiryCaseRecord.inquiry_id == revision.inquiry_id,
            )
            .with_for_update()
        )
        if case is None:
            raise InquiryNotFoundError("inquiry was not found in this organization")
        if case.rfq_id != revision.rfq_id:
            raise InquiryConflictError("reply RFQ does not match the inquiry")
        current = None
        if case.current_reply_revision_id is not None:
            current = session.scalar(
                select(InquiryReplyRevisionRecord).where(
                    InquiryReplyRevisionRecord.org_id == revision.org_id,
                    InquiryReplyRevisionRecord.inquiry_id == revision.inquiry_id,
                    InquiryReplyRevisionRecord.reply_revision_id
                    == case.current_reply_revision_id,
                )
            )
            if current is None:
                raise InquiryConflictError("inquiry current reply pointer is invalid")
        current_hash = current.content_hash if current is not None else None
        if expected_content_hash != current_hash:
            raise InquiryHashMismatchError(
                "reply content changed; refresh before creating a revision"
            )
        expected_no = (current.revision_no if current is not None else 0) + 1
        expected_parent = current.reply_revision_id if current is not None else None
        if (
            revision.revision_no != expected_no
            or revision.parent_reply_revision_id != expected_parent
        ):
            raise InquiryConflictError(
                "reply revision is not the next revision of the current snapshot"
            )
        if revision.source_language.lower() != case.original_language.lower():
            raise InquiryConflictError(
                "reply source language must match the inquiry original language"
            )
        rfq_revision = session.scalar(
            select(RFQRevisionRecord).where(
                RFQRevisionRecord.org_id == revision.org_id,
                RFQRevisionRecord.rfq_id == revision.rfq_id,
                RFQRevisionRecord.revision_id == revision.rfq_revision_id,
            )
        )
        if rfq_revision is None:
            raise InquiryNotFoundError(
                "RFQ revision was not found in this organization"
            )

        now = revision.created_at
        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=revision.org_id,
                actor_id=revision.created_by,
                endpoint=_REPLY_CREATE_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=INQUIRY_IDEMPOTENCY_TYPE,
                result_aggregate_id=revision.inquiry_id,
                result_revision_id=revision.reply_revision_id,
                created_at=now,
            )
        )
        session.add(
            InquiryReplyRevisionRecord(
                reply_revision_id=revision.reply_revision_id,
                org_id=revision.org_id,
                inquiry_id=revision.inquiry_id,
                rfq_id=revision.rfq_id,
                revision_no=revision.revision_no,
                parent_reply_revision_id=revision.parent_reply_revision_id,
                rfq_revision_id=revision.rfq_revision_id,
                source_language=revision.source_language,
                target_language=revision.target_language,
                source_content=revision.source_content,
                translated_content=revision.translated_content,
                template_version=revision.template_version,
                attachments=revision.model_dump(mode="json")["attachments"],
                content_hash=revision.content_hash,
                created_at=now,
                created_by=revision.created_by,
            )
        )
        case.current_reply_revision_id = revision.reply_revision_id
        case.current_reply_revision_no = revision.revision_no
        case.updated_at = now
        session.flush()
    return ReplyWriteResult(
        inquiry_id=revision.inquiry_id,
        reply_revision_id=revision.reply_revision_id,
        revision_no=revision.revision_no,
        content_hash=revision.content_hash,
        idempotent_replay=False,
    )


def review_inquiry_translation(  # noqa: C901, PLR0913
    session: Session,
    *,
    org_id: str,
    inquiry_id: str,
    reply_revision_id: str,
    content_hash: str,
    decision: TranslationReviewDecision,
    actor_id: str,
    actor_role: str,
    idempotency_key: str,
    reason: str | None = None,
    reviewed_at: datetime | None = None,
) -> TranslationReviewResult:
    """Record one final reviewer/admin decision for an exact reply hash."""
    _validate_identity(org_id, inquiry_id, reply_revision_id, actor_id)
    if actor_role not in {"reviewer", "admin"}:
        raise InquiryAuthorizationError(
            "only reviewers and admins can review translations"
        )
    _validate_hash(content_hash)
    _validate_key(idempotency_key)
    now = reviewed_at or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone")
    request_hash = _request_hash({
        "content_hash": content_hash,
        "decision": decision.value,
        "inquiry_id": inquiry_id,
        "reason": reason,
        "reply_revision_id": reply_revision_id,
    })
    with session.begin_nested():
        existing = _find_idempotency(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _validate_idempotency(
                existing,
                endpoint=_TRANSLATION_REVIEW_ENDPOINT,
                request_hash=request_hash,
                aggregate_id=inquiry_id,
            )
            prior = session.scalar(
                select(InquiryTranslationReviewRecord).where(
                    InquiryTranslationReviewRecord.org_id == org_id,
                    InquiryTranslationReviewRecord.inquiry_id == inquiry_id,
                    InquiryTranslationReviewRecord.review_id
                    == existing.result_revision_id,
                )
            )
            if prior is None:
                raise InquiryConflictError(
                    "translation review idempotency record references a missing review"
                )
            return _review_result(prior, idempotent_replay=True)

        reply = session.scalar(
            select(InquiryReplyRevisionRecord)
            .where(
                InquiryReplyRevisionRecord.org_id == org_id,
                InquiryReplyRevisionRecord.inquiry_id == inquiry_id,
                InquiryReplyRevisionRecord.reply_revision_id == reply_revision_id,
            )
            .with_for_update()
        )
        if reply is None:
            raise InquiryNotFoundError(
                "reply revision was not found in this organization"
            )
        if reply.content_hash != content_hash:
            raise InquiryHashMismatchError(
                "reply content changed; refresh before reviewing translation"
            )
        if reply.created_by == actor_id:
            raise InquirySelfReviewError(
                "reply authors cannot review their own translation"
            )
        if reply.source_language.lower() == reply.target_language.lower():
            raise TranslationNotRequiredError(
                "translation review is not required for a same-language reply"
            )
        prior = session.scalar(
            select(InquiryTranslationReviewRecord).where(
                InquiryTranslationReviewRecord.org_id == org_id,
                InquiryTranslationReviewRecord.inquiry_id == inquiry_id,
                InquiryTranslationReviewRecord.reply_revision_id == reply_revision_id,
            )
        )
        if prior is not None:
            raise InquiryConflictError("reply translation already has a final review")

        review_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint=_TRANSLATION_REVIEW_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=INQUIRY_IDEMPOTENCY_TYPE,
                result_aggregate_id=inquiry_id,
                result_revision_id=review_id,
                created_at=now,
            )
        )
        session.add(
            InquiryTranslationReviewRecord(
                review_id=review_id,
                org_id=org_id,
                inquiry_id=inquiry_id,
                reply_revision_id=reply_revision_id,
                content_hash=content_hash,
                decision=decision.value,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                reason=reason,
                idempotency_record_id=record_id,
                reviewed_at=now,
            )
        )
        session.flush()
        review = session.get(InquiryTranslationReviewRecord, review_id)
        if review is None:
            raise RuntimeError("translation review was not persisted")
    return _review_result(review, idempotent_replay=False)


def get_inquiry_case(
    session: Session, *, org_id: str, inquiry_id: str
) -> InquiryCaseRecord | None:
    """Read one inquiry under an explicit organization scope."""
    return session.scalar(
        select(InquiryCaseRecord).where(
            InquiryCaseRecord.org_id == org_id,
            InquiryCaseRecord.inquiry_id == inquiry_id,
        )
    )


def list_inquiry_queue(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> list[InquiryQueueItem]:
    """Return an org-scoped queue with SLA state derived at query time."""
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset cannot be negative")
    current_time = now or datetime.now(UTC)
    if current_time.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current_time = current_time.astimezone(UTC)
    statement = select(InquiryCaseRecord).where(InquiryCaseRecord.org_id == org_id)
    if not include_closed:
        statement = statement.where(InquiryCaseRecord.queue_state != "closed")
    statement = (
        statement.order_by(
            InquiryCaseRecord.response_due_at.asc(), InquiryCaseRecord.inquiry_id.asc()
        )
        .offset(offset)
        .limit(limit)
    )
    return [
        _queue_item(record, now=current_time) for record in session.scalars(statement)
    ]


def effective_inquiry_queue_state(
    record: InquiryCaseRecord, *, now: datetime | None = None
) -> tuple[str, bool]:
    """Calculate overdue state without persisting a time-dependent queue value."""
    current_time = now or datetime.now(UTC)
    if current_time.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current_time = current_time.astimezone(UTC)
    state = record.queue_state
    if state in {"open", "overdue"}:
        due_at = record.response_due_at
        if due_at.utcoffset() is None:
            raise ValueError("response_due_at must include a timezone")
        is_overdue = due_at.astimezone(UTC) <= current_time
        return ("overdue" if is_overdue else "open"), is_overdue
    return state, False


def _queue_item(record: InquiryCaseRecord, *, now: datetime) -> InquiryQueueItem:
    state, is_overdue = effective_inquiry_queue_state(record, now=now)
    return InquiryQueueItem(record, state, is_overdue)


def get_current_reply(
    session: Session, *, org_id: str, inquiry_id: str
) -> InquiryReplyRevisionRecord | None:
    case = get_inquiry_case(session, org_id=org_id, inquiry_id=inquiry_id)
    if case is None or case.current_reply_revision_id is None:
        return None
    return session.scalar(
        select(InquiryReplyRevisionRecord).where(
            InquiryReplyRevisionRecord.org_id == org_id,
            InquiryReplyRevisionRecord.inquiry_id == inquiry_id,
            InquiryReplyRevisionRecord.reply_revision_id
            == case.current_reply_revision_id,
        )
    )


def get_translation_review(
    session: Session, *, org_id: str, inquiry_id: str, reply_revision_id: str
) -> InquiryTranslationReviewRecord | None:
    return session.scalar(
        select(InquiryTranslationReviewRecord).where(
            InquiryTranslationReviewRecord.org_id == org_id,
            InquiryTranslationReviewRecord.inquiry_id == inquiry_id,
            InquiryTranslationReviewRecord.reply_revision_id == reply_revision_id,
        )
    )


# Short aliases keep the service vocabulary convenient for callers building an API.
create_inquiry = create_inquiry_case
create_reply_revision = append_inquiry_reply_revision
decide_translation_review = review_inquiry_translation


def _review_result(
    record: InquiryTranslationReviewRecord, *, idempotent_replay: bool
) -> TranslationReviewResult:
    return TranslationReviewResult(
        review_id=record.review_id,
        inquiry_id=record.inquiry_id,
        reply_revision_id=record.reply_revision_id,
        content_hash=record.content_hash,
        decision=TranslationReviewDecision(record.decision),
        idempotent_replay=idempotent_replay,
    )


def _find_idempotency(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    idempotency_key: str,
) -> IdempotencyRecord | None:
    return session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.org_id == org_id,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )


def _lock_idempotency_scope(
    session: Session,
    *,
    org_id: str,
    actor_id: str,
    idempotency_key: str,
) -> None:
    """Serialize first writers for one idempotency scope on PostgreSQL.

    The unique constraint still protects the table, while the transaction lock
    turns a concurrent first request into a normal replay instead of a 500.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = f"trade-agent:{org_id}:{actor_id}:{idempotency_key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": lock_key},
    )


def _validate_idempotency(
    existing: IdempotencyRecord,
    *,
    endpoint: str,
    request_hash: str,
    aggregate_id: str,
) -> None:
    if existing.endpoint != endpoint:
        raise InquiryIdempotencyConflictError(
            "idempotency key was already used by another endpoint"
        )
    if existing.request_hash != request_hash:
        raise InquiryIdempotencyConflictError(
            "idempotency key was reused with a different request"
        )
    if (
        existing.result_aggregate_type != INQUIRY_IDEMPOTENCY_TYPE
        or existing.result_aggregate_id != aggregate_id
    ):
        raise InquiryIdempotencyConflictError(
            "idempotency key belongs to another inquiry"
        )


def _validate_identity(*values: str) -> None:
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("inquiry identity fields must be non-empty")


def _validate_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("idempotency_key must be non-empty")


def _validate_hash(value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("content_hash must be a lowercase SHA-256 digest")


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
