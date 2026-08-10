"""The product's one way to make a workspace fit for an interactive Codex head to start in.

Every Codex head is a TUI now, and a TUI asks about directory trust before it will take a prompt.
That question is asked of whoever is sitting in front of the pane, and in this product nobody is:
a head whose root codex has never seen sits on the dialog, never answers Orca's readiness probe,
never receives its prompt, and the tick that created it can only time out. So the answer is
written before the pane exists rather than waited for afterwards.

That makes one ordering the contract for every interactive Codex head, whichever launcher brings it
up: **ensure trust, create the pane, wait for readiness, deliver the prompt, confirm the turn.**
The first step is here, the last three are `tui_delivery`. A preflight that fails must fail here,
with no pane created and nothing for a caller to mistake for a head that ran.

It lives in `runtime` beside `tui_delivery` for the same reason that one does: both callers need it
and only one of them may import the other. The Secretary dispatcher (`secretary`) reads this
package; the triggered-agents tick cannot read `secretary` back. Nothing here knows about boards,
roles or sessions — a caller passes a head profile and the workspace it will run in.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping

CODEX_HOME_DEFAULT = str(Path.home() / ".config" / "orca" / "codex-runtime-home" / "home")
# The file codex itself reads trust from, inside whatever CODEX_HOME the head runs with.
CODEX_CONFIG_FILE = "config.toml"


class CodexPreflightError(RuntimeError):
    """A workspace could not be made fit for an interactive Codex head to start in.

    Raised only before a pane exists, so a caller that sees it knows nothing was launched and no
    head has been asked to do anything.
    """


def codex_home(profile: Mapping[str, Any]) -> str:
    """The CODEX_HOME a head with this profile runs with — and therefore the config it reads trust
    from. The launch command names the same one, so the file written here is the file that head
    will actually consult."""
    return str(profile.get("codex_home") or os.environ.get("TA_CODEX_HOME") or CODEX_HOME_DEFAULT)


def codex_trust_paths(workspace: str) -> list[str]:
    """The paths codex asks about for a head started in `workspace`.

    Both the workspace and its repository root, because codex checks the repository root of the
    directory it starts in when that directory is inside a git repo — a worktree inherits the answer
    given to the repo it was cut from — and the directory itself when it is not, and which of the
    two applies is not knowable from here. Command-line trust overrides and the config write are
    rendered from this one list, so the paths a launch states are the paths a preflight recorded.
    """
    workspace_path = Path(workspace).resolve(strict=False)
    paths = [workspace_path]
    repo_root = _codex_repository_trust_root(workspace_path)
    if repo_root is not None:
        paths.append(repo_root)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def ensure_codex_workspace_trusted(
    profile: Mapping[str, Any],
    workspace: str,
    config: Path | None = None,
) -> None:
    """Answer the codex trust question for one workspace before a head starts in it.

    An interactive codex asks about trust before it takes a prompt, and it asks about the
    repository root of the directory it starts in rather than that directory: a worktree inherits
    the answer given to the repo it was cut from. The `-c projects...trust_level` overrides the
    launch command carries do not reach that check (codex 0.145 still shows the dialog with them
    in place), so a head whose root codex has never seen sits on the dialog, never goes idle, and
    never receives its prompt. The answer lives in `config.toml` of the CODEX_HOME the head runs
    with, which is where codex writes it when a human picks "Yes, continue", so the product writes
    it there instead of leaving the workspace waiting for that human.

    Both the workspace and its repository root are recorded, for the reason `codex_trust_paths`
    gives. Trust already on file is left alone, and a path the file keeps at another trust level is
    somebody's decision, so it fails the bring-up with a readable reason instead of being
    overwritten.
    """
    config_path = config or Path(codex_home(profile)) / CODEX_CONFIG_FILE
    text = _read_codex_config(config_path)
    projects = _codex_config_projects(text, config_path)
    additions: list[str] = []
    for target in codex_trust_paths(workspace):
        entry = projects.get(target)
        if entry is None:
            additions.append(target)
            continue
        if not isinstance(entry, dict):
            raise CodexPreflightError(
                f"codex config {config_path} has a non-table project entry for {target}"
            )
        level = str(entry.get("trust_level") or "")
        if level == "trusted":
            continue
        raise CodexPreflightError(
            f"codex config {config_path} keeps {target} at trust_level {level or '(none)'!r}"
        )
    if not additions:
        return
    body = text if text.endswith("\n") or not text else f"{text}\n"
    for target in additions:
        body += f"\n[projects.{json.dumps(target)}]\ntrust_level = \"trusted\"\n"
    _save_codex_config(config_path, body)


def reject_symlinked_config(config: Path, kind: str) -> None:
    """Refuse to treat anything but a regular file as a head runtime's own config.

    A bring-up rewrites installation state shared by every head on the host, so the one thing it
    must never do is follow a symlink or a device somebody put in that path and replace whatever is
    on the other end. Public because the Claude side of the same bring-up writes its config under
    the same rule; there is one answer here about what a launcher may replace, not one per provider.
    """
    try:
        mode = config.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodexPreflightError(f"cannot inspect {kind} config {config}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise CodexPreflightError(f"refusing symlinked {kind} config {config}")
    if not stat.S_ISREG(mode):
        raise CodexPreflightError(f"{kind} config {config} is not a regular file")


def _read_codex_config(config: Path) -> str:
    reject_symlinked_config(config, "codex")
    try:
        return config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None


def _codex_config_projects(text: str, config: Path) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None
    projects = loaded.get("projects", {})
    if not isinstance(projects, dict):
        raise CodexPreflightError(f"codex config {config} has a non-table projects value")
    return projects


def _save_codex_config(config: Path, text: str) -> None:
    """Replace the codex config with `text`, but only once it parses as the TOML codex will read.

    The new trust tables are appended to the file as it stands rather than re-rendered from a
    parse, because this file is the installation's own: comments, ordering and everything the
    dispatcher has no opinion about survive a bring-up untouched. Appending can only produce
    invalid TOML if the file already declared `projects` in a form a table header cannot extend,
    so the result is parsed back before it replaces anything: a codex config the dispatcher cannot
    write safely leaves the bring-up deferred with a reason, not a codex that no longer starts.
    """
    _codex_config_projects(text, config)
    temp_path: Path | None = None
    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        reject_symlinked_config(config, "codex")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config.name}.", suffix=".tmp", dir=config.parent, text=True
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        reject_symlinked_config(config, "codex")
        os.replace(temp_path, config)
    except OSError as exc:
        raise CodexPreflightError(f"cannot update codex config {config}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _resolve_git_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _workspace_git_dir(workspace_path: Path) -> Path | None:
    dotgit = workspace_path / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve(strict=False)
        if dotgit.is_file():
            first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
        else:
            return None
    except (OSError, IndexError, UnicodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    return _resolve_git_path(first.split(":", 1)[1].strip(), workspace_path)


def _git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    try:
        if common.is_file():
            value = common.read_text(encoding="utf-8").splitlines()[0].strip()
            if value:
                return _resolve_git_path(value, git_dir)
    except (OSError, IndexError, UnicodeError):
        pass
    return git_dir.resolve(strict=False)


def _codex_repository_trust_root(workspace_path: Path) -> Path | None:
    """Codex' TUI trust check keys linked worktrees by the common git dir's repo root."""
    git_dir = _workspace_git_dir(workspace_path)
    if git_dir is None:
        return None
    common_dir = _git_common_dir(git_dir)
    if common_dir.name != ".git":
        return None
    try:
        if git_dir != common_dir and not git_dir.is_relative_to(common_dir / "worktrees"):
            return None
    except ValueError:
        return None
    return common_dir.parent.resolve(strict=False)
