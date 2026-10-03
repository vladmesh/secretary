"""File rewrites of the transition: the data plane (§T3.6), the instance (§T3.7) and the Claude and
Codex keys (§T4). Each function is idempotent: a file already in its new form is left as it is."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .context import TransitionError
from .names import DROPPED_RUNTIME_KEYS, NEW, OLD, ROLES, Names

Prefixes = list[tuple[str, str]]


def claude_key(path: Path) -> str:
    """The `~/.claude/projects` directory name Claude Code gives a cwd (every non-alphanumeric -> '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def swap_prefix(value: str, prefixes: Prefixes) -> str:
    """`value` with the first matching path prefix replaced; a prefix matches whole components only,
    so `~/secretary` never matches `~/secretary-instance`."""
    for old, new in prefixes:
        if value == old or value.startswith(old + "/"):
            return new + value[len(old):]
    return value


def swap_prefixes(payload: Any, prefixes: Prefixes) -> Any:
    if isinstance(payload, str):
        return swap_prefix(payload, prefixes)
    if isinstance(payload, list):
        return [swap_prefixes(item, prefixes) for item in payload]
    if isinstance(payload, dict):
        return {swap_prefix(str(key), prefixes): swap_prefixes(value, prefixes) for key, value in payload.items()}
    return payload


def backup(path: Path, root: Path, backup_dir: Path) -> None:
    """Copy `path` (relative to `root`) aside once; the first copy is the pre-transition one."""
    if not path.exists():
        return
    target = backup_dir / path.relative_to(root)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, target, symlinks=True)
    else:
        shutil.copy2(path, target)


def write_text(path: Path, text: str) -> bool:
    """Replace the file whole, keeping its mode; False when it already holds `text`."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    temporary = path.with_name(f".{path.name}.transition")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return True


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransitionError(f"cannot read {path}: {exc}") from None


# -- data plane (step 6) -------------------------------------------------------------------------


def data_plane_prefixes(home: Path) -> Prefixes:
    """Longest first: the data dir before the checkout, which is its prefix as a string."""
    return [
        (str(home / OLD.data_dir), str(home / NEW.data_dir)),
        (str(home / OLD.product_dir), str(home / NEW.product_dir)),
        (str(home / "orca" / "workspaces" / OLD.role_workspaces), str(home / "orca" / "workspaces" / NEW.role_workspaces)),
    ]


def rewrite_production_state(path: Path, home: Path) -> bool:
    """Path prefixes and the dispatcher owner. `records` is empty by precondition."""
    if not path.exists():
        return False
    payload = _json(path)
    if not isinstance(payload, dict):
        raise TransitionError(f"{path} is not an object")
    rewritten = swap_prefixes(payload, data_plane_prefixes(home))
    if rewritten.get("owner") == OLD.dispatcher_owner:
        rewritten["owner"] = NEW.dispatcher_owner
    return write_text(path, json.dumps(rewritten, indent=2, sort_keys=True) + "\n")


def rewrite_data_manifest(path: Path, home: Path) -> bool:
    if not path.exists():
        return False
    payload = _json(path)
    if not isinstance(payload, dict):
        raise TransitionError(f"{path} is not an object")
    payload["data_dir"] = swap_prefix(str(payload.get("data_dir", "")), data_plane_prefixes(home))
    return write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def live_trust_paths(home: Path) -> list[tuple[Path, Path]]:
    """The cwds a live head opens in, old and new: the checkout, the PO workspace, the observer root
    and the role worktrees. Per-card workspaces are history and keep their keys."""
    pairs = [
        (home / OLD.product_dir, home / NEW.product_dir),
        (home / OLD.data_dir / "po", home / NEW.data_dir / "po"),
        (home / OLD.data_dir / "dispatcher" / "observer-root" / "observers",
         home / NEW.data_dir / "dispatcher" / "observer-root" / "observers"),
    ]
    workspaces = home / "orca" / "workspaces"
    pairs += [(workspaces / OLD.role_workspaces / role, workspaces / NEW.role_workspaces / role) for role in ROLES]
    return pairs


def rename_codex_projects(path: Path, pairs: Iterable[tuple[Path, Path]]) -> list[str]:
    """Rename the `[projects."<old>"]` tables of live cwds; one whose new table exists is left."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    renamed = []
    for old, new in pairs:
        old_header = f'[projects."{old}"]'
        new_header = f'[projects."{new}"]'
        lines = text.split("\n")
        if old_header in lines and new_header not in lines:
            lines[lines.index(old_header)] = new_header
            text = "\n".join(lines)
            renamed.append(str(old))
    if renamed:
        write_text(path, text)
    return renamed


def drop_stale_mcp_servers(path: Path, old_root: Path) -> list[str]:
    """Remove the `[mcp_servers.<name>]` tables (and their subtables) whose command runs from the old
    checkout; the upgrade's memory-clients step renders them again for the new one."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").split("\n")
    tables: list[tuple[int, int, str]] = []
    starts = [index for index, line in enumerate(lines) if line.startswith("[")]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        tables.append((start, end, lines[start]))
    stale = set()
    for start, end, header in tables:
        match = re.fullmatch(r"\[mcp_servers\.([^.\]]+)\]", header.strip())
        if not match:
            continue
        body = "\n".join(lines[start + 1:end])
        if re.search(r'^command\s*=\s*"' + re.escape(str(old_root)) + "/", body, re.MULTILINE):
            stale.add(match.group(1))
    if not stale:
        return []
    kept: list[str] = []
    for start, end, header in [(0, starts[0] if starts else len(lines), ""), *tables]:
        name = re.match(r"\[mcp_servers\.([^.\]]+)", header.strip())
        if name and name.group(1) in stale:
            continue
        kept.extend(lines[start:end])
    write_text(path, "\n".join(kept))
    return sorted(stale)


def worktree_links(roots: Iterable[Path], prefixes: Prefixes) -> dict[Path, list[Path]]:
    """Linked worktrees under `roots`, grouped by the repository their `.git` file names (as it
    stands after the moves: an old checkout or data-dir prefix is mapped to its new place)."""
    repositories: dict[Path, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for marker in sorted(root.glob("*/.git")) + sorted(root.glob("*/*/.git")):
            if not marker.is_file():
                continue
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not text.startswith("gitdir:"):
                continue
            gitdir = Path(text.split(":", 1)[1].strip())
            if gitdir.parent.name != "worktrees" or gitdir.parent.parent.name != ".git":
                continue
            repository = Path(swap_prefix(str(gitdir.parent.parent.parent), prefixes))
            repositories.setdefault(repository, []).append(marker.parent)
    return repositories


# -- instance (step 7) ---------------------------------------------------------------------------


def _word(name: str) -> re.Pattern[str]:
    """The bare product word, not a longer name built on it (`secretary-instance`, `…_x`)."""
    return re.compile(rf"(?<![-\w./]){re.escape(name)}(?![-\w])")


def edit_instance_yaml(text: str, home: Path) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == f"name: {OLD.instance_name}":
            line = f"name: {NEW.instance_name}"
        elif line.startswith("description: "):
            line = _word(OLD.product_id).sub(NEW.product_id, line)
        elif line == f"data_dir: {home / OLD.data_dir}":
            line = f"data_dir: {home / NEW.data_dir}"
        elif re.fullmatch(rf"\s+unit_prefix: {re.escape(OLD.unit_prefix)}", line):
            line = line.replace(OLD.unit_prefix, NEW.unit_prefix)
        elif re.fullmatch(r"(\s*)foreign_units:\s*", line):
            indent = len(line) - len(line.lstrip())
            items: list[str] = []
            cursor = index + 1
            while cursor < len(lines) and lines[cursor].strip().startswith("- ") and (
                len(lines[cursor]) - len(lines[cursor].lstrip()) > indent
            ):
                items.append(lines[cursor])
                cursor += 1
            # Units outside the new prefix are not the reconciler's to protect any more.
            kept = [item for item in items if not item.strip()[2:].strip().startswith(OLD.unit_prefix)]
            if kept:
                out.append(line)
                out.extend(kept)
            index = cursor
            continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _drop_block(lines: list[str], key: str) -> list[str]:
    out: list[str] = []
    skipping = False
    for line in lines:
        if line == f"{key}:":
            skipping = True
            continue
        if skipping and (line.startswith((" ", "-"))):
            continue
        skipping = False
        out.append(line)
    return out


def edit_project_yaml(text: str, home: Path) -> str:
    replacements = {
        f"id: {OLD.project_id}": f"id: {NEW.project_id}",
        f"repo: {home / OLD.product_dir}": f"repo: {home / NEW.product_dir}",
        f"remote: {OLD.remote}": f"remote: {NEW.remote}",
        f"orca_binding: {OLD.project_id}": f"orca_binding: {NEW.project_id}",
        f"adapter: {OLD.project_id}": f"adapter: {NEW.project_id}",
    }
    lines = [replacements.get(line, line) for line in text.split("\n")]
    # The three curator roots name directories that no longer exist (§10).
    return "\n".join(_drop_block(lines, "curator_roots"))


def edit_adapter_yaml(text: str) -> str:
    return re.sub(
        rf"^(\s*)import_package: {re.escape(OLD.package)}$",
        rf"\1import_package: {NEW.package}",
        text,
        flags=re.MULTILINE,
    )


def edit_tools_paths(text: str) -> str:
    return text.replace(f"/{OLD.tools_dir}", f"/{NEW.tools_dir}")


def edit_heads_toml(text: str) -> str:
    return text.replace(f"-m {OLD.package}.runtime.resource_probe", f"-m {NEW.package}.runtime.resource_probe")


def edit_catalog(text: str, home: Path) -> str:
    text = re.sub(
        rf"^(\s*environment: ){re.escape(OLD.env_prefix)}",
        rf"\g<1>{NEW.env_prefix}",
        text,
        flags=re.MULTILINE,
    )
    return text.replace(f"path: {home / OLD.data_dir}/", f"path: {home / NEW.data_dir}/")


def edit_runtime_env(text: str) -> str:
    out = []
    for line in text.split("\n"):
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else ""
        if key in DROPPED_RUNTIME_KEYS:
            continue
        if key.startswith(OLD.env_prefix):
            line = NEW.env_prefix + line[len(OLD.env_prefix):]
        out.append(line)
    return "\n".join(out)


def rename_env_keys(text: str, old: Names, new: Names) -> str:
    """Plain key renaming, for files whose values do not change."""
    return "\n".join(
        new.env_prefix + line[len(old.env_prefix):] if line.startswith(old.env_prefix) else line
        for line in text.split("\n")
    )


# -- Claude and Codex (step 9) -------------------------------------------------------------------


def claude_move_paths(home: Path, sprint: str) -> list[tuple[Path, Path]]:
    """The cwds whose Claude project directories carry live memory and sessions (§T4), old to new."""
    data_old, data_new = home / OLD.data_dir, home / NEW.data_dir
    pairs = [
        (data_old / "po", data_new / "po"),
        (data_old / "po" / "handoffs", data_new / "po" / "handoffs"),
        (data_old / "dispatcher" / "observer-root" / "observers",
         data_new / "dispatcher" / "observer-root" / "observers"),
        (home / OLD.product_dir, home / NEW.product_dir),
    ]
    if sprint:
        slug = sprint.replace(":", "-")
        pairs.append((data_old / "workspaces" / "observers" / slug, data_new / "workspaces" / "observers" / slug))
    workspaces = home / "orca" / "workspaces"
    pairs += [(workspaces / OLD.role_workspaces / role, workspaces / NEW.role_workspaces / role) for role in ROLES]
    return pairs


def claude_moves(home: Path, sprint: str) -> list[tuple[str, str]]:
    """The Claude project directories that carry live memory and sessions (§T4), old key to new."""
    return [(claude_key(old), claude_key(new)) for old, new in claude_move_paths(home, sprint)]


def move_dir(source: Path, target: Path) -> str:
    """`mv -T source target`, refusing a non-empty target. Returns what happened."""
    if not source.exists():
        return "done" if target.exists() else "absent"
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise TransitionError(f"refusing to move {source} onto the non-empty {target}")
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)
    return "moved"


def copy_claude_trust(path: Path, pairs: Iterable[tuple[Path, Path]]) -> list[str]:
    """Copy the `~/.claude.json` per-project entries of live cwds to their new keys."""
    if not path.exists():
        return []
    payload = _json(path)
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, dict):
        return []
    copied = []
    for old, new in pairs:
        if str(old) in projects and str(new) not in projects:
            projects[str(new)] = json.loads(json.dumps(projects[str(old)]))
            copied.append(str(new))
    if copied:
        write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return copied


def drop_claude_trust(path: Path, keys: Iterable[str]) -> list[str]:
    """Rollback of `copy_claude_trust`: remove exactly the keys it added."""
    if not path.exists():
        return []
    payload = _json(path)
    projects = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(projects, dict):
        return []
    dropped = [key for key in keys if projects.pop(key, None) is not None]
    if dropped:
        write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return dropped


__all__ = [
    "backup",
    "claude_key",
    "claude_move_paths",
    "claude_moves",
    "copy_claude_trust",
    "data_plane_prefixes",
    "drop_claude_trust",
    "drop_stale_mcp_servers",
    "edit_adapter_yaml",
    "edit_catalog",
    "edit_heads_toml",
    "edit_instance_yaml",
    "edit_project_yaml",
    "edit_runtime_env",
    "edit_tools_paths",
    "live_trust_paths",
    "move_dir",
    "rename_codex_projects",
    "rename_env_keys",
    "rewrite_data_manifest",
    "rewrite_production_state",
    "swap_prefix",
    "swap_prefixes",
    "worktree_links",
]
