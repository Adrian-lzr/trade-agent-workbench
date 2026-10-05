from decimal import Decimal
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.db.repository import (
    get_price_tiers,
    get_product,
    seed_synthetic_catalog,
)

DATABASE_URL = os.environ.get("TRADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.integration


@pytest.fixture
def migrated_test_database():
    if not DATABASE_URL:
        pytest.skip("Set TRADE_TEST_DATABASE_URL to a disposable PostgreSQL *_test DB")
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TRADE_TEST_DATABASE_URL must use PostgreSQL")
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("Refusing to reset schema unless database name ends in _test")

    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS trade_agent CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

        config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("DATABASE_URL", DATABASE_URL)
            command.upgrade(config, "head")
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS trade_agent CASCADE"))
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


def test_migration_and_seed_are_repeatable(migrated_test_database) -> None:
    catalog = build_synthetic_catalog()
    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, catalog)
    with Session(migrated_test_database) as session, session.begin():
        seed_synthetic_catalog(session, catalog)
        product = get_product(session, catalog.catalog_version, "BOLT-M8-30-A2")
        tiers = get_price_tiers(session, catalog.price_list_version, "BOLT-M8-30-A2")
        prices = [tier.unit_price for tier in tiers]
        counts = session.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM trade_agent.products), "
                "(SELECT count(*) FROM trade_agent.price_lists), "
                "(SELECT count(*) FROM trade_agent.price_list_items)"
            )
        ).one()

    assert counts == (60, 1, 120)
    assert product is not None
    assert prices == [Decimal("0.1000"), Decimal("0.0800")]
