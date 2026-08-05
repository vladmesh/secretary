"""The private instance repository as a shared commit target.

Contract: docs/RECOVERY.md, sections "Layout" and "Writer". Five writers commit
into `state/`: the tick writer (`state/board`, `state/runs`), the memory writer
(`state/memory`) and the knowledge writer (`state/knowledge`). The secret store
writes `secrets/` beside them on the same terms. The local-configuration writer
owns `.gitignore` through :func:`ensure_ignored`. They own disjoint pathspecs and
never `git add -A`, so none can pick up another's half-written tree, and
`state_repo_lock` serializes the index operations git itself does not make
concurrency-safe.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from secretary._fsutil import file_lock, write_text_atomic


STATE_LOCK_NAME = "secretary-state-writer.lock"

FALLBACK_IDENTITY = ("secretary checkpoint", "secretary-checkpoint@localhost")

# Pathspec each writer owns. Disjoint by construction; see the module docstring.
BOARD_RUNS_PATHSPEC = ("state/board", "state/runs")
MEMORY_PATHSPEC = ("state/memory",)
KNOWLEDGE_PATHSPEC = ("state/knowledge",)
# The secret store sits beside `state/`, not inside it: the tick writer must never
# pick it up, and the store commits its own catalog and envelopes.
SECRETS_PATHSPEC = ("secrets",)
GITIGNORE_PATHSPEC = (".gitignore",)

MEMORY_FACTS_RELATIVE = Path("state") / "memory" / "facts"
KNOWLEDGE_RELATIVE = Path("state") / "knowledge"
SECRETS_RELATIVE = Path("secrets")


class StateRepoError(RuntimeError):
    """A git command against the instance repo did not run or did not succeed."""


def memory_facts_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / MEMORY_FACTS_RELATIVE


def knowledge_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / KNOWLEDGE_RELATIVE


def secrets_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / SECRETS_RELATIVE


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
        try:
            configured = git(instance_dir, ["config", "--get", key], label="inspect commit identity")
        except StateRepoError:
            configured = ""
        if not configured.strip():
            name, email = FALLBACK_IDENTITY
            return ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    return []


def git(instance_dir: Path, args: list[str], *, label: str, timeout: float = 120) -> str:
    instance_dir = Path(instance_dir).expanduser().resolve()
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={instance_dir}", "-C", str(instance_dir), *args],
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


def ensure_ignored(
    instance_dir: Path, entry: str, *, dry_run: bool = False, _locked: bool = False,
) -> bool:
    """Durably exclude one local file from an instance repository.

    Returns whether the ignore file needs (or received) a change.  The caller
    owns the semantic name of its local file; this module owns all index writes.
    """
    instance_dir = require_repo(instance_dir)
    if _locked:
        return _ensure_ignored_locked(instance_dir, entry, dry_run=dry_run)
    with state_repo_lock(instance_dir):
        return _ensure_ignored_locked(instance_dir, entry, dry_run=dry_run)


def _ensure_ignored_locked(instance_dir: Path, entry: str, *, dry_run: bool) -> bool:
    """Implementation for writers that already hold :func:`state_repo_lock`."""
    ignore = instance_dir / ".gitignore"
    try:
        current = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    except OSError as exc:
        raise StateRepoError(f"read gitignore failed: {exc}") from None
    changed = entry not in current.splitlines()
    if changed and not dry_run:
        suffix = "" if not current or current.endswith("\n") else "\n"
        write_text_atomic(ignore, current + suffix + entry + "\n")
        commit(instance_dir, GITIGNORE_PATHSPEC, f"Ignore local {entry.lstrip('/')}")
    try:
        git(instance_dir, ["check-ignore", "--quiet", "--", entry.lstrip("/")], label="verify exclusion")
    except StateRepoError:
        if dry_run and changed:
            return True
        raise
    return changed


def is_ignored(instance_dir: Path, entry: str) -> bool:
    """Whether Git's canonical matcher excludes one instance-relative entry."""
    try:
        git(instance_dir, ["check-ignore", "--quiet", "--", entry.lstrip("/")], label="verify exclusion")
    except StateRepoError:
        return False
    return True
