"""persistent_diff.py — Git checkpoint management for persistent tasks.

Git operations isolated here for error handling and lock cleanup.
All commits happen on a dedicated branch per task.
"""

import os
import subprocess
import logging

logger = logging.getLogger("tical-code.persistent_diff")


class GitCheckpointManager:
    """Manages git checkpoints for persistent task execution."""

    def __init__(self, workspace: str):
        self.workspace = workspace

    def _run(self, cmd: str, check: bool = True, timeout: int = 30) -> str:
        """Run a git command. Cleans stale locks first."""
        lock_file = os.path.join(self.workspace, ".git", "index.lock")
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                logger.warning("Removed stale .git/index.lock")
            except OSError:
                pass
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=self.workspace,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Git command timed out: {cmd[:80]}")
        if check and result.returncode != 0:
            raise RuntimeError(f"Git error (exit={result.returncode}): {result.stderr[:200]}")
        return result.stdout.strip()

    def get_head(self) -> str:
        return self._run("git rev-parse HEAD")

    def create_branch(self, branch_name: str, commit_msg: str):
        self._run(f"git checkout -b {branch_name}")
        self._run(f"git commit --allow-empty -m '{commit_msg}'")

    def commit_checkpoint(self, msg: str) -> str:
        self._run("git add -A")
        self._run(f"git commit -m '{msg}' --allow-empty", check=False)
        return self.get_head()

    def checkout(self, branch: str):
        self._run(f"git checkout {branch}")

    def try_rebase(self) -> bool:
        """Rebase onto origin/main. Returns True if clean, False on conflict."""
        try:
            self._run("git fetch origin", timeout=60)
            self._run("git rebase origin/main", timeout=60)
            return True
        except RuntimeError:
            self._run("git rebase --abort", check=False)
            return False

    def try_merge_to_main(self, branch_name: str) -> bool:
        """Merge task branch into main. Returns True if clean."""
        self._run("git checkout main")
        try:
            self._run(f"git merge --no-ff {branch_name}", timeout=60)
            return True
        except RuntimeError:
            self._run("git merge --abort", check=False)
            return False

    def cleanup_branch(self, branch_name: str):
        self._run(f"git branch -D {branch_name}", check=False)
