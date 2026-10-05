"""PostgreSQL-backed LangGraph checkpoint lifecycle helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import connect
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def open_postgres_checkpointer(connection_string: str) -> Iterator[PostgresSaver]:
    """Open and initialize a PostgreSQL checkpointer for one process lifetime."""
    if not isinstance(connection_string, str) or not connection_string.strip():
        raise ValueError("connection_string must be a non-empty PostgreSQL DSN")
    url = make_url(connection_string)
    if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("connection_string must use PostgreSQL with psycopg")
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)

    with connect(
        dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        checkpointer = PostgresSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=None),
        )
        checkpointer.setup()
        yield checkpointer
