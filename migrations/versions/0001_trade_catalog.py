"""Create versioned product catalog and USD price-list tables."""

from alembic import op
import sqlalchemy as sa

revision = "0001_trade_catalog"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "trade_agent"


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    op.create_table(
        "catalog_versions",
        sa.Column("version", sa.String(length=64), primary_key=True),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "products",
        sa.Column("catalog_version", sa.String(length=64), primary_key=True),
        sa.Column("sku", sa.String(length=80), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("standard", sa.String(length=64), nullable=False),
        sa.Column("material", sa.String(length=80), nullable=False),
        sa.Column("grade", sa.String(length=32), nullable=False),
        sa.Column("diameter_mm", sa.Numeric(8, 3), nullable=False),
        sa.Column("length_mm", sa.Numeric(8, 3), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("diameter_mm > 0", name="ck_products_diameter_positive"),
        sa.CheckConstraint("length_mm > 0", name="ck_products_length_positive"),
        sa.ForeignKeyConstraint(
            ["catalog_version"],
            [f"{SCHEMA}.catalog_versions.version"],
            ondelete="CASCADE",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "price_lists",
        sa.Column("version", sa.String(length=64), primary_key=True),
        sa.Column("catalog_version", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_price_lists_currency_usd"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_price_lists_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version"],
            [f"{SCHEMA}.catalog_versions.version"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "version", "catalog_version", name="uq_price_lists_version_catalog"
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "price_list_items",
        sa.Column("price_list_version", sa.String(length=64), primary_key=True),
        sa.Column("catalog_version", sa.String(length=64), primary_key=True),
        sa.Column("sku", sa.String(length=80), primary_key=True),
        sa.Column("minimum_quantity", sa.BigInteger(), primary_key=True),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "minimum_quantity > 0", name="ck_price_items_quantity_positive"
        ),
        sa.CheckConstraint("unit_price > 0", name="ck_price_items_price_positive"),
        sa.CheckConstraint("currency = 'USD'", name="ck_price_items_currency_usd"),
        sa.ForeignKeyConstraint(
            ["price_list_version", "catalog_version"],
            [
                f"{SCHEMA}.price_lists.version",
                f"{SCHEMA}.price_lists.catalog_version",
            ],
            name="fk_price_items_list_catalog",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version", "sku"],
            [f"{SCHEMA}.products.catalog_version", f"{SCHEMA}.products.sku"],
            name="fk_price_items_product",
            ondelete="RESTRICT",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("price_list_items", schema=SCHEMA)
    op.drop_table("price_lists", schema=SCHEMA)
    op.drop_table("products", schema=SCHEMA)
    op.drop_table("catalog_versions", schema=SCHEMA)
    op.execute(sa.text(f"DROP SCHEMA IF EXISTS {SCHEMA}"))
