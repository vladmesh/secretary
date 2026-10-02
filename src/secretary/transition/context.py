"""Where the transition runs: the runtime home, the host roots, the command runner and the journal.

Every path the transition touches is derived here from four roots (the runtime home, the instance,
`/opt`, the systemd units directory, and the directory holding the `orca` link), so a test lays a
fixture installation out under temporary roots and the live run passes none of them.

The runner is the one door to `git`, `sudo`, `systemctl` and `docker`. A test substitutes a stub
that records the privileged calls and lets `git` through to a fixture repository.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .names import JOURNAL_NAME, LOCK_NAME, NEW, OLD, STATE_DIR_NAME, Names


class TransitionError(RuntimeError):
    """A step refused or failed; the journal keeps every step finished before it."""


@dataclass(frozen=True)
class Layout:
    home: Path
    instance: Path
    opt_root: Path = Path("/opt")
    units_dir: Path = Path("/etc/systemd/system")
    bin_dir: Path = Path("/usr/local/bin")

    def product_root(self, names: Names) -> Path:
        return self.home / names.product_dir

    def data_dir(self, names: Names) -> Path:
        return self.home / names.data_dir

    def tools_dir(self, names: Names) -> Path:
        return self.home / names.tools_dir

    def opt_dir(self, names: Names) -> Path:
        return self.opt_root / names.opt_dir

    def compose_path(self, names: Names) -> Path:
        return self.opt_dir(names) / "postgres-compose.yml"

    def role_workspaces(self, names: Names) -> Path:
        return self.home / "orca" / "workspaces" / names.role_workspaces

    def cli(self, names: Names) -> Path:
        return self.product_root(names) / ".venv" / "bin" / names.package

    def python(self, names: Names) -> Path:
        return self.product_root(names) / ".venv" / "bin" / "python3"

    @property
    def orca_link(self) -> Path:
        return self.bin_dir / "orca"

    @property
    def uv_link(self) -> Path:
        return self.home / ".local" / "bin" / "uv"

    @property
    def journal_path(self) -> Path:
        return self.home / JOURNAL_NAME

    @property
    def lock_path(self) -> Path:
        return self.home / LOCK_NAME

    @property
    def state_dir(self) -> Path:
        return self.home / STATE_DIR_NAME

    @property
    def claude_projects(self) -> Path:
        return self.home / ".claude" / "projects"

    @property
    def claude_json(self) -> Path:
        return self.home / ".claude.json"

    @property
    def codex_config(self) -> Path:
        return self.home / ".codex" / "config.toml"

    def code_root(self) -> Path:
        """The checkout as it stands now: the old path until step 4 moves it."""
        old = self.product_root(OLD)
        return old if old.exists() else self.product_root(NEW)

    def live_data_dir(self) -> Path:
        old = self.data_dir(OLD)
        return old if old.exists() else self.data_dir(NEW)


class Runner:
    """Runs one argument vector; never a shell string."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        timeout: float | None = 900,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransitionError(f"{argv[0]} could not run: {exc}") from None
        if check and result.returncode:
            reason = (result.stderr or result.stdout or "").strip().splitlines()
            raise TransitionError(
                f"`{' '.join(argv)}` exited {result.returncode}: {reason[-1] if reason else 'no output'}"
            )
        return result

    def git(self, root: Path, *args: str, check: bool = True) -> str:
        return self.run(["git", "-C", str(root), *args], check=check).stdout.strip()

    def git_probe(self, root: Path, *args: str) -> str | None:
        result = self.run(["git", "-C", str(root), *args], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def sudo(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run(["sudo", "-n", *argv], check=check)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Journal:
    """`~/ummanu-transition.json`: one record per finished step, plus the facts later steps and the
    rollback read (the pre-transition SHA, the units and their states, the moves, the commits)."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Journal:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            raise TransitionError(f"the journal {path} is unreadable: {exc}") from None
        if not isinstance(data, dict):
            raise TransitionError(f"the journal {path} is not an object")
        data.setdefault("version", 1)
        data.setdefault("steps", {})
        data.setdefault("facts", {})
        return cls(path, data)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def done(self, step: str) -> bool:
        return self.data["steps"].get(step, {}).get("status") == "done"

    def finish(self, step: str, **facts: Any) -> None:
        self.data["steps"][step] = {"status": "done", "finished_at": now(), **facts}
        self.save()

    def fact(self, key: str, default: Any = None) -> Any:
        return self.data["facts"].get(key, default)

    def record(self, key: str, value: Any) -> None:
        """Keep a fact before acting on it, so an interrupted step still leaves what rollback needs."""
        self.data["facts"][key] = value
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


@contextlib.contextmanager
def apply_lock(path: Path) -> Iterator[None]:
    """One `--apply` or `--rollback` at a time. `--plan` never takes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TransitionError(f"another transition run holds {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class Context:
    layout: Layout
    runner: Runner
    journal: Journal
    #: The sprint the observer prepared (`--sprint`), or found by its baseline marker.
    sprint: str = ""
    allow_extra_merges: tuple[str, ...] = ()
    #: The board-store half of steps 1, 3 and 8 (`steps.BoardOps`; a stand-in under unit tests).
    board: Any = None
    #: Steps 5-12 run only from the renamed package; tests of the pre-rename tree switch this off.
    require_renamed: bool = True
    #: Lines describing what was done; printed at the end of a run and kept in the report.
    log: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.log.append(line)
        print(line, flush=True)


__all__ = [
    "Context",
    "Journal",
    "Layout",
    "Runner",
    "TransitionError",
    "apply_lock",
    "now",
]
