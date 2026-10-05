from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from trade_agent.db.runs import (
    RunAlreadyClaimedError,
    RunConflictError,
    RunLeaseLostError,
    append_run_event,
    claim_run,
    complete_run,
    create_run,
    get_run,
    list_run_events,
    requeue_expired_runs,
    resume_run,
    set_waiting_input,
)

DATABASE_URL = os.environ.get("TRADE_TEST_DATABASE_URL")
pytestmark = pytest.mark.integration
STARTED_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


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


def _create(session: Session, run_id: str = "run-lifecycle-1") -> None:
    create_run(
        session,
        run_id=run_id,
        org_id="org-demo",
        rfq_id="rfq-1",
        rfq_revision_id="rfq-rev-1",
        graph_version="trade-v1",
        thread_id=run_id,
        created_by="sales-1",
        now=STARTED_AT,
    )


def test_run_claim_is_single_executor_and_resume_is_idempotent(
    migrated_test_database,
) -> None:
    with Session(migrated_test_database) as session, session.begin():
        _create(session)
        claimed = claim_run(session, "run-lifecycle-1", "worker-a", now=STARTED_AT)
        assert claimed.status == "running"

        with pytest.raises(RunAlreadyClaimedError):
            claim_run(session, "run-lifecycle-1", "worker-b", now=STARTED_AT)

        waiting = set_waiting_input(
            session,
            "run-lifecycle-1",
            worker_id="worker-a",
            wait_reason="clarification",
            event_key="wait:clarification:1",
            payload={"missing_fields": ["quantity"]},
            lease_generation=claimed.lease_generation,
            now=STARTED_AT + timedelta(seconds=2),
        )
        assert waiting.status == "waiting_input"
        first = resume_run(
            session,
            "run-lifecycle-1",
            actor_id="sales-1",
            event_key="resume:clarification:1",
            now=STARTED_AT + timedelta(seconds=3),
        )
        replay = resume_run(
            session,
            "run-lifecycle-1",
            actor_id="sales-1",
            event_key="resume:clarification:1",
            now=STARTED_AT + timedelta(seconds=4),
        )
        assert first.status == replay.status == "queued"
        assert replay.idempotent_replay
        assert len(list_run_events(session, "run-lifecycle-1")) == 4


def test_expired_lease_is_requeued_then_can_finish_without_duplicate_events(
    migrated_test_database,
) -> None:
    with Session(migrated_test_database) as session, session.begin():
        _create(session, "run-lifecycle-2")
        claim_run(session, "run-lifecycle-2", "worker-a", now=STARTED_AT)

    expired_at = STARTED_AT + timedelta(seconds=61)
    with Session(migrated_test_database) as session, session.begin():
        recovered = requeue_expired_runs(session, now=expired_at)
        assert [(item.run_id, item.status) for item in recovered] == [
            ("run-lifecycle-2", "queued")
        ]
        second_claim = claim_run(session, "run-lifecycle-2", "worker-b", now=expired_at)
        complete_run(
            session,
            "run-lifecycle-2",
            worker_id="worker-b",
            event_key="complete:1",
            lease_generation=second_claim.lease_generation,
            now=expired_at + timedelta(seconds=1),
        )
        run = get_run(session, "run-lifecycle-2")
        events = list_run_events(session, "run-lifecycle-2")
        assert run.status == "succeeded"
        assert [event.event_seq for event in events] == list(range(1, 6))
        assert [event.event_type for event in events] == [
            "run.created",
            "run.claimed",
            "run.requeued",
            "run.claimed",
            "run.succeeded",
        ]


def test_stale_worker_cannot_mutate_after_fenced_reclaim(
    migrated_test_database,
) -> None:
    with Session(migrated_test_database) as session, session.begin():
        _create(session, "run-lifecycle-fenced")
        first_claim = claim_run(
            session, "run-lifecycle-fenced", "worker-a", now=STARTED_AT
        )

    expired_at = STARTED_AT + timedelta(seconds=61)
    with Session(migrated_test_database) as session, session.begin():
        requeue_expired_runs(session, now=expired_at)
        second_claim = claim_run(
            session, "run-lifecycle-fenced", "worker-b", now=expired_at
        )

    with Session(migrated_test_database) as session, session.begin():
        with pytest.raises(RunLeaseLostError, match=r"stale|own"):
            complete_run(
                session,
                "run-lifecycle-fenced",
                worker_id="worker-a",
                lease_generation=first_claim.lease_generation,
                event_key="stale-complete:1",
                now=expired_at + timedelta(seconds=1),
            )
        with pytest.raises(RunLeaseLostError, match=r"stale|own"):
            append_run_event(
                session,
                "run-lifecycle-fenced",
                "node.stale",
                event_key="stale-node:1",
                payload={"worker": "worker-a"},
                actor_id="worker-a",
                worker_id="worker-a",
                lease_generation=first_claim.lease_generation,
                now=expired_at + timedelta(seconds=1),
            )
        assert second_claim.lease_generation != first_claim.lease_generation


def test_run_events_are_append_only_and_event_key_is_idempotent(
    migrated_test_database,
) -> None:
    with Session(migrated_test_database) as session, session.begin():
        _create(session, "run-lifecycle-3")
        first = append_run_event(
            session,
            "run-lifecycle-3",
            "node.started",
            event_key="node:load:1",
            payload={"node": "load_rfq"},
            now=STARTED_AT,
        )
        replay = append_run_event(
            session,
            "run-lifecycle-3",
            "node.started",
            event_key="node:load:1",
            payload={"node": "load_rfq"},
            now=STARTED_AT + timedelta(seconds=1),
        )
        assert replay.event_id == first.event_id
        assert replay.idempotent_replay
        with pytest.raises(RunConflictError):
            append_run_event(
                session,
                "run-lifecycle-3",
                "node.started",
                event_key="node:load:1",
                payload={"node": "different"},
                now=STARTED_AT + timedelta(seconds=2),
            )
        with pytest.raises(Exception, match="immutable"), session.begin_nested():
            session.execute(
                text(
                    "UPDATE trade_agent.run_events SET event_type = 'tampered' "
                    "WHERE event_id = :event_id"
                ),
                {"event_id": first.event_id},
            )
        assert session.scalar(text("SELECT count(*) FROM trade_agent.run_events")) == 2
