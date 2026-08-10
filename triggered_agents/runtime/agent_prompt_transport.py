"""Serialized terminal input for one interactive agent prompt.

``orca terminal send`` is the public ingress available to Secretary.  Its generic
``--text ... --enter`` form is not safe for a large Codex paste: Codex can retain the
paste in its composer while consuming Enter.  This module owns the replacement protocol
so callers cannot assemble escape sequences, body writes and submissions differently.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pane_host import PaneHost, pane_host as resolve_pane_host
from .tui_delivery_types import RunJson


AGENT_PROMPT_TRANSPORT_VERSION = "agent-prompt-v2"
# ``orca terminal send --text`` receives the body as one argv element.  Linux restricts one
# argument to 128 KiB even where ``ARG_MAX`` is larger, and other supported POSIX hosts have
# comparable limits.  Keep a substantial margin below that hard boundary rather than advertise a
# size the public ingress cannot actually execute.  The framing bytes count against this limit.
AGENT_PROMPT_MAX_BYTES = 64 * 1024
AGENT_PROMPT_SUBMIT_DELAY_S = float(os.environ.get("SECRETARY_AGENT_PROMPT_SUBMIT_DELAY_S", "0.5"))
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
_ALLOWED_C0 = frozenset({"\t", "\n"})
TRANSPORT_POLICY = "normalize-newlines-reject-c0-esc"


@dataclass(frozen=True)
class PreparedAgentPrompt:
    """A validated prompt and the one body form the terminal receives."""

    text: str
    adapter: str
    body: str
    framing: str
    policy: str = TRANSPORT_POLICY


@dataclass
class PromptTransportReceipt:
    """Metadata-only evidence for the body and its separate submit write."""

    transport_version: str = AGENT_PROMPT_TRANSPORT_VERSION
    adapter: str = ""
    framing: str = ""
    policy: str = TRANSPORT_POLICY
    body_write_accepted: bool = False
    body_bytes_written: int = 0
    body_write_count: int = 0
    submit_write_accepted: bool = False
    submit_bytes_written: int = 0
    submit_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "transport_version": self.transport_version,
            "adapter": self.adapter,
            "framing": self.framing,
            "transport_policy": self.policy,
            "body_write_accepted": self.body_write_accepted,
            "body_bytes_written": self.body_bytes_written,
            "body_write_count": self.body_write_count,
            "submit_write_accepted": self.submit_write_accepted,
            "submit_bytes_written": self.submit_bytes_written,
            "submit_count": self.submit_count,
        }


class AgentPromptTransportError(RuntimeError):
    """A rejected prompt or a failed public-terminal write with its receipt."""

    def __init__(self, reason: str, receipt: PromptTransportReceipt) -> None:
        super().__init__(reason)
        self.reason = reason
        self.receipt = receipt


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}


def prepare_agent_prompt(text: str, *, adapter: str) -> PreparedAgentPrompt:
    """Validate prompt data before any terminal interaction and choose its wire form.

    Line endings are normalised first and everything else is judged after.  A carriage return is
    not an instruction: the board's own web form submits every textarea with CRLF, so a card
    edited there would otherwise make each launch over that card a permanent transport rejection
    — a worse outcome than the unframed send this replaces.  Rewriting CR to LF changes no
    instruction and cannot forge the frame, whose delimiters are ESC-introduced.

    Past that, the explicit policy rejects ESC and every remaining C0 control.  Replacing those
    would make a user-visible prompt differ from the durable task document; rejection keeps the
    framing delimiter unforgeable without silently changing an instruction.
    """
    normalized_adapter = str(adapter or "").lower()
    receipt = PromptTransportReceipt(adapter=normalized_adapter)
    if not isinstance(text, str):
        raise AgentPromptTransportError("prompt-body-invalid", receipt)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 0x20 and char not in _ALLOWED_C0 for char in text):
        raise AgentPromptTransportError("prompt-body-rejected-control", receipt)
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise AgentPromptTransportError("prompt-body-invalid-unicode", receipt) from None
    if normalized_adapter == "codex":
        body = f"{BRACKETED_PASTE_START}{text}{BRACKETED_PASTE_END}"
        framing = "bracketed-paste-v1"
    else:
        body = text
        framing = "plain-v1"
    if len(body.encode("utf-8", "strict")) > AGENT_PROMPT_MAX_BYTES:
        raise AgentPromptTransportError("prompt-body-too-large", receipt)
    return PreparedAgentPrompt(text=text, adapter=normalized_adapter, body=body, framing=framing)


def send_agent_prompt(
    handle: str,
    prompt: PreparedAgentPrompt,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    submit_only: bool = False,
) -> PromptTransportReceipt:
    """Write one body and one separate submission while owning this terminal's ingress.

    A process-wide lock plus a per-process advisory file lock covers both the body and Enter.  The
    session manager is asked for two writes and never one: no caller gets to combine them and no
    other Secretary delivery can put bytes between them.
    """
    pane = resolve_pane_host(run_json, host=host)
    receipt = PromptTransportReceipt(adapter=prompt.adapter, framing=prompt.framing)
    with terminal_prompt_lock(handle):
        if not submit_only:
            receipt.body_write_count = 1
            try:
                answer = pane.send(handle, prompt.body, enter=False)
            except Exception as exc:
                raise AgentPromptTransportError("transport-refused-body-write", receipt) from exc
            _record_write(receipt, "body", answer)
            if not receipt.body_write_accepted:
                raise AgentPromptTransportError("body-write-refused", receipt)
            # This is the same body/submit settling interval Orca's private agent-prompt helper
            # uses.  Retain it when adapting Claude's raw body too: generic `--text --enter`
            # already provided it server-side.
            time.sleep(max(AGENT_PROMPT_SUBMIT_DELAY_S, 0.0))
        receipt.submit_count = 1
        try:
            answer = pane.send(handle, "", enter=True)
        except Exception as exc:
            raise AgentPromptTransportError("transport-refused-submit-write", receipt) from exc
        _record_write(receipt, "submit", answer)
        if not receipt.submit_write_accepted:
            raise AgentPromptTransportError("submit-write-refused", receipt)
    return receipt


def _record_write(receipt: PromptTransportReceipt, kind: str, answer: Any) -> None:
    body = answer.get("send") if isinstance(answer, dict) and isinstance(answer.get("send"), dict) else answer
    accepted = True
    bytes_written = 0
    if isinstance(body, dict):
        if body.get("accepted") is not None:
            accepted = bool(body.get("accepted"))
        try:
            bytes_written = max(0, int(body.get("bytesWritten") or 0))
        except (TypeError, ValueError):
            pass
    if kind == "body":
        receipt.body_write_accepted = accepted
        receipt.body_bytes_written += bytes_written
    else:
        receipt.submit_write_accepted = accepted
        receipt.submit_bytes_written += bytes_written


@contextmanager
def terminal_prompt_lock(handle: str):
    """Serialise one terminal's prompt pairs across threads and local processes."""
    key = hashlib.sha256(str(handle).encode("utf-8", "replace")).hexdigest()
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(key, threading.RLock())
    with lock:
        directory = Path(tempfile.gettempdir()) / "secretary-agent-prompt-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(directory / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
