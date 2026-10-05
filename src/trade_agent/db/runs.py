"""Transactional run lifecycle, worker leases, and idempotent run events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import select

from trade_agent.db.models import RunEventRecord, RunRecord

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

RUN_STATUSES = frozenset({
    "queued",
    "running",
    "waiting_input",
    "succeeded",
    "failed",
    "cancelled",
})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_MAX_IDENTIFIER_LENGTH = 255


class RunLifecycleError(RuntimeError):
    """Base error for an invalid or conflicting run operation."""


class RunNotFoundError(RunLifecycleError):
    """The requested run does not exist."""


class RunConflictError(RunLifecycleError):
    """A stable run ID was reused for a different target."""


class RunAlreadyClaimedError(RunLifecycleError):
    """Another live worker owns the run lease."""


class InvalidRunTransitionError(RunLifecycleError):
    """The requested lifecycle transition is not allowed."""


class RunLeaseLostError(InvalidRunTransitionError):
    """A worker tried to mutate a run after losing its fenced lease."""


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: str
    event_seq: int
    idempotent_replay: bool = False
    lease_generation: int | None = None


@dataclass(frozen=True, slots=True)
class RunEventResult:
    event_id: str
    run_id: str
    event_seq: int
    event_type: str
    idempotent_replay: bool = False


def create_run(  # noqa: PLR0913 - explicit command fields form the persistence contract
    session: Session,
    *,
    run_id: str,
    org_id: str,
    rfq_id: str,
    rfq_revision_id: str,
    graph_version: str,
    thread_id: str,
    created_by: str,
    event_key: str = "run.created",
    now: datetime | None = None,
) -> RunResult:
    """Create a queued run exactly once and record its start event."""
    _validate_identifier(run_id, "run_id")
    _validate_identifier(org_id, "org_id")
    _validate_identifier(rfq_id, "rfq_id")
    _validate_identifier(rfq_revision_id, "rfq_revision_id")
    _validate_identifier(graph_version, "graph_version")
    _validate_identifier(thread_id, "thread_id")
    _validate_identifier(created_by, "created_by")
    _validate_identifier(event_key, "event_key")
    moment = _utc(now)
    session = _session(session)
    existing = session.scalar(
        select(RunRecord).where(RunRecord.run_id == run_id).with_for_update()
    )
    if existing is not None:
        if (
            existing.org_id,
            existing.rfq_id,
            existing.rfq_revision_id,
            existing.graph_version,
            existing.thread_id,
        ) != (org_id, rfq_id, rfq_revision_id, graph_version, thread_id):
            raise RunConflictError("run_id already targets a different workflow")
        return RunResult(
            run_id,
            existing.status,
            existing.event_seq,
            idempotent_replay=True,
            lease_generation=existing.lease_generation,
        )

    record = RunRecord(
        run_id=run_id,
        org_id=org_id,
        rfq_id=rfq_id,
        rfq_revision_id=rfq_revision_id,
        graph_version=graph_version,
        thread_id=thread_id,
        status="queued",
        lease_generation=0,
        event_seq=0,
        created_by=created_by,
        created_at=moment,
        updated_at=moment,
    )
    session.add(record)
    session.flush()
    event = append_run_event(
        session,
        run_id,
        "run.created",
        event_key=event_key,
        actor_id=created_by,
        payload={
            "rfq_id": rfq_id,
            "rfq_revision_id": rfq_revision_id,
            "graph_version": graph_version,
            "thread_id": thread_id,
        },
        now=moment,
    )
    return RunResult(
        run_id, record.status, event.event_seq, lease_generation=record.lease_generation
    )


def claim_run(
    session: Session,
    run_id: str,
    worker_id: str,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> RunResult:
    """Claim a queued or expired run under a row lock."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = _utc(now)
    session = _session(session)
    record = _locked_run(session, run_id)
    if record.status in TERMINAL_STATUSES or record.status == "waiting_input":
        raise InvalidRunTransitionError(f"cannot claim run in {record.status} state")
    if (
        record.status == "running"
        and record.lease_expires_at is not None
        and record.lease_expires_at > moment
    ):
        if record.worker_id == worker_id:
            return RunResult(
                run_id,
                record.status,
                record.event_seq,
                idempotent_replay=True,
                lease_generation=record.lease_generation,
            )
        raise RunAlreadyClaimedError("run has a live worker lease")

    record.lease_generation += 1
    record.status = "running"
    record.worker_id = worker_id
    record.error_code = None
    record.heartbeat_at = moment
    record.lease_expires_at = moment + timedelta(seconds=lease_seconds)
    record.updated_at = moment
    event = append_run_event(
        session,
        run_id,
        "run.claimed",
        event_key=f"claim:{worker_id}:{record.event_seq + 1}",
        actor_id=worker_id,
        payload={
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "lease_generation": record.lease_generation,
        },
        now=moment,
    )
    return RunResult(
        run_id,
        record.status,
        event.event_seq,
        lease_generation=record.lease_generation,
    )


def heartbeat_run(  # noqa: PLR0913 - lease renewal fields are intentionally explicit
    session: Session,
    run_id: str,
    worker_id: str,
    *,
    lease_seconds: int = 60,
    heartbeat_key: str | None = None,
    lease_generation: int,
    now: datetime | None = None,
) -> RunResult:
    """Extend a live lease; a stale worker cannot revive or mutate a run."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    moment = _utc(now)
    session = _session(session)
    record = _locked_run(session, run_id)
    _assert_lease_owner(record, worker_id, lease_generation, moment)
    record.heartbeat_at = moment
    record.lease_expires_at = moment + timedelta(seconds=lease_seconds)
    record.updated_at = moment
    if heartbeat_key is None:
        return RunResult(
            run_id,
            record.status,
            record.event_seq,
            lease_generation=record.lease_generation,
        )
    event = append_run_event(
        session,
        run_id,
        "run.heartbeat",
        event_key=heartbeat_key,
        actor_id=worker_id,
        payload={
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "lease_generation": record.lease_generation,
        },
        worker_id=worker_id,
        lease_generation=lease_generation,
        now=moment,
    )
    return RunResult(
        run_id,
        record.status,
        event.event_seq,
        event.idempotent_replay,
        record.lease_generation,
    )


def set_waiting_input(  # noqa: PLR0913 - transition contract is explicit and audited
    session: Session,
    run_id: str,
    *,
    worker_id: str,
    wait_reason: str,
    event_key: str,
    payload: dict[str, object] | None = None,
    lease_generation: int,
    now: datetime | None = None,
) -> RunResult:
    """Release a worker lease only after a durable human wait is recorded."""
    _validate_identifier(wait_reason, "wait_reason")
    return _transition(
        session,
        run_id,
        expected_status="running",
        worker_id=worker_id,
        lease_generation=lease_generation,
        status="waiting_input",
        wait_reason=wait_reason,
        event_type="run.waiting_input",
        event_key=event_key,
        payload=payload or {"wait_reason": wait_reason},
        now=now,
    )


def resume_run(
    session: Session,
    run_id: str,
    *,
    actor_id: str,
    event_key: str,
    now: datetime | None = None,
) -> RunResult:
    """Move a human-waiting run back to the queue exactly once."""
    return _transition(
        session,
        run_id,
        expected_status="waiting_input",
        worker_id=None,
        lease_generation=None,
        status="queued",
        wait_reason=None,
        event_type="run.resumed",
        event_key=event_key,
        payload={},
        actor_id=actor_id,
        now=now,
    )


def complete_run(  # noqa: PLR0913 - explicit worker completion contract
    session: Session,
    run_id: str,
    *,
    worker_id: str,
    event_key: str,
    lease_generation: int,
    now: datetime | None = None,
) -> RunResult:
    """Mark a run successful and release its worker lease."""
    return _transition(
        session,
        run_id,
        expected_status="running",
        worker_id=worker_id,
        lease_generation=lease_generation,
        status="succeeded",
        wait_reason=None,
        event_type="run.succeeded",
        event_key=event_key,
        payload={},
        now=now,
    )


def fail_run(  # noqa: PLR0913 - failure transition carries its stable error code
    session: Session,
    run_id: str,
    *,
    worker_id: str,
    error_code: str,
    event_key: str,
    lease_generation: int,
    now: datetime | None = None,
) -> RunResult:
    """Mark a run failed with a stable, non-sensitive error code."""
    _validate_identifier(error_code, "error_code")
    return _transition(
        session,
        run_id,
        expected_status="running",
        worker_id=worker_id,
        lease_generation=lease_generation,
        status="failed",
        wait_reason=None,
        event_type="run.failed",
        event_key=event_key,
        payload={"error_code": error_code},
        now=now,
        error_code=error_code,
    )


def cancel_run(
    session: Session,
    run_id: str,
    *,
    actor_id: str,
    event_key: str,
    now: datetime | None = None,
) -> RunResult:
    """Cancel a queued or human-waiting run without touching checkpoints."""
    moment = _utc(now)
    session = _session(session)
    record = _locked_run(session, run_id)
    if record.status not in {"queued", "waiting_input"}:
        raise InvalidRunTransitionError(f"cannot cancel run in {record.status} state")
    record.status = "cancelled"
    record.wait_reason = None
    record.worker_id = None
    record.lease_expires_at = None
    record.heartbeat_at = None
    record.updated_at = moment
    event = append_run_event(
        session,
        run_id,
        "run.cancelled",
        event_key=event_key,
        actor_id=actor_id,
        payload={},
        now=moment,
    )
    return RunResult(run_id, record.status, event.event_seq, event.idempotent_replay)


def requeue_expired_runs(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[RunResult]:
    """Recover abandoned worker leases without resuming human decisions."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    moment = _utc(now)
    session = _session(session)
    records = list(
        session.scalars(
            select(RunRecord)
            .where(
                RunRecord.status == "running",
                RunRecord.lease_expires_at.is_not(None),
                RunRecord.lease_expires_at <= moment,
            )
            .order_by(RunRecord.updated_at, RunRecord.run_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    results: list[RunResult] = []
    for record in records:
        previous_lease = record.lease_expires_at
        record.lease_generation += 1
        record.status = "queued"
        record.worker_id = None
        record.lease_expires_at = None
        record.heartbeat_at = None
        record.wait_reason = None
        record.error_code = "worker.lease_expired"
        record.updated_at = moment
        event = append_run_event(
            session,
            record.run_id,
            "run.requeued",
            event_key=f"lease-expired:{record.run_id}:{previous_lease.isoformat()}",
            actor_id=None,
            payload={"previous_lease_expires_at": previous_lease.isoformat()},
            now=moment,
        )
        results.append(
            RunResult(
                record.run_id,
                record.status,
                event.event_seq,
                lease_generation=record.lease_generation,
            )
        )
    return results


def append_run_event(  # noqa: PLR0913 - event fields are the durable audit contract
    session: Session,
    run_id: str,
    event_type: str,
    *,
    event_key: str,
    payload: dict[str, object],
    node_name: str | None = None,
    actor_id: str | None = None,
    worker_id: str | None = None,
    lease_generation: int | None = None,
    now: datetime | None = None,
) -> RunEventResult:
    """Append one event, returning the existing row for a repeated event key."""
    _validate_identifier(event_type, "event_type")
    _validate_identifier(event_key, "event_key")
    if node_name is not None:
        _validate_identifier(node_name, "node_name")
    if actor_id is not None:
        _validate_identifier(actor_id, "actor_id")
    moment = _utc(now)
    session = _session(session)
    record = _locked_run(session, run_id)
    if worker_id is not None:
        if lease_generation is None:
            raise RunLeaseLostError("lease generation is required for worker events")
        _assert_lease_owner(record, worker_id, lease_generation, moment)
    existing = session.scalar(
        select(RunEventRecord).where(
            RunEventRecord.run_id == run_id,
            RunEventRecord.event_key == event_key,
        )
    )
    if existing is not None:
        if (
            existing.event_type != event_type
            or existing.node_name != node_name
            or existing.actor_id != actor_id
            or existing.payload != payload
        ):
            raise RunConflictError("event_key was reused with a different event")
        return RunEventResult(
            existing.event_id,
            run_id,
            existing.event_seq,
            existing.event_type,
            idempotent_replay=True,
        )
    next_seq = record.event_seq + 1
    event = RunEventRecord(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        event_seq=next_seq,
        event_key=event_key,
        event_type=event_type,
        node_name=node_name,
        payload=payload,
        actor_id=actor_id,
        occurred_at=moment,
    )
    record.event_seq = next_seq
    record.updated_at = moment
    session.add(event)
    session.flush()
    return RunEventResult(event.event_id, run_id, next_seq, event_type)


def get_run(session: Session, run_id: str) -> RunRecord:
    """Read one run or raise a stable not-found error."""
    record = _session(session).scalar(
        select(RunRecord).where(RunRecord.run_id == run_id)
    )
    if record is None:
        raise RunNotFoundError(run_id)
    return record


def list_run_events(session: Session, run_id: str) -> list[RunEventRecord]:
    """Return the append-only event stream in sequence order."""
    return list(
        _session(session).scalars(
            select(RunEventRecord)
            .where(RunEventRecord.run_id == run_id)
            .order_by(RunEventRecord.event_seq)
        )
    )


def _transition(  # noqa: PLR0913 - centralized transition validation
    session: Session,
    run_id: str,
    *,
    expected_status: str,
    worker_id: str | None,
    lease_generation: int | None,
    status: str,
    wait_reason: str | None,
    event_type: str,
    event_key: str,
    payload: dict[str, object],
    actor_id: str | None = None,
    now: datetime | None,
    error_code: str | None = None,
) -> RunResult:
    moment = _utc(now)
    session = _session(session)
    record = _locked_run(session, run_id)
    if record.status != expected_status:
        if record.status == status:
            event = session.scalar(
                select(RunEventRecord).where(
                    RunEventRecord.run_id == run_id,
                    RunEventRecord.event_key == event_key,
                )
            )
            if event is not None:
                return RunResult(
                    run_id,
                    status,
                    event.event_seq,
                    idempotent_replay=True,
                    lease_generation=record.lease_generation,
                )
        raise InvalidRunTransitionError(
            f"expected {expected_status}, found {record.status}"
        )
    if worker_id is not None:
        if lease_generation is None:
            raise RunLeaseLostError(
                "lease generation is required for worker transitions"
            )
        _assert_lease_owner(record, worker_id, lease_generation, moment)
    record.status = status
    record.wait_reason = wait_reason
    record.error_code = error_code
    record.worker_id = None
    record.lease_expires_at = None
    record.heartbeat_at = None
    record.updated_at = moment
    event = append_run_event(
        session,
        run_id,
        event_type,
        event_key=event_key,
        actor_id=actor_id or worker_id,
        payload=payload,
        now=moment,
    )
    return RunResult(
        run_id,
        status,
        event.event_seq,
        event.idempotent_replay,
        record.lease_generation,
    )


def _locked_run(session: Session, run_id: str) -> RunRecord:
    record = _session(session).scalar(
        select(RunRecord).where(RunRecord.run_id == run_id).with_for_update()
    )
    if record is None:
        raise RunNotFoundError(run_id)
    return record


def _assert_lease_owner(
    record: RunRecord,
    worker_id: str,
    lease_generation: int,
    moment: datetime,
) -> None:
    if record.status != "running" or record.worker_id != worker_id:
        raise RunLeaseLostError("worker does not own a running run")
    if record.lease_generation != lease_generation:
        raise RunLeaseLostError("worker lease generation is stale")
    # SQLite test adapters return timezone-aware PostgreSQL timestamps as
    # naive datetimes; normalize the persisted value at the comparison edge.
    persisted_expiry = record.lease_expires_at
    if persisted_expiry is not None and persisted_expiry.tzinfo is None:
        persisted_expiry = persisted_expiry.replace(tzinfo=UTC)
    if persisted_expiry is None or persisted_expiry.astimezone(UTC) <= moment:
        raise RunLeaseLostError("worker lease has expired")


def _session(value: Session) -> Session:
    return value


def _validate_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError(
            f"{name} must be a non-empty string of at most {_MAX_IDENTIFIER_LENGTH} chars"
        )


def _utc(value: datetime | None) -> datetime:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(UTC)
