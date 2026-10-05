"""Hash-bound shipment handoff, freight evidence, and booking gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import func, select, text

from trade_agent.contracts import (
    ShipmentEvidenceStatus,
    ShipmentEvidenceType,
    ShipmentHandoffStatus,
)
from trade_agent.db.documents import (
    DocumentNotFoundError,
    validate_document_set,
)
from trade_agent.db.models import (
    DocumentSetRecord,
    IdempotencyRecord,
    InquiryCaseRecord,
    QuoteApprovalRecord,
    QuoteRevisionRecord,
    RFQRevisionRecord,
    ShipmentHandoffEvidenceRecord,
    ShipmentHandoffGateDecisionRecord,
    ShipmentHandoffRecord,
)
from trade_agent.db.qualification import (
    QualificationGateError,
    ensure_quote_qualification_clear,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.contracts import ShipmentEvidence, ShipmentHandoff


class ShipmentError(Exception):
    """Base class for shipment handoff failures."""


class ShipmentNotFoundError(ShipmentError):
    """A handoff or an explicitly referenced dependency is not visible."""


class ShipmentConflictError(ShipmentError):
    """The shipment operation conflicts with its current state."""


class ShipmentAuthorizationError(ShipmentConflictError):
    """The caller cannot perform a shipment operation."""


class ShipmentHashMismatchError(ShipmentConflictError):
    """A quote or evidence hash does not match the bound snapshot."""


class ShipmentNotApprovedError(ShipmentConflictError):
    """The shipment is not anchored to an approved quote revision."""


class ShipmentIdempotencyConflictError(ShipmentConflictError):
    """An idempotency key was reused with a different request."""


class ShipmentStatusTransitionError(ShipmentConflictError):
    """The handoff cannot move to the requested state."""


class ShipmentGateError(ShipmentConflictError):
    """A deterministic shipment readiness gate is blocked."""

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__("shipment handoff gate is blocked: " + "; ".join(issues))


@dataclass(frozen=True, slots=True)
class ShipmentHandoffResult:
    shipment_handoff_id: str
    quotation_id: str
    quote_revision_id: str
    approved_content_hash: str
    status: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ShipmentEvidenceResult:
    shipment_handoff_id: str
    evidence_id: str
    revision_no: int
    evidence_type: str
    check_key: str | None
    status: str
    content_hash: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ShipmentGateResult:
    shipment_handoff_id: str
    valid: bool
    gate_hash: str
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShipmentTransitionResult:
    shipment_handoff_id: str
    status: str
    target_status: str
    decision_id: str | None
    gate_hash: str
    idempotent_replay: bool


_HANDOFF_CREATE_ENDPOINT = "shipment_handoffs.create"
_EVIDENCE_APPEND_ENDPOINT = "shipment_handoffs.evidence.append"
_TRANSITION_ENDPOINT = "shipment_handoffs.transition"


def create_shipment_handoff(
    session: Session,
    handoff: ShipmentHandoff,
    *,
    actor_id: str,
    idempotency_key: str,
    created_at: datetime | None = None,
) -> ShipmentHandoffResult:
    """Create a handoff only when the exact quote snapshot is approved."""
    _validate_key(idempotency_key)
    _validate_actor(actor_id)
    now = _utc(created_at or datetime.now(UTC))
    request_hash = _request_hash(handoff.model_dump(mode="json"))
    with session.begin_nested():
        _lock_idempotency_scope(
            session,
            org_id=handoff.org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        existing = _get_idempotency(
            session,
            org_id=handoff.org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _check_idempotency(existing, _HANDOFF_CREATE_ENDPOINT, request_hash)
            prior = session.scalar(
                select(ShipmentHandoffRecord).where(
                    ShipmentHandoffRecord.org_id == handoff.org_id,
                    ShipmentHandoffRecord.shipment_handoff_id
                    == existing.result_aggregate_id,
                )
            )
            if prior is None:
                raise ShipmentConflictError(
                    "idempotency record references a missing shipment handoff"
                )
            return _handoff_result(prior, idempotent_replay=True)

        duplicate = session.get(ShipmentHandoffRecord, handoff.shipment_handoff_id)
        if duplicate is not None:
            if duplicate.org_id != handoff.org_id:
                raise ShipmentConflictError("shipment handoff id already exists")
            raise ShipmentConflictError("shipment handoff id already exists")

        quote, approval = _approved_quote(
            session,
            org_id=handoff.org_id,
            quotation_id=handoff.quotation_id,
            quote_revision_id=handoff.quote_revision_id,
            approved_content_hash=handoff.approved_content_hash,
        )
        _ensure_quote_fields(handoff, quote)
        _ensure_document_binding(
            session,
            org_id=handoff.org_id,
            document_set_id=handoff.document_set_id,
            quotation_id=handoff.quotation_id,
            quote_revision_id=handoff.quote_revision_id,
            approved_content_hash=handoff.approved_content_hash,
        )

        record_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=record_id,
                org_id=handoff.org_id,
                actor_id=actor_id,
                endpoint=_HANDOFF_CREATE_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type="shipment_handoff",
                result_aggregate_id=handoff.shipment_handoff_id,
                result_revision_id=handoff.shipment_handoff_id,
                created_at=now,
            )
        )
        session.add(
            ShipmentHandoffRecord(
                shipment_handoff_id=handoff.shipment_handoff_id,
                org_id=handoff.org_id,
                quotation_id=handoff.quotation_id,
                quote_revision_id=handoff.quote_revision_id,
                approval_id=approval.approval_id,
                approved_content_hash=handoff.approved_content_hash,
                document_set_id=handoff.document_set_id,
                selected_incoterm=handoff.selected_incoterm,
                named_place=handoff.named_place,
                responsibility_split=handoff.responsibility_split,
                freight_forwarder_name=handoff.freight_forwarder_name,
                freight_forwarder_quote_ref=handoff.freight_forwarder_quote_ref,
                carrier_name=handoff.carrier_name,
                insurance_scope=handoff.insurance_scope,
                insurance_expires_at=handoff.insurance_expires_at,
                booking_reference=handoff.booking_reference,
                eta=handoff.eta,
                required_documents=list(handoff.required_documents),
                status=ShipmentHandoffStatus.DRAFT.value,
                created_at=now,
                updated_at=now,
                created_by=actor_id,
            )
        )
        session.flush()
        record = session.get(ShipmentHandoffRecord, handoff.shipment_handoff_id)
        if record is None:
            raise ShipmentConflictError("shipment handoff could not be created")
        return _handoff_result(record, idempotent_replay=False)


def get_shipment_handoff(
    session: Session, *, org_id: str, shipment_handoff_id: str
) -> ShipmentHandoffRecord | None:
    """Return a handoff only within the requested organization."""
    return session.scalar(
        select(ShipmentHandoffRecord).where(
            ShipmentHandoffRecord.org_id == org_id,
            ShipmentHandoffRecord.shipment_handoff_id == shipment_handoff_id,
        )
    )


def list_shipment_evidence(
    session: Session, *, org_id: str, shipment_handoff_id: str
) -> list[ShipmentHandoffEvidenceRecord]:
    _require_handoff(session, org_id=org_id, shipment_handoff_id=shipment_handoff_id)
    return list(
        session.scalars(
            select(ShipmentHandoffEvidenceRecord)
            .where(
                ShipmentHandoffEvidenceRecord.org_id == org_id,
                ShipmentHandoffEvidenceRecord.shipment_handoff_id
                == shipment_handoff_id,
            )
            .order_by(ShipmentHandoffEvidenceRecord.revision_no)
        )
    )


def list_shipment_gate_decisions(
    session: Session, *, org_id: str, shipment_handoff_id: str
) -> list[ShipmentHandoffGateDecisionRecord]:
    _require_handoff(session, org_id=org_id, shipment_handoff_id=shipment_handoff_id)
    return list(
        session.scalars(
            select(ShipmentHandoffGateDecisionRecord)
            .where(
                ShipmentHandoffGateDecisionRecord.org_id == org_id,
                ShipmentHandoffGateDecisionRecord.shipment_handoff_id
                == shipment_handoff_id,
            )
            .order_by(ShipmentHandoffGateDecisionRecord.decided_at)
        )
    )


def append_shipment_evidence(  # noqa: C901, PLR0913
    session: Session,
    *,
    org_id: str,
    shipment_handoff_id: str,
    evidence: ShipmentEvidence,
    actor_id: str,
    idempotency_key: str,
    expected_evidence_id: str | None = None,
    created_at: datetime | None = None,
) -> ShipmentEvidenceResult:
    """Append one evidence/checklist revision; corrections are new rows."""
    _validate_key(idempotency_key)
    _validate_actor(actor_id)
    now = _utc(created_at or datetime.now(UTC))
    request_hash = _request_hash({
        "evidence": evidence.model_dump(mode="json"),
        "expected_evidence_id": expected_evidence_id,
        "shipment_handoff_id": shipment_handoff_id,
    })
    with session.begin_nested():
        _lock_idempotency_scope(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        existing = _get_idempotency(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _check_idempotency(existing, _EVIDENCE_APPEND_ENDPOINT, request_hash)
            prior = session.scalar(
                select(ShipmentHandoffEvidenceRecord).where(
                    ShipmentHandoffEvidenceRecord.org_id == org_id,
                    ShipmentHandoffEvidenceRecord.evidence_id
                    == existing.result_revision_id,
                )
            )
            if prior is None:
                raise ShipmentConflictError(
                    "idempotency record references missing shipment evidence"
                )
            return _evidence_result(prior, idempotent_replay=True)

        handoff = session.scalar(
            select(ShipmentHandoffRecord)
            .where(
                ShipmentHandoffRecord.org_id == org_id,
                ShipmentHandoffRecord.shipment_handoff_id == shipment_handoff_id,
            )
            .with_for_update()
        )
        if handoff is None:
            raise ShipmentNotFoundError("shipment handoff was not found")
        if handoff.status in {
            ShipmentHandoffStatus.COMPLETED.value,
            ShipmentHandoffStatus.CANCELLED.value,
        }:
            raise ShipmentConflictError("terminal shipment handoffs cannot be revised")
        duplicate_evidence = session.scalar(
            select(ShipmentHandoffEvidenceRecord).where(
                ShipmentHandoffEvidenceRecord.org_id == org_id,
                ShipmentHandoffEvidenceRecord.shipment_handoff_id
                == shipment_handoff_id,
                ShipmentHandoffEvidenceRecord.evidence_id == evidence.evidence_id,
            )
        )
        if duplicate_evidence is not None:
            raise ShipmentConflictError("evidence_id already exists for this handoff")
        latest = _latest_evidence(session, org_id, shipment_handoff_id)
        current_id = latest[0].evidence_id if latest else None
        if expected_evidence_id != current_id:
            raise ShipmentConflictError(
                "current shipment evidence does not match expectation"
            )
        if evidence.parent_evidence_id is not None:
            parent = session.scalar(
                select(ShipmentHandoffEvidenceRecord).where(
                    ShipmentHandoffEvidenceRecord.org_id == org_id,
                    ShipmentHandoffEvidenceRecord.shipment_handoff_id
                    == shipment_handoff_id,
                    ShipmentHandoffEvidenceRecord.evidence_id
                    == evidence.parent_evidence_id,
                )
            )
            if parent is None:
                raise ShipmentNotFoundError("parent shipment evidence was not found")
        elif evidence.status is ShipmentEvidenceStatus.VOID:
            raise ShipmentConflictError("void evidence must identify its parent")

        revision_no = (
            session.scalar(
                select(func.max(ShipmentHandoffEvidenceRecord.revision_no)).where(
                    ShipmentHandoffEvidenceRecord.org_id == org_id,
                    ShipmentHandoffEvidenceRecord.shipment_handoff_id
                    == shipment_handoff_id,
                )
            )
            or 0
        ) + 1
        idem_id = str(uuid.uuid4())
        session.add(
            IdempotencyRecord(
                record_id=idem_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint=_EVIDENCE_APPEND_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type="shipment_handoff",
                result_aggregate_id=shipment_handoff_id,
                result_revision_id=evidence.evidence_id,
                created_at=now,
            )
        )
        session.add(
            ShipmentHandoffEvidenceRecord(
                evidence_id=evidence.evidence_id,
                org_id=org_id,
                shipment_handoff_id=shipment_handoff_id,
                revision_no=revision_no,
                parent_evidence_id=evidence.parent_evidence_id,
                evidence_type=evidence.evidence_type.value,
                check_key=evidence.check_key,
                evidence_ref=evidence.evidence_ref,
                content_hash=evidence.content_hash,
                status=evidence.status.value,
                notes=evidence.notes,
                expires_at=evidence.expires_at,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                idempotency_record_id=idem_id,
                created_at=now,
            )
        )
        session.flush()
        record = session.get(ShipmentHandoffEvidenceRecord, evidence.evidence_id)
        if record is None:
            raise ShipmentConflictError("shipment evidence could not be created")
        return _evidence_result(record, idempotent_replay=False)


def evaluate_shipment_gate(  # noqa: C901, PLR0912, PLR0915
    session: Session,
    *,
    org_id: str,
    shipment_handoff_id: str,
    target_status: str = ShipmentHandoffStatus.READY_FOR_BOOKING.value,
    now: datetime | None = None,
) -> ShipmentGateResult:
    """Evaluate every required dependency; no missing read is treated as clear."""
    if target_status not in {
        ShipmentHandoffStatus.READY_FOR_BOOKING.value,
        ShipmentHandoffStatus.BOOKED.value,
        ShipmentHandoffStatus.IN_TRANSIT.value,
    }:
        raise ValueError(
            "shipment gate target must be ready_for_booking, booked, or in_transit"
        )
    handoff = _require_handoff(
        session, org_id=org_id, shipment_handoff_id=shipment_handoff_id
    )
    at = _utc(now or datetime.now(UTC))
    issues: list[str] = []
    quote, _approval = _approved_quote(
        session,
        org_id=org_id,
        quotation_id=handoff.quotation_id,
        quote_revision_id=handoff.quote_revision_id,
        approved_content_hash=handoff.approved_content_hash,
    )
    try:
        _ensure_shipment_qualification_clear(session, org_id=org_id, quote=quote)
    except QualificationGateError as exc:
        issues.append(str(exc))
    try:
        document_snapshot: tuple[str | None, str] | None = None
        _ensure_document_binding(
            session,
            org_id=org_id,
            document_set_id=handoff.document_set_id,
            quotation_id=handoff.quotation_id,
            quote_revision_id=handoff.quote_revision_id,
            approved_content_hash=handoff.approved_content_hash,
        )
        if handoff.document_set_id is None:
            raise ShipmentGateError(("document_set_required",))
        document_set = session.scalar(
            select(DocumentSetRecord).where(
                DocumentSetRecord.org_id == org_id,
                DocumentSetRecord.document_set_id == handoff.document_set_id,
            )
        )
        if document_set is None:
            raise ShipmentNotFoundError("shipment document set was not found")
        document_snapshot = (
            document_set.current_revision_id,
            document_set.status,
        )
        if document_set.status not in {"ready", "issued"}:
            issues.append("document_set_not_ready")
        checked = validate_document_set(
            session, org_id=org_id, document_set_id=handoff.document_set_id
        )
        if not checked.ready:
            issues.extend("document:" + issue.code for issue in checked.issues)
    except ShipmentGateError as exc:
        issues.extend(exc.issues)
    except QualificationGateError as exc:
        issues.append("document_qualification:" + str(exc))
    except DocumentNotFoundError as exc:
        raise ShipmentNotFoundError(str(exc)) from exc

    if handoff.selected_incoterm != _quote_trade_term(quote):
        issues.append("incoterm_quote_mismatch")
    if handoff.named_place != _quote_named_place(quote):
        issues.append("named_place_quote_mismatch")
    if not handoff.freight_forwarder_quote_ref:
        issues.append("freight_forwarder_quote_required")
    if not handoff.insurance_scope or not handoff.insurance_expires_at:
        issues.append("insurance_scope_and_expiry_required")
    elif handoff.insurance_expires_at <= at:
        issues.append("insurance_expired")

    evidence = _latest_evidence(session, org_id, shipment_handoff_id)
    latest_by_key = _evidence_by_key(evidence)
    _require_verified(
        latest_by_key, ShipmentEvidenceType.FREIGHT_QUOTE.value, issues, at
    )
    _require_verified(latest_by_key, ShipmentEvidenceType.INSURANCE.value, issues, at)
    _require_verified(
        latest_by_key, ShipmentEvidenceType.PACKING_CHECK.value, issues, at
    )
    _require_verified(latest_by_key, ShipmentEvidenceType.LABEL_CHECK.value, issues, at)
    issues.extend(
        "required_document_missing:" + required
        for required in handoff.required_documents
        if not _has_verified_document(latest_by_key, required, at)
    )
    if target_status == ShipmentHandoffStatus.BOOKED.value:
        if not handoff.booking_reference:
            issues.append("booking_reference_required")
        if not handoff.carrier_name:
            issues.append("carrier_name_required")
        if handoff.eta is None:
            issues.append("eta_required")
        _require_verified(latest_by_key, ShipmentEvidenceType.BOOKING.value, issues, at)
    if target_status == ShipmentHandoffStatus.IN_TRANSIT.value:
        if not handoff.booking_reference:
            issues.append("booking_reference_required")
        if not handoff.carrier_name:
            issues.append("carrier_name_required")
        if handoff.eta is None:
            issues.append("eta_required")
        if not any(
            item.status == ShipmentEvidenceStatus.VERIFIED.value
            and (item.expires_at is None or item.expires_at > at)
            for evidence_type in (
                ShipmentEvidenceType.BILL_OF_LADING.value,
                ShipmentEvidenceType.AIR_WAYBILL.value,
            )
            for (kind, _check_key), item in latest_by_key.items()
            if kind == evidence_type
        ):
            issues.append("bill_of_lading_or_air_waybill_required")

    gate_hash = _gate_hash(
        handoff, quote, evidence, issues, target_status, document_snapshot
    )
    return ShipmentGateResult(
        shipment_handoff_id=shipment_handoff_id,
        valid=not issues,
        gate_hash=gate_hash,
        issues=tuple(dict.fromkeys(issues)),
    )


def transition_shipment_handoff(  # noqa: C901, PLR0912, PLR0913
    session: Session,
    *,
    org_id: str,
    shipment_handoff_id: str,
    target_status: str,
    actor_id: str,
    actor_role: str,
    idempotency_key: str,
    reason: str | None = None,
    override: bool = False,
    booking_reference: str | None = None,
    eta: datetime | None = None,
    now: datetime | None = None,
) -> ShipmentTransitionResult:
    """Move a handoff through explicit states and persist gate decisions."""
    _validate_key(idempotency_key)
    _validate_actor(actor_id)
    if target_status not in {item.value for item in ShipmentHandoffStatus}:
        raise ValueError("invalid shipment handoff status")
    if target_status in {
        ShipmentHandoffStatus.READY_FOR_BOOKING.value,
        ShipmentHandoffStatus.BOOKED.value,
        ShipmentHandoffStatus.BLOCKED.value,
        ShipmentHandoffStatus.COMPLETED.value,
        ShipmentHandoffStatus.CANCELLED.value,
    } and actor_role not in {"reviewer", "admin"}:
        raise ShipmentAuthorizationError(
            "only reviewers and admins can perform this shipment transition"
        )
    if override and (
        actor_role not in {"reviewer", "admin"} or not (reason and reason.strip())
    ):
        raise ShipmentAuthorizationError(
            "shipment gate override requires reviewer/admin and a non-empty reason"
        )
    timestamp = _utc(now or datetime.now(UTC))
    request_hash = _request_hash({
        "reason": reason,
        "override": override,
        "booking_reference": booking_reference,
        "eta": _utc(eta).isoformat() if eta is not None else None,
        "shipment_handoff_id": shipment_handoff_id,
        "target_status": target_status,
    })
    with session.begin_nested():  # noqa: PLR1702
        _lock_idempotency_scope(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        existing = _get_idempotency(
            session,
            org_id=org_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            _check_idempotency(existing, _TRANSITION_ENDPOINT, request_hash)
            handoff = _require_handoff(
                session, org_id=org_id, shipment_handoff_id=shipment_handoff_id
            )
            decision_id = existing.result_revision_id
            decision = session.scalar(
                select(ShipmentHandoffGateDecisionRecord).where(
                    ShipmentHandoffGateDecisionRecord.org_id == org_id,
                    ShipmentHandoffGateDecisionRecord.decision_id == decision_id,
                )
            )
            return ShipmentTransitionResult(
                shipment_handoff_id=shipment_handoff_id,
                status=handoff.status,
                target_status=target_status,
                decision_id=decision.decision_id if decision is not None else None,
                gate_hash=(
                    decision.gate_hash
                    if decision is not None
                    else _state_hash(handoff, target_status)
                ),
                idempotent_replay=True,
            )

        handoff = session.scalar(
            select(ShipmentHandoffRecord)
            .where(
                ShipmentHandoffRecord.org_id == org_id,
                ShipmentHandoffRecord.shipment_handoff_id == shipment_handoff_id,
            )
            .with_for_update()
        )
        if handoff is None:
            raise ShipmentNotFoundError("shipment handoff was not found")
        _validate_transition(handoff.status, target_status)
        if booking_reference is not None:
            if target_status not in {
                ShipmentHandoffStatus.BOOKED.value,
                ShipmentHandoffStatus.IN_TRANSIT.value,
            }:
                raise ValueError("booking_reference is only valid when booking")
            handoff.booking_reference = booking_reference.strip() or None
        if eta is not None:
            handoff.eta = _utc(eta)
        if target_status in {
            ShipmentHandoffStatus.READY_FOR_BOOKING.value,
            ShipmentHandoffStatus.BOOKED.value,
            ShipmentHandoffStatus.IN_TRANSIT.value,
        }:
            gate = evaluate_shipment_gate(
                session,
                org_id=org_id,
                shipment_handoff_id=shipment_handoff_id,
                target_status=target_status,
                now=timestamp,
            )
            if not gate.valid:
                if not override:
                    raise ShipmentGateError(gate.issues)
                if not reason or not reason.strip():
                    raise ShipmentAuthorizationError(
                        "shipment gate override requires a non-empty reason"
                    )
            elif override:
                raise ShipmentConflictError(
                    "override is only valid when a shipment gate is blocked"
                )
            gate_hash = gate.gate_hash
        else:
            gate_hash = _state_hash(handoff, target_status)
        idem_id = str(uuid.uuid4())
        decision_id: str | None = None
        if target_status in {
            ShipmentHandoffStatus.READY_FOR_BOOKING.value,
            ShipmentHandoffStatus.BOOKED.value,
        }:
            decision_id = str(uuid.uuid4())
            session.add(
                ShipmentHandoffGateDecisionRecord(
                    decision_id=decision_id,
                    org_id=org_id,
                    shipment_handoff_id=shipment_handoff_id,
                    target_status=target_status,
                    decision="approved",
                    gate_hash=gate_hash,
                    overridden=override,
                    reason=reason,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    idempotency_record_id=idem_id,
                    decided_at=timestamp,
                )
            )
        session.add(
            IdempotencyRecord(
                record_id=idem_id,
                org_id=org_id,
                actor_id=actor_id,
                endpoint=_TRANSITION_ENDPOINT,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_aggregate_type="shipment_handoff",
                result_aggregate_id=shipment_handoff_id,
                result_revision_id=decision_id or shipment_handoff_id,
                created_at=timestamp,
            )
        )
        handoff.status = target_status
        handoff.updated_at = timestamp
        session.flush()
        return ShipmentTransitionResult(
            shipment_handoff_id=shipment_handoff_id,
            status=handoff.status,
            target_status=target_status,
            decision_id=decision_id,
            gate_hash=gate_hash,
            idempotent_replay=False,
        )


def _approved_quote(
    session: Session,
    *,
    org_id: str,
    quotation_id: str,
    quote_revision_id: str,
    approved_content_hash: str,
) -> tuple[QuoteRevisionRecord, QuoteApprovalRecord]:
    quote = session.scalar(
        select(QuoteRevisionRecord).where(
            QuoteRevisionRecord.org_id == org_id,
            QuoteRevisionRecord.quotation_id == quotation_id,
            QuoteRevisionRecord.revision_id == quote_revision_id,
        )
    )
    if quote is None:
        raise ShipmentNotFoundError("quote revision was not found")
    if quote.content_hash != approved_content_hash:
        raise ShipmentHashMismatchError(
            "approved quote hash does not match the revision"
        )
    approval = session.scalar(
        select(QuoteApprovalRecord).where(
            QuoteApprovalRecord.org_id == org_id,
            QuoteApprovalRecord.quotation_id == quotation_id,
            QuoteApprovalRecord.revision_id == quote_revision_id,
        )
    )
    if approval is None or approval.decision != "approved":
        raise ShipmentNotApprovedError(
            "only an approved quote revision can anchor a shipment handoff"
        )
    if approval.content_hash != approved_content_hash:
        raise ShipmentHashMismatchError("approval hash does not match the revision")
    return quote, approval


def _ensure_shipment_qualification_clear(
    session: Session, *, org_id: str, quote: QuoteRevisionRecord
) -> None:
    rfq_revision_id = _quote_rfq_revision_id(quote)
    if not rfq_revision_id:
        raise QualificationGateError(
            "shipment readiness requires an RFQ qualification target"
        )
    rfq = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == org_id,
            RFQRevisionRecord.revision_id == rfq_revision_id,
        )
    )
    if rfq is None:
        raise QualificationGateError(
            "shipment readiness requires a visible RFQ qualification target"
        )
    plan = rfq.payload.get("plan")
    customer_id = plan.get("customer_id") if isinstance(plan, dict) else None
    inquiry_ids = list(
        session.scalars(
            select(InquiryCaseRecord.inquiry_id).where(
                InquiryCaseRecord.org_id == org_id,
                InquiryCaseRecord.rfq_id == rfq.rfq_id,
            )
        )
    )
    if not customer_id and not inquiry_ids:
        raise QualificationGateError(
            "shipment readiness requires a customer or inquiry qualification target"
        )
    ensure_quote_qualification_clear(
        session, org_id=org_id, rfq_revision_id=rfq_revision_id
    )


def _ensure_quote_fields(handoff: ShipmentHandoff, quote: QuoteRevisionRecord) -> None:
    if handoff.selected_incoterm != _quote_trade_term(quote):
        raise ShipmentConflictError(
            "selected Incoterm must match the approved quote snapshot"
        )
    if handoff.named_place != _quote_named_place(quote):
        raise ShipmentConflictError(
            "named place must match the approved quote snapshot"
        )


def _ensure_document_binding(  # noqa: PLR0913
    session: Session,
    *,
    org_id: str,
    document_set_id: str | None,
    quotation_id: str,
    quote_revision_id: str,
    approved_content_hash: str,
) -> None:
    if document_set_id is None:
        return
    document_set = session.scalar(
        select(DocumentSetRecord).where(
            DocumentSetRecord.org_id == org_id,
            DocumentSetRecord.document_set_id == document_set_id,
        )
    )
    if document_set is None:
        raise ShipmentNotFoundError("shipment document set was not found")
    if (
        document_set.quotation_id != quotation_id
        or document_set.quote_revision_id != quote_revision_id
    ):
        raise ShipmentConflictError("document set is bound to another quote revision")
    if document_set.approved_content_hash != approved_content_hash:
        raise ShipmentHashMismatchError("document set hash does not match quote")


def _quote_rfq_revision_id(quote: QuoteRevisionRecord) -> str | None:
    value = quote.payload.get("rfq_revision_id")
    return value if isinstance(value, str) else None


def _quote_trade_term(quote: QuoteRevisionRecord) -> str:
    value = quote.payload.get("trade_term")
    return value if isinstance(value, str) else ""


def _quote_named_place(quote: QuoteRevisionRecord) -> str:
    value = quote.payload.get("named_place")
    return value if isinstance(value, str) else ""


def _latest_evidence(
    session: Session, org_id: str, shipment_handoff_id: str
) -> list[ShipmentHandoffEvidenceRecord]:
    return list(
        session.scalars(
            select(ShipmentHandoffEvidenceRecord)
            .where(
                ShipmentHandoffEvidenceRecord.org_id == org_id,
                ShipmentHandoffEvidenceRecord.shipment_handoff_id
                == shipment_handoff_id,
            )
            .order_by(ShipmentHandoffEvidenceRecord.revision_no.desc())
        )
    )


def _evidence_by_key(
    evidence: list[ShipmentHandoffEvidenceRecord],
) -> dict[tuple[str, str | None], ShipmentHandoffEvidenceRecord]:
    latest: dict[tuple[str, str | None], ShipmentHandoffEvidenceRecord] = {}
    for item in evidence:
        key = (item.evidence_type, item.check_key)
        if key not in latest:
            latest[key] = item
    return latest


def _require_verified(
    latest: dict[tuple[str, str | None], ShipmentHandoffEvidenceRecord],
    evidence_type: str,
    issues: list[str],
    at: datetime,
) -> None:
    item = latest.get((evidence_type, None))
    if item is None:
        issues.append(evidence_type + "_evidence_required")
    elif item.status != ShipmentEvidenceStatus.VERIFIED.value:
        issues.append(evidence_type + "_evidence_not_verified")
    elif item.expires_at is not None and item.expires_at <= at:
        issues.append(evidence_type + "_evidence_expired")


def _has_verified_document(
    latest: dict[tuple[str, str | None], ShipmentHandoffEvidenceRecord],
    check_key: str,
    at: datetime,
) -> bool:
    for evidence_type in (
        ShipmentEvidenceType.REQUIRED_DOCUMENT.value,
        ShipmentEvidenceType.EXPORT_DOCUMENT.value,
        ShipmentEvidenceType.IMPORT_DOCUMENT.value,
    ):
        item = latest.get((evidence_type, check_key))
        if item is not None:
            return item.status == ShipmentEvidenceStatus.VERIFIED.value and (
                item.expires_at is None or item.expires_at > at
            )
    return False


def _gate_hash(  # noqa: PLR0913, PLR0917
    handoff: ShipmentHandoffRecord,
    quote: QuoteRevisionRecord,
    evidence: list[ShipmentHandoffEvidenceRecord],
    issues: list[str],
    target_status: str,
    document_snapshot: tuple[str | None, str] | None,
) -> str:
    payload = {
        "approved_content_hash": handoff.approved_content_hash,
        "document_set_id": handoff.document_set_id,
        "evidence": [
            {
                "content_hash": row.content_hash,
                "evidence_id": row.evidence_id,
                "evidence_type": row.evidence_type,
                "check_key": row.check_key,
                "revision_no": row.revision_no,
                "status": row.status,
            }
            for row in sorted(evidence, key=lambda item: item.revision_no)
        ],
        "issues": sorted(issues),
        "selected_incoterm": handoff.selected_incoterm,
        "named_place": handoff.named_place,
        "carrier_name": handoff.carrier_name,
        "freight_forwarder_name": handoff.freight_forwarder_name,
        "freight_forwarder_quote_ref": handoff.freight_forwarder_quote_ref,
        "insurance_scope": handoff.insurance_scope,
        "insurance_expires_at": (
            handoff.insurance_expires_at.isoformat()
            if handoff.insurance_expires_at
            else None
        ),
        "required_documents": handoff.required_documents,
        "document_snapshot": document_snapshot,
        "responsibility_split": handoff.responsibility_split,
        "booking_reference": handoff.booking_reference,
        "eta": handoff.eta.isoformat() if handoff.eta else None,
        "quote_revision_id": quote.revision_id,
        "shipment_handoff_id": handoff.shipment_handoff_id,
        "target_status": target_status,
    }
    return _request_hash(payload)


def _state_hash(handoff: ShipmentHandoffRecord, target_status: str) -> str:
    return _request_hash({
        "approved_content_hash": handoff.approved_content_hash,
        "shipment_handoff_id": handoff.shipment_handoff_id,
        "target_status": target_status,
    })


def _validate_transition(current: str, target: str) -> None:
    allowed = {
        "draft": {"ready_for_booking", "blocked", "cancelled"},
        "blocked": {"ready_for_booking", "cancelled"},
        "ready_for_booking": {"booked", "blocked", "cancelled"},
        "booked": {"in_transit", "blocked", "cancelled"},
        "in_transit": {"completed", "blocked", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }
    if target not in allowed.get(current, set()):
        raise ShipmentStatusTransitionError(
            f"cannot transition shipment handoff from {current} to {target}"
        )


def _require_handoff(
    session: Session, *, org_id: str, shipment_handoff_id: str
) -> ShipmentHandoffRecord:
    handoff = get_shipment_handoff(
        session, org_id=org_id, shipment_handoff_id=shipment_handoff_id
    )
    if handoff is None:
        raise ShipmentNotFoundError("shipment handoff was not found")
    return handoff


def _handoff_result(
    record: ShipmentHandoffRecord, *, idempotent_replay: bool
) -> ShipmentHandoffResult:
    return ShipmentHandoffResult(
        shipment_handoff_id=record.shipment_handoff_id,
        quotation_id=record.quotation_id,
        quote_revision_id=record.quote_revision_id,
        approved_content_hash=record.approved_content_hash,
        status=record.status,
        idempotent_replay=idempotent_replay,
    )


def _evidence_result(
    record: ShipmentHandoffEvidenceRecord, *, idempotent_replay: bool
) -> ShipmentEvidenceResult:
    return ShipmentEvidenceResult(
        shipment_handoff_id=record.shipment_handoff_id,
        evidence_id=record.evidence_id,
        revision_no=record.revision_no,
        evidence_type=record.evidence_type,
        check_key=record.check_key,
        status=record.status,
        content_hash=record.content_hash,
        idempotent_replay=idempotent_replay,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shipment timestamps must include a timezone")
    return value.astimezone(UTC)


def _validate_actor(value: str) -> None:
    if not value or not value.strip():
        raise ValueError("actor_id is required")


def _validate_key(value: str) -> None:
    if not value or not value.strip():
        raise ValueError("idempotency_key is required")


def _get_idempotency(
    session: Session, *, org_id: str, actor_id: str, idempotency_key: str
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
    session: Session, *, org_id: str, actor_id: str, idempotency_key: str
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"trade-agent:shipments:{org_id}:{actor_id}:{idempotency_key}"},
    )


def _check_idempotency(
    record: IdempotencyRecord, endpoint: str, request_hash: str
) -> None:
    if record.endpoint != endpoint or record.request_hash != request_hash:
        raise ShipmentIdempotencyConflictError(
            "idempotency key was reused with a different request"
        )


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
