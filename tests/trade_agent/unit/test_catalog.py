from dataclasses import replace
from decimal import Decimal

import pytest

from trade_agent.catalog import (
    CATALOG_VERSION,
    PRICE_LIST_VERSION,
    SYNTHETIC_SOURCE,
    build_synthetic_catalog,
    validate_catalog,
)


def test_synthetic_catalog_is_repeatable_and_versioned() -> None:
    first = build_synthetic_catalog()
    second = build_synthetic_catalog()

    assert first == second
    assert first.catalog_version == CATALOG_VERSION
    assert first.price_list_version == PRICE_LIST_VERSION
    assert first.source == SYNTHETIC_SOURCE
    assert len(first.products) == 60
    assert len({product.sku for product in first.products}) == 60
    assert len(first.prices) == 120
    assert all(product.is_synthetic for product in first.products)
    assert all(price.is_synthetic for price in first.prices)
    assert all(isinstance(price.unit_price, Decimal) for price in first.prices)


def test_reference_sku_has_versioned_usd_tiers_with_decimal_amounts() -> None:
    catalog = build_synthetic_catalog()
    sku = "BOLT-M8-30-A2"
    product = next(item for item in catalog.products if item.sku == sku)
    price_tiers = [price for price in catalog.prices if price.sku == sku]

    assert product.grade == "A2-70"
    assert product.diameter_mm == Decimal("8")
    assert product.length_mm == Decimal("30")
    assert [(price.minimum_quantity, price.unit_price) for price in price_tiers] == [
        (1, Decimal("0.10")),
        (1000, Decimal("0.080")),
    ]
    assert {price.currency for price in price_tiers} == {"USD"}
    assert {price.unit for price in price_tiers} == {"piece"}


def test_seed_validation_rejects_non_synthetic_or_incomplete_prices() -> None:
    catalog = build_synthetic_catalog()
    non_synthetic = replace(catalog, source="unverified supplier list")
    invalid_price = replace(catalog.prices[0], sku="UNKNOWN-SKU")
    missing_product = replace(catalog, prices=(invalid_price,))

    with pytest.raises(ValueError, match="synthetic"):
        validate_catalog(non_synthetic)
    with pytest.raises(ValueError, match="unknown SKU"):
        validate_catalog(missing_product)


def test_seed_validation_rejects_non_positive_price_tier() -> None:
    catalog = build_synthetic_catalog()
    invalid_price = replace(catalog.prices[0], unit_price=Decimal("0"))

    with pytest.raises(ValueError, match="must be positive"):
        validate_catalog(replace(catalog, prices=(invalid_price,)))


def test_seed_validation_requires_each_product_to_have_a_price() -> None:
    catalog = build_synthetic_catalog()
    unpriced_catalog = replace(
        catalog,
        prices=tuple(
            price for price in catalog.prices if price.sku != catalog.products[0].sku
        ),
    )

    with pytest.raises(ValueError, match="at least one price tier"):
        validate_catalog(unpriced_catalog)
