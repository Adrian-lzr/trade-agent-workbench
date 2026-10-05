"""Deterministic orchestration from a confirmed RFQ to a quote draft."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from trade_agent.catalog import CatalogProduct, SyntheticCatalog
from trade_agent.contracts import (
    ProductSnapshot,
    QuoteLineSnapshot,
    QuoteRevision,
    RFQItem,
    RFQRevision,
    SpecificationFact,
)
from trade_agent.matching import (
    CatalogSearchResult,
    ProductMatch,
    compare_product_specs,
    search_catalog,
)
from trade_agent.pricing import (
    PricingRequest,
    PricingResult,
    QuoteQualityError,
    price_line,
    validate_quote_quality,
)


class DraftBlockerCode(StrEnum):
    VALIDITY_REQUIRED = "VALIDITY_REQUIRED"
    VALIDITY_IN_PAST = "VALIDITY_IN_PAST"
    SELECTION_REQUIRED = "SELECTION_REQUIRED"
    INVALID_SELECTION = "INVALID_SELECTION"
    SELECTION_FOR_UNKNOWN_ITEM = "SELECTION_FOR_UNKNOWN_ITEM"
    MULTIPLE_SELECTIONS = "MULTIPLE_SELECTIONS"
    SELECTED_SKU_NOT_FOUND = "SELECTED_SKU_NOT_FOUND"
    SPECIFICATION_CONFLICT = "SPECIFICATION_CONFLICT"
    SPECIFICATION_UNKNOWN = "SPECIFICATION_UNKNOWN"
    RFQ_ITEM_INCOMPLETE = "RFQ_ITEM_INCOMPLETE"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    QUOTE_QUALITY = "QUOTE_QUALITY"
    DUPLICATE_CATALOG_SKU = "DUPLICATE_CATALOG_SKU"


@dataclass(frozen=True, slots=True)
class ManualProductSelection:
    rfq_item_id: str
    sku: str


@dataclass(frozen=True, slots=True)
class QuoteDraftRequest:
    rfq_revision: RFQRevision
    catalog: SyntheticCatalog
    selected_products: tuple[ManualProductSelection, ...]
    quotation_id: str
    revision_id: str
    revision_no: int
    parent_revision_id: str | None
    created_by: str
    created_at: datetime
    as_of: date
    valid_until: date | None
    template_version: str = "quote-v1"
    response_body: str | None = None


@dataclass(frozen=True, slots=True)
class DraftBlocker:
    code: DraftBlockerCode
    rfq_item_id: str | None
    details: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ItemMatchReview:
    rfq_item_id: str
    search_result: CatalogSearchResult
    selected_match: ProductMatch | None


@dataclass(frozen=True, slots=True)
class QuoteDraftResult:
    quote_revision: QuoteRevision | None
    item_reviews: tuple[ItemMatchReview, ...]
    pricing_results: tuple[PricingResult, ...]
    blockers: tuple[DraftBlocker, ...]

    @property
    def is_ready(self) -> bool:
        return self.quote_revision is not None and not self.blockers


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    review: ItemMatchReview
    pricing_result: PricingResult | None
    priced_line: tuple[str, ProductSnapshot, PricingResult] | None
    blockers: tuple[DraftBlocker, ...]


def build_quote_draft(request: QuoteDraftRequest) -> QuoteDraftResult:
    """Build a sourced quote snapshot after explicit salesperson SKU choices.

    The application layer must derive ``created_by`` from its authenticated
    request context and submit only selections explicitly confirmed by a seller.
    """
    if not isinstance(request.as_of, date) or isinstance(request.as_of, datetime):
        raise TypeError("as_of must be a date")

    blockers = list(_validity_blockers(request.as_of, request.valid_until))

    products_by_sku = {product.sku: product for product in request.catalog.products}
    if len(products_by_sku) != len(request.catalog.products):
        return QuoteDraftResult(
            quote_revision=None,
            item_reviews=(),
            pricing_results=(),
            blockers=(
                DraftBlocker(
                    DraftBlockerCode.DUPLICATE_CATALOG_SKU,
                    None,
                    ("The selected catalog contains duplicate SKU values.",),
                ),
            ),
        )

    items = request.rfq_revision.plan.items
    item_ids = {item.rfq_item_id for item in items}
    selections_by_item: dict[str, list[str]] = {}
    for selection in request.selected_products:
        selections_by_item.setdefault(selection.rfq_item_id, []).append(selection.sku)
    blockers.extend(
        DraftBlocker(
            DraftBlockerCode.SELECTION_FOR_UNKNOWN_ITEM,
            item_id,
            ("A product selection references an item outside this RFQ revision.",),
        )
        for item_id in sorted(selections_by_item.keys() - item_ids)
    )

    prepared = tuple(
        _prepare_item(
            request,
            item,
            products_by_sku,
            tuple(selections_by_item.get(item.rfq_item_id, ())),
        )
        for item in items
    )
    item_reviews = tuple(result.review for result in prepared)
    pricing_results = tuple(
        result.pricing_result
        for result in prepared
        if result.pricing_result is not None
    )
    priced_lines = tuple(
        result.priced_line for result in prepared if result.priced_line is not None
    )
    blockers.extend(blocker for result in prepared for blocker in result.blockers)

    if not blockers:
        try:
            validate_quote_quality(
                tuple(pricing_results),
                expected_rfq_item_ids=tuple(item.rfq_item_id for item in items),
                as_of=request.as_of,
                valid_until=request.valid_until,
            )
        except QuoteQualityError as exc:
            blockers.append(
                DraftBlocker(
                    DraftBlockerCode.QUOTE_QUALITY,
                    None,
                    (str(exc),),
                )
            )

    if blockers:
        return QuoteDraftResult(
            quote_revision=None,
            item_reviews=item_reviews,
            pricing_results=pricing_results,
            blockers=tuple(blockers),
        )

    lines = tuple(
        _quote_line(request.revision_id, priced_line) for priced_line in priced_lines
    )
    quote_revision = _make_quote_revision(request, lines)
    return QuoteDraftResult(
        quote_revision=quote_revision,
        item_reviews=item_reviews,
        pricing_results=pricing_results,
        blockers=(),
    )


def _validity_blockers(
    as_of: date, valid_until: date | None
) -> tuple[DraftBlocker, ...]:
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise TypeError("as_of must be a date")
    if valid_until is None:
        return (
            DraftBlocker(
                DraftBlockerCode.VALIDITY_REQUIRED,
                None,
                ("A quote validity date is required.",),
            ),
        )
    if not isinstance(valid_until, date) or isinstance(valid_until, datetime):
        raise TypeError("valid_until must be a date")
    if valid_until < as_of:
        return (
            DraftBlocker(
                DraftBlockerCode.VALIDITY_IN_PAST,
                None,
                ("The quote validity date is before the pricing date.",),
            ),
        )
    return ()


def _prepare_item(
    request: QuoteDraftRequest,
    item: RFQItem,
    products_by_sku: dict[str, CatalogProduct],
    choices: tuple[str, ...],
) -> _PreparedItem:
    search_result = search_catalog(item, request.catalog.products)
    selected_sku, selection_blockers = _resolve_selection(item.rfq_item_id, choices)
    blockers = [*_incomplete_item_blockers(item), *selection_blockers]
    if selected_sku is None:
        return _PreparedItem(
            ItemMatchReview(item.rfq_item_id, search_result, None),
            None,
            None,
            tuple(blockers),
        )

    selected_product = products_by_sku.get(selected_sku)
    if selected_product is None:
        blockers.append(
            DraftBlocker(
                DraftBlockerCode.SELECTED_SKU_NOT_FOUND,
                item.rfq_item_id,
                (selected_sku,),
            )
        )
        return _PreparedItem(
            ItemMatchReview(item.rfq_item_id, search_result, None),
            None,
            None,
            tuple(blockers),
        )

    selected_match = compare_product_specs(item, selected_product)
    blockers.extend(_matching_blockers(item.rfq_item_id, selected_match))
    review = ItemMatchReview(item.rfq_item_id, search_result, selected_match)
    if blockers:
        return _PreparedItem(review, None, None, tuple(blockers))

    pricing = price_line(
        request.catalog,
        PricingRequest(
            sku=selected_product.sku,
            quantity=item.quantity,
            unit=item.unit_canonical,
            currency=request.rfq_revision.plan.requested_currency,
            as_of=request.as_of,
            rfq_item_id=item.rfq_item_id,
        ),
    )
    if not pricing.is_priced:
        return _PreparedItem(
            review,
            pricing,
            None,
            (
                DraftBlocker(
                    DraftBlockerCode.PRICE_UNAVAILABLE,
                    item.rfq_item_id,
                    tuple(issue.value for issue in pricing.issues),
                ),
            ),
        )
    snapshot = _product_snapshot(request, selected_product)
    return _PreparedItem(
        review,
        pricing,
        (item.rfq_item_id, snapshot, pricing),
        (),
    )


def _resolve_selection(
    rfq_item_id: str, choices: tuple[str, ...]
) -> tuple[str | None, tuple[DraftBlocker, ...]]:
    if len(choices) > 1:
        return None, (
            DraftBlocker(
                DraftBlockerCode.MULTIPLE_SELECTIONS,
                rfq_item_id,
                choices,
            ),
        )
    if not choices:
        return None, (
            DraftBlocker(
                DraftBlockerCode.SELECTION_REQUIRED,
                rfq_item_id,
                ("A salesperson must confirm a catalog SKU for this item.",),
            ),
        )
    if not isinstance(choices[0], str) or not choices[0].strip():
        return None, (
            DraftBlocker(
                DraftBlockerCode.INVALID_SELECTION,
                rfq_item_id,
                ("The selected SKU must not be empty.",),
            ),
        )
    return choices[0], ()


def _incomplete_item_blockers(item: RFQItem) -> tuple[DraftBlocker, ...]:
    missing_fields = tuple(sorted(set(item.missing_fields)))
    if not missing_fields:
        return ()
    return (
        DraftBlocker(
            DraftBlockerCode.RFQ_ITEM_INCOMPLETE,
            item.rfq_item_id,
            missing_fields,
        ),
    )


def _matching_blockers(
    rfq_item_id: str, match: ProductMatch
) -> tuple[DraftBlocker, ...]:
    blockers: list[DraftBlocker] = []
    if match.hard_conflict_fields:
        blockers.append(
            DraftBlocker(
                DraftBlockerCode.SPECIFICATION_CONFLICT,
                rfq_item_id,
                match.hard_conflict_fields,
            )
        )
    if match.unknown_fields:
        blockers.append(
            DraftBlocker(
                DraftBlockerCode.SPECIFICATION_UNKNOWN,
                rfq_item_id,
                match.unknown_fields,
            )
        )
    return tuple(blockers)


def _product_snapshot(
    request: QuoteDraftRequest, product: CatalogProduct
) -> ProductSnapshot:
    return ProductSnapshot(
        sku=product.sku,
        catalog_version=request.catalog.catalog_version,
        name=product.name,
        specifications=(
            SpecificationFact(name="category", value=product.category),
            SpecificationFact(name="standard", value=product.standard),
            SpecificationFact(name="material", value=product.material),
            SpecificationFact(name="grade", value=product.grade),
            SpecificationFact(
                name="diameter_mm", value=format(product.diameter_mm, "f")
            ),
            SpecificationFact(name="length_mm", value=format(product.length_mm, "f")),
        ),
    )


def _quote_line(
    revision_id: str,
    priced_line: tuple[str, ProductSnapshot, PricingResult],
) -> QuoteLineSnapshot:
    rfq_item_id, product, pricing = priced_line
    return pricing.to_quote_line_snapshot(
        quote_item_id=uuid5(
            NAMESPACE_URL,
            f"trade-agent:{revision_id}:{rfq_item_id}",
        ).hex,
        product=product,
    )


def _make_quote_revision(
    request: QuoteDraftRequest, lines: tuple[QuoteLineSnapshot, ...]
) -> QuoteRevision:
    plan = request.rfq_revision.plan
    return QuoteRevision(
        org_id=request.rfq_revision.org_id,
        quotation_id=request.quotation_id,
        revision_id=request.revision_id,
        revision_no=request.revision_no,
        parent_revision_id=request.parent_revision_id,
        rfq_revision_id=request.rfq_revision.revision_id,
        catalog_version=request.catalog.catalog_version,
        price_list_version=request.catalog.price_list_version,
        currency=plan.requested_currency,
        items=lines,
        trade_term=plan.trade_term,
        named_place=plan.named_place,
        delivery_date=plan.requested_delivery_date,
        valid_until=request.valid_until,
        response_body=request.response_body,
        template_version=request.template_version,
        created_at=request.created_at,
        created_by=request.created_by,
    )
