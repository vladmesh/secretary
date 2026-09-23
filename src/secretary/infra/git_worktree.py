"""Cut and take back a plain `git worktree`, for every workspace this product owns without Orca.

Two callers share it: a product run's detached workspace (`webproto.workspaces`) and a card's
branch workspace (`dispatch.git_workspace`). What differs between them is how a git call is run and
what a refusal is called, so the caller hands both in: `git(argv, cwd)` runs `git <argv>` in `cwd`
and returns the completed process without raising on a non-zero exit. `git` is the only executable
this module names, and it is never run through a shell.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

#: Runs `git <argv>` with `cwd` as its working directory and returns what git answered.
GitRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def add(
    git: GitRunner, repo: Path, target: Path, start: str, *, branch: str = ""
) -> subprocess.CompletedProcess[str]:
    """Cut a worktree of `repo` at `start` into `target`: on a new `branch`, or detached without one.

    A new branch is created with `-b`, never `-B`: a name some other checkout already owns is a
    refusal git reports, not a reference to move out from under it.
    """
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    placement = ["-b", branch] if branch else ["--detach"]
    return git(["worktree", "add", *placement, str(target), start], Path(repo))


def remove(git: GitRunner, repo: Path, target: Path) -> bool:
    """Take a worktree of `repo` back, and say whether its directory is gone.

    `--force` because what is being taken back is the whole checkout, untracked files included; a
    directory git would not remove is removed from disk, and `prune` then drops git's record of it.
    """
    target = Path(target)
    if target.exists():
        git(["worktree", "remove", "--force", str(target)], Path(repo))
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    git(["worktree", "prune"], Path(repo))
    return not target.exists()


def head_of(git: GitRunner, workspace: Path) -> str:
    """The commit `workspace` is at, or "" when git will not say."""
    result = git(["rev-parse", "HEAD"], Path(workspace))
    return (result.stdout or "").strip() if result.returncode == 0 else ""
