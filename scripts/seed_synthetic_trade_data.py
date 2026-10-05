"""Load the fixed synthetic fastener catalog into PostgreSQL."""

import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trade_agent.catalog import build_synthetic_catalog
from trade_agent.db.repository import seed_synthetic_catalog

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://trade_agent:local-dev-only@localhost:5432/trade_agent"
)
logger = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = create_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            catalog = build_synthetic_catalog()
            seed_synthetic_catalog(session, catalog)
        logger.info(
            "Seeded %d synthetic products and %d price tiers (%s)",
            len(catalog.products),
            len(catalog.prices),
            catalog.price_list_version,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
