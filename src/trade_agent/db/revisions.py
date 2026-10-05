"""Transactional append-only RFQ/quote revisions with audit and idempotency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from trade_agent.contracts import AggregateType, AuditEvent
from trade_agent.db.models import (
    AuditEventRecord,
    IdempotencyRecord,
    QuotationAggregate,
    QuoteRevisionRecord,
    RFQAggregate,
    RFQRevisionRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.contracts import QuoteRevision, RFQRevision

type AggregateModel = type[RFQAggregate] | type[QuotationAggregate]
type RevisionModel = type[RFQRevisionRecord] | type[QuoteRevisionRecord]


class RevisionConflictError(Exception):
    """Raised when a caller's expected aggregate revision is stale."""


class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different request body."""


@dataclass(frozen=True, slots=True)
class RevisionWriteResult:
    revision_id: str
    revision_no: int
    content_hash: str | None
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class _RevisionCommand:
    org_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    revision_id: str
    revision_no: int
    parent_revision_id: str | None
    expected_revision_id: str | None
    actor_id: str
    idempotency_actor_id: str
    endpoint: str
    idempotency_key: str
    request_hash: str
    payload: dict[str, object]
    content_hash: str | None
    reason: str | None
    created_at: datetime
    run_id: str | None = None
    action_type: str | None = None
    business_version: str | None = None


def create_rfq_revision(
    session: Session,
    revision: RFQRevision,
    *,
    expected_revision_id: str | None,
    idempotency_key: str,
    reason: str | None = None,
) -> RevisionWriteResult:
    """Append an RFQ snapshot and atomically move its current revision pointer.

    The caller owns the outer transaction and is responsible for committing it.
    """
    _validate_revision_chain(
        revision.revision_no, revision.parent_revision_id, expected_revision_id
    )
    payload = revision.model_dump(mode="json")
    request_hash = _request_hash({
        "expected_revision_id": expected_revision_id,
        "reason": reason,
        "revision": _logical_revision_payload(payload),
    })
    command = _RevisionCommand(
        org_id=revision.org_id,
        aggregate_type=AggregateType.RFQ,
        aggregate_id=revision.rfq_id,
        revision_id=revision.revision_id,
        revision_no=revision.revision_no,
        parent_revision_id=revision.parent_revision_id,
        expected_revision_id=expected_revision_id,
        actor_id=revision.created_by,
        idempotency_actor_id=revision.created_by,
        endpoint="rfqs.revisions.create",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        payload=payload,
        content_hash=None,
        reason=reason,
        created_at=revision.created_at,
    )
    return _append_revision(session, command)


def create_quote_revision(  # noqa: PLR0913 - explicit persistence contract
    session: Session,
    revision: QuoteRevision,
    *,
    expected_revision_id: str | None,
    idempotency_key: str,
    reason: str | None = None,
    run_id: str | None = None,
    action_type: str | None = None,
    business_version: str | None = None,
) -> RevisionWriteResult:
    """Append an immutable quote snapshot, content hash and audit event.

    The optional action tuple is the durable idempotency key for graph side
    effects. It remains stable across worker or HTTP request retries.
    """
    _validate_action_key(run_id, action_type, business_version)
    _validate_revision_chain(
        revision.revision_no, revision.parent_revision_id, expected_revision_id
    )
    payload = revision.model_dump(mode="json", exclude={"content_hash"})
    request_hash = _request_hash({
        "expected_revision_id": expected_revision_id,
        "reason": reason,
        "revision": _logical_revision_payload(payload),
        "run_id": run_id,
        "action_type": action_type,
        "business_version": business_version,
    })
    effective_key = (
        _stable_action_key(run_id, action_type, business_version)
        if run_id is not None
        else idempotency_key
    )
    idempotency_actor_id = (
        _stable_action_actor(run_id, action_type, business_version)
        if run_id is not None
        else revision.created_by
    )
    command = _RevisionCommand(
        org_id=revision.org_id,
        aggregate_type=AggregateType.QUOTATION,
        aggregate_id=revision.quotation_id,
        revision_id=revision.revision_id,
        revision_no=revision.revision_no,
        parent_revision_id=revision.parent_revision_id,
        expected_revision_id=expected_revision_id,
        actor_id=revision.created_by,
        idempotency_actor_id=idempotency_actor_id,
        endpoint="quotations.revisions.create",
        idempotency_key=effective_key,
        request_hash=request_hash,
        payload=payload,
        content_hash=revision.content_hash,
        reason=reason,
        created_at=revision.created_at,
        run_id=run_id,
        action_type=action_type,
        business_version=business_version,
    )
    return _append_revision(session, command)


def _append_revision(
    session: Session, command: _RevisionCommand
) -> RevisionWriteResult:
    with session.begin_nested():
        record_id, replay = _reserve_idempotency_key(session, command)
        if replay is not None:
            return replay

        aggregate_model, aggregate_id_field, revision_model = _models_for(
            command.aggregate_type
        )
        aggregate_id_column = getattr(aggregate_model, aggregate_id_field)
        session.execute(
            insert(aggregate_model)
            .values({
                aggregate_id_field: command.aggregate_id,
                "org_id": command.org_id,
                "current_revision_id": None,
                "current_revision_no": None,
            })
            .on_conflict_do_nothing(index_elements=[aggregate_id_column])
        )
        aggregate = session.scalar(
            select(aggregate_model)
            .where(
                aggregate_id_column == command.aggregate_id,
                aggregate_model.org_id == command.org_id,
            )
            .with_for_update()
        )
        if aggregate is None:
            raise RevisionConflictError("aggregate does not exist in the requested org")
        if aggregate.current_revision_id != command.expected_revision_id:
            raise RevisionConflictError("current revision does not match expectation")
        current_revision_no = aggregate.current_revision_no or 0
        if command.revision_no != current_revision_no + 1:
            raise RevisionConflictError(
                "revision_no must be the next aggregate revision"
            )

        revision_values: dict[str, object] = {
            "revision_id": command.revision_id,
            "org_id": command.org_id,
            aggregate_id_field: command.aggregate_id,
            "revision_no": command.revision_no,
            "parent_revision_id": command.parent_revision_id,
            "payload": command.payload,
            "created_at": command.created_at,
            "created_by": command.actor_id,
        }
        if command.aggregate_type == AggregateType.QUOTATION:
            revision_values["run_id"] = command.run_id
            revision_values["action_type"] = command.action_type
            revision_values["business_version"] = command.business_version
            revision_values["content_hash"] = command.content_hash
        session.add(revision_model(**revision_values))
        aggregate.current_revision_id = command.revision_id
        aggregate.current_revision_no = command.revision_no
        aggregate.updated_at = datetime.now(UTC)

        audit = AuditEvent(
            event_id=str(uuid.uuid4()),
            org_id=command.org_id,
            aggregate_type=command.aggregate_type,
            aggregate_id=command.aggregate_id,
            action="revision_created" if command.parent_revision_id else "created",
            prior_revision_id=command.parent_revision_id,
            revision_id=command.revision_id,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
            request_hash=command.request_hash,
            reason=command.reason,
            occurred_at=command.created_at,
        )
        session.add(
            AuditEventRecord(
                event_id=audit.event_id,
                org_id=audit.org_id,
                aggregate_type=audit.aggregate_type.value,
                aggregate_id=audit.aggregate_id,
                action=audit.action,
                prior_revision_id=audit.prior_revision_id,
                revision_id=audit.revision_id,
                actor_id=audit.actor_id,
                idempotency_record_id=record_id,
                reason=audit.reason,
                occurred_at=audit.occurred_at,
            )
        )
        session.flush()
        return RevisionWriteResult(
            revision_id=command.revision_id,
            revision_no=command.revision_no,
            content_hash=command.content_hash,
            idempotent_replay=False,
        )


def _reserve_idempotency_key(
    session: Session, command: _RevisionCommand
) -> tuple[str | None, RevisionWriteResult | None]:
    record_id = str(uuid.uuid4())
    statement = (
        insert(IdempotencyRecord)
        .values(
            record_id=record_id,
            org_id=command.org_id,
            actor_id=command.idempotency_actor_id,
            endpoint=command.endpoint,
            idempotency_key=command.idempotency_key,
            request_hash=command.request_hash,
            result_aggregate_type=command.aggregate_type.value,
            result_aggregate_id=command.aggregate_id,
            result_revision_id=command.revision_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                IdempotencyRecord.org_id,
                IdempotencyRecord.actor_id,
                IdempotencyRecord.idempotency_key,
            ]
        )
        .returning(IdempotencyRecord.record_id)
    )
    inserted_id = session.scalar(statement)
    if inserted_id is not None:
        return inserted_id, None

    existing = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.org_id == command.org_id,
            IdempotencyRecord.actor_id == command.idempotency_actor_id,
            IdempotencyRecord.idempotency_key == command.idempotency_key,
        )
    )
    if existing is None:
        raise RevisionConflictError(
            "idempotency reservation changed; retry the request"
        )
    if existing.endpoint != command.endpoint:
        raise IdempotencyConflictError(
            "idempotency key was already used for a different endpoint"
        )
    if existing.request_hash != command.request_hash:
        raise IdempotencyConflictError(
            "idempotency key was reused with a different request"
        )
    if (
        existing.result_aggregate_type != command.aggregate_type.value
        or existing.result_aggregate_id != command.aggregate_id
    ):
        raise IdempotencyConflictError(
            "idempotency key belongs to a different aggregate"
        )
    _, _, revision_model = _models_for(command.aggregate_type)
    prior_revision = session.scalar(
        select(revision_model).where(
            revision_model.revision_id == existing.result_revision_id
        )
    )
    if prior_revision is None:
        raise RuntimeError("idempotency record references a missing revision")
    return None, RevisionWriteResult(
        revision_id=prior_revision.revision_id,
        revision_no=prior_revision.revision_no,
        content_hash=getattr(prior_revision, "content_hash", None),
        idempotent_replay=True,
    )


def _models_for(
    aggregate_type: AggregateType,
) -> tuple[AggregateModel, str, RevisionModel]:
    if aggregate_type == AggregateType.RFQ:
        return RFQAggregate, "rfq_id", RFQRevisionRecord
    return QuotationAggregate, "quotation_id", QuoteRevisionRecord


def _validate_action_key(
    run_id: str | None,
    action_type: str | None,
    business_version: str | None,
) -> None:
    values = (run_id, action_type, business_version)
    if any(value is not None for value in values) and not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise ValueError(
            "run_id, action_type and business_version must be supplied together"
        )


def _stable_action_key(
    run_id: str | None,
    action_type: str | None,
    business_version: str | None,
) -> str:
    _validate_action_key(run_id, action_type, business_version)
    if run_id is None or action_type is None or business_version is None:
        raise ValueError("a complete business action key is required")
    raw = f"{run_id}\0{action_type}\0{business_version}"
    return f"run-action:{sha256(raw.encode('utf-8')).hexdigest()}"


def _stable_action_actor(
    run_id: str | None,
    action_type: str | None,
    business_version: str | None,
) -> str:
    return _stable_action_key(run_id, action_type, business_version)


def _validate_revision_chain(
    revision_no: int,
    parent_revision_id: str | None,
    expected_revision_id: str | None,
) -> None:
    if parent_revision_id != expected_revision_id:
        raise RevisionConflictError("parent revision must match expected revision")
    if revision_no == 1 and expected_revision_id is not None:
        raise RevisionConflictError("initial revision must not have a parent")
    if revision_no > 1 and expected_revision_id is None:
        raise RevisionConflictError(
            "later revisions require an expected current revision"
        )


def _request_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _logical_revision_payload(payload: dict[str, object]) -> dict[str, object]:
    logical_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"created_at", "revision_id"}
    }
    plan = logical_payload.get("plan")
    if isinstance(plan, dict):
        logical_payload["plan"] = {
            key: value for key, value in plan.items() if key != "rfq_revision_id"
        }
    items = logical_payload.get("items")
    if isinstance(items, list):
        logical_payload["items"] = [
            {key: value for key, value in item.items() if key != "quote_item_id"}
            if isinstance(item, dict)
            else item
            for item in items
        ]
    return logical_payload
