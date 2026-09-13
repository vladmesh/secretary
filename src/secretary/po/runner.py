"""Headless turns of the PO head: one Claude or Codex process per turn, in the PO workspace.

No Orca and no local-pty: a turn is an ordinary child process started with `subprocess`, in its
own process group, with the PO workspace as its working directory and full permissions. The owner's
message goes to the child on stdin; its stdout is kept raw in a file under
``<data_dir>/po-runs/<session>/`` (outside the workspace the agent can write), and a waiter thread
settles the turn when the process exits. Only the owner's message and the agent's final answer reach
the feed in the board store (`secretary.po.store`).

The CLIs own their conversation memory, addressed by their native flags:

* Claude: turn 1 ``claude -p --session-id <uuid>`` with a uuid the secretary chose at session
  creation, later turns ``--resume <uuid>``; ``--output-format json`` carries the final answer.
* Codex: turn 1 ``codex exec --json``, whose event stream names the ``thread_id`` the session then
  keeps; later turns ``codex exec resume <thread_id>``. ``-o`` writes the final answer to a file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.po.store import (
    CLIS,
    FAILED,
    INTERRUPTED,
    PoStore,
    Session,
    Turn,
)
from secretary.po.workspace import workspace_dir

RUNS_DIR_NAME = "po-runs"
STOPPED_REASON = "stopped by the owner"
RECOVERED_REASON = "the web service restarted while this turn was running"
# How much of stderr a failed turn quotes in its reason.
STDERR_TAIL_BYTES = 2000
STOP_JOIN_SECONDS = 10.0


class RunnerError(RuntimeError):
    """A PO turn could not be started or addressed."""


def runs_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / RUNS_DIR_NAME


def process_identity(pid: int) -> str | None:
    """Boot id and kernel start time of a live process, or ``None`` when it cannot be read.

    A PID alone names whatever process holds it now; with the start time it names the one process
    the runner started, so a restart never kills a stranger that inherited the number.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces and parentheses; the rest starts after the last ')'.
    fields = stat[stat.rindex(")") + 2 :].split()
    if len(fields) < 20:
        return None
    return f"{boot_id}:{fields[19]}"


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def claude_final_answer(stdout: str) -> tuple[str | None, str | None]:
    """The final answer from ``--output-format json``, or ``None`` and why there is none."""
    candidates: list[Any] = []
    text = stdout.strip()
    try:
        candidates.append(json.loads(text))
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                candidates.append(json.loads(line))
            except ValueError:
                continue
    for document in candidates:
        if not isinstance(document, dict) or document.get("type", "result") != "result":
            continue
        result = document.get("result")
        if document.get("is_error"):
            return None, f"claude reported an error: {result or document.get('subtype') or 'no detail'}"
        if isinstance(result, str) and result.strip():
            return result.strip(), None
        return None, "claude's result object carries no final answer"
    return None, "claude printed no result object"


def codex_thread_id(stdout: str) -> str | None:
    """The first ``thread_id`` named by Codex's ``--json`` event stream."""

    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            found = value.get("thread_id")
            if isinstance(found, str) and found:
                return found
            for item in value.values():
                found = find(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = find(item)
                if found:
                    return found
        return None

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        found = find(event)
        if found:
            return found
    return None


@dataclass(frozen=True)
class TurnFiles:
    directory: Path
    prompt: Path
    stdout: Path
    stderr: Path
    last_message: Path


@dataclass
class _Live:
    process: subprocess.Popen[bytes]
    thread: threading.Thread


class PoRunner:
    """Sessions of the PO head, each turn one process; sessions run their turns in parallel."""

    def __init__(
        self,
        store: PoStore,
        data_dir: Path | str,
        *,
        executables: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.workspace = workspace_dir(self.data_dir)
        self.runs = runs_dir(self.data_dir)
        self.executables = {"claude": "claude", "codex": "codex", **dict(executables or {})}
        self.env = dict(env) if env is not None else None
        # Held only while a turn is started, stopped or recovered, never while one runs.
        self._lock = threading.Lock()
        self._live: dict[tuple[str, int], _Live] = {}

    @classmethod
    def for_instance(cls, instance_dir: Path | str, data_dir: Path | str, **kwargs: Any) -> PoRunner:
        return cls(PoStore.for_instance(instance_dir), data_dir, **kwargs)

    # --- sessions ---------------------------------------------------------------------------

    def create_session(self, cli: str, model: str) -> Session:
        if cli not in CLIS:
            raise RunnerError(f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        if not model.strip():
            raise RunnerError("a PO session needs a model")
        return self.store.create_session(
            session_id=str(uuid.uuid4()),
            cli=cli,
            model=model.strip(),
            cwd=str(self.workspace),
            cli_session_id=str(uuid.uuid4()) if cli == "claude" else None,
        )

    def files(self, session_id: str, seq: int) -> TurnFiles:
        directory = self.runs / session_id
        stem = f"turn-{seq:04d}"
        return TurnFiles(
            directory=directory,
            prompt=directory / f"{stem}.prompt",
            stdout=directory / f"{stem}.stdout",
            stderr=directory / f"{stem}.stderr",
            last_message=directory / f"{stem}.last-message",
        )

    def argv(self, session: Session, seq: int, files: TurnFiles) -> list[str]:
        executable = self.executables[session.cli]
        if session.cli == "claude":
            assert session.cli_session_id is not None
            argv = [
                executable,
                "-p",
                "--output-format",
                "json",
                "--model",
                session.model,
                "--dangerously-skip-permissions",
            ]
            if seq > 1:
                return [*argv, "--resume", session.cli_session_id]
            return [*argv, "--session-id", session.cli_session_id]
        options = [
            "--json",
            "-m",
            session.model,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-o",
            str(files.last_message),
        ]
        if session.cli_session_id:
            return [executable, "exec", "resume", *options, session.cli_session_id, "-"]
        return [executable, "exec", *options, "-C", session.cwd, "-"]

    # --- turns ------------------------------------------------------------------------------

    def send(self, session_id: str, text: str) -> Turn:
        """Start one turn, or refuse with nothing written when one is already running."""
        if not text.strip():
            raise RunnerError("an empty message starts no turn")
        with self._lock:
            turn = self.store.begin_turn(
                session_id, text, lambda seq: self.files(session_id, seq).stdout
            )
            # Read after the claim: the previous turn may have recorded Codex's thread id.
            session = self.store.session(session_id)
            files = self.files(session_id, turn.seq)
            argv = self.argv(session, turn.seq, files)
            try:
                files.directory.mkdir(parents=True, exist_ok=True)
                files.prompt.write_text(text, encoding="utf-8")
                with (
                    files.prompt.open("rb") as stdin,
                    files.stdout.open("wb") as stdout,
                    files.stderr.open("wb") as stderr,
                ):
                    process = subprocess.Popen(
                        argv,
                        cwd=session.cwd,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        env=self.env,
                        start_new_session=True,
                    )
            except OSError as exc:
                reason = f"could not start {argv[0]}: {exc}"
                self.store.finish_turn(session_id, turn.seq, FAILED, reason)
                raise RunnerError(reason) from None
            if not self.store.record_process(
                session_id, turn.seq, process.pid, process_identity(process.pid)
            ):
                _kill_group(process.pid)
            thread = threading.Thread(
                target=self._wait,
                args=(session, turn.seq, process, files),
                name=f"po-turn-{session_id}-{turn.seq}",
                daemon=True,
            )
            self._live[(session_id, turn.seq)] = _Live(process, thread)
            thread.start()
        return self.store.turn(session_id, turn.seq)

    def stop(self, session_id: str) -> Turn | None:
        """Kill the running turn's process group; the turn is `interrupted`, the session goes on."""
        session = self.store.session(session_id)
        with self._lock:
            running = self.store.running_turns(session_id)
            if not running:
                return None
            turn = running[0]
            # Mark first, so the waiter sees a settled turn and does not call the kill a failure.
            self.store.finish_turn(session_id, turn.seq, INTERRUPTED, STOPPED_REASON)
            live = self._live.get((session_id, turn.seq))
            if live is not None:
                _kill_group(live.process.pid)
            elif turn.pid and turn.process_identity and process_identity(turn.pid) == turn.process_identity:
                _kill_group(turn.pid)
        if live is not None:
            live.thread.join(STOP_JOIN_SECONDS)
        else:
            self._capture_thread_id(session, Path(turn.stdout_path))
        return self.store.turn(session_id, turn.seq)

    def recover(self) -> list[Turn]:
        """At service start: every `running` turn is `interrupted`, its own live process killed."""
        recovered: list[Turn] = []
        for turn in self.store.running_turns():
            with self._lock:
                if (turn.session_id, turn.seq) in self._live:
                    continue
                killed = bool(
                    turn.pid
                    and turn.process_identity
                    and process_identity(turn.pid) == turn.process_identity
                )
                reason = RECOVERED_REASON + ("; its process was killed" if killed else "")
                settled = self.store.finish_turn(turn.session_id, turn.seq, INTERRUPTED, reason)
                if killed and settled:
                    _kill_group(int(turn.pid))
            if settled:
                self._capture_thread_id(self.store.session(turn.session_id), Path(turn.stdout_path))
                recovered.append(self.store.turn(turn.session_id, turn.seq))
        return recovered

    def wait(self, session_id: str, seq: int, timeout: float | None = None) -> Turn:
        """Block until this runner's waiter has settled the turn (a convenience for callers)."""
        with self._lock:
            live = self._live.get((session_id, seq))
        if live is not None:
            live.thread.join(timeout)
        return self.store.turn(session_id, seq)

    # --- settling ---------------------------------------------------------------------------

    def _wait(
        self, session: Session, seq: int, process: subprocess.Popen[bytes], files: TurnFiles
    ) -> None:
        try:
            code = process.wait()
            self._settle(session, seq, code, files)
        except Exception as exc:  # noqa: BLE001 - a waiter must never leave a turn running without a word
            try:
                self.store.finish_turn(
                    session.session_id,
                    seq,
                    FAILED,
                    f"the runner could not settle this turn: {type(exc).__name__}: {exc}",
                )
            except Exception as nested:  # noqa: BLE001 - the store itself is what failed
                print(
                    f"secretary po: turn {session.session_id}/{seq} left running: "
                    f"{type(exc).__name__}: {exc}; then {type(nested).__name__}: {nested}",
                    file=sys.stderr,
                )
        finally:
            with self._lock:
                self._live.pop((session.session_id, seq), None)

    def _settle(self, session: Session, seq: int, code: int, files: TurnFiles) -> None:
        stdout = files.stdout.read_bytes().decode("utf-8", errors="replace")
        self._capture_thread_id(session, files.stdout, stdout)
        if session.cli == "claude":
            answer, missing = claude_final_answer(stdout)
        else:
            answer, missing = self._codex_final_answer(files)
        if code != 0:
            reason = f"{session.cli} exited with status {code}"
            tail = self._stderr_tail(files)
            if tail:
                reason += f": {tail}"
            self.store.finish_turn(session.session_id, seq, FAILED, reason)
        elif answer is None:
            self.store.finish_turn(session.session_id, seq, FAILED, missing or "no final answer")
        else:
            self.store.complete_turn(session.session_id, seq, answer)

    @staticmethod
    def _codex_final_answer(files: TurnFiles) -> tuple[str | None, str | None]:
        try:
            answer = files.last_message.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None, "codex wrote no last message"
        return (answer, None) if answer else (None, "codex's last message is empty")

    @staticmethod
    def _stderr_tail(files: TurnFiles) -> str:
        try:
            data = files.stderr.read_bytes()
        except OSError:
            return ""
        return data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace").strip()

    def _capture_thread_id(self, session: Session, stdout_path: Path, stdout: str | None = None) -> None:
        """Keep Codex's thread id from any turn that reached it, so the next turn can resume."""
        if session.cli != "codex" or session.cli_session_id:
            return
        if stdout is None:
            try:
                stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                return
        thread_id = codex_thread_id(stdout)
        if thread_id:
            self.store.set_cli_session_id(session.session_id, thread_id)


__all__ = [
    "RUNS_DIR_NAME",
    "PoRunner",
    "RunnerError",
    "TurnFiles",
    "claude_final_answer",
    "codex_thread_id",
    "process_identity",
    "runs_dir",
]
