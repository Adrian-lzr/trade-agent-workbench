"""Persistence for buyer qualification and sanctions-screening evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import or_, select

from trade_agent.contracts import QualificationResult
from trade_agent.db.models import (
    IdempotencyRecord,
    InquiryCaseRecord,
    QualificationCheckRecord,
    QualificationDecisionRecord,
    RFQRevisionRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.contracts import QualificationCheck


QUALIFICATION_IDEMPOTENCY_TYPE = "qualification"
_CHECK_CREATE_ENDPOINT = "qualification_checks.create"
_DECISION_ENDPOINT = "qualification_checks.decide"
_SHA256_HEX_LENGTH = 64
_MAX_LIST_LIMIT = 200


class QualificationConflictError(Exception):
    """Raised when a qualification operation conflicts with current state."""


class QualificationNotFoundError(Exception):
    """Raised when a qualification check is not visible in the organization."""


class QualificationAuthorizationError(Exception):
    """Raised when an actor role cannot create or decide a screening result."""


class QualificationHashMismatchError(QualificationConflictError):
    """Raised when a decision is submitted against a stale evidence hash."""


class QualificationIdempotencyConflictError(QualificationConflictError):
    """Raised when an idempotency key is reused for a different operation."""


class QualificationAlreadyDecidedError(QualificationConflictError):
    """Raised when a check already has a final reviewer decision."""


class QualificationGateError(QualificationConflictError):
    """Raised when unresolved screening evidence blocks a quote action."""


@dataclass(frozen=True, slots=True)
class QualificationWriteResult:
    check_id: str
    result: QualificationResult
    evidence_hash: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class QualificationDecisionResult:
    decision_id: str
    check_id: str
    evidence_hash: str
    result: QualificationResult
    reviewer_id: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class QualificationView:
    check: QualificationCheckRecord
    decision: QualificationDecisionRecord | None
    effective_result: QualificationResult


def create_qualification_check(
    session: Session,
    check: QualificationCheck,
    *,
    actor_role: str,
    idempotency_key: str,
) -> QualificationWriteResult:
    """Persist one immutable screening observation with scoped idempotency.

    A clear result is a final confirmation and therefore requires reviewer/admin;
    sales actors can record unresolved evidence for later authorized review.
    """
    _validate_identity(check.org_id, check.check_id, check.created_by)
    _validate_key(idempotency_key)
    if check.result is QualificationResult.CLEAR and actor_role not in {
        "reviewer",
        "admin",
    }:
        raise QualificationAuthorizationError(
            "only reviewers and admins can record a clear result"
        )
    request_hash = _request_hash({
        "customer_id": check.customer_id,
        "evidence_attachment_ref": check.evidence_attachment_ref,
        "evidence_hash": check.evidence_hash,
        "inquiry_id": check.inquiry_id,
        "notes": check.notes,
        "reference": check.reference,
        "result": check.result.value,
        "source": check.source,
        "checked_at": check.checked_at.isoformat(),
    })
    with session.begin_nested():
        existing = _find_idempotency(
            session,
            org_id=check.org_id,
            actor_id=check.created_by,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _validate_idempotency(
                existing,
                endpoint=_CHECK_CREATE_ENDPOINT,
                request_hash=request_hash,
                aggregate_id=existing.result_aggregate_id,
            )
            prior = session.scalar(
                select(QualificationCheckRecord).where(
                    QualificationCheckRecord.org_id == check.org_id,
                    QualificationCheckRecord.check_id == existing.result_revision_id,
                )
            )
            if prior is None:
                raise QualificationConflictError(
                    "qualification idempotency record references a missing check"
                )
            return QualificationWriteResult(
                check_id=prior.check_id,
                result=QualificationResult(prior.result),
                evidence_hash=prior.evidence_hash,
                idempotent_replay=True,
            )

        if check.inquiry_id is not None:
            inquiry = session.scalar(
                select(InquiryCaseRecord).where(
                    InquiryCaseRecord.org_id == check.org_id,
                    InquiryCaseRecord.inquiry_id == check.inquiry_id,
                )
            )
            if inquiry is None:
                raise QualificationNotFoundError(
                    "inquiry was not found in this organization"
                )

        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=check.org_id,
                actor_id=check.created_by,
                endpoint=_CHECK_CREATE_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=QUALIFICATION_IDEMPOTENCY_TYPE,
                result_aggregate_id=check.check_id,
                result_revision_id=check.check_id,
                created_at=check.created_at,
            )
        )
        session.add(
            QualificationCheckRecord(
                check_id=check.check_id,
                org_id=check.org_id,
                customer_id=check.customer_id,
                inquiry_id=check.inquiry_id,
                source=check.source,
                reference=check.reference,
                checked_at=check.checked_at,
                result=check.result.value,
                evidence_hash=check.evidence_hash,
                evidence_attachment_ref=check.evidence_attachment_ref,
                notes=check.notes,
                reviewer_id=(
                    check.created_by
                    if check.result is QualificationResult.CLEAR
                    else check.reviewer_id
                ),
                actor_id=check.created_by,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                idempotency_record_id=record_id,
                created_at=check.created_at,
                created_by=check.created_by,
            )
        )
        session.flush()
    return QualificationWriteResult(
        check_id=check.check_id,
        result=check.result,
        evidence_hash=check.evidence_hash,
        idempotent_replay=False,
    )


def decide_qualification_check(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    check_id: str,
    evidence_hash: str,
    result: QualificationResult,
    actor_id: str,
    actor_role: str,
    idempotency_key: str,
    notes: str | None = None,
    decided_at: datetime | None = None,
) -> QualificationDecisionResult:
    """Append one authorized final decision for an exact evidence snapshot."""
    _validate_identity(org_id, check_id, actor_id)
    _validate_hash(evidence_hash)
    _validate_key(idempotency_key)
    if actor_role not in {"reviewer", "admin"}:
        raise QualificationAuthorizationError(
            "only reviewers and admins can decide a qualification check"
        )
    now = decided_at or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("decided_at must include a timezone")
    now = now.astimezone(UTC)
    request_hash = _request_hash({
        "check_id": check_id,
        "evidence_hash": evidence_hash,
        "notes": notes,
        "result": result.value,
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
                endpoint=_DECISION_ENDPOINT,
                request_hash=request_hash,
                aggregate_id=check_id,
            )
            prior = session.scalar(
                select(QualificationDecisionRecord).where(
                    QualificationDecisionRecord.org_id == org_id,
                    QualificationDecisionRecord.decision_id
                    == existing.result_revision_id,
                )
            )
            if prior is None:
                raise QualificationConflictError(
                    "qualification idempotency record references a missing decision"
                )
            return _decision_result(prior, idempotent_replay=True)

        check = session.scalar(
            select(QualificationCheckRecord)
            .where(
                QualificationCheckRecord.org_id == org_id,
                QualificationCheckRecord.check_id == check_id,
            )
            .with_for_update()
        )
        if check is None:
            raise QualificationNotFoundError(
                "qualification check was not found in this organization"
            )
        if check.evidence_hash != evidence_hash:
            raise QualificationHashMismatchError(
                "screening evidence changed; refresh before deciding"
            )
        prior = session.scalar(
            select(QualificationDecisionRecord).where(
                QualificationDecisionRecord.org_id == org_id,
                QualificationDecisionRecord.check_id == check_id,
            )
        )
        if prior is not None:
            raise QualificationAlreadyDecidedError(
                "qualification check already has a final decision"
            )

        decision_id = str(uuid.uuid4())
        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint=_DECISION_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type=QUALIFICATION_IDEMPOTENCY_TYPE,
                result_aggregate_id=check_id,
                result_revision_id=decision_id,
                created_at=now,
            )
        )
        session.add(
            QualificationDecisionRecord(
                decision_id=decision_id,
                org_id=org_id,
                check_id=check_id,
                evidence_hash=evidence_hash,
                result=result.value,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                notes=notes,
                idempotency_record_id=record_id,
                decided_at=now,
            )
        )
        session.flush()
        decision = session.get(QualificationDecisionRecord, decision_id)
        if decision is None:
            raise RuntimeError("qualification decision was not persisted")
    return _decision_result(decision, idempotent_replay=False)


def get_qualification_check(
    session: Session, *, org_id: str, check_id: str
) -> QualificationView | None:
    """Return one check and its final decision under an explicit org scope."""
    check = session.scalar(
        select(QualificationCheckRecord).where(
            QualificationCheckRecord.org_id == org_id,
            QualificationCheckRecord.check_id == check_id,
        )
    )
    if check is None:
        return None
    decision = session.scalar(
        select(QualificationDecisionRecord).where(
            QualificationDecisionRecord.org_id == org_id,
            QualificationDecisionRecord.check_id == check_id,
        )
    )
    effective = (
        QualificationResult(decision.result)
        if decision is not None
        else QualificationResult(check.result)
    )
    return QualificationView(check=check, decision=decision, effective_result=effective)


def list_qualification_checks(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    customer_id: str | None = None,
    inquiry_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[QualificationView]:
    """List checks and decisions without leaking records across organizations."""
    if limit < 1 or limit > _MAX_LIST_LIMIT or offset < 0:
        raise ValueError(
            f"limit must be between 1 and {_MAX_LIST_LIMIT} and offset non-negative"
        )
    statement = select(QualificationCheckRecord).where(
        QualificationCheckRecord.org_id == org_id
    )
    if customer_id is not None:
        statement = statement.where(QualificationCheckRecord.customer_id == customer_id)
    if inquiry_id is not None:
        statement = statement.where(QualificationCheckRecord.inquiry_id == inquiry_id)
    checks = list(
        session.scalars(
            statement.order_by(
                QualificationCheckRecord.checked_at.desc(),
                QualificationCheckRecord.check_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )
    if not checks:
        return []
    check_ids = [check.check_id for check in checks]
    decisions = {
        decision.check_id: decision
        for decision in session.scalars(
            select(QualificationDecisionRecord).where(
                QualificationDecisionRecord.org_id == org_id,
                QualificationDecisionRecord.check_id.in_(check_ids),
            )
        )
    }
    return [
        QualificationView(
            check=check,
            decision=decisions.get(check.check_id),
            effective_result=QualificationResult(
                decisions[check.check_id].result
                if check.check_id in decisions
                else check.result
            ),
        )
        for check in checks
    ]


def ensure_quote_qualification_clear(
    session: Session,
    *,
    org_id: str,
    rfq_revision_id: str | None,
) -> None:
    """Require clear evidence before approving a quote for a known target.

    RFQ and inquiry records can exist without a customer target in synthetic
    fixtures. Those records retain the original quote behavior. Once a target
    is present, missing evidence and every non-clear effective result block the
    approval transaction conservatively.
    """
    if not rfq_revision_id:
        return
    rfq_revision = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == org_id,
            RFQRevisionRecord.revision_id == rfq_revision_id,
        )
    )
    if rfq_revision is None:
        return
    plan = rfq_revision.payload.get("plan")
    customer_id = plan.get("customer_id") if isinstance(plan, dict) else None
    inquiry_ids = list(
        session.scalars(
            select(InquiryCaseRecord.inquiry_id).where(
                InquiryCaseRecord.org_id == org_id,
                InquiryCaseRecord.rfq_id == rfq_revision.rfq_id,
            )
        )
    )
    if customer_id is None and not inquiry_ids:
        return

    target_filter = []
    if isinstance(customer_id, str) and customer_id:
        target_filter.append(QualificationCheckRecord.customer_id == customer_id)
    if inquiry_ids:
        target_filter.append(QualificationCheckRecord.inquiry_id.in_(inquiry_ids))
    checks = list(
        session.scalars(
            select(QualificationCheckRecord).where(
                QualificationCheckRecord.org_id == org_id,
                or_(*target_filter),
            )
        )
    )
    if not checks:
        raise QualificationGateError(
            "quote approval requires a recorded clear qualification result"
        )
    check_ids = [check.check_id for check in checks]
    decisions = {
        decision.check_id: decision
        for decision in session.scalars(
            select(QualificationDecisionRecord).where(
                QualificationDecisionRecord.org_id == org_id,
                QualificationDecisionRecord.check_id.in_(check_ids),
            )
        )
    }
    unresolved: list[str] = []
    for check in checks:
        decision = decisions.get(check.check_id)
        effective_result = decision.result if decision is not None else check.result
        if effective_result != QualificationResult.CLEAR.value:
            unresolved.append(effective_result)
    if unresolved:
        raise QualificationGateError(
            "quote approval is blocked by unresolved qualification evidence: "
            + ", ".join(sorted(set(unresolved)))
        )


def _decision_result(
    record: QualificationDecisionRecord, *, idempotent_replay: bool
) -> QualificationDecisionResult:
    return QualificationDecisionResult(
        decision_id=record.decision_id,
        check_id=record.check_id,
        evidence_hash=record.evidence_hash,
        result=QualificationResult(record.result),
        reviewer_id=record.actor_id,
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


def _validate_idempotency(
    existing: IdempotencyRecord,
    *,
    endpoint: str,
    request_hash: str,
    aggregate_id: str,
) -> None:
    if existing.endpoint != endpoint:
        raise QualificationIdempotencyConflictError(
            "idempotency key was already used by another endpoint"
        )
    if existing.request_hash != request_hash:
        raise QualificationIdempotencyConflictError(
            "idempotency key was reused with a different request"
        )
    if (
        existing.result_aggregate_type != QUALIFICATION_IDEMPOTENCY_TYPE
        or existing.result_aggregate_id != aggregate_id
    ):
        raise QualificationIdempotencyConflictError(
            "idempotency key belongs to another qualification aggregate"
        )


def _validate_identity(*values: str) -> None:
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("qualification identity fields must be non-empty")


def _validate_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("idempotency_key must be non-empty")


def _validate_hash(value: str) -> None:
    if len(value) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("evidence_hash must be a lowercase SHA-256 digest")


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


# Short aliases keep the service vocabulary convenient for API callers.
create_qualification = create_qualification_check
decide_qualification = decide_qualification_check
