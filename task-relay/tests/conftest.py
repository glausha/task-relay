import subprocess
import sqlite3

import pytest
from pathlib import Path
from collections.abc import Iterator


@pytest.fixture
def sqlite_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    from task_relay.db.connection import connect
    from task_relay.db.migrations import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True)
    return repo
