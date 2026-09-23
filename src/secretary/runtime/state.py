"""Watermark + lock, shared by every triggered-agent. Each agent gets its own state dir.

Watermark: an agent remembers how far it has already processed each source (curator:
lines past a JSONL count). The watermark advances ONLY after the agent's durable output
is committed (two-phase), so a crash mid-run re-processes rather than silently dropping.

Lock: one lockfile guards a run. Orca already serializes runs of one automation; this is
a backstop against a manual run overlapping a scheduled one. It is an `flock`, so a killed
run cannot keep it; the file records the holder's pid and start time for diagnostics.

State root is `TA_STATE` or `~/secretary-data/automation-state`, then `/<agent>`.
a watermark file.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

STATE_ROOT = Path(os.environ.get("TA_STATE", str(Path.home() / "secretary-data" / "automation-state")))

# Shared precheck protocol: 0 dispatches; explicit skip/defer codes are clean; all others fail.
# Skip is not 1, Python's uncaught-exception exit code.
PRECHECK_SKIP = 100

# Board unavailability is retryable and distinct from a clean skip.
PRECHECK_BOARD_UNREACHABLE = 101


class BoardUnavailable(RuntimeError):
    """The board store is not reachable yet: nothing was read or written.

    A precheck reports it as PRECHECK_BOARD_UNREACHABLE, a deferred tick, rather than as its own
    failure; any other exception means the precheck itself is broken.
    """

# Durable settlement is in progress; exit cleanly without racing live-head cleanup.
PRECHECK_DEFERRED = 102


def publish_state_atomic(
    writes: list[tuple[Path, str]],
    *,
    removes: list[Path] | None = None,
) -> None:
    """Publish one triggered-agent state transition or restore every changed path.

    Curator baseline settlement updates its watermark and audit as one local transaction.
    Staging each replacement first lets an audit-write failure leave the prior watermark and
    pending record intact; a later replace or removal failure restores every affected file.
    """
    removals = removes or []
    paths = [path for path, _ in writes] + removals
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((path, Path(temporary)))
        for path, staged_file in staged:
            os.replace(staged_file, path)
        for path in removals:
            path.unlink(missing_ok=True)
    except OSError:
        for path in reversed(paths):
            _restore_state_file(path, before[path])
        raise
    finally:
        for _, staged_file in staged:
            staged_file.unlink(missing_ok=True)


def _restore_state_file(path: Path, before: bytes | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class AgentState:
    """Per-agent watermark + lock under STATE_ROOT/<agent>/."""

    def __init__(self, agent: str, state_dir: Path | None = None):
        self.agent = agent
        self.dir = Path(state_dir) if state_dir is not None else STATE_ROOT / agent
        self.watermark_file = self.dir / "watermark.json"
        self.pending_file = self.dir / "pending.json"
        self.lockfile = self.dir / "lock"
        self.head_profile_file = self.dir / "head_profile.json"
        self.terminal_handle_file = self.dir / "terminal_handle.json"
        self.terminal_generation_file = self.dir / "terminal_generation.json"
        self.head_run_file = self.dir / "head_run.json"
        self.active_report_file = self.dir / "active_report.json"

    def ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def load_watermark(self) -> dict:
        if not self.watermark_file.is_file():
            return {}
        try:
            return json.loads(self.watermark_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_watermark(self, mark: dict) -> None:
        self.ensure_dir()
        tmp = self.watermark_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(mark, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.watermark_file)

    def load_head_profile(self) -> str | None:
        """The heads.toml profile id the agent's live terminal was actually launched with, or
        None if it was never recorded (agent has no `head`, or predates this tracking) — a warm
        terminal keeps whatever profile it started on and never re-resolves on its own, so
        idle-reuse needs this to check the resource the terminal is really running against
        instead of the agent's static preferred head (triggered-agents-275)."""
        if not self.head_profile_file.is_file():
            return None
        try:
            return json.loads(self.head_profile_file.read_text(encoding="utf-8")).get("profile")
        except json.JSONDecodeError:
            return None

    def save_head_profile(self, profile: str | None) -> None:
        """Record `profile` as the one the just-(re)spawned terminal is running on. Called after
        every fresh create / watchdog restart / red-fallback relaunch, never after a plain warm
        reuse (the terminal's profile hasn't changed)."""
        self.ensure_dir()
        tmp = self.head_profile_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"profile": profile}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.head_profile_file)

    def load_terminal_handle(self) -> str | None:
        """The Orca terminal handle last created for this singleton agent."""
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("handle")
        except json.JSONDecodeError:
            return None

    def next_terminal_generation(self) -> int:
        """Monotonic per-agent counter, bumped once per real terminal create and stamped into that
        terminal's own self-teardown trailer (triggered-agents-445, PR #95 review B2, round 4).

        Never reset — not even when a terminal is torn down. A finalizer running after its head
        exits must be able to tell its OWN completed terminal apart from a replacement a concurrent
        tick created in the meantime: it compares the generation baked into its trailer against the
        generation recorded for the workspace's current terminal (`load_terminal_generation`). A
        counter that reset on teardown could hand a replacement the same number a late finalizer is
        still carrying, which would let that finalizer's blanket `terminal stop` kill the live
        replacement. Kept in its own file so tearing the handle down (which unlinks
        terminal_handle.json) never rolls the counter back."""
        cur = 0
        if self.terminal_generation_file.is_file():
            try:
                cur = int(
                    json.loads(self.terminal_generation_file.read_text(encoding="utf-8")).get("counter", 0)
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                cur = 0
        nxt = cur + 1
        self.ensure_dir()
        tmp = self.terminal_generation_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"counter": nxt}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.terminal_generation_file)
        return nxt

    def load_terminal_generation(self) -> int | None:
        """The generation stamped on the workspace's CURRENT live terminal (the one recorded in
        terminal_handle.json), or None if none is recorded. `dispatch.finalize` compares the
        generation baked into its own trailer against this to decide whether the live terminal is
        still its own (safe to stop) or a replacement a concurrent tick already put in its place
        (must not be touched) — triggered-agents-445, PR #95 review B2, round 4."""
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("generation")
        except json.JSONDecodeError:
            return None

    def load_terminal_created_at(self) -> float | None:
        """When `_create_terminal` last actually spawned a process for this agent (epoch
        seconds), or None if never recorded — the marker `dispatch.run`'s "no terminal" branch
        checks before creating another one, since a terminal it just created may not be visible
        in `terminal list` yet (triggered-agents-445, PR #95 review B2): a second dispatch landing
        in that visibility gap must not read "no terminal" as "nothing was ever spawned" and
        create a duplicate."""
        if not self.terminal_handle_file.is_file():
            return None
        try:
            return json.loads(self.terminal_handle_file.read_text(encoding="utf-8")).get("created_at")
        except json.JSONDecodeError:
            return None

    def save_terminal_handle(
        self, handle: str | None, created_at: float | None = None, generation: int | None = None
    ) -> None:
        """Record the terminal handle from the latest fresh spawn.

        Codex can rename its tab away from the explicit `triggered-agent:<name>` title after
        startup, so title matching alone is not stable enough for singleton reuse.

        `created_at` (epoch seconds) is set only by `_create_terminal` at the moment it actually
        spawns a process — a plain warm-reuse call (the terminal already existed and is just being
        re-confirmed as the survivor) passes none, which drops any previously recorded timestamp:
        by the time reuse runs the terminal is already confirmed visible, so there is nothing left
        for `load_terminal_created_at`'s visibility-gap check to guard.

        `generation` is the monotonic id from `next_terminal_generation`, recorded only for an
        ephemeral agent's fresh create so `dispatch.finalize` can tell whether the workspace's live
        terminal is still the one its own trailer belongs to (triggered-agents-445, PR #95 review
        B2, round 4). A `handle=None` call tears the record down (finalize after a confirmed
        teardown); the monotonic counter in its own file is deliberately left untouched."""
        self.ensure_dir()
        if not handle:
            try:
                self.terminal_handle_file.unlink()
            except FileNotFoundError:
                pass
            return
        payload: dict[str, object] = {"handle": handle}
        if created_at is not None:
            payload["created_at"] = created_at
        if generation is not None:
            payload["generation"] = generation
        tmp = self.terminal_handle_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.terminal_handle_file)

    def load_head_run(self) -> dict | None:
        """The `HeadRun` of the head this agent's last tick raised on a backend of this product's
        own, as that backend's receipt recorded it, or None when no tick has raised one.

        A pane is Orca's to remember and this is not: a `local-pty` head outlives the tick that
        started it under a supervisor with no session store behind it, so the run id, workspace and
        spec that reach it again have to be written down here. It is what the next tick hands
        `LocalPtyHeadRuntime.start`, which is what makes that bring-up a refusal over a head that
        is still working rather than a second head beside it.

        A record that will not parse is read as no record: a bring-up fenced out by a corrupt file
        is a role that never goes on duty again, and nothing rewrites this file except the tick
        that the refusal would be preventing.
        """
        if not self.head_run_file.is_file():
            return None
        try:
            record = json.loads(self.head_run_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return record if isinstance(record, dict) else None

    def save_head_run(self, run: dict | None) -> None:
        """Record the head this tick raised, or forget the one it tore down (`run=None`).

        Two-phase like every neighbour in this directory: a tick that dies mid-write leaves the
        previous run readable rather than a half-written record the next tick would discard.
        """
        self.ensure_dir()
        if run is None:
            try:
                self.head_run_file.unlink()
            except FileNotFoundError:
                pass
            return
        tmp = self.head_run_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.head_run_file)

    def load_active_report(self) -> dict | None:
        if not self.active_report_file.is_file():
            return None
        try:
            data = json.loads(self.active_report_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def save_active_report(self, reference: str | None, terminal_handle: str | None) -> None:
        self.ensure_dir()
        if not reference or not terminal_handle:
            self.clear_active_report(reference)
            return
        tmp = self.active_report_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"reference": reference, "terminal_handle": terminal_handle}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.active_report_file)

    def clear_active_report(self, reference: str | None = None) -> None:
        if reference is not None:
            current = self.load_active_report()
            if current and current.get("reference") != reference:
                return
        try:
            self.active_report_file.unlink()
        except FileNotFoundError:
            pass

    def log_run(self, event: str, **fields: object) -> None:
        """Append a run-telemetry line to runs.jsonl. Best-effort: a logging failure
        must never break the run itself, so any error is swallowed."""
        try:
            self.ensure_dir()
            rec = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
            with (self.dir / "runs.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive run lock. Raises if another run of this agent holds it.

        `flock` on the lock file is the mutex: the kernel drops it when the holder dies, so a
        SIGKILLed run leaves a file but no lock. The pid/start record is diagnostics, and it still
        refuses a live holder that took the lock without `flock` (the pre-flock bare-pid form).
        Reclaiming a dead holder's file logs `lock-reclaimed`; a refusal logs `lock-refused`.
        """
        self.ensure_dir()
        fd = self._acquire_lockfile()
        try:
            yield
        finally:
            try:
                if _same_file(self.lockfile, fd):
                    self.lockfile.unlink()
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)

    def _acquire_lockfile(self) -> int:
        for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
            fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self._refuse(_read_lock_record(fd))
                # A releasing holder unlinks the path before closing; a lock on an unlinked
                # inode guards nothing, so start over on whatever the path names now.
                if not _same_file(self.lockfile, fd):
                    os.close(fd)
                    continue
                record = _read_lock_record(fd)
                if record.get("raw"):
                    if _holder_alive(record, os.fstat(fd).st_mtime):
                        self._refuse(record)
                    self.log_run(
                        "lock-reclaimed",
                        stale_pid=record.get("pid"),
                        recorded_start=record.get("start"),
                        lock_age_s=round(time.time() - os.fstat(fd).st_mtime, 3),
                        reclaimer_pid=os.getpid(),
                    )
                body = json.dumps(
                    {"pid": os.getpid(), "start": _process_start(os.getpid()), "source": _lock_source()}
                ).encode()
                os.ftruncate(fd, 0)
                os.pwrite(fd, body, 0)
                os.fsync(fd)
                return fd
            except BaseException:
                with suppress(OSError):
                    os.close(fd)
                raise
        raise SystemExit(f"{self.agent}: lock file keeps changing ({self.lockfile})")

    def _refuse(self, record: dict) -> NoReturn:
        holder = record.get("pid") if record.get("pid") is not None else (record.get("raw") or "?")
        self.log_run("lock-refused", holder_pid=holder, recorded_start=record.get("start"))
        raise SystemExit(f"{self.agent}: another run holds the lock ({self.lockfile}, pid {holder})")


_LOCK_ACQUIRE_ATTEMPTS = 8


def _same_file(path: Path, fd: int) -> bool:
    try:
        on_path = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (on_path.st_dev, on_path.st_ino) == (held.st_dev, held.st_ino)


def _read_lock_record(fd: int) -> dict:
    """Parse a lock record: JSON `{"pid", "start", "source"}` or the legacy bare decimal pid."""
    try:
        raw = os.pread(fd, 4096, 0).decode("utf-8", errors="replace").strip()
    except OSError:
        raw = ""
    record: dict = {"raw": raw}
    if raw.isdigit():
        record["pid"] = int(raw)
        return record
    try:
        parsed = json.loads(raw)
    except ValueError:
        return record
    if isinstance(parsed, dict) and isinstance(parsed.get("pid"), int):
        record["pid"] = parsed["pid"]
        if isinstance(parsed.get("start"), int):
            record["start"] = parsed["start"]
    return record


def _holder_alive(record: dict, written_at: float) -> bool:
    """Whether the recorded holder still runs: the pid exists and is the same process.

    A recorded start time must match. The legacy form has none, so a process that started
    after the lock file was written is a reused pid rather than the holder.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    start = _process_start(pid)
    if "start" in record:
        return start is None or start == record["start"]
    started_at = _process_started_at(start)
    return started_at is None or started_at <= written_at + 1


def _process_start(pid: int) -> int | None:
    """Start time of `pid` in clock ticks since boot (`/proc/<pid>/stat` field 22), if known."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return int(stat[stat.rindex(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _process_started_at(start: int | None) -> float | None:
    if start is None:
        return None
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + start / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    return None


def _lock_source() -> str:
    return " ".join(sys.argv)[:200]
