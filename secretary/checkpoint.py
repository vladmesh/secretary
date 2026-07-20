"""Checkpoint writer and pusher for the private instance repository.

Contract: docs/RECOVERY.md, sections "Layout", "Каденция и RPO", "Writer",
"Валидационный гейт", "Failure и divergence", "Observability". The writer
regenerates the normalized board and runs exports, validates the snapshot, and
commits `state/board` and `state/runs` into the private repo. It runs at the end
of a dispatcher tick under `tick_lock`, and also takes the instance repo writer
lock so checkpoint writes cannot overlap a green-card publish against the same
checkout. The pusher runs on the same tick but on its own 30-minute window, and
`checkpoint_snapshot` turns both into the freshness view `status` and `doctor`
print.

Memory (`state/memory`) is written by its own writer (`secretary.memory_write`)
directly into the same repo, so it is deliberately outside this pathspec: the
two writers share the repo but never the paths. `state_repo_lock` keeps their
index operations from overlapping.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from secretary._fsutil import (
    cleanup_staging_dir as _cleanup_staging_dir,
    ensure_dir as _ensure_dir,
    publish_component_entries as _publish_component_entries,
    remove_path as _remove_path,
    write_text_atomic as _write_text_atomic,
)
from secretary.data import (
    PIPELINE_STATE_DIR,
    PIPELINE_WORKTREE,
    export_board,
    export_runs,
)
from secretary import state_repo
from secretary.state_repo import BOARD_RUNS_PATHSPEC
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
            with state_repo.state_repo_lock(self.instance_dir):
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
            raise CheckpointBlocked(
                f"checkpoint is not tracked by the instance repo: {', '.join(missing)}"
            )

    def _identity(self) -> list[str]:
        return state_repo.commit_identity(self.instance_dir)

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


class _GitFailure(Exception):
    """A git command the pusher needs did not run or did not succeed.

    `output` keeps the whole stderr: git reports a rejected push on a different
    line than the one that reads best in `status`, so the classifier needs all
    of it while the message stays short.
    """

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

    Fail-closed on the checkpoint, not on the work: a failed push leaves its
    reason and a growing lag in state while the dispatcher keeps running, and
    the next window retries. A remote holding commits the local repo does not
    have stops the push and raises `remote diverged` for an operator; there is
    no force-push path here by construction.
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

    def _record(
        self, state: dict[str, Any], outcome: PushOutcome, now: float
    ) -> dict[str, Any]:
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
        return result.stdout.strip() if result.returncode == 0 else ""

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

        A tip the local repo has never even seen is divergence, not a missing
        object: the remote moved on without us.
        """
        if self._run(["cat-file", "-e", f"{remote_head}^{{commit}}"], timeout=120).returncode != 0:
            return False
        return self._run(["merge-base", "--is-ancestor", remote_head, head], timeout=120).returncode == 0

    def _git(self, args: list[str], label: str, *, timeout: float = 120) -> str:
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            detail = output.splitlines()
            raise _GitFailure(
                f"{label} failed: {detail[-1] if detail else 'git error'}", output
            )
        return result.stdout

    def _run(self, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(self.instance_dir), *args],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=_noninteractive_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _GitFailure(f"git {args[0]} failed: {exc}") from None


def checkpoint_snapshot(
    instance_dir: Path,
    *,
    write_state: dict[str, Any] | None = None,
    push_state: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Checkpoint freshness for `status` and `doctor`.

    Contract: docs/RECOVERY.md, "Observability". Last commit, last successful
    push, lag in minutes and commits, the gate's blocking reason and the
    `remote diverged` alarm.
    """
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
    known = pushed and _read_git(
        instance_dir, ["cat-file", "-e", f"{pushed}^{{commit}}"], ok_only=True
    ) is not None
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
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((now - moment.timestamp()) // 60))


def _read_git(instance_dir: Path, args: list[str], *, ok_only: bool = False) -> Any:
    try:
        result = subprocess.run(
            ["git", "-C", str(instance_dir), *args],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None if ok_only else ""
    if result.returncode != 0:
        return None if ok_only else ""
    return result.stdout


def _noninteractive_env() -> dict[str, str]:
    """No credential prompt may ever block the tick waiting on a terminal.

    Missing credentials have to surface as a failed push with a reason, not as
    a git process parked on a password prompt nobody is there to answer.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _float_field(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _drop_vanished(destination: Path, entries: tuple[str, ...], staged: tuple[str, ...]) -> None:
    """An optional entry the source no longer has must leave the checkpoint too.

    Otherwise a once-written `events.ndjson` would stay in `state/board` forever and keep
    getting committed as if it were current.
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
            raise CheckpointBlocked(
                f"could not drop stale {destination.name}/{entry}: {exc}"
            ) from None


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
