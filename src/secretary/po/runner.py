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

A session's reasoning effort, unless it is ``default``, is passed on every turn: ``--effort <level>``
to Claude, ``-c model_reasoning_effort=<level>`` to Codex. What model a turn actually ran is kept on
the turn: Claude's result object keys ``modelUsage`` by full model id, the session's own model first
and any subagent's after it; Codex's event stream names no model, so it is read from the
``turn_context`` of the thread's rollout under ``$CODEX_HOME/sessions`` (``~/.codex`` by default).
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
    COMPLETED,
    DEFAULT_EFFORT,
    FAILED,
    INTERRUPTED,
    RUNNING,
    PoStore,
    Session,
    Turn,
)
from secretary.po.workspace import workspace_dir
from secretary.runtime.provider_models import codex_rollout_path, codex_session_models

RUNS_DIR_NAME = "po-runs"
STOPPED_REASON = "stopped by the owner"
RECOVERED_REASON = "the web service restarted while this turn was running"
# How much of stderr a failed turn quotes in its reason.
STDERR_TAIL_BYTES = 2000
STOP_JOIN_SECONDS = 10.0
# Claude Code's refusal of `--session-id` for a conversation that already exists (checked on 2.1.270).
CLAUDE_SESSION_IN_USE = "is already in use"


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


def turn_environment(
    environ: Mapping[str, str] | None = None, *, interpreter: str | None = None
) -> dict[str, str]:
    """The environment of a turn: the service's own, with the product runtime first on ``PATH``.

    The PO workspace tells the head to run ``python3 -P -m secretary``; with the service's ``PATH``
    that is the system Python, which lacks the product's dependencies. The directory of the
    interpreter running this process (the production runtime) goes first, so ``python3`` and the
    ``secretary`` console script resolve there, and the source this process imports goes first on
    ``PYTHONPATH``, as the control-plane commands keep it importable. Everything else is kept.
    """
    env = dict(os.environ if environ is None else environ)
    bin_dir = str(Path(interpreter or sys.executable).parent)
    path = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != bin_dir]
    env["PATH"] = os.pathsep.join([bin_dir, *path])
    source = str(Path(__file__).resolve().parents[2])
    pythonpath = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry and entry != source]
    env["PYTHONPATH"] = os.pathsep.join([source, *pythonpath])
    return env


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def claude_final_answer(stdout: str) -> tuple[str | None, str | None]:
    """The final answer from ``--output-format json``, or ``None`` and why there is none."""
    for document in _json_documents(stdout):
        if not isinstance(document, dict) or document.get("type", "result") != "result":
            continue
        result = document.get("result")
        if document.get("is_error"):
            return None, f"claude reported an error: {result or document.get('subtype') or 'no detail'}"
        if isinstance(result, str) and result.strip():
            return result.strip(), None
        return None, "claude's result object carries no final answer"
    return None, "claude printed no result object"


def claude_resolved_model(stdout: str) -> str | None:
    """The session's own model from ``--output-format json``: the first ``modelUsage`` key.

    Claude Code keys ``modelUsage`` by the full model id each model ran under (`claude-opus-5-5`, or
    `claude-opus-5-5[1m]` with the long context), the session's model first and the models its
    subagents ran after it.
    """
    for document in _json_documents(stdout):
        if not isinstance(document, dict) or document.get("type", "result") != "result":
            continue
        usage = document.get("modelUsage")
        if isinstance(usage, dict):
            return next((key for key in usage if isinstance(key, str) and key.strip()), None)
        return None
    return None


def codex_resolved_model(codex_home: Path | str, thread_id: str | None) -> str | None:
    """The model the last turn of a Codex thread ran, from its rollout's ``turn_context``."""
    path = codex_rollout_path(codex_home, thread_id or "")
    if path is None:
        return None
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    return codex_session_models(_json_documents(text, whole=False)).model or None


def _json_documents(text: str, *, whole: bool = True) -> list[Any]:
    """The JSON value `text` is, else every line that parses: last line first, or in order without `whole`.

    A turn relaunched with `--resume` appends a second result object to the same stdout, so the one
    that counts is the last.
    """
    text = text.strip()
    if whole:
        try:
            return [json.loads(text)]
        except ValueError:
            pass
    found: list[Any] = []
    for line in text.splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(found)) if whole else found


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
        # A turn gets `turn_environment()` unless the caller passes its own.
        self.env = dict(env) if env is not None else turn_environment()
        # Held only while a turn is started, stopped or recovered, never while one runs.
        self._lock = threading.Lock()
        self._live: dict[tuple[str, int], _Live] = {}

    @classmethod
    def for_instance(cls, instance_dir: Path | str, data_dir: Path | str, **kwargs: Any) -> PoRunner:
        return cls(PoStore.for_instance(instance_dir), data_dir, **kwargs)

    # --- sessions ---------------------------------------------------------------------------

    def create_session(self, cli: str, model: str, effort: str = DEFAULT_EFFORT) -> Session:
        return self._create(cli, model, effort, None)[0]

    def create_session_request(
        self, cli: str, model: str, request_id: str, effort: str = DEFAULT_EFFORT
    ) -> tuple[Session, bool]:
        """`create_session` under a form's request id; the flag says whether this call created it."""
        return self._create(cli, model, effort, request_id)

    def _create(self, cli: str, model: str, effort: str, request_id: str | None) -> tuple[Session, bool]:
        if cli not in CLIS:
            raise RunnerError(f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        if not model.strip():
            raise RunnerError("a PO session needs a model")
        if not effort.strip():
            raise RunnerError(f"a PO session needs an effort, {DEFAULT_EFFORT!r} for the CLI's own")
        return self.store.claim_session(
            session_id=str(uuid.uuid4()),
            cli=cli,
            model=model.strip(),
            cwd=str(self.workspace),
            cli_session_id=str(uuid.uuid4()) if cli == "claude" else None,
            request_id=request_id,
            effort=effort.strip(),
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

    def argv(self, session: Session, files: TurnFiles, *, established: bool = False) -> list[str]:
        """One turn's command. `established`: a Claude conversation is known to exist under its id."""
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
                *self._effort_options(session),
                "--dangerously-skip-permissions",
            ]
            if established:
                return [*argv, "--resume", session.cli_session_id]
            return [*argv, "--session-id", session.cli_session_id]
        options = [
            "--json",
            "-m",
            session.model,
            *self._effort_options(session),
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-o",
            str(files.last_message),
        ]
        if session.cli_session_id:
            return [executable, "exec", "resume", *options, session.cli_session_id, "-"]
        return [executable, "exec", *options, "-C", session.cwd, "-"]

    @staticmethod
    def _effort_options(session: Session) -> list[str]:
        """The effort flag of this session's CLI, or none for `default`."""
        if session.effort == DEFAULT_EFFORT:
            return []
        if session.cli == "claude":
            return ["--effort", session.effort]
        return ["-c", f"model_reasoning_effort={session.effort}"]

    # --- turns ------------------------------------------------------------------------------

    def send(self, session_id: str, text: str) -> Turn:
        """Start one turn, or refuse with nothing written when one is already running."""
        return self._send(session_id, text, None)[0]

    def send_request(self, session_id: str, text: str, request_id: str) -> tuple[Turn, bool]:
        """`send` under a form's request id; the flag says whether this call started the turn.

        A request id that already started this send gets that turn back and no process, and one that
        belongs to anything else is `RequestConflict` (`PoStore.claim_turn`): a CLI is launched only for
        a turn this call created, after its transaction committed.
        """
        return self._send(session_id, text, request_id)

    def _send(self, session_id: str, text: str, request_id: str | None) -> tuple[Turn, bool]:
        if not text.strip():
            raise RunnerError("an empty message starts no turn")
        with self._lock:
            turn, created = self.store.claim_turn(
                session_id, text, lambda seq: self.files(session_id, seq).stdout, request_id=request_id
            )
            if not created:
                return turn, False
            files = self.files(session_id, turn.seq)
            try:
                # Read after the claim: the previous turn may have recorded Codex's thread id.
                session = self.store.session(session_id)
                # Claude is resumed only once a turn completed; after a stopped or failed first turn
                # the conversation may or may not exist, and the waiter settles that (`_resume_instead`).
                established = any(
                    earlier.state == COMPLETED
                    for earlier in self.store.turns(session_id)
                    if earlier.seq < turn.seq
                )
                argv = self.argv(session, files, established=established)
                files.directory.mkdir(parents=True, exist_ok=True)
                files.prompt.write_text(text, encoding="utf-8")
            except Exception as exc:
                self._abandon(
                    session_id, turn.seq, None, f"could not prepare the turn: {type(exc).__name__}: {exc}"
                )
                raise
            process = self._launch(session, turn.seq, argv, files)
            try:
                thread = threading.Thread(
                    target=self._wait,
                    args=(session, turn.seq, process, argv, files),
                    name=f"po-turn-{session_id}-{turn.seq}",
                    daemon=True,
                )
                self._live[(session_id, turn.seq)] = _Live(process, thread)
                thread.start()
            except BaseException as exc:
                self._live.pop((session_id, turn.seq), None)
                self._abandon(
                    session_id,
                    turn.seq,
                    process,
                    f"the turn's waiter did not start: {type(exc).__name__}: {exc}",
                )
                raise
        return self.store.turn(session_id, turn.seq), True

    def _launch(
        self, session: Session, seq: int, argv: list[str], files: TurnFiles
    ) -> subprocess.Popen[bytes]:
        """Start one CLI process for a turn and record it, or leave no live process group behind.

        Output is appended, so a turn relaunched by `_resume_instead` keeps both attempts' raw output.
        """
        try:
            with (
                files.prompt.open("rb") as stdin,
                files.stdout.open("ab") as stdout,
                files.stderr.open("ab") as stderr,
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
            self._abandon(session.session_id, seq, None, reason)
            raise RunnerError(reason) from None
        try:
            if not self.store.record_process(
                session.session_id, seq, process.pid, process_identity(process.pid)
            ):
                raise RunnerError(
                    f"turn {seq} of PO session {session.session_id} was settled while it started"
                )
        except BaseException as exc:
            self._abandon(
                session.session_id,
                seq,
                process,
                f"the turn's process could not be recorded, so it was killed: {type(exc).__name__}: {exc}",
            )
            raise
        return process

    def _abandon(
        self, session_id: str, seq: int, process: subprocess.Popen[bytes] | None, reason: str
    ) -> None:
        """Kill and reap a turn's process group, then settle the turn `failed` if the store answers.

        If it does not, the row stays `running` with no recorded process, and `recover()` marks it
        `interrupted` later; there is nothing left alive for it to kill.
        """
        if process is not None:
            _kill_group(process.pid)
            try:
                process.wait(STOP_JOIN_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        try:
            self.store.finish_turn(session_id, seq, FAILED, reason)
        except Exception as exc:  # noqa: BLE001 - the unsettled row is recover()'s to settle
            print(
                f"secretary po: turn {session_id}/{seq} left running for recovery: {reason}; "
                f"settling it failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def stop(self, session_id: str) -> Turn | None:
        """Kill the running turn's process group; the turn is `interrupted`, the session goes on."""
        return self._interrupt(session_id, None)

    def stop_turn(self, session_id: str, seq: int) -> Turn | None:
        """`stop`, but only when turn `seq` is the one running: a stop form left open stops nothing newer.

        The check and the kill happen under the same lock, so a turn started in between is never hit.
        """
        return self._interrupt(session_id, seq)

    def _interrupt(self, session_id: str, seq: int | None) -> Turn | None:
        session = self.store.session(session_id)
        with self._lock:
            running = self.store.running_turns(session_id)
            if not running or (seq is not None and running[0].seq != seq):
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
                    turn.pid and turn.process_identity and process_identity(turn.pid) == turn.process_identity
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
        self,
        session: Session,
        seq: int,
        process: subprocess.Popen[bytes],
        argv: list[str],
        files: TurnFiles,
    ) -> None:
        try:
            code = process.wait()
            relaunched = self._resume_instead(session, seq, code, argv, files)
            if relaunched is not None:
                code = relaunched.wait()
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

    def _resume_instead(
        self, session: Session, seq: int, code: int, argv: list[str], files: TurnFiles
    ) -> subprocess.Popen[bytes] | None:
        """Relaunch as `--resume` when Claude says an earlier stopped or failed turn saved the conversation.

        Claude Code 2.1.270 answers `--session-id` over an existing conversation with
        `Error: Session ID <uuid> is already in use.` and exit 1, and `--resume` over a missing one with
        `No conversation found with session ID: <uuid>`. Only the first can follow a turn that never
        completed, and it is retried here, inside the same turn.
        """
        if session.cli != "claude" or code == 0 or "--session-id" not in argv:
            return None
        if CLAUDE_SESSION_IN_USE not in self._stderr_tail(files):
            return None
        with self._lock:
            if self.store.turn(session.session_id, seq).state != RUNNING:
                return None
            process = self._launch(session, seq, self.argv(session, files, established=True), files)
            live = self._live.get((session.session_id, seq))
            if live is not None:
                live.process = process
        return process

    def _settle(self, session: Session, seq: int, code: int, files: TurnFiles) -> None:
        stdout = files.stdout.read_bytes().decode("utf-8", errors="replace")
        self._capture_thread_id(session, files.stdout, stdout)
        if session.cli == "claude":
            answer, missing = claude_final_answer(stdout)
            resolved = claude_resolved_model(stdout)
        else:
            answer, missing = self._codex_final_answer(files)
            resolved = codex_resolved_model(
                self.codex_home(), session.cli_session_id or codex_thread_id(stdout)
            )
        if code != 0:
            reason = f"{session.cli} exited with status {code}"
            tail = self._stderr_tail(files)
            if tail:
                reason += f": {tail}"
            self.store.finish_turn(session.session_id, seq, FAILED, reason, resolved_model=resolved)
        elif answer is None:
            self.store.finish_turn(
                session.session_id, seq, FAILED, missing or "no final answer", resolved_model=resolved
            )
        else:
            self.store.complete_turn(session.session_id, seq, answer, resolved_model=resolved)

    def codex_home(self) -> Path:
        """The Codex home a turn runs with: `$CODEX_HOME` of the turn environment, else `~/.codex`."""
        configured = self.env.get("CODEX_HOME")
        if configured:
            return Path(configured)
        return Path(self.env.get("HOME") or Path.home()) / ".codex"

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
    "claude_resolved_model",
    "codex_resolved_model",
    "codex_thread_id",
    "process_identity",
    "runs_dir",
    "turn_environment",
]
