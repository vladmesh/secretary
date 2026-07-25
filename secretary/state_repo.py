"""The private instance repository as a shared commit target.

Contract: docs/RECOVERY.md, sections "Layout" and "Writer". Three writers commit
into `state/`: the tick writer (`state/board`, `state/runs`), the memory writer
(`state/memory`) and the knowledge writer (`state/knowledge`). They own disjoint
pathspecs and never `git add -A`, so none can pick up another's half-written
tree, and `state_repo_lock` serializes the index operations git itself does not
make concurrency-safe.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from secretary._fsutil import file_lock


STATE_LOCK_NAME = "secretary-state-writer.lock"

FALLBACK_IDENTITY = ("secretary checkpoint", "secretary-checkpoint@localhost")

# Pathspec each writer owns. Disjoint by construction; see the module docstring.
BOARD_RUNS_PATHSPEC = ("state/board", "state/runs")
MEMORY_PATHSPEC = ("state/memory",)
KNOWLEDGE_PATHSPEC = ("state/knowledge",)

MEMORY_FACTS_RELATIVE = Path("state") / "memory" / "facts"
KNOWLEDGE_RELATIVE = Path("state") / "knowledge"


class StateRepoError(RuntimeError):
    """A git command against the instance repo did not run or did not succeed."""


def memory_facts_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / MEMORY_FACTS_RELATIVE


def knowledge_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / KNOWLEDGE_RELATIVE


@contextmanager
def state_repo_lock(instance_dir: Path) -> Iterator[None]:
    """Hold the index of the instance repo for one writer at a time.

    The lock lives in the git dir rather than the worktree so it never shows up
    as an untracked file in the operator's `git status`.
    """
    with file_lock(_lock_path(Path(instance_dir).expanduser().resolve())):
        yield


def _lock_path(instance_dir: Path) -> Path:
    git_dir = instance_dir / ".git"
    if git_dir.is_dir():
        return git_dir / STATE_LOCK_NAME
    # A worktree or a not-yet-initialized repo: keep the lock beside the tree.
    return instance_dir / f".{STATE_LOCK_NAME}"


def commit_identity(instance_dir: Path) -> list[str]:
    """Fall back to a writer identity only when the repo declares none."""
    for key in ("user.name", "user.email"):
        configured = subprocess.run(
            ["git", "-C", str(instance_dir), "config", "--get", key],
            text=True,
            capture_output=True,
            check=False,
        )
        if configured.returncode != 0 or not configured.stdout.strip():
            name, email = FALLBACK_IDENTITY
            return ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    return []


def git(instance_dir: Path, args: list[str], *, label: str, timeout: float = 120) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(instance_dir), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateRepoError(f"{label} failed: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise StateRepoError(f"{label} failed: {detail[-1] if detail else 'git error'}")
    return result.stdout


def require_repo(instance_dir: Path) -> Path:
    instance_dir = Path(instance_dir).expanduser().resolve()
    if not (instance_dir / ".git").exists():
        raise StateRepoError(f"instance repo is not a git repository: {instance_dir}")
    return instance_dir


def head(instance_dir: Path) -> str | None:
    try:
        return git(instance_dir, ["rev-parse", "--verify", "HEAD"], label="inspect head").strip()
    except StateRepoError:
        return None


def status(instance_dir: Path, pathspec: tuple[str, ...]) -> str:
    return git(
        instance_dir,
        ["status", "--porcelain", "--untracked-files=all", "--", *pathspec],
        label="inspect state status",
    ).strip()


def commit(instance_dir: Path, pathspec: tuple[str, ...], message: str) -> str | None:
    """Stage and commit one writer's pathspec. Returns the new HEAD, or None.

    None means the pathspec held nothing to commit; the caller decides whether
    that is normal (an unchanged tick) or a fault (a write that changed nothing).
    """
    git(instance_dir, ["add", "--", *pathspec], label="stage state")
    if not status(instance_dir, pathspec):
        return None
    git(
        instance_dir,
        [*commit_identity(instance_dir), "commit", "--quiet", "--message", message, "--", *pathspec],
        label="commit state",
    )
    return head(instance_dir)
