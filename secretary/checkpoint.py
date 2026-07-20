"""Checkpoint writer for the private instance repository.

Contract: docs/RECOVERY.md, sections "Layout", "Каденция и RPO", "Writer",
"Валидационный гейт". The writer regenerates the normalized board and runs
exports, validates the snapshot, and commits `state/board` and `state/runs`
into the private repo. It runs at the end of a dispatcher tick under
`tick_lock`, so it needs no concurrency model of its own.

Memory (`state/memory`) and push/divergence handling live in separate cards
and are deliberately absent here.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    ensure_dir as _ensure_dir,
    publish_component_entries as _publish_component_entries,
    write_text_atomic as _write_text_atomic,
)
from secretary.data import (
    PIPELINE_STATE_DIR,
    PIPELINE_WORKTREE,
    export_board,
    export_runs,
)
from secretary.tasks import TaskAudit

from triggered_agents.runtime.redact import redact


# Canonical checkpoint entries per component. `events.ndjson` is optional: an
# instance that has never appended a task event has no audit log yet.
BOARD_ENTRIES = ("cards.ndjson", "events.ndjson", "export.json")
BOARD_REQUIRED = ("cards.ndjson", "export.json")
RUNS_ENTRIES = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")
RUNS_REQUIRED = RUNS_ENTRIES

# Derived neighbours of the canon. They are never copied into `state/`; the
# ignore files keep them out if anything else drops them there.
BOARD_IGNORE = ("cards.json", "kanboard-raw-*/", "pending-audit/", ".audit.lock")
RUNS_IGNORE = ("cards.json",)

STAGED_PATHSPEC = ("state/board", "state/runs")

FALLBACK_IDENTITY = ("secretary checkpoint", "secretary-checkpoint@localhost")


class CheckpointBlocked(Exception):
    """The snapshot did not pass the gate; nothing is committed this tick."""


@dataclass(frozen=True)
class CheckpointResult:
    status: str
    reason: str = ""
    commit: str = ""
    board_cards: int = 0
    run_records: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "commit": self.commit,
            "board_cards": self.board_cards,
            "run_records": self.run_records,
        }


class CheckpointWriter:
    """Regenerate, validate and commit the normalized `state/` snapshot."""

    def __init__(
        self,
        data_dir: Path,
        instance_dir: Path,
        *,
        pipeline_worktree: Path = PIPELINE_WORKTREE,
        state_dir: Path = PIPELINE_STATE_DIR,
        command: list[str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.pipeline_worktree = Path(pipeline_worktree)
        self.state_dir = Path(state_dir)
        self.command = command

    def write(self) -> CheckpointResult:
        try:
            return self._write()
        except CheckpointBlocked as exc:
            return CheckpointResult(status="blocked", reason=str(exc))

    def _write(self) -> CheckpointResult:
        audit = TaskAudit(self.data_dir).status()
        if not audit["ok"]:
            raise CheckpointBlocked(
                f"task audit has {audit['pending']} unresolved pending record(s)"
            )

        board, runs = self._regenerate()
        self._publish("board", BOARD_ENTRIES, BOARD_REQUIRED, BOARD_IGNORE, _validate_board)
        self._publish("runs", RUNS_ENTRIES, RUNS_REQUIRED, RUNS_IGNORE, _validate_runs)
        return self._commit(board_cards=board, run_records=runs)

    def _regenerate(self) -> tuple[int, int]:
        """Rebuild the exports from the live board and pipeline state."""
        try:
            board = export_board(
                self.data_dir,
                pipeline_worktree=self.pipeline_worktree,
                command=self.command,
            )
            runs = export_runs(self.data_dir, state_dir=self.state_dir)
        except RuntimeError as exc:
            raise CheckpointBlocked(str(exc)) from None
        return board.count, runs.count

    def _publish(
        self,
        component: str,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        ignore: tuple[str, ...],
        validate: Callable[[Path], None],
    ) -> None:
        source = self.data_dir / component
        destination = self.instance_dir / "state" / component
        _ensure_dir(destination, f"checkpoint {component} dir")
        try:
            staging = Path(
                tempfile.mkdtemp(prefix=f".{component}-checkpoint-", suffix=".tmp", dir=destination)
            )
        except OSError as exc:
            raise CheckpointBlocked(f"could not stage checkpoint {component}: {exc}") from None

        try:
            staged = self._stage(source, staging, entries, required, component)
            validate(staging)
            _scan_for_secrets(staging, staged, component)
            _publish_component_entries(staging, destination, list(staged), f"checkpoint {component}")
        except RuntimeError as exc:
            _cleanup_staging_dir(staging)
            raise CheckpointBlocked(str(exc)) from None
        except CheckpointBlocked:
            _cleanup_staging_dir(staging)
            raise

        _write_text_atomic(destination / ".gitignore", "".join(f"{line}\n" for line in ignore))

    def _stage(
        self,
        source: Path,
        staging: Path,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        component: str,
    ) -> tuple[str, ...]:
        staged: list[str] = []
        for entry in entries:
            origin = source / entry
            if not origin.exists():
                if entry in required:
                    raise CheckpointBlocked(f"checkpoint {component} export is missing {entry}")
                continue
            try:
                _write_text_atomic(staging / entry, _read_text(origin, entry))
            except OSError as exc:
                raise CheckpointBlocked(f"could not stage {component}/{entry}: {exc}") from None
            staged.append(entry)
        return tuple(staged)

    def _commit(self, *, board_cards: int, run_records: int) -> CheckpointResult:
        pathspec = ["--", *STAGED_PATHSPEC]
        try:
            self._git(["add", *pathspec], "checkpoint stage")
        except CheckpointBlocked:
            # A repo that ignores `state/` fails the add with a git hint; say why.
            self._require_tracked()
            raise
        self._require_tracked()
        status = self._git(["status", "--porcelain", *pathspec], "checkpoint status")
        if not status.stdout.strip():
            return CheckpointResult(
                status="unchanged",
                board_cards=board_cards,
                run_records=run_records,
            )

        message = f"checkpoint(state): {board_cards} card(s), {run_records} run record(s)"
        self._git(
            [*self._identity(), "commit", "--quiet", "--message", message, *pathspec],
            "checkpoint commit",
        )
        head = self._git(["rev-parse", "HEAD"], "checkpoint head")
        return CheckpointResult(
            status="committed",
            commit=head.stdout.strip(),
            board_cards=board_cards,
            run_records=run_records,
        )

    def _require_tracked(self) -> None:
        """An ignored `state/` stages nothing, which otherwise reads as unchanged."""
        canon = ["state/board/cards.ndjson", "state/runs/runs.ndjson"]
        tracked = self._git(["ls-files", "--", *canon], "checkpoint tracked").stdout.split()
        missing = [path for path in canon if path not in tracked]
        if missing:
            raise CheckpointBlocked(
                f"checkpoint is not tracked by the instance repo: {', '.join(missing)}"
            )

    def _identity(self) -> list[str]:
        """Fall back to a writer identity only when the repo declares none."""
        for key in ("user.name", "user.email"):
            configured = subprocess.run(
                ["git", "-C", str(self.instance_dir), "config", "--get", key],
                text=True,
                capture_output=True,
                check=False,
            )
            if configured.returncode != 0 or not configured.stdout.strip():
                name, email = FALLBACK_IDENTITY
                return ["-c", f"user.name={name}", "-c", f"user.email={email}"]
        return []

    def _git(self, args: list[str], label: str) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.instance_dir), *args],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckpointBlocked(f"{label} failed: {exc}") from None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise CheckpointBlocked(f"{label} failed: {detail[-1] if detail else 'git error'}")
        return result


def _scan_for_secrets(staging: Path, staged: tuple[str, ...], component: str) -> None:
    """`state/` is what leaves the host, so a pasted token stops the commit here."""
    for entry in staged:
        text = _read_text(staging / entry, entry)
        if redact(text) != text:
            raise CheckpointBlocked(f"secret detected in state/{component}/{entry}")


def _validate_board(staging: Path) -> None:
    summary = _read_json(staging / "export.json", "board export.json")
    declared = _int_field(summary, "card_count", "board export.json")
    actual = _count_lines(staging / "cards.ndjson", "board cards.ndjson")
    if declared != actual:
        raise CheckpointBlocked(
            f"board export count mismatch: export.json={declared} cards.ndjson={actual}"
        )


def _validate_runs(staging: Path) -> None:
    summary = _read_json(staging / "export.json", "runs export.json")
    declared = _int_field(summary, "run_record_count", "runs export.json")
    actual = _count_lines(staging / "runs.ndjson", "runs runs.ndjson")
    if declared != actual:
        raise CheckpointBlocked(
            f"runs export count mismatch: export.json={declared} runs.ndjson={actual}"
        )

    watermarks = _read_json(staging / "watermarks.json", "runs watermarks.json")
    files = watermarks.get("files")
    if not isinstance(files, list):
        raise CheckpointBlocked("runs watermarks.json has no file list")
    declared = _int_field(summary, "watermark_count", "runs export.json")
    if declared != len(files):
        raise CheckpointBlocked(
            f"runs watermark count mismatch: export.json={declared} watermarks.json={len(files)}"
        )

    claims = _read_json(staging / "claims.json", "runs claims.json")
    entries = claims.get("claims")
    if not isinstance(entries, dict):
        raise CheckpointBlocked("runs claims.json has no claim mapping")
    declared = _int_field(summary, "claim_count", "runs export.json")
    if declared != len(entries):
        raise CheckpointBlocked(
            f"runs claim count mismatch: export.json={declared} claims.json={len(entries)}"
        )


def _count_lines(path: Path, label: str) -> int:
    return sum(1 for line in _read_text(path, label).splitlines() if line.strip())


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_text(path, label))
    except ValueError as exc:
        raise CheckpointBlocked(f"could not parse {label}: {exc}") from None
    if not isinstance(payload, dict):
        raise CheckpointBlocked(f"{label} must be an object")
    return payload


def _int_field(payload: dict[str, Any], key: str, label: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckpointBlocked(f"{label} has no integer {key}")
    return value


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckpointBlocked(f"could not read {label}: {exc}") from None
    except UnicodeError as exc:
        raise CheckpointBlocked(f"could not decode {label}: {exc}") from None
