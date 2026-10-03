"""The snapshot export allowlist, and the one answer to "is this live-root path exported?".

Contract: docs/RECOVERY.md, "Snapshot repository" and "Writers". The snapshot exporter copies exactly
the live-root paths `SNAPSHOT_ALLOWLIST` names, so a local file that must never leave the host
(`secrets/installation.key`, `runtime.env`, `board-store.env`) is excluded by not matching it.
Every writer that used to ask Git whether such a file was ignored or tracked asks
:func:`is_exported` instead; it reads no file and starts no process.

It lives apart from `checkpoint`, which re-exports the allowlist, so the readers of `runtime.env`
and `board-store.env` can ask without importing the checkpoint writer.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath

# The closed set of live-root paths a cut copies, byte for byte and at the same relative path. `*`
# matches within one path segment, a trailing `**` everything below a directory. Everything else in
# the live root stays out of the snapshot: generated heads files, onboarding and gate drafts, locks,
# `state/board` and `state/runs` (a cut takes those from the export), and above all
# `secrets/installation.key`, `runtime.env` and `board-store.env`.
SNAPSHOT_ALLOWLIST = (
    "instance.yaml",
    "projects/*.yaml",
    "adapters/*.yaml",
    "heads/heads.toml",
    "persona/**",
    "skills/manifest.toml",
    "secrets/catalog.yaml",
    "secrets/installation-key.json",
    "secrets/values/*.enc.json",
    "state/knowledge/**",
    "state/memory/**",
)


def matches(pattern: str, relative: str | PurePosixPath) -> bool:
    """Whether the live-root relative path `relative` is matched by one allowlist pattern."""
    parts = _parts(relative)
    if parts is None:
        return False
    *directories, last = pattern.split("/")
    if last == "**":
        return len(parts) > len(directories) and _segments_match(directories, parts[: len(directories)])
    return _segments_match([*directories, last], parts)


def is_exported(relative: str | PurePosixPath) -> bool:
    """Whether the snapshot export would copy the live-root path `relative`.

    `relative` is relative to the live root (a leading `/` is the root itself). A path that leaves
    the live root (`..`) or names nothing is never exported.
    """
    return any(matches(pattern, relative) for pattern in SNAPSHOT_ALLOWLIST)


def _parts(relative: str | PurePosixPath) -> list[str] | None:
    text = str(relative).replace("\\", "/").lstrip("/")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or ".." in parts:
        return None
    return parts


def _segments_match(patterns: list[str], parts: list[str]) -> bool:
    return len(patterns) == len(parts) and all(
        fnmatchcase(part, segment) for segment, part in zip(patterns, parts, strict=True)
    )
