"""Small, versioned state contract for the trade workflow graph."""

from __future__ import annotations

from typing import Literal, TypedDict

WaitReason = Literal["clarification", "product_selection", "quote_review"]
ValidationStatus = Literal["ready", "needs_clarification", "conflict"]
MatchStatus = Literal["confirmed", "needs_review", "no_safe_match"]
QuoteStatus = Literal["ready", "blocked"]
QualityStatus = Literal["passed", "blocked"]


class TradeGraphState(TypedDict, total=False):
    """IDs and routing metadata only; business snapshots stay in repositories."""

    schema_version: int
    run_id: str
    rfq_id: str
    rfq_revision_id: str
    source_document_ids: list[str]
    catalog_version_id: str
    price_list_version_id: str
    match_decision_ids: list[str]
    quote_revision_id: str
    wait_reason: WaitReason | None
    error_code: str | None
    error_node: str | None
    retry_count: int
    validation_status: ValidationStatus
    missing_fields: list[str]
    conflict_fields: list[str]
    match_status: MatchStatus
    candidate_product_ids: list[str]
    quote_status: QuoteStatus
    quality_status: QualityStatus
    quality_issue_codes: list[str]
    quote_review_decision_id: str
