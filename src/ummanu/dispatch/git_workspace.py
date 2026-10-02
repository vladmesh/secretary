"""Place and validate Git-managed card workspaces.

Path shape selects this manager rather than legacy Orca handling. Destructive
ownership is proved and settled only by dispatch.cleanup, whose durable intent
survives the card, its claim and the active dispatcher record.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ummanu.dispatch.helpers import _legacy_worker_branch, _tail
from ummanu.dispatch.types import HostError
from ummanu.infra import git_worktree

if TYPE_CHECKING:
    from ummanu.dispatch.host import CommandHostRuntime

#: The directory under the data dir that git-managed card workspaces live in.
WORKSPACES_DIR = "workspaces"
#: Names the Orca workspaces root, and only it. Read to recognise a legacy record, route curator
#: discovery and keep role worktrees apart from this root; never to place a card.
ORCA_WORKSPACES_ROOT_ENV = "UMMANU_DISPATCHER_WORKSPACES_ROOT"


def orca_workspaces_root() -> Path:
    """Where Orca's worktrees are namespaced: `UMMANU_DISPATCHER_WORKSPACES_ROOT`, else ~/orca/workspaces."""
    return Path(os.environ.get(ORCA_WORKSPACES_ROOT_ENV, str(Path.home() / "orca" / "workspaces")))


def workspace_roots_overlap(orca_root: Path, data_dir: Path) -> str | None:
    """Why the Orca root and `<data_dir>/workspaces` cannot both be served, or None when disjoint.

    Workspace routing is read from its path shape (`owns`, `_is_git_observer_workspace`),
    so two roots that are equal or nest would make one path both a git workspace and a legacy Orca
    one. The dispatcher refuses to start on that rather than let one reading silently win.
    """
    orca = _resolved(orca_root)
    git = _resolved(Path(data_dir) / WORKSPACES_DIR)
    if orca == git:
        relation = "are the same directory"
    elif git.is_relative_to(orca):
        relation = "overlap: the git workspaces root is inside the Orca workspaces root"
    elif orca.is_relative_to(git):
        relation = "overlap: the Orca workspaces root is inside the git workspaces root"
    else:
        return None
    return (
        f"the Orca workspaces root {orca} and the git workspaces root {git} {relation}; "
        f"point {ORCA_WORKSPACES_ROOT_ENV} or the instance data_dir elsewhere so neither contains the other"
    )


class GitWorkspaceManager:
    """Create, verify, discard and remove one card's git-managed worktree."""

    def __init__(self, host: CommandHostRuntime) -> None:
        self._host = host

    @property
    def root(self) -> Path:
        return Path(self._host.data_dir) / WORKSPACES_DIR

    def path(self, project: str, worker: str) -> Path:
        """Where this card's worktree goes: one directory per project, one per worker under it."""
        for part in (project, worker):
            if not part or part in {".", ".."} or Path(part).name != part:
                raise HostError(f"workspace name {part!r} is not a single path component")
        return self.root / project / worker

    def owns(self, workspace: str | Path) -> bool:
        """Select this manager's placement namespace; this is not deletion authority.

        The exact root/project/worker shape is disjoint from legacy Orca routing.
        Cleanup separately proves registration, attempt, identity and ownership.
        """
        if not workspace:
            return False
        path = _resolved(Path(workspace))
        root = _resolved(self.root)
        return path.is_relative_to(root) and len(path.relative_to(root).parts) == 2

    def create(self, task: dict[str, Any], worker_id: str, seed: str, *, expected: str = "") -> str:
        """Cut this card's branch from `seed` into its worktree and accept it only as that.

        A refused registration or adoption is preserved for explicit inventory;
        failure of the creation contract never authorizes destructive cleanup.
        """
        project = str(task["project"])
        repo = Path(str(self._host.catalog.binding(project)["repo"])).expanduser()
        if not repo.is_absolute() or not repo.is_dir():
            raise HostError(f"project repo for {project!r} is unavailable")
        target = self.path(project, worker_id)
        if expected and _resolved(Path(expected)) != _resolved(target):
            raise HostError(f"the worker workspace belongs at {target}, not {expected}")
        start = self._host._fetch_seed(repo, seed, project=project)
        branch = _legacy_worker_branch(str(task["ref"]))
        existed = target.exists()
        result = git_worktree.add(self._git, repo, target, start, branch=branch)
        if result.returncode != 0:
            detail = _tail((result.stderr or result.stdout or "").strip())
            leftover = "" if existed else f"; any residue at {target} is preserved for owned inventory"
            raise HostError(f"git worktree add failed: {detail}{leftover}")
        try:
            self.verify(task, str(target))
        except HostError as exc:
            raise HostError(
                f"{exc}{self.discard(str(target), branch=branch)}",
                bring_up_cause=exc.bring_up_cause,
            ) from None
        return str(target)

    def verify(self, task: dict[str, Any], workspace: str) -> None:
        """The check a resumed card's workspace passes: this repo's worktree, on this card's branch."""
        self._host._validate_resumable_workspace(task, workspace)

    def discard(self, workspace: str, *, branch: str = "") -> str:
        """A rejected workspace has no admitted destructive ownership proof."""
        return f"; rejected workspace {workspace} and candidate {branch or '(unknown)'} preserved"

    def teardown(self, workspace: str) -> None:
        """Require the durable owner; a path alone is not destructive authority."""
        raise HostError("workspace teardown requires an exact durable cleanup intent")

    def _git(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return self._host.run_capture(["git", "-C", str(cwd), *argv], "git workspace")


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser().absolute()
