from decimal import Decimal

from trade_agent.catalog import CatalogProduct
from trade_agent.contracts import FactOrigin, FieldFact, RFQItem, SpecificationFact
from trade_agent.matching import (
    MatchStatus,
    Recommendation,
    compare_product_specs,
    search_catalog,
)


def _item(
    specifications: dict[str, str | None] | None = None,
    description: str = "fastener request",
    raw_specifications: dict[str, str | None] | None = None,
) -> RFQItem:
    specs = tuple(
        SpecificationFact(name=name, value=value)
        for name, value in (specifications or {}).items()
    )
    facts = [
        FieldFact(field_name="quantity", origin=FactOrigin.EXTRACTED),
        FieldFact(field_name="unit", origin=FactOrigin.EXTRACTED),
    ]
    facts.extend(
        FieldFact(
            field_name=f"specifications.{specification.name}",
            origin=FactOrigin.EXTRACTED,
            raw_value=(raw_specifications or {}).get(
                specification.name, specification.value
            ),
            normalized_value=specification.value,
        )
        for specification in specs
    )
    return RFQItem(
        rfq_item_id="item_001",
        description_raw=description,
        missing_fields=("quantity", "unit"),
        specifications=specs,
        facts=tuple(facts),
    )


def _product(sku: str, **overrides: str) -> CatalogProduct:
    values = {
        "material": "stainless steel",
        "grade": "A2-70",
        "standard": "ISO 4014",
        "diameter": "8",
        "length": "30",
    }
    values.update(overrides)
    return CatalogProduct(
        sku=sku,
        name=f"Hex bolt {sku}",
        category="hex_bolt",
        standard=values["standard"],
        material=values["material"],
        grade=values["grade"],
        diameter_mm=Decimal(values["diameter"]),
        length_mm=Decimal(values["length"]),
    )


def test_same_sku_with_conflicting_material_is_not_recommended() -> None:
    item = _item({"sku": "BOLT-M8-30-A2", "material": "carbon steel"})
    exact = _product("BOLT-M8-30-A2")
    alternative = _product("BOLT-CARBON", material="carbon steel")

    result = search_catalog(item, (alternative, exact))

    assert result.candidates[0].product == exact
    assert result.candidates[0].exact_sku is True
    assert result.candidates[0].conflict_fields == ("material",)
    assert result.candidates[0].hard_conflict_fields == ("material",)
    assert result.candidates[0].eligible is False
    assert result.candidates[0].recommendation is Recommendation.NOT_RECOMMENDED
    assert result.suggested_sku == alternative.sku
    assert result.requires_salesperson_confirmation is True


def test_missing_certification_evidence_is_unknown_and_requires_review() -> None:
    item = _item({"certification": "ISO 9001"})
    product = _product("BOLT-M8-30-A2")

    candidate = compare_product_specs(item, product)
    result = search_catalog(item, (product,))

    assert candidate.unknown_fields == ("certification",)
    assert candidate.comparisons[0].status is MatchStatus.UNKNOWN
    assert candidate.comparisons[0].requested_value == "ISO 9001"
    assert candidate.comparisons[0].catalog_value is None
    assert candidate.eligible is False
    assert candidate.recommendation is Recommendation.REVIEW_BEFORE_CONFIRMATION
    assert result.suggested_sku == product.sku
    assert result.requires_salesperson_confirmation is True
    assert result.recommendation is Recommendation.REVIEW_BEFORE_CONFIRMATION


def test_exact_sku_ranks_first_and_is_not_automatically_confirmed() -> None:
    item = _item({"sku": "  SKU-B  "})
    first_alphabetically = _product("SKU-A")
    exact = _product("SKU-B")

    result = search_catalog(item, (first_alphabetically, exact))

    assert result.candidates[0].product == exact
    assert result.candidates[0].exact_sku is True
    assert result.candidates[0].eligible is True
    assert result.suggested_sku == exact.sku
    assert result.requires_salesperson_confirmation is True


def test_dimensions_grade_and_standard_are_compared_conservatively() -> None:
    item = _item({
        "diameter_mm": "M8",
        "length_mm": "3 cm",
        "material_grade": "A2-70",
        "standard": " iso 4014 ",
    })
    product = _product("SKU-A")

    match = compare_product_specs(item, product)

    assert tuple(comparison.status for comparison in match.comparisons) == (
        MatchStatus.MATCH,
        MatchStatus.MATCH,
        MatchStatus.MATCH,
        MatchStatus.MATCH,
    )
    assert match.eligible is True
    assert match.comparisons[0].requested_value == "M8"


def test_comparison_keeps_the_raw_requested_value() -> None:
    item = _item(
        {"material": "stainless steel"},
        raw_specifications={"material": "STAINLESS   STEEL"},
    )

    comparison = compare_product_specs(item, _product("SKU-A")).comparisons[0]

    assert comparison.status is MatchStatus.MATCH
    assert comparison.requested_value == "stainless steel"
    assert comparison.raw_requested_value == "STAINLESS   STEEL"


def test_unsupported_or_unparseable_specs_remain_unknown() -> None:
    item = _item({"thread_direction": "right-handed", "length_mm": "about 30 mm"})

    match = compare_product_specs(item, _product("SKU-A"))

    assert match.unknown_fields == ("thread_direction", "length_mm")
    assert match.conflict_fields == ()
    assert match.eligible is False
    assert all(
        comparison.status is MatchStatus.UNKNOWN for comparison in match.comparisons
    )


def test_search_order_is_stable_and_empty_search_is_explicit() -> None:
    item = _item()
    products = (_product("SKU-C"), _product("SKU-A"), _product("SKU-B"))

    forward = search_catalog(item, products)
    reverse = search_catalog(item, tuple(reversed(products)))
    empty = search_catalog(item, ())

    assert tuple(candidate.product.sku for candidate in forward.candidates) == (
        "SKU-A",
        "SKU-B",
        "SKU-C",
    )
    assert tuple(candidate.product.sku for candidate in forward.candidates) == tuple(
        candidate.product.sku for candidate in reverse.candidates
    )
    assert forward.suggested_sku is None
    assert empty.candidates == ()
    assert empty.suggested_sku is None
    assert empty.requires_salesperson_confirmation is False
    assert empty.recommendation is Recommendation.NO_CANDIDATE


def test_keyword_search_suggests_best_deterministic_text_match() -> None:
    item = _item(description="hex bolt stainless steel M8 x 30 ISO 4014")
    weak = _product("SKU-B", material="alloy steel", grade="8.8")
    strong = _product("SKU-A")

    result = search_catalog(item, (weak, strong))

    assert result.candidates[0].product == strong
    assert result.candidates[0].text_matches
    assert result.suggested_sku == strong.sku
    assert result.requires_salesperson_confirmation is True
