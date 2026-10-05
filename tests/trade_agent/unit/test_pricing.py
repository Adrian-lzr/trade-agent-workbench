from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.contracts import (
    CanonicalUnit,
    Currency,
    ProductSnapshot,
)
from trade_agent.pricing import (
    PricingConfigurationError,
    PricingIssue,
    PricingRequest,
    QuoteQualityError,
    price_line,
    validate_quote_quality,
)

SKU = "BOLT-M8-30-A2"
AS_OF = date(2026, 9, 29)


def _price_line(**overrides: object):
    values: dict[str, object] = {
        "sku": SKU,
        "quantity": Decimal("5000"),
        "unit": CanonicalUnit.PIECE,
        "currency": Currency.USD,
        "as_of": AS_OF,
        "rfq_item_id": "rfq-item-1",
    }
    values.update(overrides)
    return price_line(build_synthetic_catalog(), PricingRequest(**values))


def test_price_line_selects_quantity_tier_and_preserves_price_provenance() -> None:
    result = _price_line()

    assert result.is_priced
    assert result.quantity == Decimal("5000")
    assert result.unit_price == Decimal("0.080")
    assert result.line_amount == Decimal("400.00")
    assert result.selected_minimum_quantity == 1000
    assert result.price_list_version == "SYNTH-USD-2026-01"
    assert result.price_list_source == "Synthetic demo data; no commercial value"


@pytest.mark.parametrize(
    ("quantity", "expected_tier", "expected_price"),
    [
        (Decimal("1"), 1, Decimal("0.10")),
        (Decimal("999.99"), 1, Decimal("0.10")),
        (Decimal("1000"), 1000, Decimal("0.080")),
    ],
)
def test_quantity_tier_boundary_is_inclusive(
    quantity: Decimal,
    expected_tier: int,
    expected_price: Decimal,
) -> None:
    result = _price_line(quantity=quantity)

    assert result.selected_minimum_quantity == expected_tier
    assert result.unit_price == expected_price


def test_expired_or_not_yet_effective_price_list_is_not_used() -> None:
    catalog = build_synthetic_catalog()
    expired = replace(catalog, effective_to=date(2026, 9, 28))
    not_yet_effective = replace(catalog, effective_from=date(2026, 10, 1))

    expired_result = price_line(
        expired,
        PricingRequest(sku=SKU, quantity=Decimal("5000"), unit="piece", as_of=AS_OF),
    )
    future_result = price_line(
        not_yet_effective,
        PricingRequest(sku=SKU, quantity=Decimal("5000"), unit="piece", as_of=AS_OF),
    )

    assert expired_result.issues == (PricingIssue.PRICE_EXPIRED,)
    assert future_result.issues == (PricingIssue.PRICE_NOT_EFFECTIVE,)
    assert expired_result.unit_price is None


def test_no_price_is_reported_without_falling_back_to_zero() -> None:
    catalog = build_synthetic_catalog()
    no_sku_prices = replace(
        catalog,
        prices=tuple(tier for tier in catalog.prices if tier.sku != SKU),
    )

    result = price_line(
        no_sku_prices,
        PricingRequest(sku=SKU, quantity=Decimal("5000"), unit="piece", as_of=AS_OF),
    )

    assert result.issues == (PricingIssue.PRICE_MISSING,)
    assert result.unit_price is None
    assert result.line_amount is None


def test_quantity_below_first_tier_is_missing_price() -> None:
    result = _price_line(quantity=Decimal("0.5"))

    assert result.issues == (PricingIssue.PRICE_MISSING,)
    assert result.line_amount is None


def test_unit_and_currency_must_match_exactly() -> None:
    wrong_unit = _price_line(unit=CanonicalUnit.BOX)
    wrong_currency = _price_line(currency="EUR")
    catalog = build_synthetic_catalog()
    eur_catalog = replace(
        catalog,
        prices=tuple(
            replace(tier, currency="EUR") if tier.sku == SKU else tier
            for tier in catalog.prices
        ),
    )
    non_usd_tier = price_line(
        eur_catalog,
        PricingRequest(
            sku=SKU,
            quantity=Decimal("5000"),
            unit=CanonicalUnit.PIECE,
            currency="EUR",
            as_of=AS_OF,
        ),
    )

    assert wrong_unit.issues == (PricingIssue.UNIT_MISMATCH,)
    assert wrong_currency.issues == (PricingIssue.CURRENCY_MISMATCH,)
    assert non_usd_tier.issues == (PricingIssue.CURRENCY_MISMATCH,)


def test_duplicate_price_tiers_are_configuration_errors() -> None:
    catalog = build_synthetic_catalog()
    duplicate = replace(catalog, prices=(*catalog.prices, catalog.prices[0]))

    with pytest.raises(PricingConfigurationError, match="duplicate price tier"):
        price_line(
            duplicate,
            PricingRequest(
                sku=catalog.prices[0].sku,
                quantity=Decimal("1"),
                unit="piece",
                as_of=AS_OF,
            ),
        )


def test_line_amount_uses_half_up_rounding() -> None:
    catalog = build_synthetic_catalog()
    sku_tiers = tuple(
        replace(tier, minimum_quantity=1, unit_price=Decimal("0.085"))
        for tier in catalog.prices
        if tier.sku == SKU
    )
    one_tier_catalog = replace(
        catalog,
        prices=tuple(tier for tier in catalog.prices if tier.sku != SKU)
        + sku_tiers[:1],
    )

    result = price_line(
        one_tier_catalog,
        PricingRequest(sku=SKU, quantity=Decimal("1"), unit="piece", as_of=AS_OF),
    )

    assert result.unit_price == Decimal("0.085")
    assert result.line_amount == Decimal("0.09")


@pytest.mark.parametrize("quantity", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_quantity_must_be_positive_and_finite(quantity: Decimal) -> None:
    with pytest.raises(ValueError, match="finite value greater than zero"):
        _price_line(quantity=quantity)


def test_quality_gate_checks_associations_pricing_and_expected_items() -> None:
    first = _price_line(rfq_item_id="rfq-item-1")
    second = _price_line(rfq_item_id="rfq-item-2")
    quote_validity = {"as_of": AS_OF, "valid_until": date(2026, 10, 29)}

    validate_quote_quality(
        (first, second),
        expected_rfq_item_ids=("rfq-item-1", "rfq-item-2"),
        **quote_validity,
    )
    with pytest.raises(QuoteQualityError, match="missing"):
        validate_quote_quality(
            (first,),
            expected_rfq_item_ids=("rfq-item-1", "rfq-item-2"),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="must not be duplicated"):
        validate_quote_quality(
            (first, first),
            expected_rfq_item_ids=("rfq-item-1",),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="is not priced"):
        validate_quote_quality(
            (_price_line(unit=CanonicalUnit.BOX),),
            expected_rfq_item_ids=("rfq-item-1",),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="incomplete price provenance"):
        validate_quote_quality(
            (replace(first, price_list_source=""),),
            expected_rfq_item_ids=("rfq-item-1",),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="incomplete price provenance"):
        validate_quote_quality(
            (replace(first, unit=None),),
            expected_rfq_item_ids=("rfq-item-1",),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="at least one line"):
        validate_quote_quality((), expected_rfq_item_ids=(), **quote_validity)
    with pytest.raises(QuoteQualityError, match="association"):
        validate_quote_quality(
            (_price_line(rfq_item_id=None),),
            expected_rfq_item_ids=("rfq-item-1",),
            **quote_validity,
        )
    with pytest.raises(QuoteQualityError, match="valid_until is required"):
        validate_quote_quality(
            (first,),
            expected_rfq_item_ids=("rfq-item-1",),
            as_of=AS_OF,
            valid_until=None,
        )
    with pytest.raises(QuoteQualityError, match="in the past"):
        validate_quote_quality(
            (first,),
            expected_rfq_item_ids=("rfq-item-1",),
            as_of=AS_OF,
            valid_until=date(2026, 9, 28),
        )


def test_price_result_converts_to_immutable_quote_line_snapshot() -> None:
    catalog = build_synthetic_catalog()
    product = next(product for product in catalog.products if product.sku == SKU)
    result = _price_line()

    snapshot = result.to_quote_line_snapshot(
        quote_item_id="quote-item-1",
        product=ProductSnapshot(
            sku=product.sku,
            catalog_version=catalog.catalog_version,
            name=product.name,
        ),
    )

    assert snapshot.price_list_version == result.price_list_version
    assert snapshot.price_list_source == result.price_list_source
    assert snapshot.unit_price == Decimal("0.080")
    assert snapshot.line_amount == Decimal("400.00")
    with pytest.raises(ValueError, match="frozen"):
        snapshot.unit_price = Decimal("0.50")


def test_result_is_frozen() -> None:
    result = _price_line()

    with pytest.raises(AttributeError):
        result.unit_price = Decimal("1.00")
