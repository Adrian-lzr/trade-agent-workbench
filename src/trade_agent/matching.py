"""Deterministic RFQ-to-catalog matching without changing source values."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trade_agent.catalog import CatalogProduct
    from trade_agent.contracts import RFQItem, SpecificationFact


class MatchStatus(StrEnum):
    MATCH = "match"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class Recommendation(StrEnum):
    ELIGIBLE_FOR_CONFIRMATION = "eligible_for_confirmation"
    REVIEW_BEFORE_CONFIRMATION = "review_before_confirmation"
    NOT_RECOMMENDED = "not_recommended"
    NO_CANDIDATE = "no_candidate"


@dataclass(frozen=True, slots=True)
class SpecificationComparison:
    field: str
    requested_value: str | None
    catalog_value: str | None
    status: MatchStatus
    reason: str
    hard_conflict: bool = False
    raw_requested_value: str | None = None


@dataclass(frozen=True, slots=True)
class ProductMatch:
    product: CatalogProduct
    exact_sku: bool
    text_matches: tuple[str, ...]
    comparisons: tuple[SpecificationComparison, ...]
    matched_fields: tuple[str, ...]
    conflict_fields: tuple[str, ...]
    hard_conflict_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    eligible: bool
    recommendation: Recommendation


@dataclass(frozen=True, slots=True)
class CatalogSearchResult:
    candidates: tuple[ProductMatch, ...]
    suggested_sku: str | None
    requires_salesperson_confirmation: bool
    recommendation: Recommendation


@dataclass(frozen=True, slots=True)
class _ComparisonSummary:
    matched_fields: tuple[str, ...]
    conflict_fields: tuple[str, ...]
    hard_conflict_fields: tuple[str, ...]
    unknown_fields: tuple[str, ...]


_SKU_FIELDS = frozenset({"sku", "product_sku", "part_number", "item_number"})
_TEXT_FIELDS: dict[str, tuple[str, bool]] = {
    "material": ("material", True),
    "material_type": ("material", True),
    "grade": ("grade", True),
    "material_grade": ("grade", True),
    "standard": ("standard", True),
    "category": ("category", False),
    "unit": ("unit", False),
    "name": ("name", False),
}
_DIMENSION_FIELDS = {
    "diameter": ("diameter_mm", True),
    "diameter_mm": ("diameter_mm", True),
    "nominal_diameter": ("diameter_mm", True),
    "length": ("length_mm", True),
    "length_mm": ("length_mm", True),
}
_CERTIFICATION_FIELDS = frozenset({
    "cert",
    "certificate",
    "certification",
    "certifications",
    "certificate_status",
    "certification_status",
    "compliance",
    "ce",
    "reach",
    "rohs",
})
_DIMENSION_PATTERN = re.compile(
    r"^\s*(?P<metric_prefix>M\s*)?(?P<number>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mm|millimeters?|cm|centimeters?|m|meters?|inches?|in|\")?\s*$",
    re.IGNORECASE,
)
_KEYWORD_PATTERN = re.compile(r"[a-z]+\d+[a-z0-9]*|\d+(?:\.\d+)?|[a-z]+", re.I)
_KEYWORD_STOP_WORDS = frozenset({
    "a",
    "an",
    "and",
    "cm",
    "for",
    "in",
    "mm",
    "of",
    "pcs",
    "piece",
    "the",
    "x",
})
_UNIT_TO_MM = {
    "mm": Decimal("1"),
    "millimeter": Decimal("1"),
    "millimeters": Decimal("1"),
    "cm": Decimal("10"),
    "centimeter": Decimal("10"),
    "centimeters": Decimal("10"),
    "m": Decimal("1000"),
    "meter": Decimal("1000"),
    "meters": Decimal("1000"),
    "in": Decimal("25.4"),
    "inch": Decimal("25.4"),
    "inches": Decimal("25.4"),
    '"': Decimal("25.4"),
}


def compare_product_specs(item: RFQItem, product: CatalogProduct) -> ProductMatch:
    """Compare only explicitly supplied RFQ specifications against catalog data."""
    requested_sku = _requested_sku(item)
    raw_description_sku = None if requested_sku else _description_sku(item, product)
    exact_sku = (
        _same_sku(requested_sku, product.sku)
        if requested_sku is not None
        else raw_description_sku is not None
    )
    text_matches = _text_matches(item.description_raw, product)
    comparisons = tuple(
        _compare_specification(
            specification,
            _raw_specification_value(item, specification),
            product,
        )
        for specification in item.specifications
        if _canonical_field(specification.name) not in _SKU_FIELDS
    )
    summary = _summarize_comparisons(comparisons)
    eligible = (
        bool(comparisons or exact_sku)
        and not summary.hard_conflict_fields
        and not summary.unknown_fields
    )
    recommendation = _candidate_recommendation(eligible=eligible, summary=summary)
    reasons = _build_reasons(
        comparisons,
        requested_sku,
        exact_sku=exact_sku,
        raw_description_sku=raw_description_sku,
    )
    if text_matches:
        reasons = (
            *reasons,
            f"Description keywords matched: {', '.join(text_matches)}.",
        )

    return ProductMatch(
        product=product,
        exact_sku=exact_sku,
        text_matches=text_matches,
        comparisons=tuple(comparisons),
        matched_fields=summary.matched_fields,
        conflict_fields=summary.conflict_fields,
        hard_conflict_fields=summary.hard_conflict_fields,
        unknown_fields=summary.unknown_fields,
        reasons=reasons,
        eligible=eligible,
        recommendation=recommendation,
    )


def _compare_specification(
    specification: SpecificationFact,
    raw_requested_value: str | None,
    product: CatalogProduct,
) -> SpecificationComparison:
    field = _canonical_field(specification.name)
    if field in _DIMENSION_FIELDS:
        attribute, hard_conflict = _DIMENSION_FIELDS[field]
        return _dimension_comparison(
            specification.name,
            specification.value,
            getattr(product, attribute, None),
            hard_conflict=hard_conflict,
            raw_requested=raw_requested_value,
        )
    if field in _TEXT_FIELDS:
        attribute, hard_conflict = _TEXT_FIELDS[field]
        return _text_comparison(
            field=specification.name,
            requested=specification.value,
            catalog=getattr(product, attribute, None),
            hard_conflict=hard_conflict,
            raw_requested=raw_requested_value,
        )
    if field in _CERTIFICATION_FIELDS:
        return _text_comparison(
            field=specification.name,
            requested=specification.value,
            catalog=getattr(product, "certifications", None),
            hard_conflict=False,
            raw_requested=raw_requested_value,
        )
    return SpecificationComparison(
        field=specification.name,
        requested_value=specification.value,
        catalog_value=None,
        status=MatchStatus.UNKNOWN,
        reason="No catalog comparison is defined for this field.",
        raw_requested_value=raw_requested_value,
    )


def _summarize_comparisons(
    comparisons: tuple[SpecificationComparison, ...],
) -> _ComparisonSummary:
    matched = tuple(
        comparison.field
        for comparison in comparisons
        if comparison.status is MatchStatus.MATCH
    )
    conflicts = tuple(
        comparison.field
        for comparison in comparisons
        if comparison.status is MatchStatus.CONFLICT
    )
    hard_conflicts = tuple(
        comparison.field
        for comparison in comparisons
        if comparison.status is MatchStatus.CONFLICT and comparison.hard_conflict
    )
    unknown = tuple(
        comparison.field
        for comparison in comparisons
        if comparison.status is MatchStatus.UNKNOWN
    )
    return _ComparisonSummary(matched, conflicts, hard_conflicts, unknown)


def _candidate_recommendation(
    summary: _ComparisonSummary, *, eligible: bool
) -> Recommendation:
    if summary.hard_conflict_fields:
        return Recommendation.NOT_RECOMMENDED
    if not eligible or summary.conflict_fields:
        return Recommendation.REVIEW_BEFORE_CONFIRMATION
    return Recommendation.ELIGIBLE_FOR_CONFIRMATION


def _build_reasons(
    comparisons: tuple[SpecificationComparison, ...],
    requested_sku: str | None,
    *,
    exact_sku: bool,
    raw_description_sku: str | None,
) -> tuple[str, ...]:
    reasons = tuple(
        f"{comparison.field}: {comparison.reason}" for comparison in comparisons
    )
    if not comparisons:
        reasons = ("No explicit specifications were available for comparison.",)
    if requested_sku is not None:
        sku_reason = (
            "Exact SKU matches the requested SKU."
            if exact_sku
            else f"Requested SKU {requested_sku!r} differs from this product."
        )
        return (sku_reason, *reasons)
    if raw_description_sku is not None:
        return (f"Exact SKU found in RFQ text: {raw_description_sku}.", *reasons)
    return reasons


def search_catalog(
    item: RFQItem, products: Iterable[CatalogProduct]
) -> CatalogSearchResult:
    """Return a stable, ranked catalog search with an explicit human-confirmation gate."""
    candidates = tuple(compare_product_specs(item, product) for product in products)
    ordered = tuple(sorted(candidates, key=_candidate_sort_key))
    suggested = next(
        (
            candidate
            for candidate in ordered
            if (candidate.comparisons or candidate.exact_sku or candidate.text_matches)
            and not candidate.hard_conflict_fields
        ),
        None,
    )
    if suggested is None:
        recommendation = (
            Recommendation.NO_CANDIDATE if not ordered else ordered[0].recommendation
        )
    else:
        recommendation = suggested.recommendation
    return CatalogSearchResult(
        candidates=ordered,
        suggested_sku=suggested.product.sku if suggested else None,
        requires_salesperson_confirmation=suggested is not None,
        recommendation=recommendation,
    )


def _candidate_sort_key(
    candidate: ProductMatch,
) -> tuple[object, ...]:
    product = candidate.product
    return (
        not candidate.exact_sku,
        bool(candidate.hard_conflict_fields),
        len(candidate.conflict_fields),
        len(candidate.unknown_fields),
        -len(candidate.matched_fields),
        -len(candidate.text_matches),
        not (candidate.comparisons or candidate.exact_sku or candidate.text_matches),
        product.sku.casefold(),
        product.sku,
        product.name.casefold(),
        product.standard.casefold(),
        product.category.casefold(),
        product.material.casefold(),
        product.grade.casefold(),
        str(product.diameter_mm),
        str(product.length_mm),
        product.unit.casefold(),
        product.is_synthetic,
    )


def _canonical_field(name: str) -> str:
    normalized = re.sub(r"[\s./-]+", "_", name.casefold()).strip("_")
    if normalized in _ALL_FIELDS:
        return normalized
    return next(
        (field for field in _SORTED_FIELDS if normalized.endswith(f"_{field}")),
        normalized,
    )


def _text_matches(description: str, product: CatalogProduct) -> tuple[str, ...]:
    requested_terms = _keyword_terms(description)
    catalog_text = " ".join((
        product.name,
        product.category,
        product.standard,
        product.material,
        product.grade,
        product.sku,
    ))
    return tuple(sorted(requested_terms.intersection(_keyword_terms(catalog_text))))


def _keyword_terms(text: str) -> set[str]:
    return {
        term
        for term in _KEYWORD_PATTERN.findall(text.casefold())
        if not term.isdecimal() and term not in _KEYWORD_STOP_WORDS
    }


_ALL_FIELDS = frozenset({
    *_SKU_FIELDS,
    *_TEXT_FIELDS,
    *_DIMENSION_FIELDS,
    *_CERTIFICATION_FIELDS,
})
_SORTED_FIELDS = tuple(sorted(_ALL_FIELDS, key=lambda field: (-len(field), field)))


def _requested_sku(item: RFQItem) -> str | None:
    for specification in item.specifications:
        if _canonical_field(specification.name) in _SKU_FIELDS:
            return (
                specification.value
                if specification.value and specification.value.strip()
                else None
            )
    return None


def _raw_specification_value(
    item: RFQItem, specification: SpecificationFact
) -> str | None:
    fact = next(
        (
            candidate
            for candidate in item.facts
            if candidate.field_name == f"specifications.{specification.name}"
        ),
        None,
    )
    if fact is not None and fact.raw_value is not None:
        return fact.raw_value
    return specification.value


def _description_sku(item: RFQItem, product: CatalogProduct) -> str | None:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(product.sku)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    match = pattern.search(item.description_raw)
    return match.group(0) if match else None


def _same_sku(requested: str | None, catalog: str) -> bool:
    return (
        requested is not None
        and requested.strip().casefold() == catalog.strip().casefold()
    )


def _text_comparison(
    field: str,
    requested: str | None,
    catalog: object,
    *,
    hard_conflict: bool,
    raw_requested: str | None,
) -> SpecificationComparison:
    catalog_raw = _catalog_text(catalog)
    if requested is None or not requested.strip():
        return SpecificationComparison(
            field=field,
            requested_value=None,
            catalog_value=catalog_raw,
            status=MatchStatus.UNKNOWN,
            reason="The requested value is missing.",
            raw_requested_value=raw_requested,
        )
    if catalog_raw is None:
        return SpecificationComparison(
            field=field,
            requested_value=requested,
            catalog_value=None,
            status=MatchStatus.UNKNOWN,
            reason="The catalog has no evidence for this field.",
            raw_requested_value=raw_requested,
        )

    normalized_request = requested.strip().casefold()
    normalized_catalog = catalog_raw.strip().casefold()
    is_match = " ".join(normalized_request.split()) == " ".join(
        normalized_catalog.split()
    )
    status = MatchStatus.MATCH if is_match else MatchStatus.CONFLICT
    reason = (
        "Requested value matches catalog evidence."
        if is_match
        else "Requested value conflicts with catalog evidence."
    )
    return SpecificationComparison(
        field=field,
        requested_value=requested,
        catalog_value=catalog_raw,
        status=status,
        reason=reason,
        hard_conflict=hard_conflict,
        raw_requested_value=raw_requested,
    )


def _dimension_comparison(
    field: str,
    requested: str | None,
    catalog: object,
    *,
    hard_conflict: bool,
    raw_requested: str | None,
) -> SpecificationComparison:
    catalog_raw = _catalog_text(catalog)
    if requested is None or not requested.strip():
        return SpecificationComparison(
            field=field,
            requested_value=None,
            catalog_value=catalog_raw,
            status=MatchStatus.UNKNOWN,
            reason="The requested value is missing.",
            raw_requested_value=raw_requested,
        )
    if catalog is None or catalog_raw is None:
        return SpecificationComparison(
            field=field,
            requested_value=requested,
            catalog_value=catalog_raw,
            status=MatchStatus.UNKNOWN,
            reason="The catalog has no evidence for this field.",
            raw_requested_value=raw_requested,
        )
    canonical_field = _canonical_field(field)
    allows_metric_prefix = canonical_field in {
        "diameter",
        "diameter_mm",
        "nominal_diameter",
    }
    request_mm = _to_millimeters(requested, allow_metric_prefix=allows_metric_prefix)
    catalog_mm = _to_millimeters(catalog_raw, allow_metric_prefix=allows_metric_prefix)
    if request_mm is None or catalog_mm is None:
        return SpecificationComparison(
            field=field,
            requested_value=requested,
            catalog_value=catalog_raw,
            status=MatchStatus.UNKNOWN,
            reason="The dimension could not be parsed conservatively.",
            raw_requested_value=raw_requested,
        )
    is_match = request_mm == catalog_mm
    return SpecificationComparison(
        field=field,
        requested_value=requested,
        catalog_value=catalog_raw,
        status=MatchStatus.MATCH if is_match else MatchStatus.CONFLICT,
        reason=(
            "Requested dimension matches catalog evidence."
            if is_match
            else "Requested dimension conflicts with catalog evidence."
        ),
        hard_conflict=hard_conflict,
        raw_requested_value=raw_requested,
    )


def _to_millimeters(value: str, *, allow_metric_prefix: bool) -> Decimal | None:
    match = _DIMENSION_PATTERN.fullmatch(value)
    if match is None or (match.group("metric_prefix") and not allow_metric_prefix):
        return None
    try:
        number = Decimal(match.group("number"))
    except InvalidOperation:
        return None
    if number <= 0:
        return None
    unit = (match.group("unit") or "mm").casefold()
    factor = _UNIT_TO_MM.get(unit)
    if factor is None:
        return None
    try:
        with localcontext() as context:
            context.prec = max(len(number.as_tuple().digits) + 4, 28)
            return number * factor
    except InvalidOperation:
        return None


def _catalog_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list, set, frozenset)):
        if not value:
            return None
        return ", ".join(
            str(part) for part in sorted(value, key=lambda part: str(part).casefold())
        )
    text = str(value)
    return text if text.strip() else None
