"""The workspace a product run works in, provisioned and torn down by this product.

The pipeline's workspaces are Orca worktrees: `orca worktree create` makes them, Orca's repo
inventory knows about them, and Orca's teardown removes them. That is precisely the dependency
criterion 2 of secretary-1562 asks the product runtime to shed, and the whole of the replacement is
in this file: `git worktree add --detach` out of the project's own repository, into a directory
under the installation's data plane, and `git worktree remove` to take it back.

Three properties, and each is a decision rather than an accident:

**It is detached, and it creates no branch.** A product run is a run, not an attempt: nothing here
integrates, pushes or opens a pull request (the card puts merge and deploy out of scope), so a
branch would be a name nobody ever uses and a reference the project's repository would keep. The
worktree is cut at the project's declared default branch and left detached there.

**It lives under the data plane, not under the repository.** `<data>/webproto/workspaces/<run_id>`
— so every workspace of every product run is in one place an operator can list, and so a run's
debris is never confused with a pipeline worktree.

**Its git calls go through the product's own process gateway** (`secretary._proc`), never through
a session manager and never through a shell. `git` is the only executable this module names.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from secretary import _proc
from secretary.webproto.errors import RuntimeUnavailable
from secretary.webproto.runs import WORKSPACES_RELATIVE

#: How long a git call this module makes may take. Local worktree operations are fast; a value this
#: high is only here so that a repository under load fails as a timeout rather than hanging a run.
GIT_TIMEOUT_SECONDS = 120.0


def workspace_root(data_dir: str | os.PathLike[str]) -> Path:
    return Path(os.fspath(data_dir)) / WORKSPACES_RELATIVE


def workspace_path(data_dir: str | os.PathLike[str], run_id: str) -> Path:
    return workspace_root(data_dir) / run_id


def provision(repo: str | os.PathLike[str], workspace: Path, *, base: str) -> str:
    """Cut a detached worktree of `repo` at `base` into `workspace`, and say what it is at.

    Idempotent in the one way that matters to a retried start: a workspace directory that already
    holds a git worktree of this repository is accepted as it is rather than replaced, because the
    run that owns it may still be working in it.
    """
    target = Path(workspace)
    if (target / ".git").exists():
        return _head_of(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()):
        raise RuntimeUnavailable(
            f"the workspace path {target} already exists and is not a worktree of {repo}"
        )
    result = _git(["worktree", "add", "--detach", str(target), base], cwd=repo)
    if result.returncode != 0:
        raise RuntimeUnavailable(
            f"a workspace for this run could not be cut from {repo} at {base}: "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )
    return _head_of(target)


def release(repo: str | os.PathLike[str], workspace: Path) -> bool:
    """Take one product workspace back, and say whether it is gone.

    Best-effort by contract, because a workspace that will not go is a fact to report and not a
    reason to leave a run unreadable: the caller records what happened and the operator has both
    paths in the run record either way.
    """
    target = Path(workspace)
    if not target.exists():
        return True
    _git(["worktree", "remove", "--force", str(target)], cwd=repo)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    _git(["worktree", "prune"], cwd=repo)
    return not target.exists()


def _head_of(workspace: Path) -> str:
    result = _git(["rev-parse", "HEAD"], cwd=workspace)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _git(argv: list[str], *, cwd: str | os.PathLike[str]):
    try:
        return _proc.run(["git", *argv], cwd=cwd, timeout=GIT_TIMEOUT_SECONDS)
    except OSError as exc:
        raise RuntimeUnavailable(f"git could not be run for this workspace: {exc}") from None
