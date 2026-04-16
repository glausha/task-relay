from __future__ import annotations

import subprocess
from pathlib import Path

from task_relay.runner.worktree import (
    create_worktree,
    feature_branch_name,
    remove_worktree,
    worktree_exists,
    worktree_is_clean,
)


def test_feature_branch_name_uses_task_relay_prefix() -> None:
    assert feature_branch_name("task-123") == "task-relay/task-123"


def test_create_worktree_creates_feature_branch_and_directory(git_repo: Path, tmp_path: Path) -> None:
    feature_branch, worktree_path = create_worktree(
        repo_root=git_repo,
        workspace_root=tmp_path / "workspaces",
        task_id="task-123",
        lease_branch="main",
    )

    branch_result = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", feature_branch],
        check=True,
        capture_output=True,
        text=True,
    )

    assert feature_branch == "task-relay/task-123"
    assert worktree_path == tmp_path / "workspaces" / "task-123"
    assert worktree_exists(worktree_path)
    assert feature_branch in branch_result.stdout


def test_remove_worktree_removes_directory_and_feature_branch(git_repo: Path, tmp_path: Path) -> None:
    feature_branch, worktree_path = create_worktree(
        repo_root=git_repo,
        workspace_root=tmp_path / "workspaces",
        task_id="task-456",
        lease_branch="main",
    )

    remove_worktree(repo_root=git_repo, worktree_path=worktree_path, feature_branch=feature_branch)

    branch_result = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", feature_branch],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not worktree_path.exists()
    assert branch_result.stdout.strip() == ""


def test_worktree_is_clean_tracks_untracked_files(git_repo: Path, tmp_path: Path) -> None:
    _, worktree_path = create_worktree(
        repo_root=git_repo,
        workspace_root=tmp_path / "workspaces",
        task_id="task-clean",
        lease_branch="main",
    )

    assert worktree_is_clean(worktree_path) is True

    (worktree_path / "README.md").write_text("dirty\n")

    assert worktree_is_clean(worktree_path) is False
