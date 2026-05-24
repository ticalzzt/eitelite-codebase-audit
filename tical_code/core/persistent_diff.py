"""persistent_diff.py — Git checkpoint management for persistent tasks.

Owns the persistent/<task_id> branch lifecycle.
Every git operation handles stale locks, dirty trees, and conflicts.
"""

import os
import subprocess
import logging

logger = logging.getLogger("tical-code.persistent_diff")


def _run_git(cmd: str, cwd: str, check: bool = True) -> str:
    """Run a git command. Cleans stale .git/index.lock first."""
    lock = os.path.join(cwd, ".git", "index.lock")
    if os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git timeout: {cmd[:60]}")
    if check and r.returncode != 0:
        raise RuntimeError(f"git [{r.returncode}]: {r.stderr.strip()[:200]}")
    return (r.stdout or r.stderr).strip()


class GitCheckpointManager:
    """Manages persistent/<task_id> branch: create, checkpoint, rebase, merge, cleanup."""

    def __init__(self, workspace: str, task_id: int):
        self.workspace = workspace
        self.task_id = task_id
        self.branch = f"persistent/{task_id}"

    def get_head(self) -> str:
        return _run_git("git rev-parse HEAD", self.workspace)

    def create_branch(self):
        """Create persistent/<task_id> from main. Idempotent: if exists, check out."""
        current = _run_git("git branch --show-current", self.workspace, check=False).strip()
        if current != "main":
            _run_git("git checkout main", self.workspace, check=False)
        exists = _run_git(f"git branch --list {self.branch}", self.workspace, check=False).strip()
        if exists:
            _run_git(f"git checkout {self.branch}", self.workspace)
        else:
            _run_git(f"git checkout -b {self.branch}", self.workspace)
            _run_git(f'git commit --allow-empty -m "[persistent] task #{self.task_id}: start"', self.workspace)

    def checkout_branch(self):
        """Switch to persistent branch (used on resume)."""
        _run_git(f"git checkout {self.branch}", self.workspace)

    def commit_all(self, msg: str):
        """git add -A && git commit. No-op on failure (dirty is OK)."""
        _run_git("git add -A", self.workspace, check=False)
        _run_git(f'git commit --allow-empty -m "{msg}"', self.workspace, check=False)

    def autosave(self, task_id: int, subtask_id: int, step: int):
        """Periodic checkpoint: commit_all then return HEAD."""
        self.commit_all(f"[persistent] task #{task_id} sub#{subtask_id} step{step}")
        return self.get_head()

    def rebase_main(self) -> tuple[bool, str]:
        """Fetch + rebase onto origin/main. Returns (ok, error_detail)."""
        _run_git("git fetch origin", self.workspace, check=False)
        try:
            _run_git("git rebase origin/main", self.workspace)
            return True, ""
        except RuntimeError as e:
            conflicts = self._conflicted_files()
            _run_git("git rebase --abort", self.workspace, check=False)
            return False, str(conflicts or e)

    def merge_to_main(self) -> tuple[bool, str]:
        """Merge persistent branch into main with --no-ff. Returns (ok, error)."""
        _run_git("git checkout main", self.workspace)
        try:
            _run_git(f"git merge --no-ff {self.branch}", self.workspace)
            return True, ""
        except RuntimeError as e:
            conflicts = self._conflicted_files()
            _run_git("git merge --abort", self.workspace, check=False)
            return False, str(conflicts or e)

    def cleanup_branch(self):
        """Delete persistent branch. Best-effort only."""
        _run_git("git checkout main", self.workspace, check=False)
        _run_git(f"git branch -D {self.branch}", self.workspace, check=False)

    def reset_to_head(self):
        """Hard reset + clean (used on resume to guarantee clean state)."""
        _run_git("git reset --hard HEAD", self.workspace)
        _run_git("git clean -fd", self.workspace, check=False)

    def _conflicted_files(self) -> list[str]:
        out = _run_git("git diff --name-only --diff-filter=U", self.workspace, check=False)
        return [f for f in out.splitlines() if f.strip()]
