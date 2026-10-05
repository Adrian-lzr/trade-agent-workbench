"""Durable Run worker primitives.

The worker uses the existing PostgreSQL ``runs`` table as its queue.  Claim,
heartbeat, completion, failure, and lease recovery all happen through the
transactional functions in :mod:`trade_agent.db.runs`; no in-memory queue is
used.  ``run_once`` is deliberately explicit so a process supervisor can
choose its polling and shutdown policy.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import os
import time
from typing import TYPE_CHECKING, Literal
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from trade_agent.db.models import RunRecord
from trade_agent.db.runs import (
    InvalidRunTransitionError,
    RunAlreadyClaimedError,
    claim_run,
    complete_run,
    fail_run,
    heartbeat_run,
    requeue_expired_runs,
    set_waiting_input,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from trade_agent.db.runs import RunResult


@dataclass(frozen=True, slots=True)
class RunLease:
    """The fenced identity a worker must present for every mutation."""

    run_id: str
    worker_id: str
    lease_generation: int


@dataclass(frozen=True, slots=True)
class RunDispatch:
    """Explicit handler outcome used by :meth:`RunWorker.dispatch`."""

    status: Literal["succeeded", "waiting_input", "failed"] = "succeeded"
    wait_reason: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status == "waiting_input" and not self.wait_reason:
            raise ValueError("waiting_input dispatch requires wait_reason")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed dispatch requires error_code")


@dataclass(frozen=True, slots=True)
class WorkerCycle:
    """Counts returned by one explicit worker polling cycle."""

    recovered: int
    dispatched: int


RunHandler = Callable[[RunRecord], RunDispatch | None]


class RunWorker:
    """Database-backed worker with fenced leases and explicit dispatch."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.session_factory = session_factory
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds

    def recover(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> list[RunResult]:
        """Requeue expired running leases in one transaction."""
        with self.session_factory() as session, session.begin():
            return requeue_expired_runs(session, now=now, limit=limit)

    def claim(self, run_id: str, *, now: datetime | None = None) -> RunLease:
        """Claim a durable run and return its fenced lease."""
        with self.session_factory() as session, session.begin():
            result = claim_run(
                session,
                run_id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
                now=now,
            )
        if result.lease_generation is None:
            raise RuntimeError("claim did not return a lease generation")
        return RunLease(run_id, self.worker_id, result.lease_generation)

    def heartbeat(
        self,
        lease: RunLease,
        *,
        now: datetime | None = None,
        heartbeat_key: str | None = None,
    ) -> RunResult:
        """Renew a lease; a stale or expired worker is rejected by fencing."""
        with self.session_factory() as session, session.begin():
            return heartbeat_run(
                session,
                lease.run_id,
                lease.worker_id,
                lease_seconds=self.lease_seconds,
                heartbeat_key=heartbeat_key,
                lease_generation=lease.lease_generation,
                now=now,
            )

    def dispatch(
        self,
        run_id: str,
        handler: RunHandler,
        *,
        now: datetime | None = None,
    ) -> RunResult:
        """Claim, invoke a handler, and durably finalize one run.

        The handler executes outside the claim transaction.  A crash in that
        interval leaves a lease that ``recover`` can requeue; repeated work is
        expected to use the business-layer idempotency keys already persisted
        by the trade workflow.
        """
        lease = self.claim(run_id, now=now)
        with self.session_factory() as session:
            record = session.get(RunRecord, run_id)
            if record is None:
                raise RuntimeError(f"run disappeared after claim: {run_id}")
            try:
                outcome = handler(record) or RunDispatch()
            except Exception:  # noqa: BLE001 - handler failures become durable error codes.
                outcome = RunDispatch(
                    status="failed", error_code="worker.dispatch_failed"
                )
        completed_at = now or datetime.now(UTC)
        if outcome.status == "succeeded":
            with self.session_factory() as session, session.begin():
                return complete_run(
                    session,
                    run_id,
                    worker_id=lease.worker_id,
                    event_key=f"worker:{self.worker_id}:succeeded:{lease.lease_generation}",
                    lease_generation=lease.lease_generation,
                    now=completed_at,
                )
        if outcome.status == "waiting_input":
            with self.session_factory() as session, session.begin():
                return set_waiting_input(
                    session,
                    run_id,
                    worker_id=lease.worker_id,
                    wait_reason=outcome.wait_reason or "worker.waiting_input",
                    event_key=f"worker:{self.worker_id}:waiting:{lease.lease_generation}",
                    payload={
                        "wait_reason": outcome.wait_reason or "worker.waiting_input"
                    },
                    lease_generation=lease.lease_generation,
                    now=completed_at,
                )
        with self.session_factory() as session, session.begin():
            return fail_run(
                session,
                run_id,
                worker_id=lease.worker_id,
                error_code=outcome.error_code or "worker.dispatch_failed",
                event_key=f"worker:{self.worker_id}:failed:{lease.lease_generation}",
                lease_generation=lease.lease_generation,
                now=completed_at,
            )

    def dispatch_queued(
        self,
        handler: RunHandler,
        *,
        limit: int = 1,
        now: datetime | None = None,
    ) -> list[RunResult]:
        """Select queued run IDs from the DB and dispatch at most ``limit``."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.session_factory() as session:
            run_ids = list(
                session.scalars(
                    select(RunRecord.run_id)
                    .where(RunRecord.status == "queued")
                    .order_by(RunRecord.updated_at, RunRecord.run_id)
                    .limit(limit)
                )
            )
        results: list[RunResult] = []
        for run_id in run_ids:
            try:
                results.append(self.dispatch(run_id, handler, now=now))
            except (InvalidRunTransitionError, RunAlreadyClaimedError):
                # Another worker may claim or finish a selected row between
                # the read and the fenced claim. Leave it for that worker.
                continue
        return results

    def run_once(
        self,
        handler: RunHandler | None = None,
        *,
        limit: int = 1,
        now: datetime | None = None,
    ) -> WorkerCycle:
        """Recover leases and optionally dispatch queued work once."""
        recovered = self.recover(now=now)
        dispatched = (
            len(self.dispatch_queued(handler, limit=limit, now=now)) if handler else 0
        )
        return WorkerCycle(recovered=len(recovered), dispatched=dispatched)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """Build a worker session factory from explicit or environment config."""
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is required for the worker")
    return sessionmaker(create_engine(url, pool_pre_ping=True), expire_on_commit=False)


def main(argv: list[str] | None = None) -> int:
    """Run a small supervisor-friendly worker process.

    The default server mode only performs lease recovery.  Dispatch requires
    an application handler, which is intentionally injected by the host
    application instead of being replaced with an in-memory demo queue.
    """
    parser = argparse.ArgumentParser(description="Trade Agent durable Run worker")
    parser.add_argument("--once", action="store_true", help="recover once and exit")
    parser.add_argument("--health", action="store_true", help="check DB connectivity")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    factory = create_session_factory()
    if args.health:
        with factory() as session:
            session.execute(select(1))
        return 0
    worker = RunWorker(factory)
    if args.once:
        worker.run_once()
        return 0
    interval = max(args.interval, 0.5)
    while True:
        worker.run_once()
        time.sleep(interval)


__all__ = [
    "RunDispatch",
    "RunLease",
    "RunWorker",
    "WorkerCycle",
    "create_session_factory",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
