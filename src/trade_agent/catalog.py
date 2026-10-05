"""Versioned catalog records and deterministic synthetic fastener data."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

CATALOG_VERSION = "SYNTH-CAT-1"
PRICE_LIST_VERSION = "SYNTH-USD-2026-01"
SYNTHETIC_SOURCE = "Synthetic demo data; no commercial value"
MIN_SYNTHETIC_PRODUCTS = 50


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    sku: str
    name: str
    category: str
    standard: str
    material: str
    grade: str
    diameter_mm: Decimal
    length_mm: Decimal
    unit: str = "piece"
    is_synthetic: bool = True


@dataclass(frozen=True, slots=True)
class PriceTier:
    sku: str
    minimum_quantity: int
    unit: str
    unit_price: Decimal
    currency: str = "USD"
    is_synthetic: bool = True


@dataclass(frozen=True, slots=True)
class SyntheticCatalog:
    catalog_version: str
    price_list_version: str
    effective_from: date
    effective_to: date | None
    source: str
    products: tuple[CatalogProduct, ...]
    prices: tuple[PriceTier, ...]


def build_synthetic_catalog() -> SyntheticCatalog:
    """Build a stable 60-SKU catalog with two USD price tiers per SKU."""
    products: list[CatalogProduct] = []
    prices: list[PriceTier] = []

    for diameter in (4, 5, 6, 8, 10):
        for length in (16, 20, 25, 30, 40, 50):
            for suffix, material, grade, surcharge_cents in (
                ("A2", "stainless steel", "A2-70", 0),
                ("A4", "stainless steel", "A4-80", 4),
            ):
                sku = f"BOLT-M{diameter}-{length}-{suffix}"
                products.append(
                    CatalogProduct(
                        sku=sku,
                        name=f"Hex bolt M{diameter} x {length} {grade}",
                        category="hex_bolt",
                        standard="ISO 4014",
                        material=material,
                        grade=grade,
                        diameter_mm=Decimal(diameter),
                        length_mm=Decimal(length),
                    )
                )
                base_cents = (
                    10 + (diameter - 8) + ((length - 30) // 10) + surcharge_cents
                )
                base_price = Decimal(base_cents) / Decimal(100)
                prices.extend((
                    PriceTier(
                        sku=sku,
                        minimum_quantity=1,
                        unit="piece",
                        unit_price=base_price,
                    ),
                    PriceTier(
                        sku=sku,
                        minimum_quantity=1000,
                        unit="piece",
                        unit_price=base_price * Decimal("0.8"),
                    ),
                ))

    return SyntheticCatalog(
        catalog_version=CATALOG_VERSION,
        price_list_version=PRICE_LIST_VERSION,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source=SYNTHETIC_SOURCE,
        products=tuple(products),
        prices=tuple(prices),
    )


def validate_catalog(catalog: SyntheticCatalog) -> None:
    """Reject inconsistent demo catalog data before writing it to PostgreSQL."""
    if len(catalog.products) < MIN_SYNTHETIC_PRODUCTS:
        raise ValueError("Synthetic catalog must include at least 50 products")
    product_skus = {product.sku for product in catalog.products}
    if len(product_skus) != len(catalog.products):
        raise ValueError("Catalog SKU values must be unique")
    if not catalog.source.lower().startswith("synthetic"):
        raise ValueError("Seed source must explicitly identify synthetic data")
    if not all(product.is_synthetic for product in catalog.products):
        raise ValueError("All seeded products must be marked synthetic")
    if not all(price.is_synthetic for price in catalog.prices):
        raise ValueError("All seeded prices must be marked synthetic")

    _validate_price_tiers(catalog.prices, product_skus)
    priced_skus = {price.sku for price in catalog.prices}
    if product_skus - priced_skus:
        raise ValueError("Every seeded product must have at least one price tier")


def _validate_price_tiers(
    prices: tuple[PriceTier, ...], product_skus: set[str]
) -> None:
    keys: set[tuple[str, int]] = set()
    for price in prices:
        if price.sku not in product_skus:
            raise ValueError(f"Price references unknown SKU: {price.sku}")
        if not isinstance(price.unit_price, Decimal):
            raise TypeError("Price tier unit price must be Decimal")
        if price.minimum_quantity <= 0:
            raise ValueError("Price tier minimum quantity must be positive")
        if price.unit_price <= Decimal("0"):
            raise ValueError("Price tier unit price must be positive")
        if price.currency != "USD":
            raise ValueError("The synthetic price list only supports USD")
        key = (price.sku, price.minimum_quantity)
        if key in keys:
            raise ValueError(f"Duplicate price tier: {key}")
        keys.add(key)
