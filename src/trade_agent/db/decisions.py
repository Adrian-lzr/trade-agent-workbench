"""Audited human product-selection decisions tied to an immutable RFQ revision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime datetime type
from hashlib import sha256
import json
from typing import TYPE_CHECKING
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from trade_agent.catalog import CatalogProduct
from trade_agent.contracts import RFQRevision
from trade_agent.db.models import (
    AuditEventRecord,
    IdempotencyRecord,
    MatchDecisionRecord,
    Product,
    RFQAggregate,
    RFQRevisionRecord,
)
from trade_agent.db.revisions import (
    IdempotencyConflictError,
    RevisionConflictError,
)
from trade_agent.matching import compare_product_specs

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_ENDPOINT = "rfqs.match_decisions.create"


class MatchDecisionRejectedError(ValueError):
    """A requested product cannot safely be confirmed for this RFQ item."""


class MatchDecisionCommand(BaseModel):
    """Authenticated, version-checked command to confirm a catalog product."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    decision_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=128)
    rfq_id: str = Field(min_length=1, max_length=128)
    rfq_revision_id: str = Field(min_length=1, max_length=128)
    rfq_item_id: str = Field(min_length=1, max_length=128)
    catalog_version: str = Field(min_length=1, max_length=64)
    selected_sku: str = Field(min_length=1, max_length=80)
    actor_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=2000)
    created_at: datetime

    @model_validator(mode="after")
    def require_timezone(self) -> MatchDecisionCommand:
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return self


@dataclass(frozen=True, slots=True)
class MatchDecisionWriteResult:
    decision_id: str
    idempotent_replay: bool


def record_match_decision(
    session: Session,
    command: MatchDecisionCommand,
) -> MatchDecisionWriteResult:
    """Record a safe product choice and its audit/idempotency rows atomically.

    The caller owns the surrounding transaction. The command's actor and org must
    come from authenticated server context, not client-supplied identity fields.
    """
    with session.begin_nested():
        record_id, replay = _reserve_idempotency(session, command)
        if replay is not None:
            return MatchDecisionWriteResult(
                decision_id=replay.decision_id,
                idempotent_replay=True,
            )

        revision = _load_current_revision(session, command)
        item = next(
            (
                item
                for item in revision.plan.items
                if item.rfq_item_id == command.rfq_item_id
            ),
            None,
        )
        if item is None:
            raise MatchDecisionRejectedError("RFQ item does not exist in this revision")
        if item.missing_fields or item.quantity is None or item.unit_canonical is None:
            raise MatchDecisionRejectedError(
                "RFQ item must be clarified before product selection"
            )

        product_record = session.scalar(
            select(Product).where(
                Product.catalog_version == command.catalog_version,
                Product.sku == command.selected_sku,
            )
        )
        if product_record is None:
            raise MatchDecisionRejectedError(
                "selected product does not exist in the requested catalog version"
            )
        product = CatalogProduct(
            sku=product_record.sku,
            name=product_record.name,
            category=product_record.category,
            standard=product_record.standard,
            material=product_record.material,
            grade=product_record.grade,
            diameter_mm=product_record.diameter_mm,
            length_mm=product_record.length_mm,
            unit=product_record.unit,
            is_synthetic=product_record.is_synthetic,
        )
        comparison = compare_product_specs(item, product)
        if comparison.conflict_fields or comparison.unknown_fields:
            fields = (*comparison.conflict_fields, *comparison.unknown_fields)
            raise MatchDecisionRejectedError(
                f"selection has unresolved specification fields: {', '.join(fields)}"
            )

        decision = MatchDecisionRecord(
            decision_id=command.decision_id,
            org_id=command.org_id,
            rfq_id=command.rfq_id,
            rfq_revision_id=command.rfq_revision_id,
            rfq_item_id=command.rfq_item_id,
            catalog_version=command.catalog_version,
            selected_sku=command.selected_sku,
            actor_id=command.actor_id,
            idempotency_record_id=record_id,
            reason=command.reason,
            created_at=command.created_at,
        )
        event = AuditEventRecord(
            event_id=str(uuid.uuid4()),
            org_id=command.org_id,
            aggregate_type="rfq",
            aggregate_id=command.rfq_id,
            action="match_decision_recorded",
            prior_revision_id=None,
            revision_id=command.rfq_revision_id,
            actor_id=command.actor_id,
            idempotency_record_id=record_id,
            reason=command.reason,
            occurred_at=command.created_at,
        )
        session.add_all((decision, event))
        session.flush()
        return MatchDecisionWriteResult(
            decision_id=decision.decision_id,
            idempotent_replay=False,
        )


def _reserve_idempotency(
    session: Session,
    command: MatchDecisionCommand,
) -> tuple[str | None, MatchDecisionRecord | None]:
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
            result_revision_id=command.rfq_revision_id,
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
        return inserted_id, None

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
        or existing.result_revision_id != command.rfq_revision_id
    ):
        raise IdempotencyConflictError(
            "idempotency key was reused with a different request"
        )
    previous = session.scalar(
        select(MatchDecisionRecord).where(
            MatchDecisionRecord.idempotency_record_id == existing.record_id
        )
    )
    if previous is None:
        raise RuntimeError("idempotency record references a missing match decision")
    return None, previous


def _load_current_revision(
    session: Session,
    command: MatchDecisionCommand,
) -> RFQRevision:
    aggregate = session.scalar(
        select(RFQAggregate)
        .where(
            RFQAggregate.org_id == command.org_id,
            RFQAggregate.rfq_id == command.rfq_id,
        )
        .with_for_update()
    )
    if aggregate is None or aggregate.current_revision_id != command.rfq_revision_id:
        raise RevisionConflictError("RFQ revision is no longer current")
    revision_record = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == command.org_id,
            RFQRevisionRecord.rfq_id == command.rfq_id,
            RFQRevisionRecord.revision_id == command.rfq_revision_id,
        )
    )
    if revision_record is None:
        raise RevisionConflictError("RFQ revision does not exist in this organization")
    return RFQRevision.model_validate(revision_record.payload)


def _request_hash(command: MatchDecisionCommand) -> str:
    payload = {
        "catalog_version": command.catalog_version,
        "org_id": command.org_id,
        "reason": command.reason,
        "rfq_id": command.rfq_id,
        "rfq_item_id": command.rfq_item_id,
        "rfq_revision_id": command.rfq_revision_id,
        "selected_sku": command.selected_sku,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
