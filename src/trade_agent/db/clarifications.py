"""Persist human RFQ clarifications as versioned, idempotent revisions."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime type.
from hashlib import sha256
import json
from typing import TYPE_CHECKING
import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,  # noqa: TC002 - Pydantic resolves model annotations at runtime.
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from trade_agent.contracts import RFQRevision
from trade_agent.db.models import (
    IdempotencyRecord,
    RFQAggregate,
    RFQRevisionRecord,
)
from trade_agent.db.revisions import (
    IdempotencyConflictError,
    RevisionConflictError,
    create_rfq_revision,
)
from trade_agent.workflow.clarification import apply_clarification_answers

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_ENDPOINT = "rfqs.clarifications.apply"


class ClarificationCommand(BaseModel):
    """Authenticated clarification answers for one expected RFQ revision."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    org_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    expected_revision_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=2000)
    answers: dict[str, dict[str, JsonValue]] = Field(min_length=1, max_length=50)
    created_at: datetime

    @model_validator(mode="after")
    def require_timezone_and_answers(self) -> ClarificationCommand:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if any(not item_answers for item_answers in self.answers.values()):
            raise ValueError("each answered RFQ item must include field answers")
        return self


class ClarificationWriteResult(BaseModel):
    """Persisted child revision and whether this was an idempotent replay."""

    model_config = ConfigDict(frozen=True)

    revision: RFQRevision
    idempotent_replay: bool


def apply_persisted_clarification(
    session: Session,
    command: ClarificationCommand,
) -> ClarificationWriteResult:
    """Append a confirmed child revision and its audit/idempotency records.

    The caller owns the outer database transaction. Actor and organization values
    must be supplied from authenticated server context.
    """
    with session.begin_nested():
        replay = _reserve_clarification(session, command)
        if replay is not None:
            return replay

        aggregate = session.scalar(
            select(RFQAggregate)
            .where(
                RFQAggregate.org_id == command.org_id,
                RFQAggregate.rfq_id == command.rfq_id,
            )
            .with_for_update()
        )
        if (
            aggregate is None
            or aggregate.current_revision_id != command.expected_revision_id
        ):
            raise RevisionConflictError("RFQ revision is no longer current")

        base_record = session.scalar(
            select(RFQRevisionRecord).where(
                RFQRevisionRecord.org_id == command.org_id,
                RFQRevisionRecord.rfq_id == command.rfq_id,
                RFQRevisionRecord.revision_id == command.expected_revision_id,
            )
        )
        if base_record is None:
            raise RevisionConflictError("RFQ revision does not exist")
        current_revision = RFQRevision.model_validate(base_record.payload)
        revised = apply_clarification_answers(
            current_revision,
            command.answers,
            actor_id=command.actor_id,
            revision_id=command.revision_id,
            revision_no=current_revision.revision_no + 1,
            created_at=command.created_at,
        )
        write_result = create_rfq_revision(
            session,
            revised,
            expected_revision_id=command.expected_revision_id,
            idempotency_key=_revision_idempotency_key(command),
            reason=command.reason,
        )
        stored = session.scalar(
            select(RFQRevisionRecord).where(
                RFQRevisionRecord.org_id == command.org_id,
                RFQRevisionRecord.rfq_id == command.rfq_id,
                RFQRevisionRecord.revision_id == write_result.revision_id,
            )
        )
        if stored is None:
            raise RuntimeError("idempotency record references a missing RFQ revision")
        return ClarificationWriteResult(
            revision=RFQRevision.model_validate(stored.payload),
            idempotent_replay=write_result.idempotent_replay,
        )


def _reserve_clarification(
    session: Session,
    command: ClarificationCommand,
) -> ClarificationWriteResult | None:
    request_hash = _request_hash(command)
    proposed_id = str(uuid.uuid4())
    inserted_id = session.scalar(
        insert(IdempotencyRecord)
        .values(
            record_id=proposed_id,
            org_id=command.org_id,
            actor_id=command.actor_id,
            endpoint=_ENDPOINT,
            idempotency_key=command.idempotency_key,
            request_hash=request_hash,
            result_aggregate_type="rfq",
            result_aggregate_id=command.rfq_id,
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
    if inserted_id is not None:
        return None

    existing = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.org_id == command.org_id,
            IdempotencyRecord.actor_id == command.actor_id,
            IdempotencyRecord.idempotency_key == command.idempotency_key,
        )
    )
    if existing is None:
        raise RevisionConflictError("idempotency reservation changed; retry request")
    if existing.endpoint != _ENDPOINT:
        raise IdempotencyConflictError(
            "idempotency key was already used for a different endpoint"
        )
    if (
        existing.request_hash != request_hash
        or existing.result_aggregate_type != "rfq"
        or existing.result_aggregate_id != command.rfq_id
    ):
        raise IdempotencyConflictError(
            "idempotency key was reused with a different request"
        )
    revision_record = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == command.org_id,
            RFQRevisionRecord.rfq_id == command.rfq_id,
            RFQRevisionRecord.revision_id == existing.result_revision_id,
        )
    )
    if revision_record is None:
        raise RuntimeError("idempotency record references a missing RFQ revision")
    return ClarificationWriteResult(
        revision=RFQRevision.model_validate(revision_record.payload),
        idempotent_replay=True,
    )


def _request_hash(command: ClarificationCommand) -> str:
    payload = {
        "answers": command.answers,
        "expected_revision_id": command.expected_revision_id,
        "reason": command.reason,
        "rfq_id": command.rfq_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _revision_idempotency_key(command: ClarificationCommand) -> str:
    scope = f"{command.org_id}\0{command.actor_id}\0{command.idempotency_key}"
    return f"clarification-revision:{sha256(scope.encode('utf-8')).hexdigest()}"
