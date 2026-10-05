"""PostgreSQL inserts for immutable versioned catalog and price-list data."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trade_agent.catalog import SyntheticCatalog, validate_catalog
from trade_agent.db.models import (
    CatalogVersion,
    PriceList,
    PriceListItem,
    Product,
)


def get_product(session: Session, catalog_version: str, sku: str) -> Product | None:
    """Read one SKU from an explicit catalog version."""
    return session.scalar(
        select(Product).where(
            Product.catalog_version == catalog_version,
            Product.sku == sku,
        )
    )


def get_price_tiers(
    session: Session, price_list_version: str, sku: str
) -> list[PriceListItem]:
    """Read a SKU's price tiers in quantity order from one list version."""
    return list(
        session.scalars(
            select(PriceListItem)
            .where(
                PriceListItem.price_list_version == price_list_version,
                PriceListItem.sku == sku,
            )
            .order_by(PriceListItem.minimum_quantity)
        )
    )


def seed_synthetic_catalog(session: Session, catalog: SyntheticCatalog) -> None:
    """Insert one synthetic catalog, preserving any already-seeded version.

    The caller owns the transaction and commits only after this function returns.
    """
    validate_catalog(catalog)
    session.execute(
        insert(CatalogVersion)
        .values(
            version=catalog.catalog_version,
            source=catalog.source,
            is_synthetic=True,
        )
        .on_conflict_do_nothing(index_elements=[CatalogVersion.version])
    )
    session.execute(
        insert(PriceList)
        .values(
            version=catalog.price_list_version,
            catalog_version=catalog.catalog_version,
            currency="USD",
            effective_from=catalog.effective_from,
            effective_to=catalog.effective_to,
            source=catalog.source,
            is_synthetic=True,
        )
        .on_conflict_do_nothing(index_elements=[PriceList.version])
    )
    session.execute(
        insert(Product)
        .values([
            {
                "catalog_version": catalog.catalog_version,
                "sku": product.sku,
                "name": product.name,
                "category": product.category,
                "standard": product.standard,
                "material": product.material,
                "grade": product.grade,
                "diameter_mm": product.diameter_mm,
                "length_mm": product.length_mm,
                "unit": product.unit,
                "is_synthetic": product.is_synthetic,
            }
            for product in catalog.products
        ])
        .on_conflict_do_nothing(
            index_elements=[Product.catalog_version, Product.sku],
        )
    )
    session.execute(
        insert(PriceListItem)
        .values([
            {
                "price_list_version": catalog.price_list_version,
                "catalog_version": catalog.catalog_version,
                "sku": price.sku,
                "minimum_quantity": price.minimum_quantity,
                "unit": price.unit,
                "unit_price": price.unit_price,
                "currency": price.currency,
                "is_synthetic": price.is_synthetic,
            }
            for price in catalog.prices
        ])
        .on_conflict_do_nothing(
            index_elements=[
                PriceListItem.price_list_version,
                PriceListItem.catalog_version,
                PriceListItem.sku,
                PriceListItem.minimum_quantity,
            ],
        )
    )
