"""The product owner's permanent workspace in the installation's data directory.

A PO head, Claude or Codex, runs with this directory as its working directory. Install and upgrade
materialize it: the instructions shipped with the product, a one-line ``CLAUDE.md`` pointing at them,
the ``po_memory`` MCP entry for both CLIs, and a notes file that belongs to the PO, created once and
never rewritten. Skills reach it through the ``@po/`` roots of the skill manifest
(``secretary role-skills sync``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from secretary._fsutil import write_text_atomic
from secretary.memory.client_config import (
    ClientConfigError,
    bridge_executable,
    reconcile_claude,
    reconcile_codex,
)

# The one name of the workspace under the data directory.
WORKSPACE_NAME = "po"
# The instructions source, relative to the product checkout being installed.
AGENTS_SOURCE_RELATIVE = Path("packaging") / "po-workspace" / "AGENTS.md"
AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
NOTES_FILE = "NOTES.md"
CLAUDE_MCP_FILE = ".mcp.json"
CODEX_CONFIG_RELATIVE = Path(".codex") / "config.toml"

CLAUDE_POINTER = "@AGENTS.md\n"
NOTES_SEED = (
    "# PO notes\n\n"
    "Local notes of the product owner on this installation. Install and upgrade create this file\n"
    "once and never rewrite it.\n"
)


class WorkspaceError(RuntimeError):
    """The PO workspace cannot be materialized."""


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    # Workspace-relative names that were (or, on a dry run, would be) written.
    changed: tuple[str, ...]


def workspace_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / WORKSPACE_NAME


def agents_source(product_root: Path) -> Path:
    return product_root / AGENTS_SOURCE_RELATIVE


def _replace_owned(path: Path, text: str, *, dry_run: bool) -> bool:
    """Write a product-owned file unless it already says exactly this."""
    try:
        if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == text:
            return False
        if not dry_run:
            write_text_atomic(path, text)
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise WorkspaceError(f"cannot write {path}: {exc}") from None
    return True


def _create_notes(path: Path, *, dry_run: bool) -> bool:
    """Create the notes file if nothing is there; whatever is there is the PO's."""
    if path.exists() or path.is_symlink():
        return False
    if dry_run:
        return True
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(NOTES_SEED)
    except FileExistsError:
        return False
    except OSError as exc:
        raise WorkspaceError(f"cannot create {path}: {exc}") from None
    return True


def materialize(product_root: Path, data_dir: Path, *, dry_run: bool = False) -> WorkspaceResult:
    """Bring the workspace to the shipped state without touching the PO's notes."""
    workspace = workspace_dir(data_dir)
    source = agents_source(product_root)
    try:
        agents = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceError(f"cannot read the packaged PO instructions {source}: {exc}") from None
    command = bridge_executable(product_root)
    if not dry_run and not command.is_file():
        raise WorkspaceError(f"PO bridge executable is missing: {command}")

    changed: list[str] = []
    if _replace_owned(workspace / AGENTS_FILE, agents, dry_run=dry_run):
        changed.append(AGENTS_FILE)
    if _replace_owned(workspace / CLAUDE_FILE, CLAUDE_POINTER, dry_run=dry_run):
        changed.append(CLAUDE_FILE)
    if _create_notes(workspace / NOTES_FILE, dry_run=dry_run):
        changed.append(NOTES_FILE)
    try:
        if reconcile_claude(workspace / CLAUDE_MCP_FILE, command, data_dir, dry_run=dry_run):
            changed.append(CLAUDE_MCP_FILE)
        if reconcile_codex(workspace / CODEX_CONFIG_RELATIVE, command, data_dir, dry_run=dry_run):
            changed.append(str(CODEX_CONFIG_RELATIVE))
    except ClientConfigError as exc:
        raise WorkspaceError(str(exc)) from None
    return WorkspaceResult(workspace, tuple(changed))
