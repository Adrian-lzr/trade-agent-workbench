from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from trade_agent.db.models import RunRecord
from trade_agent.db.runs import create_run, get_run
from trade_agent.worker import RunDispatch, RunWorker

STARTED_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS trade_agent")
        RunRecord.__table__.create(connection)
        connection.execute(
            text(
                "CREATE TABLE trade_agent.run_events ("
                "event_id VARCHAR(128) PRIMARY KEY, run_id VARCHAR(128) NOT NULL, "
                "event_seq INTEGER NOT NULL, event_key VARCHAR(255) NOT NULL, "
                "event_type VARCHAR(64) NOT NULL, node_name VARCHAR(64), "
                "payload JSON NOT NULL, actor_id VARCHAR(128), "
                "occurred_at DATETIME NOT NULL, UNIQUE(run_id, event_seq), "
                "UNIQUE(run_id, event_key))"
            )
        )
    return sessionmaker(engine, expire_on_commit=False)


def _seed(factory: sessionmaker[Session], run_id: str = "run-worker-1") -> None:
    with factory() as session, session.begin():
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


def test_worker_claims_heartbeats_dispatches_and_recovers_expired_lease() -> None:
    factory = _factory()
    _seed(factory)
    worker = RunWorker(factory, worker_id="worker-a", lease_seconds=60)
    lease = worker.claim("run-worker-1", now=STARTED_AT)
    heartbeat = worker.heartbeat(
        lease,
        now=STARTED_AT + timedelta(seconds=10),
        heartbeat_key="heartbeat:1",
    )
    assert heartbeat.status == "running"
    with factory() as session:
        assert get_run(session, "run-worker-1").status == "running"

    # A second run proves a process crash can be recovered and re-dispatched.
    _seed(factory, "run-worker-2")
    worker.claim("run-worker-2", now=STARTED_AT)
    recovered = worker.recover(now=STARTED_AT + timedelta(seconds=61))
    assert [item.run_id for item in recovered] == ["run-worker-2"]
    result = worker.dispatch(
        "run-worker-2",
        lambda _record: RunDispatch(),
        now=STARTED_AT + timedelta(seconds=62),
    )
    assert result.status == "succeeded"


def test_worker_handler_can_release_waiting_run_without_background_queue() -> None:
    factory = _factory()
    _seed(factory, "run-worker-wait")
    worker = RunWorker(factory, worker_id="worker-wait")
    result = worker.dispatch(
        "run-worker-wait",
        lambda _record: RunDispatch(
            status="waiting_input", wait_reason="clarification"
        ),
        now=STARTED_AT,
    )
    assert result.status == "waiting_input"
    with factory() as session:
        assert get_run(session, "run-worker-wait").wait_reason == "clarification"
