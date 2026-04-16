"""Git worktree management: detailed-design §5, §7.1."""

from __future__ import annotations

import subprocess
from pathlib import Path


def feature_branch_name(task_id: str) -> str:
    """task-relay/<task_id> の命名規則。"""
    return f"task-relay/{task_id}"


def create_worktree(
    *,
    repo_root: Path,
    workspace_root: Path,
    task_id: str,
    lease_branch: str,
) -> tuple[str, Path]:
    """
    lease_branch から feature_branch を派生し worktree を作成。
    戻り値: (feature_branch, worktree_path)
    """
    feature_branch = feature_branch_name(task_id)
    worktree_path = workspace_root / task_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", feature_branch, str(worktree_path), lease_branch],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return feature_branch, worktree_path


def remove_worktree(
    *,
    repo_root: Path,
    worktree_path: Path,
    feature_branch: str | None,
) -> None:
    """worktree 除去 + feature_branch 削除。"""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if feature_branch:
        subprocess.run(
            ["git", "branch", "-D", feature_branch],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )


def worktree_exists(worktree_path: Path) -> bool:
    return worktree_path.is_dir()


def worktree_is_clean(worktree_path: Path) -> bool:
    """git status --porcelain が空なら clean。"""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == ""
