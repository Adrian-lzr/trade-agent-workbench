import os
from pathlib import Path
import subprocess  # noqa: S404 - Runs a fixed repository worker with the current interpreter.
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url

from trade_agent.workflow import trade_thread_config

pytestmark = pytest.mark.integration


def test_postgres_checkpoint_resumes_across_process_restart() -> None:
    database_url = os.environ.get("TRADE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TRADE_TEST_DATABASE_URL to a disposable PostgreSQL *_test DB")

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TRADE_TEST_DATABASE_URL must use PostgreSQL")
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("Refusing to use a database unless its name ends in _test")

    run_id = str(uuid4())
    config = trade_thread_config(run_id)
    assert config == trade_thread_config(run_id)
    assert config["configurable"]["thread_id"] == str(UUID(run_id))

    worker_path = Path(__file__).with_name("_checkpoint_worker.py")
    repo_root = Path(__file__).resolve().parents[3]
    for phase in ("pause", "resume"):
        result = subprocess.run(
            [sys.executable, str(worker_path), phase, run_id],
            check=False,
            capture_output=True,
            cwd=repo_root,
            text=True,
        )
        assert result.returncode == 0, result.stderr
