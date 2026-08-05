"""The one validated reader for host ``runtime.env`` configuration."""
from __future__ import annotations

import stat
from pathlib import Path

from secretary import state_repo
from triggered_agents.runtime.board_transport import TRANSPORT_ENV


class RuntimeEnvError(RuntimeError):
    """The host runtime file is unsafe or does not use the supported syntax."""


class RuntimeEnvMissing(RuntimeEnvError):
    """The optional host runtime file is absent."""


def instance_runtime_env_path(instance_dir: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else instance_dir / "runtime.env"


def read_runtime_env(
    instance_dir: Path, override: str | None = None, *, require_ignored: bool = True,
) -> dict[str, str]:
    """Read the supported ``KEY=VALUE`` dialect, after private-file checks.

    Transport entries deliberately cannot use whitespace-padded spelling: migration
    removes only exact legacy keys, so accepting another spelling would leave a
    live duplicate behind. Other runtime settings retain the established tolerant
    whitespace behavior.
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
        try:
            relative = path.resolve().relative_to(instance_dir.resolve())
        except ValueError:
            relative = None
        if relative is not None:
            try:
                ignored = state_repo.is_ignored(instance_dir, str(relative))
            except OSError:
                raise RuntimeEnvError("could not verify that runtime.env is gitignored") from None
            if not ignored:
                raise RuntimeEnvError("runtime.env is inside the instance checkout but is not gitignored")
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
        if key in TRANSPORT_ENV and raw != line:
            raise RuntimeEnvError(
                f"runtime.env line {number} has whitespace-padded legacy Kanboard configuration"
            )
        if key in TRANSPORT_ENV and key in values:
            raise RuntimeEnvError(
                f"legacy Kanboard runtime configuration is ambiguous: {key} appears more than once "
                f"(line {number})"
            )
        values[key] = value
    return values
