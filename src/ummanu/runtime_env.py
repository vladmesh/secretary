"""The one validated reader for host ``runtime.env`` configuration."""

from __future__ import annotations

import stat
from pathlib import Path

from ummanu.infra.export_allowlist import is_exported


class RuntimeEnvError(RuntimeError):
    """The host runtime file is unsafe or does not use the supported syntax."""


class RuntimeEnvMissing(RuntimeEnvError):
    """The optional host runtime file is absent."""


def instance_runtime_env_path(instance_dir: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else instance_dir / "runtime.env"


def read_runtime_env(
    instance_dir: Path,
    override: str | None = None,
    *,
    require_ignored: bool = True,
) -> dict[str, str]:
    """Read the supported ``KEY=VALUE`` dialect, after private-file checks.

    `require_ignored` refuses a file inside the live root at a path the snapshot export allowlist
    matches (`infra.export_allowlist.is_exported`); a file outside the live root is never exported.
    """
    path = instance_runtime_env_path(instance_dir, override)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise RuntimeEnvMissing(
            f"runtime credentials are required: create {path}, chmod 0600, then rerun with --recover"
        ) from None
    except OSError as exc:
        raise RuntimeEnvError(f"runtime.env metadata is unreadable: {path}: {exc}") from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeEnvError("runtime.env must be a regular file, not a symlink")
    if mode & 0o077:
        raise RuntimeEnvError("runtime.env permissions are too broad; run chmod 0600")
    if require_ignored:
        # Excluded means "not exported": the snapshot export allowlist is the one boundary between
        # the live root and what leaves the host, whether or not the live root is a Git work tree.
        try:
            relative = path.resolve().relative_to(instance_dir.resolve())
        except ValueError:
            relative = None
        if relative is not None and is_exported(relative.as_posix()):
            raise RuntimeEnvError(
                f"runtime.env is at {relative.as_posix()}, a live-root path the snapshot export copies; "
                "move it out of the export allowlist"
            )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise RuntimeEnvError("runtime.env is unreadable") from None
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line or line.startswith("export "):
            raise RuntimeEnvError(f"runtime.env line {number} must use KEY=VALUE syntax")
        key, value = line.split("=", 1)
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise RuntimeEnvError(f"runtime.env line {number} has an invalid variable name")
        values[key] = value
    return values
