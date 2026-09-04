"""The private instance repository as a shared commit target.

Contract: docs/RECOVERY.md, sections "Layout" and "Writer". Six writers commit
to the private instance repository: the tick writer (`state/board`, `state/runs`),
the memory writer (`state/memory`), the knowledge writer (`state/knowledge`), the
secret store (`secrets/`) and the local-configuration writer (`.gitignore` through
:func:`ensure_ignored`). They own disjoint pathspecs and
never `git add -A`, so none can pick up another's half-written tree, and
`state_repo_lock` serializes the index operations git itself does not make
concurrency-safe.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
# The installed head registry is a recovery-canon pair.  Keep the two files in
# one writer's deliberately narrow ownership: no checkpoint or configuration
# writer may pick either one up by accident.
HEADS_PATHSPEC = ("heads/heads.yaml", "heads/source.yaml")

# Variables with which the caller's environment selects a *different* repository than
# the one named on the command line.  Git honours them ahead of `-C`, so an inherited
# `GIT_DIR` silently redirects an instance write into whatever repository the caller
# happened to be working in.  Every Git child this product starts drops them first.
GIT_SELECTION_VARIABLES = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
)

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
    instance_dir = Path(instance_dir).expanduser().resolve()
    lock_path = _lock_path(instance_dir)
    with file_lock(lock_path):
        _make_repo_user_owned(lock_path, instance_dir)
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


def git_command(instance_dir: Path, args: list[str]) -> list[str]:
    """The only Git invocation shape allowed for an instance repository.

    An instance checkout is runtime-user-owned even when install, recovery or
    an operator's repair is root-initiated.  Git reads repository configuration
    before it executes its subcommand, so crossing to that owner must happen
    before every operation, including a harmless-looking reachability probe.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    return [
        "git",
        "-c",
        f"safe.directory={instance_dir}",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(instance_dir),
        *args,
    ]


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every Git child of this product starts with.

    Two policies, one place.  Noninteractive: no credential prompt and no
    interactive SSH, so an operation is bounded rather than hanging on a
    terminal nobody is watching.  Repository-selecting: `GIT_DIR` and its
    siblings are removed, because Git reads them ahead of the `-C`/path the
    caller asked for, and an inherited one would silently point a state or
    journal write at the caller's own repository.

    This is also the pre-checkout helper: a clone has no instance repository to
    cross into yet, but its child still must not inherit that selection.
    """
    env = dict(os.environ if base is None else base)
    for name in GIT_SELECTION_VARIABLES:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def run_git(
    instance_dir: Path,
    args: list[str],
    *,
    label: str,
    timeout: float = 120,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one instance-repository Git command through its privilege boundary.

    Unlike :func:`git`, this returns non-zero results for callers such as the
    checkpoint pusher that need to distinguish an expected false predicate
    from a command failure.  It still owns command construction, identity
    crossing, hook suppression and noninteractive execution for all callers.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    command = git_command(instance_dir, args)
    env = git_env()
    if extra_env:
        env.update(extra_env)
    # The instance checkout is runtime-user-owned.  Root install/upgrade may need to reconcile
    # it, but Git reads repository configuration before a command (including fsmonitor), so a
    # root Git process would execute runtime-user-controlled configuration.  Cross that boundary
    # once, before Git starts, rather than trying to suppress every executable Git feature.
    # `getuid` deliberately tests the process's real privilege.  Some lifecycle tests (and
    # wrappers) only override effective identity for their own preflight, which must not make an
    # unprivileged process attempt `runuser`.
    if os.getuid() == 0:
        try:
            owner = instance_dir.stat()
            if owner.st_uid != 0:
                # `runuser` rebuilds the calling environment, and what it keeps
                # depends on its PAM configuration. Restate the whole policy as
                # command arguments, so neither a credential prompt nor an
                # inherited repository selection can come back after the owner
                # crossing.
                command = [
                    "runuser",
                    "--user",
                    pwd.getpwuid(owner.st_uid).pw_name,
                    "--",
                    "env",
                    *[argument for name in GIT_SELECTION_VARIABLES for argument in ("--unset", name)],
                    f"GIT_TERMINAL_PROMPT={env['GIT_TERMINAL_PROMPT']}",
                    f"GIT_SSH_COMMAND={env['GIT_SSH_COMMAND']}",
                    # PAM decides which parent environment entries survive
                    # runuser.  `extra_env` is the explicit, controlled seam
                    # for non-secret per-command context such as the managed
                    # credential helper's instance location, so restate it
                    # after the privilege crossing as well.
                    *[f"{name}={value}" for name, value in sorted((extra_env or {}).items())],
                    *command,
                ]
        except (KeyError, OSError) as exc:
            raise StateRepoError(f"{label} failed: could not select instance runtime user: {exc}") from None
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateRepoError(f"{label} failed: {exc}") from None
    return result


def git(instance_dir: Path, args: list[str], *, label: str, timeout: float = 120) -> str:
    """Run a required instance-repository Git command and return its stdout."""
    result = run_git(instance_dir, args, label=label, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise StateRepoError(f"{label} failed: {detail[-1] if detail else 'git error'}")
    return result.stdout


def _make_repo_user_owned(path: Path, instance_dir: Path) -> None:
    """Hand root-created lifecycle files back before the runtime user invokes Git."""
    if os.getuid() != 0:
        return
    try:
        repo_owner = instance_dir.stat()
        if repo_owner.st_uid != 0:
            os.chown(path, repo_owner.st_uid, repo_owner.st_gid)
    except OSError as exc:
        raise StateRepoError(f"prepare instance lifecycle file failed: {exc}") from None


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
    instance_dir: Path,
    entry: str,
    *,
    dry_run: bool = False,
    _locked: bool = False,
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
    if is_tracked(instance_dir, entry):
        raise StateRepoError(
            f"{entry.lstrip('/')} is tracked; remove it from the instance repository before enabling this local file"
        )
    ignore = instance_dir / ".gitignore"
    try:
        current = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    except OSError as exc:
        raise StateRepoError(f"read gitignore failed: {exc}") from None
    changed = entry not in current.splitlines()
    if changed and not dry_run:
        suffix = "" if not current or current.endswith("\n") else "\n"
        try:
            write_text_atomic(ignore, current + suffix + entry + "\n")
        except RuntimeError as exc:
            raise StateRepoError(f"write gitignore failed: {exc}") from None
        _make_repo_user_owned(ignore, instance_dir)
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


def is_tracked(instance_dir: Path, entry: str) -> bool:
    """Whether an entry is already in the instance index."""
    try:
        git(
            instance_dir,
            ["ls-files", "--error-unmatch", "--", entry.lstrip("/")],
            label="inspect tracked entry",
        )
    except StateRepoError:
        return False
    return True
