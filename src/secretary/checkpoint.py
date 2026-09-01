"""Checkpoint writer and pusher for the private instance repository.

Contract: docs/RECOVERY.md, sections "Layout", "Cadence and RPO", "Writers", "Validation gate",
"Failure and divergence", "Observability". The writer regenerates the normalized board and runs
exports, validates the snapshot, and commits `state/board` and `state/runs` into the private
repo. It runs at the end of a dispatcher tick under `tick_lock`, and also takes the instance repo
writer lock so checkpoint writes cannot overlap a green-card publish against the same checkout.

Memory (`state/memory`) and knowledge (`state/knowledge`) are written by their own writers
directly into the same repo, so both are deliberately outside this pathspec; `state_repo_lock`
keeps their index operations from overlapping.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary import state_repo
from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
)
from secretary._fsutil import (
    ensure_dir as _ensure_dir,
)
from secretary._fsutil import (
    publish_component_entries as _publish_component_entries,
)
from secretary._fsutil import (
    remove_path as _remove_path,
)
from secretary._fsutil import (
    write_text_atomic as _write_text_atomic,
)
from secretary.board.models import Event
from secretary.data import (
    PIPELINE_STATE_DIR,
    export_board,
    export_runs,
)
from secretary.product_issues import (
    ProductIssueTransaction,
    ProductIssueValidationError,
    registered_projects,
    validate_product_issue_records,
)
from secretary.state_repo import BOARD_RUNS_PATHSPEC
from secretary.tasks import TaskAudit, TaskError
from triggered_agents.runtime.redact import redact

# Canonical checkpoint entries per component. New board cuts always include an
# empty events journal when no event has been written. Older checkpoints remain
# readable without either events.ndjson or the analytics seal.
ANALYTICS_MANIFEST = "analytics-manifest.json"
ANALYTICS_SCHEMA = "secretary.board.analytics-checkpoint"
ANALYTICS_VERSION = 1
ANALYTICS_FILES = ("events.ndjson", "cards.ndjson", "sprints.ndjson", "export.json")
BOARD_ENTRIES = ("cards.ndjson", "sprints.ndjson", "events.ndjson", "export.json", ANALYTICS_MANIFEST)
BOARD_REQUIRED = ("cards.ndjson", "sprints.ndjson", "export.json")
RUNS_ENTRIES = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")
RUNS_REQUIRED = RUNS_ENTRIES

# Derived neighbours of the canon. They are never copied into `state/`; the
# ignore files keep them out if anything else drops them there.
BOARD_IGNORE = ("cards.json", "sprints.json", "kanboard-raw-*/", "pending-audit/", ".audit.lock")
RUNS_IGNORE = ("cards.json",)

STAGED_PATHSPEC = BOARD_RUNS_PATHSPEC

# Commit runs on every tick, push on its own window. 30 minutes is the durable
# RPO the contract promises.
PUSH_INTERVAL_SECONDS = 30 * 60
DEFAULT_REMOTE = "origin"
# The push runs inside the tick, so a stalled remote must not hold the dispatcher.
# A normalized state repo pushes in seconds; past a minute the window is better
# spent moving cards, and the next window retries.
PUSH_TIMEOUT_SECONDS = 60


class CheckpointBlocked(Exception):
    """The snapshot did not pass the gate; nothing is committed this tick."""


class AnalyticsManifestError(ValueError):
    """A directory is not a sealed analytics checkpoint input."""


@dataclass(frozen=True)
class AnalyticsCheckpoint:
    """Verified metadata for one immutable analytics input, never analytics rows."""

    checkpoint_id: str
    directory: Path


def _write_analytics_manifest(directory: Path) -> None:
    """Seal the already validated board files; publication places this file last."""
    entries: list[dict[str, Any]] = []
    for name in ANALYTICS_FILES:
        path = directory / name
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CheckpointBlocked(f"could not read board/{name} for analytics manifest: {exc}") from None
        entry: dict[str, Any] = {
            "path": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
        if name.endswith(".ndjson"):
            try:
                entry["line_count"] = _analytics_line_count(payload, path)
            except AnalyticsManifestError as exc:
                raise CheckpointBlocked(str(exc)) from None
        entries.append(entry)
    payload = {
        "schema": ANALYTICS_SCHEMA,
        "version": ANALYTICS_VERSION,
        "checkpoint_id": _analytics_checkpoint_id(entries),
        "files": entries,
    }
    try:
        _write_text_atomic(
            directory / ANALYTICS_MANIFEST, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
    except RuntimeError as exc:
        raise CheckpointBlocked(f"could not write board analytics manifest: {exc}") from None


def verify_analytics_checkpoint(directory: Path) -> AnalyticsCheckpoint:
    """Verify a sealed board cut using only files beneath ``directory``.

    This deliberately has no board, dispatcher, provider, transcript, comment,
    or runtime dependency. It returns only the sealed-cut identity; a later
    projection must call it before it parses analytics rows.
    """
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        _analytics_failure(root, "analytics checkpoint directory is missing")

    manifest_path = root / ANALYTICS_MANIFEST
    manifest = _read_analytics_json(manifest_path)
    expected_top_level = {"schema", "version", "checkpoint_id", "files"}
    if set(manifest) != expected_top_level:
        _analytics_failure(
            manifest_path, "manifest must contain exactly schema, version, checkpoint_id, files"
        )
    if manifest["schema"] != ANALYTICS_SCHEMA:
        _analytics_failure(manifest_path, f"unknown manifest schema {manifest['schema']!r}")
    if not _is_int(manifest["version"]) or manifest["version"] != ANALYTICS_VERSION:
        _analytics_failure(manifest_path, f"unknown manifest version {manifest['version']!r}")
    checkpoint_id = manifest["checkpoint_id"]
    if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_id):
        _analytics_failure(manifest_path, "checkpoint_id must be a lowercase SHA-256 digest")

    entries = manifest["files"]
    if not isinstance(entries, list):
        _analytics_failure(manifest_path, "files must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for number, entry in enumerate(entries):
        if not isinstance(entry, dict):
            _analytics_failure(manifest_path, f"files[{number}] entry must be an object")
        relative = entry.get("path")
        if not isinstance(relative, str) or relative not in ANALYTICS_FILES:
            _analytics_failure(manifest_path, f"files[{number}].path must name a required analytics file")
        if relative in indexed:
            _analytics_failure(manifest_path, f"files[{number}] duplicate manifest entry for {relative}")
        required_fields = {"path", "sha256", "bytes"}
        if relative.endswith(".ndjson"):
            required_fields.add("line_count")
        if set(entry) != required_fields:
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has invalid fields")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has malformed sha256")
        if not _is_int(entry["bytes"]) or entry["bytes"] < 0:
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has malformed bytes")
        if relative.endswith(".ndjson") and (not _is_int(entry["line_count"]) or entry["line_count"] < 0):
            _analytics_failure(manifest_path, f"files[{number}] entry for {relative} has malformed line_count")
        indexed[relative] = entry

    missing_entries = [name for name in ANALYTICS_FILES if name not in indexed]
    if missing_entries:
        _analytics_failure(manifest_path, f"missing manifest entry for {', '.join(missing_entries)}")
    if len(entries) != len(ANALYTICS_FILES):
        _analytics_failure(manifest_path, "files must list each required analytics file exactly once")
    _verify_analytics_directory_files(root)

    canonical_entries: list[dict[str, Any]] = []
    for name in ANALYTICS_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            _analytics_failure(path, "required analytics file is missing or is not a regular file")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _analytics_failure(path, f"could not read analytics file: {exc}")
        entry = indexed[name]
        actual_digest = hashlib.sha256(payload).hexdigest()
        if entry["sha256"] != actual_digest:
            _analytics_failure(path, "sha256 does not match manifest")
        if entry["bytes"] != len(payload):
            _analytics_failure(path, "byte count does not match manifest")
        canonical = {"path": name, "sha256": actual_digest, "bytes": len(payload)}
        if name.endswith(".ndjson"):
            line_count = _analytics_line_count(payload, path)
            if entry["line_count"] != line_count:
                _analytics_failure(path, "line count does not match manifest")
            canonical["line_count"] = line_count
        canonical_entries.append(canonical)

    expected_id = _analytics_checkpoint_id(canonical_entries)
    if checkpoint_id != expected_id:
        _analytics_failure(manifest_path, "checkpoint_id does not match manifest file entries")
    _verify_analytics_export_summary(root)
    return AnalyticsCheckpoint(checkpoint_id=checkpoint_id, directory=root)


def _analytics_failure(path: Path, detail: str) -> None:
    raise AnalyticsManifestError(f"{path}: {detail}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_analytics_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        _analytics_failure(path, "manifest is missing or is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _analytics_failure(path, f"could not parse manifest: {exc}")
    if not isinstance(payload, dict):
        _analytics_failure(path, "manifest must be an object")
    return payload


def _analytics_line_count(payload: bytes, path: Path | None = None) -> int:
    try:
        return sum(1 for line in payload.decode("utf-8").splitlines() if line.strip())
    except UnicodeDecodeError as exc:
        if path is not None:
            _analytics_failure(path, f"could not decode analytics NDJSON as UTF-8: {exc}")
        raise AnalyticsManifestError(f"analytics NDJSON: could not decode UTF-8: {exc}") from None


def _analytics_checkpoint_id(entries: list[dict[str, Any]]) -> str:
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_analytics_directory_files(root: Path) -> None:
    allowed = {*ANALYTICS_FILES, ANALYTICS_MANIFEST, ".gitignore"}
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        _analytics_failure(root, f"could not list analytics checkpoint directory: {exc}")
    for entry in entries:
        if entry.name not in allowed:
            _analytics_failure(entry, "unlisted file in analytics checkpoint directory")


def _verify_analytics_export_summary(root: Path) -> None:
    export_path = root / "export.json"
    try:
        summary = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _analytics_failure(export_path, f"could not parse export summary: {exc}")
    if not isinstance(summary, dict):
        _analytics_failure(export_path, "export summary must be an object")
    for key, name in (("card_count", "cards.ndjson"), ("sprint_count", "sprints.ndjson")):
        declared = summary.get(key)
        if not _is_int(declared) or declared < 0:
            _analytics_failure(export_path, f"export summary has malformed {key}")
        try:
            actual = _analytics_line_count((root / name).read_bytes(), root / name)
        except OSError as exc:
            _analytics_failure(root / name, f"could not read analytics file: {exc}")
        if declared != actual:
            _analytics_failure(export_path, f"stale {key}: export.json={declared} {name}={actual}")


def _canonical_run_journals(path: Path, label: str) -> dict[str, list[str]]:
    """Compare each source journal by JSON value, never incidental spelling."""
    try:
        journals: dict[str, list[str]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            source = record.get("source") if isinstance(record, dict) else None
            if not isinstance(source, str) or not source:
                raise ValueError("run record has no source")
            journals.setdefault(source, []).append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return journals
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is unreadable: {exc}") from None


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
        state_dir: Path = PIPELINE_STATE_DIR,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.state_dir = Path(state_dir)

    def write(self) -> CheckpointResult:
        try:
            with state_repo.state_repo_lock(self.instance_dir):
                return self._write()
        except CheckpointBlocked as exc:
            return CheckpointResult(status="blocked", reason=str(exc))

    def _write(self) -> CheckpointResult:
        audit = TaskAudit(self.data_dir).status()
        if not audit["ok"]:
            raise CheckpointBlocked(f"task audit has {audit['pending']} unresolved pending record(s)")
        product_issue = ProductIssueTransaction(self.data_dir, TaskAudit(self.data_dir)).status()
        if not product_issue["ok"]:
            raise CheckpointBlocked(
                f"Product/Issue transactions have {product_issue['pending']} unresolved pending record(s)"
            )

        board, runs = self._regenerate()
        self._prevent_run_history_loss()
        from secretary.secret_store import SecretStoreError, redaction_values

        try:
            secret_values = redaction_values(self.instance_dir)
        except SecretStoreError as exc:
            raise CheckpointBlocked(f"could not load checkpoint redaction values: {exc}") from None
        self._publish(
            "board",
            BOARD_ENTRIES,
            BOARD_REQUIRED,
            BOARD_IGNORE,
            lambda staging: _validate_board(staging, instance=self.instance_dir),
            secret_values=secret_values,
        )
        self._publish(
            "runs",
            RUNS_ENTRIES,
            RUNS_REQUIRED,
            RUNS_IGNORE,
            _validate_runs,
            secret_values=secret_values,
        )
        return self._commit(board_cards=board, run_records=runs)

    def _regenerate(self) -> tuple[int, int]:
        """Rebuild the exports from the live board and pipeline runtime state."""
        try:
            board = export_board(
                self.data_dir,
                instance_dir=self.instance_dir,
            )
            runs = export_runs(self.data_dir, state_dir=self.state_dir)
        except RuntimeError as exc:
            raise CheckpointBlocked(str(exc)) from None
        return board.count, runs.count

    def _prevent_run_history_loss(self) -> None:
        """Never truncate or rewrite canonical history from a live export.

        Normal operation appends history, so an empty replacement is an unsafe recovery signal, not
        routine compaction.
        """
        canonical = self.instance_dir / "state" / "runs" / "runs.ndjson"
        live = self.data_dir / "runs" / "runs.ndjson"
        try:
            existing = _canonical_run_journals(canonical, "canonical run history")
        except FileNotFoundError:
            return
        try:
            current = _canonical_run_journals(live, "live run export")
        except (OSError, UnicodeError, ValueError) as exc:
            raise CheckpointBlocked(f"could not inspect canonical run history: {exc}") from None
        for source, canonical_history in existing.items():
            live_history = current.get(source, [])
            if live_history[: len(canonical_history)] != canonical_history:
                raise CheckpointBlocked(
                    "refusing to truncate or rewrite non-empty canonical run history "
                    f"for {source} from the live export"
                )

    def _publish(
        self,
        component: str,
        entries: tuple[str, ...],
        required: tuple[str, ...],
        ignore: tuple[str, ...],
        validate: Callable[[Path], None],
        *,
        secret_values: tuple[str, ...],
    ) -> None:
        source = self.data_dir / component
        destination = self.instance_dir / "state" / component
        _ensure_dir(destination, f"checkpoint {component} dir")
        try:
            staging = Path(
                tempfile.mkdtemp(prefix=f".{component}-checkpoint-", suffix=".tmp", dir=destination.parent)
            )
        except OSError as exc:
            raise CheckpointBlocked(f"could not stage checkpoint {component}: {exc}") from None

        try:
            staged = self._stage(source, staging, entries, required, component)
            validate(staging)
            if component == "board":
                _write_analytics_manifest(staging)
                try:
                    verify_analytics_checkpoint(staging)
                except AnalyticsManifestError as exc:
                    raise CheckpointBlocked(str(exc)) from None
                staged = (*staged, ANALYTICS_MANIFEST)
            _scan_for_secrets(
                staging,
                staged,
                component,
                runtime_env=self.instance_dir / "runtime.env",
                secret_values=secret_values,
            )
            _publish_component_entries(
                staging,
                destination,
                list(staged),
                f"checkpoint {component}",
                publish_last=ANALYTICS_MANIFEST if component == "board" else None,
            )
            _drop_vanished(destination, entries, staged)
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
            if component == "board" and entry == ANALYTICS_MANIFEST:
                continue
            origin = source / entry
            if not origin.exists():
                if component == "board" and entry == "events.ndjson":
                    try:
                        _write_text_atomic(staging / entry, "")
                    except RuntimeError as exc:
                        raise CheckpointBlocked(f"could not stage {component}/{entry}: {exc}") from None
                    staged.append(entry)
                    continue
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
        return self._commit_locked(board_cards=board_cards, run_records=run_records)

    def _commit_locked(self, *, board_cards: int, run_records: int) -> CheckpointResult:
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
            raise CheckpointBlocked(f"checkpoint is not tracked by the instance repo: {', '.join(missing)}")

    def _identity(self) -> list[str]:
        return state_repo.commit_identity(self.instance_dir)

    def _git(self, args: list[str], label: str) -> subprocess.CompletedProcess[str]:
        # The instance repository owns command shape, owner crossing and the Git
        # environment; this writer owns only the semantic failure it reports.
        try:
            result = state_repo.run_git(self.instance_dir, args, label=label)
        except state_repo.StateRepoError as exc:
            raise CheckpointBlocked(str(exc)) from None
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise CheckpointBlocked(f"{label} failed: {detail[-1] if detail else 'git error'}")
        return result


class _GitFailure(Exception):
    """A git command the pusher needs did not run or did not succeed."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output or message


@dataclass(frozen=True)
class PushOutcome:
    status: str
    reason: str = ""
    commit: str = ""


class CheckpointPusher:
    """Send the committed checkpoint to the remote, fast-forward only.

    Fail-closed on the checkpoint, not on the work: a failed push leaves its reason and a growing lag
    in state while the dispatcher keeps running. A remote holding commits the local repo does not
    have stops the push and raises `remote diverged`; there is no force-push path here.
    """

    def __init__(
        self,
        instance_dir: Path,
        *,
        remote: str = DEFAULT_REMOTE,
        interval_seconds: float = PUSH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance_dir = Path(instance_dir).expanduser().resolve()
        self.remote = remote
        self.interval_seconds = float(interval_seconds)
        self._clock = clock

    def push(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the push if its window is due; return the new push state."""
        current = dict(state or {})
        now = float(self._clock())
        if not self._due(current, now):
            return current
        return self._record(current, self._attempt(), now)

    def _due(self, state: dict[str, Any], now: float) -> bool:
        if state.get("remote_diverged") or state.get("status") == "diverged":
            return True
        attempted = _float_field(state, "attempted_epoch")
        if attempted <= 0:
            return True
        # A clock that jumped backwards must not park the push forever.
        return now < attempted or now - attempted >= self.interval_seconds

    def _attempt(self) -> PushOutcome:
        try:
            with state_repo.state_repo_lock(self.instance_dir):
                branch = self._branch()
                if not branch:
                    return PushOutcome("skipped", "instance repo has no checked-out branch")
                if not self._has_remote():
                    return PushOutcome("skipped", f"instance repo has no remote '{self.remote}'")
                head = self._git(["rev-parse", "HEAD"], "checkpoint head").strip()
                remote_head = self._remote_head(branch)
                if remote_head and remote_head == head:
                    return PushOutcome("unchanged", commit=head)
                if remote_head and not self._fast_forward(remote_head, head):
                    return PushOutcome(
                        "diverged",
                        f"remote {self.remote}/{branch} is at {remote_head[:12]}, "
                        "which the checkpoint history does not contain",
                    )
                self._git(
                    ["push", "--quiet", self.remote, f"HEAD:refs/heads/{branch}"],
                    "checkpoint push",
                    timeout=PUSH_TIMEOUT_SECONDS,
                )
                return PushOutcome("pushed", commit=head)
        except _GitFailure as exc:
            # The remote can move between the probe and the push. Git rejects the
            # non-ff itself; read that rejection as the divergence it is.
            if any(mark in exc.output for mark in ("non-fast-forward", "fetch first", "[rejected]")):
                return PushOutcome("diverged", str(exc))
            return PushOutcome("failed", str(exc))

    def _record(self, state: dict[str, Any], outcome: PushOutcome, now: float) -> dict[str, Any]:
        state.update(
            {
                "remote": self.remote,
                "status": outcome.status,
                "reason": outcome.reason,
                "attempted_epoch": now,
                "attempted_at": _rfc3339(now),
            }
        )
        state.setdefault("failures", 0)
        state.setdefault("last_push_at", "")
        state.setdefault("last_push_commit", "")
        if outcome.status in ("pushed", "unchanged"):
            state.update(
                {
                    "last_push_epoch": now,
                    "last_push_at": _rfc3339(now),
                    "last_push_commit": outcome.commit,
                    "failures": 0,
                    "remote_diverged": False,
                }
            )
        elif outcome.status == "skipped":
            state["remote_diverged"] = False
        else:
            state["failures"] = int(_float_field(state, "failures")) + 1
            state["remote_diverged"] = outcome.status == "diverged"
        return state

    def _branch(self) -> str:
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], timeout=120)
        if result.returncode == 0:
            return result.stdout.strip()
        # `symbolic-ref --quiet` uses 1 for the expected detached-HEAD case.
        # Any other result is a Git failure, not evidence that the repository
        # has no branch.  In particular, a root process that cannot read a
        # runtime-user checkout must preserve that actionable cause.
        if result.returncode == 1:
            return ""
        self._raise_git_failure(result, "checkpoint branch discovery")

    def _has_remote(self) -> bool:
        remotes = self._git(["remote"], "checkpoint remote").split()
        return self.remote in remotes

    def _remote_head(self, branch: str) -> str:
        """The remote branch tip, or "" when the remote does not carry it yet."""
        listing = self._git(
            ["ls-remote", "--heads", self.remote, f"refs/heads/{branch}"],
            "checkpoint ls-remote",
            timeout=PUSH_TIMEOUT_SECONDS,
        )
        for line in listing.splitlines():
            sha = line.split("\t")[0].strip()
            if sha:
                return sha
        return ""

    def _fast_forward(self, remote_head: str, head: str) -> bool:
        """True when the remote tip is already in the local history.

        A tip the local repo has never even seen is divergence, not a missing object.
        """
        known = self._run(["cat-file", "-e", f"{remote_head}^{{commit}}"], timeout=120)
        # Git reports an object absent from this clone as either 1 or 128
        # depending on its version.  That is the expected divergence case;
        # another 128 (for example an ownership/configuration refusal) is a
        # failed probe with a cause the operator needs to see.
        if known.returncode == 1 or "not a valid object name" in self._git_output(known).lower():
            return False
        if known.returncode != 0:
            self._raise_git_failure(known, "checkpoint remote reachability")
        ancestor = self._run(["merge-base", "--is-ancestor", remote_head, head], timeout=120)
        if ancestor.returncode == 0:
            return True
        if ancestor.returncode == 1:
            return False
        self._raise_git_failure(ancestor, "checkpoint ancestry check")

    def _git(self, args: list[str], label: str, *, timeout: float = 120) -> str:
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            self._raise_git_failure(result, label)
        return result.stdout

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return state_repo.run_git(
                self.instance_dir,
                args,
                label=f"checkpoint {args[0]}",
                timeout=timeout,
            )
        except state_repo.StateRepoError as exc:
            raise _GitFailure(str(exc)) from None

    @staticmethod
    def _raise_git_failure(result: subprocess.CompletedProcess[str], label: str) -> None:
        output = CheckpointPusher._git_output(result)
        detail = output.splitlines()
        raise _GitFailure(f"{label} failed: {detail[-1] if detail else 'git error'}", output)

    @staticmethod
    def _git_output(result: subprocess.CompletedProcess[str]) -> str:
        return (result.stderr or result.stdout or "").strip()


def checkpoint_snapshot(
    instance_dir: Path,
    *,
    write_state: dict[str, Any] | None = None,
    push_state: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Checkpoint freshness for `status` and `doctor`."""
    write = dict(write_state or {})
    push = dict(push_state or {})
    stamp = time.time() if now is None else float(now)
    commit, commit_at = _last_commit(Path(instance_dir))
    pushed = str(push.get("last_push_commit") or "")
    lag_commits, oldest_at = _unpushed(Path(instance_dir), pushed)
    return {
        "last_commit": commit,
        "last_commit_at": commit_at,
        "last_push_at": str(push.get("last_push_at") or ""),
        "last_push_commit": pushed,
        "lag_commits": lag_commits,
        # The RPO exposure is the age of the oldest change the remote lacks, not
        # the time since the last push: a quiet instance with nothing to push is
        # not behind.
        "lag_minutes": _age_minutes(oldest_at, stamp),
        "push_status": str(push.get("status") or "pending"),
        "push_reason": str(push.get("reason") or ""),
        "push_failures": int(_float_field(push, "failures")),
        "remote_diverged": bool(push.get("remote_diverged")),
        "blocked_reason": str(write.get("reason") or "") if write.get("status") == "blocked" else "",
    }


def render_checkpoint_lines(snapshot: dict[str, Any]) -> list[str]:
    """The freshness block `doctor` prints, indented by its caller."""
    lag_commits = snapshot.get("lag_commits")
    lag_minutes = snapshot.get("lag_minutes")
    lag = "unknown" if lag_commits is None else f"{lag_commits} commit(s)"
    if lag_minutes is not None:
        lag = f"{lag}, {lag_minutes} min"
    lines = [
        f"last commit: {snapshot.get('last_commit') or '(none)'} "
        f"{snapshot.get('last_commit_at') or ''}".strip(),
        f"last push: {snapshot.get('last_push_at') or '(never)'}",
        f"lag: {lag}",
        f"push: {snapshot.get('push_status') or 'pending'}",
    ]
    reason = snapshot.get("push_reason")
    if reason:
        lines.append(f"push reason: {reason}")
    blocked = snapshot.get("blocked_reason")
    if blocked:
        lines.append(f"blocked: {blocked}")
    if snapshot.get("remote_diverged"):
        lines.append("alarm: remote diverged")
    return lines


def _last_commit(instance_dir: Path) -> tuple[str, str]:
    out = _read_git(instance_dir, ["log", "-1", "--format=%H %cI"])
    parts = out.strip().split(" ", 1)
    if not parts or not parts[0]:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def _unpushed(instance_dir: Path, pushed: str) -> tuple[int | None, str]:
    """Count the commits the remote lacks and stamp the oldest of them."""
    # A recorded tip this history no longer holds leaves every commit unpushed,
    # which is the honest reading: nothing local is known to be on the remote.
    known = (
        pushed
        and _read_git(instance_dir, ["cat-file", "-e", f"{pushed}^{{commit}}"], ok_only=True) is not None
    )
    scope = [f"{pushed}..HEAD"] if known else ["HEAD"]
    out = _read_git(instance_dir, ["log", "--format=%cI", *scope], ok_only=True)
    if out is None:
        return None, ""
    stamps = [line.strip() for line in out.splitlines() if line.strip()]
    if not stamps:
        return 0, ""
    return len(stamps), stamps[-1]


def _age_minutes(stamp: str, now: float) -> int | None:
    if not stamp:
        return 0
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((now - moment.timestamp()) // 60))


def _read_git(instance_dir: Path, args: list[str], *, ok_only: bool = False) -> Any:
    try:
        result = state_repo.run_git(instance_dir, args, label=f"checkpoint snapshot {args[0]}")
    except state_repo.StateRepoError:
        return None if ok_only else ""
    if result.returncode != 0:
        return None if ok_only else ""
    return result.stdout


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _float_field(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _drop_vanished(destination: Path, entries: tuple[str, ...], staged: tuple[str, ...]) -> None:
    """An optional entry the source no longer has must leave the checkpoint too.

    Otherwise a once-written `events.ndjson` would stay in `state/board` forever and keep getting
    committed as if it were current.
    """
    for entry in entries:
        if entry in staged:
            continue
        stale = destination / entry
        if not stale.exists():
            continue
        try:
            _remove_path(stale)
        except OSError as exc:
            raise CheckpointBlocked(f"could not drop stale {destination.name}/{entry}: {exc}") from None


def _scan_for_secrets(
    staging: Path,
    staged: tuple[str, ...],
    component: str,
    *,
    runtime_env: Path,
    secret_values: tuple[str, ...],
) -> None:
    """`state/` is what leaves the host, so a pasted token stops the commit here."""
    for entry in staged:
        text = _read_text(staging / entry, entry)
        # Scan against this installation rather than the process home default:
        # a recovery/doctor can intentionally point at another instance, and a
        # credential in that instance must still fail closed.
        scrubbed = redact(
            text,
            env_files=[runtime_env],
            secret_values=secret_values,
        )
        if scrubbed != text:
            raise CheckpointBlocked(f"secret detected in state/{component}/{entry}")


def _validate_board(
    staging: Path,
    *,
    instance: Path | None = None,
    registered_project_ids: set[str] | None = None,
) -> None:
    summary = _read_json(staging / "export.json", "board export.json")
    declared = _int_field(summary, "card_count", "board export.json")
    actual = _count_lines(staging / "cards.ndjson", "board cards.ndjson")
    if declared != actual:
        raise CheckpointBlocked(f"board export count mismatch: export.json={declared} cards.ndjson={actual}")
    try:
        cards = _read_ndjson(staging / "cards.ndjson", "board cards.ndjson")
        typed_records = any(
            isinstance(card.get("metadata"), dict)
            and card["metadata"].get("record_type") in {"product", "issue"}
            for card in cards
        )
        if typed_records and registered_project_ids is None and instance is not None:
            try:
                registered_project_ids = registered_projects(instance)
            except TaskError as exc:
                raise CheckpointBlocked(f"cannot validate Product projects: {exc.message}") from None
        validate_product_issue_records(cards, registered_project_ids=registered_project_ids)
    except ProductIssueValidationError as exc:
        raise CheckpointBlocked(f"invalid Product/Issue board record: {exc}") from None
    declared_sprints = _int_field(summary, "sprint_count", "board export.json")
    actual_sprints = _count_lines(staging / "sprints.ndjson", "board sprints.ndjson")
    if declared_sprints != actual_sprints:
        raise CheckpointBlocked(
            f"board sprint count mismatch: export.json={declared_sprints} sprints.ndjson={actual_sprints}"
        )
    events_path = staging / "events.ndjson"
    if events_path.exists():
        _validate_board_events(events_path)


def _validate_board_events(path: Path) -> None:
    """Validate new typed records while retaining released generic audit rows."""
    for number, record in enumerate(_read_ndjson(path, "board events.ndjson"), start=1):
        if record.get("record_type") != Event.RECORD_TYPE:
            continue
        try:
            Event.from_record(record)
        except ValueError as exc:
            raise CheckpointBlocked(
                f"invalid board protocol event at board events.ndjson line {number}: {exc}"
            ) from None


def _validate_runs(staging: Path) -> None:
    summary = _read_json(staging / "export.json", "runs export.json")
    declared = _int_field(summary, "run_record_count", "runs export.json")
    actual = _count_lines(staging / "runs.ndjson", "runs runs.ndjson")
    if declared != actual:
        raise CheckpointBlocked(f"runs export count mismatch: export.json={declared} runs.ndjson={actual}")

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


def _read_ndjson(path: Path, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(_read_text(path, label).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise CheckpointBlocked(f"could not parse {label} line {number}: {exc}") from None
        if not isinstance(record, dict):
            raise CheckpointBlocked(f"{label} line {number} must be an object")
        records.append(record)
    return records


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
