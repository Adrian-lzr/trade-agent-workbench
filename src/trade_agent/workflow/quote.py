"""Use audited product decisions to run the deterministic quote service."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy import select

from trade_agent.contracts import RFQRevision
from trade_agent.db.models import (
    MatchDecisionRecord,
    RFQAggregate,
    RFQRevisionRecord,
)
from trade_agent.db.revisions import RevisionConflictError
from trade_agent.drafting import (
    ManualProductSelection,
    QuoteDraftRequest,
    QuoteDraftResult,
    build_quote_draft,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.drafting import QuoteDraftRequest, QuoteDraftResult


def build_quote_draft_from_match_decisions(
    session: Session,
    request: QuoteDraftRequest,
) -> QuoteDraftResult:
    """Build M1's quote draft from the current RFQ and saved seller decisions."""
    aggregate = session.scalar(
        select(RFQAggregate)
        .where(
            RFQAggregate.org_id == request.rfq_revision.org_id,
            RFQAggregate.rfq_id == request.rfq_revision.rfq_id,
        )
        .with_for_update()
    )
    if (
        aggregate is None
        or aggregate.current_revision_id != request.rfq_revision.revision_id
    ):
        raise RevisionConflictError("RFQ revision is no longer current")

    revision_record = session.scalar(
        select(RFQRevisionRecord).where(
            RFQRevisionRecord.org_id == request.rfq_revision.org_id,
            RFQRevisionRecord.rfq_id == request.rfq_revision.rfq_id,
            RFQRevisionRecord.revision_id == request.rfq_revision.revision_id,
        )
    )
    if revision_record is None:
        raise RevisionConflictError("RFQ revision does not exist")
    revision = RFQRevision.model_validate(revision_record.payload)

    decisions = session.scalars(
        select(MatchDecisionRecord)
        .where(
            MatchDecisionRecord.org_id == revision.org_id,
            MatchDecisionRecord.rfq_id == revision.rfq_id,
            MatchDecisionRecord.rfq_revision_id == revision.revision_id,
        )
        .order_by(
            MatchDecisionRecord.created_at.desc(),
            MatchDecisionRecord.decision_id.desc(),
        )
    )
    latest_by_item: dict[str, MatchDecisionRecord] = {}
    for decision in decisions:
        latest_by_item.setdefault(decision.rfq_item_id, decision)
    if any(
        decision.catalog_version != request.catalog.catalog_version
        for decision in latest_by_item.values()
    ):
        raise RevisionConflictError("match decision uses a different catalog version")

    selections = tuple(
        ManualProductSelection(item.rfq_item_id, decision.selected_sku)
        for item in revision.plan.items
        if (decision := latest_by_item.get(item.rfq_item_id)) is not None
    )
    return build_quote_draft(
        replace(
            request,
            rfq_revision=revision,
            selected_products=selections,
        )
    )
