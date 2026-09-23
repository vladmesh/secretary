"""A card's workspace on plain `git worktree`, for a card whose heads all run supervised.

The Orca path gives a card an Orca worktree: `orca worktree create` places it, Orca's record of it
is what makes it acceptable, and `orca worktree rm` takes it back. A card whose worker and reviewer
both run on a supervised runtime puts nothing in an Orca pane, so this manager gives it a worktree
Orca never hears of: cut with `git worktree add -b <card branch>` from the card's seed into
`<data_dir>/workspaces/<project id>/<worker>`, accepted by the same resumable-workspace check a
resumed card already passes, and taken back with `git worktree remove --force` and `prune`.

Which manager a card has is decided once, when its workspace is created, and is read back from the
workspace path ever after (`owns`): the dispatcher record carries no new field, and a card whose
profiles move mid-round stays on the manager its checkout was made by.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from secretary.dispatch.helpers import _legacy_worker_branch, _tail
from secretary.dispatch.types import HostError
from secretary.infra import git_worktree

if TYPE_CHECKING:
    from secretary.dispatch.host import CommandHostRuntime

#: The directory under the data dir that git-managed card workspaces live in.
WORKSPACES_DIR = "workspaces"


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

    def owns(self, workspace: str | Path, *, orca_root: Path) -> bool:
        """Whether this workspace path is one this manager made.

        Exactly `<root>/<project>/<worker>`, the one shape it cuts, and not under the Orca root: a
        configuration that points the Orca root at or above the git root leaves every such path to
        Orca, which is what it was before.
        """
        if not workspace:
            return False
        path = _resolved(Path(workspace))
        root = _resolved(self.root)
        return (
            path.is_relative_to(root)
            and len(path.relative_to(root).parts) == 2
            and not path.is_relative_to(_resolved(orca_root))
        )

    def create(self, task: dict[str, Any], worker_id: str, seed: str, *, expected: str = "") -> str:
        """Cut this card's branch from `seed` into its worktree and accept it only as that.

        A worktree git made but the resumable-workspace check refuses is removed, branch included,
        before the refusal is raised, so the next attempt finds neither.
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
            leftover = "" if existed else self._remove(repo, target)
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
        """Remove a worktree that must not be adopted, and say what is left of it.

        `branch` is named only by the create that just cut it, and is deleted with the worktree;
        nothing else ever deletes a branch here.
        """
        try:
            repo = self._repo_of(workspace)
        except (HostError, KeyError, ValueError) as exc:
            return f"; the rejected worktree at {workspace} could not be removed either: {exc}"
        left = self._remove(repo, Path(workspace))
        if branch and not left:
            self._git(["branch", "-D", branch], repo)
        return left

    def teardown(self, workspace: str) -> None:
        """Take a stopped card's worktree back. Best-effort, like the Orca removal it stands for."""
        try:
            repo = self._repo_of(workspace)
        except (HostError, KeyError, ValueError):
            return
        self._remove(repo, Path(workspace))

    def _repo_of(self, workspace: str) -> Path:
        """The project checkout a worktree under `<root>/<project>/<worker>` was cut from."""
        parts = _resolved(Path(workspace)).relative_to(_resolved(self.root)).parts
        if len(parts) != 2:
            raise HostError(f"{workspace} is not a card workspace under {self.root}")
        return Path(str(self._host.catalog.binding(parts[0])["repo"])).expanduser()

    def _remove(self, repo: Path, workspace: Path) -> str:
        try:
            gone = git_worktree.remove(self._git, repo, workspace)
        except HostError as exc:
            return f"; the rejected worktree at {workspace} could not be removed either: {exc}"
        return "" if gone else f"; the rejected worktree at {workspace} could not be removed either"

    def _git(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return self._host.run_capture(["git", "-C", str(cwd), *argv], "git workspace")


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser().absolute()
