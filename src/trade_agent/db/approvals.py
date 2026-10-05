"""Transactional, hash-bound reviewer decisions for quote revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Literal
import uuid

from sqlalchemy import select

from trade_agent.contracts import AggregateType
from trade_agent.db.models import (
    AuditEventRecord,
    IdempotencyRecord,
    QuotationAggregate,
    QuoteApprovalRecord,
    QuoteRevisionRecord,
)
from trade_agent.db.qualification import ensure_quote_qualification_clear

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

ApprovalDecision = Literal["approved", "rejected"]
_SHA256_HEX_LENGTH = 64


class ApprovalConflictError(Exception):
    """Raised when a reviewer submits an outdated or already decided version."""


class ApprovalNotFoundError(Exception):
    """Raised when the requested quote revision is not visible in the org."""


class ApprovalHashMismatchError(ApprovalConflictError):
    """Raised when the displayed quote content differs from the stored snapshot."""


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    approval_id: str
    quotation_id: str
    revision_id: str
    content_hash: str
    decision: ApprovalDecision
    idempotent_replay: bool


def decide_quote_revision(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    quotation_id: str,
    revision_id: str,
    content_hash: str,
    decision: ApprovalDecision,
    actor_id: str,
    idempotency_key: str,
    reason: str | None = None,
    decided_at: datetime | None = None,
) -> ApprovalResult:
    """Approve or reject the exact current quote revision in one transaction.

    The aggregate row is locked before checking the current revision. This makes
    an approval submitted from an old browser page fail when another revision has
    become current, while retries of the same idempotency key replay the decision.
    The caller owns the outer transaction; this function uses a savepoint so a
    rejected request does not poison a surrounding unit of work.
    """
    _validate_inputs(
        org_id,
        quotation_id,
        revision_id,
        content_hash,
        actor_id,
        idempotency_key,
        decision,
    )
    now = decided_at or datetime.now(UTC)
    request_hash = _request_hash({
        "content_hash": content_hash,
        "decision": decision,
        "quotation_id": quotation_id,
        "reason": reason,
        "revision_id": revision_id,
    })

    with session.begin_nested():
        existing = session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.org_id == org_id,
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            return _replay_or_conflict(
                session,
                existing,
                request_hash=request_hash,
                org_id=org_id,
                quotation_id=quotation_id,
                revision_id=revision_id,
            )

        aggregate = session.scalar(
            select(QuotationAggregate)
            .where(
                QuotationAggregate.org_id == org_id,
                QuotationAggregate.quotation_id == quotation_id,
            )
            .with_for_update()
        )
        if aggregate is None:
            raise ApprovalNotFoundError("quotation was not found in this organization")
        if aggregate.current_revision_id != revision_id:
            raise ApprovalConflictError(
                "quote revision is stale; refresh before submitting the decision"
            )

        revision = session.scalar(
            select(QuoteRevisionRecord).where(
                QuoteRevisionRecord.org_id == org_id,
                QuoteRevisionRecord.quotation_id == quotation_id,
                QuoteRevisionRecord.revision_id == revision_id,
            )
        )
        if revision is None:
            raise ApprovalNotFoundError("quote revision was not found")
        if revision.content_hash != content_hash:
            raise ApprovalHashMismatchError(
                "quote content changed; refresh before submitting the decision"
            )
        rfq_revision_id = revision.payload.get("rfq_revision_id")
        ensure_quote_qualification_clear(
            session,
            org_id=org_id,
            rfq_revision_id=(
                rfq_revision_id if isinstance(rfq_revision_id, str) else None
            ),
        )

        prior = session.scalar(
            select(QuoteApprovalRecord)
            .where(
                QuoteApprovalRecord.org_id == org_id,
                QuoteApprovalRecord.quotation_id == quotation_id,
                QuoteApprovalRecord.revision_id == revision_id,
            )
            .with_for_update()
        )
        if prior is not None:
            raise ApprovalConflictError("quote revision already has a final decision")

        approval_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint="quotations.approvals.decide",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=AggregateType.QUOTATION.value,
                result_aggregate_id=quotation_id,
                result_revision_id=revision_id,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            QuoteApprovalRecord(
                approval_id=approval_id,
                org_id=org_id,
                quotation_id=quotation_id,
                revision_id=revision_id,
                content_hash=content_hash,
                decision=decision,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                reason=reason,
                decided_at=now,
            )
        )
        session.add(
            AuditEventRecord(
                event_id=str(uuid.uuid4()),
                org_id=org_id,
                aggregate_type=AggregateType.QUOTATION.value,
                aggregate_id=quotation_id,
                action=f"quote_{decision}",
                prior_revision_id=revision.parent_revision_id,
                revision_id=revision_id,
                actor_id=actor_id,
                idempotency_record_id=record_id,
                reason=reason,
                occurred_at=now,
            )
        )
        session.flush()
        return ApprovalResult(
            approval_id=approval_id,
            quotation_id=quotation_id,
            revision_id=revision_id,
            content_hash=content_hash,
            decision=decision,
            idempotent_replay=False,
        )


def get_quote_approval(
    session: Session,
    *,
    org_id: str,
    quotation_id: str,
    revision_id: str,
) -> QuoteApprovalRecord | None:
    """Return a decision scoped to the caller's organization."""
    return session.scalar(
        select(QuoteApprovalRecord).where(
            QuoteApprovalRecord.org_id == org_id,
            QuoteApprovalRecord.quotation_id == quotation_id,
            QuoteApprovalRecord.revision_id == revision_id,
        )
    )


def _replay_or_conflict(  # noqa: PLR0913
    session: Session,
    existing: IdempotencyRecord,
    *,
    request_hash: str,
    org_id: str,
    quotation_id: str,
    revision_id: str,
) -> ApprovalResult:
    if existing.endpoint != "quotations.approvals.decide":
        raise ApprovalConflictError("idempotency key was used by another endpoint")
    if existing.request_hash != request_hash:
        raise ApprovalConflictError(
            "idempotency key was reused with a different request"
        )
    if (
        existing.result_aggregate_type != AggregateType.QUOTATION.value
        or existing.result_aggregate_id != quotation_id
        or existing.result_revision_id != revision_id
    ):
        raise ApprovalConflictError("idempotency key belongs to another quote revision")
    approval = session.scalar(
        select(QuoteApprovalRecord).where(
            QuoteApprovalRecord.org_id == org_id,
            QuoteApprovalRecord.quotation_id == quotation_id,
            QuoteApprovalRecord.revision_id == revision_id,
        )
    )
    if approval is None:
        raise RuntimeError("approval idempotency record references a missing decision")
    return ApprovalResult(
        approval_id=approval.approval_id,
        quotation_id=approval.quotation_id,
        revision_id=approval.revision_id,
        content_hash=approval.content_hash,
        decision=approval.decision,  # type: ignore[arg-type]
        idempotent_replay=True,
    )


def _validate_inputs(  # noqa: PLR0913, PLR0917
    org_id: str,
    quotation_id: str,
    revision_id: str,
    content_hash: str,
    actor_id: str,
    idempotency_key: str,
    decision: str,
) -> None:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (org_id, quotation_id, revision_id, actor_id, idempotency_key)
    ):
        raise ValueError("approval identity fields must be non-empty")
    if len(content_hash) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in content_hash
    ):
        raise ValueError("content_hash must be a lowercase SHA-256 digest")
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
