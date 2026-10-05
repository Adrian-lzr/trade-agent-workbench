"""Focused contract tests for the Run/Event HTTP boundary."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from trade_agent.api import create_app
from trade_agent.db.models import RunRecord
from trade_agent.db.runs import append_run_event, claim_run


def _headers(
    *, org_id: str = "org-demo", actor_id: str = "sales-1", role: str = "sales"
) -> dict[str, str]:
    return {
        "X-Org-Id": org_id,
        "X-Actor-Id": actor_id,
        "X-Role": role,
    }


def _client() -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS trade_agent")
        connection.exec_driver_sql(
            """
            CREATE TABLE trade_agent.rfq_revisions (
                revision_id VARCHAR(128) PRIMARY KEY,
                org_id VARCHAR(128) NOT NULL,
                rfq_id VARCHAR(128) NOT NULL,
                revision_no INTEGER NOT NULL,
                parent_revision_id VARCHAR(128),
                payload JSON NOT NULL,
                created_at DATETIME NOT NULL,
                created_by VARCHAR(128) NOT NULL
            )
            """
        )
        RunRecord.__table__.create(connection)
        connection.exec_driver_sql(
            """
            CREATE TABLE trade_agent.run_events (
                event_id VARCHAR(128) PRIMARY KEY,
                run_id VARCHAR(128) NOT NULL,
                event_seq INTEGER NOT NULL,
                event_key VARCHAR(255) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                node_name VARCHAR(64),
                payload JSON NOT NULL,
                actor_id VARCHAR(128),
                occurred_at DATETIME NOT NULL,
                UNIQUE (run_id, event_seq),
                UNIQUE (run_id, event_key)
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO trade_agent.rfq_revisions
                (revision_id, org_id, rfq_id, revision_no, payload, created_at, created_by)
            VALUES ('rfq-rev-1', 'org-demo', 'rfq-1', 1, '{}',
                    '2026-10-01 00:00:00', 'sales-1')
            """
        )
    factory = sessionmaker(engine, expire_on_commit=False)
    return TestClient(create_app(factory)), factory


def _run_body(run_id: str = "run-api-1", **overrides: str) -> dict[str, str]:
    body = {
        "run_id": run_id,
        "rfq_id": "rfq-1",
        "rfq_revision_id": "rfq-rev-1",
        "graph_version": "trade-v1",
    }
    body.update(overrides)
    return body


def _seed_waiting_run(factory: sessionmaker[Session], run_id: str) -> None:
    with factory() as session, session.begin():
        claim_run(session, run_id, "worker-1")
        waiting = session.get(RunRecord, run_id)
        waiting.status = "waiting_input"
        waiting.wait_reason = "clarification"
        waiting.worker_id = None
        waiting.lease_expires_at = None
        waiting.heartbeat_at = None
        waiting.updated_at = datetime.now(UTC)
        append_run_event(
            session,
            run_id,
            "run.waiting_input",
            event_key=f"wait:{run_id}",
            payload={"missing_fields": ["quantity"]},
            actor_id="worker-1",
        )


def test_create_is_idempotent_and_conflicting_stable_id_returns_error() -> None:
    client, _factory = _client()

    first = client.post("/api/v1/runs", headers=_headers(), json=_run_body())
    assert first.status_code == 201
    assert first.json() == {
        "run_id": "run-api-1",
        "status": "queued",
        "event_seq": 1,
        "idempotent_replay": False,
    }

    replay = client.post("/api/v1/runs", headers=_headers(), json=_run_body())
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    conflict = client.post(
        "/api/v1/runs",
        headers=_headers(),
        json=_run_body(rfq_revision_id="rfq-rev-different"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "conflict"
    assert conflict.json()["request_id"]

    missing_revision = client.post(
        "/api/v1/runs",
        headers=_headers(),
        json=_run_body(run_id="run-api-new", rfq_revision_id="missing"),
    )
    assert missing_revision.status_code == 404
    assert missing_revision.json()["code"] == "rfq_revision_not_found"


def test_identity_and_org_boundaries_use_uniform_errors() -> None:
    client, _factory = _client()

    missing = client.post(
        "/api/v1/runs",
        headers={"X-Request-Id": "4b7d9f30-9a14-4c3a-b27e-1df2c321fa40"},
        json=_run_body(),
    )
    assert missing.status_code == 401
    assert missing.json()["code"] == "identity_required"
    assert missing.json()["request_id"] == "4b7d9f30-9a14-4c3a-b27e-1df2c321fa40"
    assert missing.headers["X-Request-Id"] == missing.json()["request_id"]

    forbidden = client.post(
        "/api/v1/runs", headers=_headers(role="reviewer"), json=_run_body()
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"

    created = client.post("/api/v1/runs", headers=_headers(), json=_run_body())
    assert created.status_code == 201
    cross_org = client.get(
        "/api/v1/runs/run-api-1", headers=_headers(org_id="other-org")
    )
    assert cross_org.status_code == 404
    assert cross_org.json()["code"] == "not_found"
    assert cross_org.json()["request_id"]

    cross_org_create = client.post(
        "/api/v1/runs",
        headers=_headers(org_id="other-org"),
        json=_run_body(),
    )
    assert cross_org_create.status_code == 409
    assert cross_org_create.json()["code"] == "conflict"


def test_events_are_pageable_and_resume_is_waiting_only_and_idempotent() -> None:
    client, factory = _client()
    created = client.post("/api/v1/runs", headers=_headers(), json=_run_body())
    assert created.status_code == 201

    with factory() as session, session.begin():
        claim_run(
            session,
            "run-api-1",
            "worker-1",
        )
        waiting = session.get(RunRecord, "run-api-1")
        waiting.status = "waiting_input"
        waiting.wait_reason = "clarification"
        waiting.worker_id = None
        waiting.lease_expires_at = None
        waiting.heartbeat_at = None
        waiting.updated_at = datetime.now(UTC)
        append_run_event(
            session,
            "run-api-1",
            "run.waiting_input",
            event_key="wait:1",
            payload={"missing_fields": ["quantity"]},
            actor_id="worker-1",
        )
        append_run_event(
            session,
            "run-api-1",
            "node.debug",
            event_key="debug:1",
            payload={"prompt": "secret", "cost": "internal"},
            actor_id="worker-1",
        )

    page_one = client.get(
        "/api/v1/runs/run-api-1/events?page=1&page_size=2",
        headers=_headers(role="reviewer"),
    )
    assert page_one.status_code == 200
    assert page_one.json()["total"] == 4
    assert [item["event_seq"] for item in page_one.json()["events"]] == [1, 2]
    assert "payload" not in page_one.json()["events"][0]

    page_two = client.get(
        "/api/v1/runs/run-api-1/events?page=2&page_size=2",
        headers=_headers(role="reviewer"),
    )
    assert [item["event_seq"] for item in page_two.json()["events"]] == [3, 4]

    resume_headers = {**_headers(), "Idempotency-Key": "resume-1"}
    resumed = client.post("/api/v1/runs/run-api-1/resume", headers=resume_headers)
    assert resumed.status_code == 202
    assert resumed.json()["status"] == "queued"
    replay = client.post("/api/v1/runs/run-api-1/resume", headers=resume_headers)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    not_waiting = client.post(
        "/api/v1/runs/run-api-1/resume",
        headers={**_headers(), "Idempotency-Key": "resume-2"},
    )
    assert not_waiting.status_code == 409
    assert not_waiting.json()["code"] == "invalid_transition"


def test_resume_accepts_body_event_key_and_rejects_missing_key() -> None:
    client, factory = _client()
    created = client.post(
        "/api/v1/runs", headers=_headers(), json=_run_body("run-api-2")
    )
    assert created.status_code == 201
    _seed_waiting_run(factory, "run-api-2")

    missing_key = client.post(
        "/api/v1/runs/run-api-2/resume", headers=_headers(), json={}
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "idempotency_key_required"

    mismatched_keys = client.post(
        "/api/v1/runs/run-api-2/resume",
        headers={**_headers(), "Idempotency-Key": "resume-header-1"},
        json={"event_key": "resume-body-1"},
    )
    assert mismatched_keys.status_code == 409
    assert mismatched_keys.json()["code"] == "idempotency_conflict"

    body_key = client.post(
        "/api/v1/runs/run-api-2/resume",
        headers=_headers(),
        json={"event_key": "resume-body-1"},
    )
    assert body_key.status_code == 202
    replay = client.post(
        "/api/v1/runs/run-api-2/resume",
        headers=_headers(),
        json={"event_key": "resume-body-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True


def test_approval_and_private_pdf_routes_are_published() -> None:
    paths = create_app().openapi()["paths"]
    assert "/api/v1/quotations/{quotation_id}/revisions/{revision_id}/approval" in paths
    assert (
        "/api/v1/quotations/{quotation_id}/revisions/{revision_id}/artifacts" in paths
    )
    assert "/api/v1/artifacts/{artifact_id}/download" in paths
    assert (
        paths["/api/v1/artifacts/{artifact_id}/download"]["get"]["responses"]["200"][
            "content"
        ]["application/pdf"]["schema"]["type"]
        == "string"
    )
