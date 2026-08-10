"""The product's one way to put a prompt in front of a live interactive head.

Every head that runs as a TUI — a pipeline worker, a reviewer, an observer, and the curator, retro
and steward service heads — is brought up with a command that carries no prompt, so something has
to type it into the pane afterwards. That "afterwards" is the whole problem: `terminal send`
succeeding only means Orca accepted keystrokes, a pane can still be painting, working, or holding
a dialog that swallows what it was given. The answer is one readiness classification, one resend
policy, one confirmation boundary and one failure, here — not one per caller.

It lives in `runtime` rather than next to either caller because both sides need it and only one of
them may import the other: the dispatcher (`secretary`) already reads this package, and the
triggered-agents tick cannot read `secretary` back. Nothing here knows about boards, roles or
sessions; a caller passes the way it runs Orca and the criterion that proves its own head took the
prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .agent_prompt_transport import (
    AGENT_PROMPT_TRANSPORT_VERSION,
    TRANSPORT_POLICY,
    AgentPromptTransportError,
    PreparedAgentPrompt,
    PromptTransportReceipt,
    prepare_agent_prompt,
    send_agent_prompt,
)
from .pane_host import PaneHost, pane_host as resolve_pane_host
from .tui_delivery_types import RunJson


TUI_IDLE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_TIMEOUT_MS", "60000")))
# Orca decides `tui-idle` from the pane's agent status and, failing that, from a quiescence window
# it polls. A probe shorter than that window would report every quiet pane as busy, so it is set
# above both rather than tuned for the fastest answer.
TUI_IDLE_PROBE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_PROBE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_PROBE_TIMEOUT_MS", "6000")))
TUI_DELIVERY_RETRIES = int(os.environ.get("SECRETARY_TUI_DELIVERY_RETRIES", os.environ.get("TA_TUI_DELIVERY_RETRIES", "2")))
TUI_DELIVERY_TIMEOUT_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12")))
TUI_DELIVERY_POLL_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_POLL_S", os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25")))
TUI_DELIVERY_RESEND_GRACE_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_RESEND_GRACE_S", os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1")))
# How much of a pane is fingerprinted. The screen is read for evidence, never for content, so the
# bound is on the input to the digest rather than on anything that is kept.
TUI_FINGERPRINT_LIMIT = 4000


_WAIT_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"([a-z_]+)"')
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The prompt markers the interactive heads of this product paint: Codex's `›` and Claude's `❯`.
COMPOSER_MARKERS = ("›", "❯")
# Codex covers a large paste with a placeholder instead of the text. A composer holding one is the
# exact shape of the failure this boundary exists for, so it is classified rather than only hashed.
_PASTE_RE = re.compile(r"pasted?\s+content", re.IGNORECASE)


# What one delivery attempt achieved. `accepted` means the pane took the prompt into a turn while
# the caller's own proof of delivery is expected to arrive later, outside this call.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"

# How far one delivery got, as four things that happen in order and are observed separately.
# `terminal send` answering `accepted: true` with a byte count proves only the first of them: the
# payload was written into the pane. A pane that is holding that payload in its composer, with or
# without a paste placeholder over it, has reached STAGE_PAYLOAD_WRITTEN and no further, and Orca
# will call it `tui-idle` the whole time because nothing is working in it.
STAGE_NONE = "none"
STAGE_PAYLOAD_WRITTEN = "payload_written"
STAGE_ENTER_ACCEPTED = "enter_accepted"
STAGE_TURN_OBSERVED = "turn_observed"
STAGE_ACKNOWLEDGED = "acknowledged"

_STAGE_ORDER = (
    STAGE_NONE,
    STAGE_PAYLOAD_WRITTEN,
    STAGE_ENTER_ACCEPTED,
    STAGE_TURN_OBSERVED,
    STAGE_ACKNOWLEDGED,
)

# What the composer was holding, as an answer that carries no prompt text. `unknown` is a pane
# whose screen could not be read or has no composer marker on it; the delivery then falls back to
# readiness alone, which is what this path had before the fingerprint existed.
COMPOSER_UNKNOWN = "unknown"
COMPOSER_EMPTY = "empty"

# What Orca answered about a pane. `blocked` is a pane held in a dialog: not ready for a prompt,
# and not working on one either. `unknown` is the probe failing, which is not a busy head.
READINESS_READY = "ready"
READINESS_BUSY = "busy"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"


@dataclass
class DeliveryEvidence:
    """What one delivery attempt saw, in a form that can be persisted beside the head.

    Everything here is either an identifier, a bounded classification or a digest. The prompt
    itself is represented by its size and its hash and never by its text: these records outlive
    the head they were taken on, and a sprint's durable telemetry is not a place to keep the
    contents of a prompt.
    """

    handle: str = ""
    subject: str = ""
    stage: str = STAGE_NONE
    payload_bytes: int = 0
    payload_sha256: str = ""
    # The public terminal-send adapter that carried the prompt.  The body and its submission are
    # intentionally recorded independently: neither write acceptance is proof the head began a
    # turn, which remains the later confirmation stages below.
    transport_version: str = AGENT_PROMPT_TRANSPORT_VERSION
    adapter: str = ""
    framing: str = ""
    transport_policy: str = TRANSPORT_POLICY
    body_write_accepted: bool = False
    body_bytes_written: int = 0
    body_write_count: int = 0
    submit_write_accepted: bool = False
    submit_bytes_written: int = 0
    submit_count: int = 0
    turn_confirmed: bool = False
    # `accepted`/`bytesWritten` as Orca answered the send, kept because they are what used to be
    # mistaken for delivery and are now one stage of it.
    send_accepted: bool = False
    bytes_written: int = 0
    # One attempt is one Enter: the first send and every re-entry after it.
    attempts: int = 0
    resends: int = 0
    readiness_before: str = ""
    readiness_after: str = ""
    composer_before: str = COMPOSER_UNKNOWN
    composer_after: str = COMPOSER_UNKNOWN
    payload_left_in_composer: bool = False
    modal_before: bool = False
    modal_after: bool = False
    cursor_before: str = ""
    cursor_after: str = ""
    cursor_moved: bool = False
    # Whether those cursors are Orca's own or the tail digest that stands in when a runtime
    # answers a read without one: a reader of this evidence must not mistake the second for the
    # first when it asks why a turn was or was not seen.
    cursor_from_backend: bool = False
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "subject": self.subject,
            "stage": self.stage,
            "payload_bytes": self.payload_bytes,
            "payload_sha256": self.payload_sha256,
            "transport_version": self.transport_version,
            "adapter": self.adapter,
            "framing": self.framing,
            "transport_policy": self.transport_policy,
            "body_write_accepted": self.body_write_accepted,
            "body_bytes_written": self.body_bytes_written,
            "body_write_count": self.body_write_count,
            "submit_write_accepted": self.submit_write_accepted,
            "submit_bytes_written": self.submit_bytes_written,
            "submit_count": self.submit_count,
            "turn_confirmed": self.turn_confirmed,
            "send_accepted": self.send_accepted,
            "bytes_written": self.bytes_written,
            "attempts": self.attempts,
            "resends": self.resends,
            "readiness_before": self.readiness_before,
            "readiness_after": self.readiness_after,
            "composer_before": self.composer_before,
            "composer_after": self.composer_after,
            "payload_left_in_composer": self.payload_left_in_composer,
            "modal_before": self.modal_before,
            "modal_after": self.modal_after,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "cursor_moved": self.cursor_moved,
            "cursor_from_backend": self.cursor_from_backend,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, payload: Any) -> "DeliveryEvidence":
        if not isinstance(payload, dict):
            return cls()
        fields = cls()
        for name, value in payload.items():
            if not hasattr(fields, name):
                continue
            current = getattr(fields, name)
            if isinstance(current, bool):
                setattr(fields, name, bool(value))
            elif isinstance(current, int):
                try:
                    setattr(fields, name, int(value))
                except (TypeError, ValueError):
                    pass
            else:
                setattr(fields, name, str(value or ""))
        return fields


class DeliveryOutcome(str):
    """The delivery verdict a caller compares, carrying the evidence that produced it.

    It is a string because that is what every caller of this path already reads, and the verdict
    is the only thing most of them want. A caller that has durable telemetry to write reads
    `.evidence` off the same value instead of asking the pane a second time.
    """

    evidence: DeliveryEvidence

    def __new__(cls, value: str, evidence: DeliveryEvidence) -> "DeliveryOutcome":
        outcome = super().__new__(cls, value)
        outcome.evidence = evidence
        return outcome


class TuiDeliveryError(RuntimeError):
    """A delivery that did not reach its confirmation, with what was seen of it attached."""

    def __init__(self, message: str, *, evidence: DeliveryEvidence | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence if evidence is not None else DeliveryEvidence(reason=message)


@dataclass
class PaneRead:
    """One `terminal read`: the retained tail, and the backend's own position in the output.

    The cursor is Orca's `nextCursor` — the opaque token it gives a reader to ask for "only what
    came after this". It is the authority on whether the pane printed anything, and the tail is
    not: the retained window is bounded, so a quick turn can append output and leave the tail it
    returns identical, and repaint-heavy TUI output does the same. `cursor_known` is false only
    when the runtime answered without one, and then, and only then, a digest of the tail stands in.
    """

    text: str = ""
    cursor: str = ""
    cursor_known: bool = False
    read: bool = False


@dataclass
class PaneProbe:
    """One bounded look at a pane: is it ready, what is it holding, and has it printed since."""

    readiness: str = READINESS_UNKNOWN
    composer: str = COMPOSER_UNKNOWN
    cursor: str = ""
    # True when the cursor above is Orca's own, false when it is the tail digest standing in for
    # one. Two probes are only comparable when they answered the same way.
    cursor_from_backend: bool = False
    screen_read: bool = False

    @property
    def modal(self) -> bool:
        return self.readiness == READINESS_BLOCKED


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def payload_fingerprint(prompt: str) -> tuple[int, str]:
    """The size and hash of a payload, which is all of it that is ever recorded."""
    raw = (prompt or "").encode("utf-8", "replace")
    return len(raw), _digest(prompt or "")


def read_pane(handle: str, *, run_json: RunJson | None = None, host: PaneHost | None = None) -> PaneRead:
    """One pane read: the tail, ANSI stripped, and the session manager's output cursor with it.

    A pane that cannot be read is not a failure here: it costs the delivery its composer and
    cursor evidence and leaves it on readiness alone, which is strictly what it had before.
    """
    try:
        data = resolve_pane_host(run_json, host=host).read(handle)
    except Exception:
        return PaneRead()
    terminal = data.get("terminal") if isinstance(data, dict) and isinstance(data.get("terminal"), dict) else data
    if not isinstance(terminal, dict):
        return PaneRead()
    text = ""
    tail = terminal.get("tail")
    if isinstance(tail, list):
        text = strip_ansi("\n".join(str(line) for line in tail))
    else:
        for key in ("text", "content", "screen"):
            value = terminal.get(key)
            if isinstance(value, str):
                text = strip_ansi(value)
                break
    # `nextCursor` is what a reader passes back to get only later output, so it is the position
    # this pane has reached. `latestCursor` is the same position for a read that returned
    # everything, and is taken only when the first is absent.
    cursor = ""
    for key in ("nextCursor", "latestCursor"):
        value = terminal.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            cursor = str(value)[:64]
            break
    return PaneRead(text=text, cursor=cursor, cursor_known=bool(cursor), read=bool(terminal))


def read_pane_text(handle: str, *, run_json: RunJson) -> str:
    """The pane's text alone, for callers that read a screen rather than a position."""
    return read_pane(handle, run_json=run_json).text


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def composer_fingerprint(screen: str) -> str:
    """What the composer is holding, as a classification, a length and a digest.

    The composer is the region after the last prompt marker a TUI paints — Codex's `›`, Claude's
    `❯`. A pane that pasted the payload and never entered it shows exactly that: a composer whose
    fingerprint changed across the send and is not empty. The text itself is hashed, never kept,
    and the region is bounded before it is hashed.
    """
    if not screen:
        return COMPOSER_UNKNOWN
    marker = max(screen.rfind(char) for char in COMPOSER_MARKERS)
    if marker < 0:
        return COMPOSER_UNKNOWN
    held = " ".join(screen[marker + 1:][:TUI_FINGERPRINT_LIMIT].split())
    if not held:
        return COMPOSER_EMPTY
    kind = "paste" if _PASTE_RE.search(held) else "text"
    return f"{kind}:{len(held)}:{_digest(held)}"


def output_cursor(read: PaneRead) -> tuple[str, bool]:
    """Where the pane's output has got to, and whether that came from the backend.

    Orca's own cursor is used whenever it answers with one, opaque and unparsed: it advances when
    the pane printed, which a retained tail does not have to. Comparing a digest of that tail
    instead would miss a turn whose output fell outside the window or repainted over itself, and
    it is only what stands in when the runtime returns no cursor at all.
    """
    if read.cursor_known:
        return f"orca:{read.cursor}", True
    if not read.text:
        return "", False
    marker = max(read.text.rfind(char) for char in COMPOSER_MARKERS)
    output = read.text[:marker] if marker >= 0 else read.text
    output = output[-TUI_FINGERPRINT_LIMIT:]
    return f"tail:{len(output)}:{_digest(output)}", False


def probe_pane(handle: str, *, run_json: RunJson) -> PaneProbe:
    """Readiness, composer and output cursor in one look, for the evidence of one attempt."""
    readiness = terminal_readiness(handle, run_json=run_json)
    read = read_pane(handle, run_json=run_json)
    cursor, from_backend = output_cursor(read)
    return PaneProbe(
        readiness=readiness,
        composer=composer_fingerprint(read.text),
        cursor=cursor,
        cursor_from_backend=from_backend,
        screen_read=bool(read.text),
    )


def _advance(evidence: DeliveryEvidence, stage: str) -> None:
    if _STAGE_ORDER.index(stage) > _STAGE_ORDER.index(evidence.stage):
        evidence.stage = stage


def wait_for_tui_idle(
    handle: str, *, run_json: RunJson | None = None, host: PaneHost | None = None,
    timeout_ms: int | None = None,
) -> None:
    """Wait until the session manager reports the pane ready for input; a refusal reaches the caller.

    This is also what a freshly created head is given to come up in: a TUI paints, reads its
    config and answers the readiness probe well after the pty exists, and the wait for that is
    the same wait as for a pane that is merely busy.
    """
    resolve_pane_host(run_json, host=host).wait_idle(
        handle, timeout_ms=TUI_IDLE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    )


def terminal_readiness(
    handle: str, *, run_json: RunJson | None = None, host: PaneHost | None = None,
    timeout_ms: int | None = None,
) -> str:
    """Ask whether the pane is ready for input, and answer in three states, not two.

    This is the one readiness question the product asks about an interactive head, whatever
    provider runs in it: the runtime derives it from the pane's own agent status and falls back to
    a quiescence window, so no screen is read here.

    `READINESS_BUSY` is the condition not being met by a pane that is working, which Orca reports
    as a satisfied-false answer or as a failed command carrying `code: timeout`. A pane it names a
    `blockedReason` for is `READINESS_BLOCKED`: also not ready, but held in a dialog rather than
    working, so a prompt sent to it went nowhere. `READINESS_UNKNOWN` is the probe itself failing,
    and it must not be read as an ordinary busy head: a caller that cannot ask the question is not
    looking at a working observer, it is looking at nothing.
    """
    try:
        data = resolve_pane_host(run_json, host=host).wait_idle(
            handle, timeout_ms=TUI_IDLE_PROBE_TIMEOUT_MS if timeout_ms is None else timeout_ms
        )
    except Exception as exc:
        return _refused_wait_readiness(exc)
    wait = data.get("wait") if isinstance(data, dict) and isinstance(data.get("wait"), dict) else data
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    return READINESS_READY


def _refused_wait_readiness(exc: Exception) -> str:
    """Classify a `terminal wait` the host refused, from the body Orca printed with it.

    The CLI exits non-zero both for a condition it could not satisfy and for a failure, and the
    host turns the two into the same exception, so the answer is in the text rather than in the
    outcome. It prints that text as JSON, and the host carries it into the failure it raises:

      * a `wait` object saying `satisfied: false` is a pane Orca has looked at and found working
        or blocked behind a dialog. That is busy, and busy waits for readiness;
      * `code: timeout` is the same condition not being met before the probe's own deadline;
      * anything else, a body that cannot be read included, is a probe that was never answered.
    """
    body = _json_object(str(exc))
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    wait = result.get("wait") if isinstance(result, dict) else None
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if not code:
        # A body too damaged to parse can still carry its code in the text.
        codes = _WAIT_ERROR_CODE_RE.findall(str(exc))
        code = codes[-1] if codes else ""
    return READINESS_BUSY if code == "timeout" else READINESS_UNKNOWN


def _answered_readiness(wait: dict[str, Any]) -> str:
    if wait.get("satisfied"):
        return READINESS_READY
    return READINESS_BLOCKED if wait.get("blockedReason") else READINESS_BUSY


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        parsed = json.loads(text[start:])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def deliver_interactive_prompt(
    handle: str,
    prompt: str,
    *,
    run_json: RunJson,
    adapter: str = "",
    confirm: Callable[[float], bool] | None = None,
    ack_out_of_band: bool = False,
    subject: str = "",
) -> DeliveryOutcome:
    """Deliver a prompt into a live interactive head, on one path for every role that has one.

    Delivery is four things, and every one of them is observed separately:

      1. the payload is written into the pane — `terminal send` answering `accepted` with a byte
         count, and nothing more than that;
      2. the Enter is taken — the composer no longer holds what the send put there;
      3. a turn is observed — the pane went to work, or it printed output it had not printed
         before, which is the same evidence for a turn that ended between two probes;
      4. the caller's own criterion acknowledges it.

    The failure this boundary exists for lives between 1 and 2: Codex answers `accepted: true` with
    the byte count, leaves the payload in its composer under a paste placeholder, and answers
    `tui-idle` satisfied the whole time, because a pane holding a composer really is idle. So a
    send that reports bytes and a pane that reports idle are not delivery, here or anywhere else
    in the product; the pre/post fingerprints of the composer and of the output are.

    Callers pass `confirm`, the criterion they always had: their head's turn having visibly
    started, which is stage 4. A caller whose proof arrives later sets `ack_out_of_band` and passes
    no callback at all; it gets `DELIVERY_ACCEPTED` once stage 3 is evidenced, which is the same
    rule with only the last step left out, not a weaker one.

    The verdict comes back with the evidence of the attempt attached, and so does the failure —
    including a failure of the transport itself. Once this call has an evidence record, every way
    out of it carries that record: a `terminal wait` or `terminal send` the host refuses is a
    delivery that did not happen, and it is reported with the terminal, the payload fingerprint and
    the stage reached rather than as a bare host error a caller would have to persist as prose.
    """
    if ack_out_of_band and confirm is not None:
        raise ValueError("out-of-band delivery cannot use a synchronous confirmation callback")
    if confirm is None and not ack_out_of_band:
        raise ValueError("interactive delivery requires a confirmation criterion")
    payload_bytes, payload_hash = payload_fingerprint(prompt)
    evidence = DeliveryEvidence(
        handle=handle,
        subject=subject,
        payload_bytes=payload_bytes,
        payload_sha256=payload_hash,
    )
    try:
        prepared = prepare_agent_prompt(prompt, adapter=adapter)
    except AgentPromptTransportError as exc:
        _record_transport_receipt(evidence, exc.receipt)
        evidence.reason = exc.reason
        raise TuiDeliveryError(
            f"prompt transport rejected the body (reason={exc.reason})", evidence=evidence
        ) from None
    # The fingerprint is evidence about the bytes the pane was given, so it follows the transport's
    # own normalisation rather than the caller's text. A rejected prompt keeps the caller's, which
    # is the only thing that ever existed on that path.
    evidence.payload_bytes, evidence.payload_sha256 = payload_fingerprint(prepared.text)
    with _transport_evidence(evidence, "wait-for-readiness"):
        wait_for_tui_idle(handle, run_json=run_json)
    before = probe_pane(handle, run_json=run_json)
    evidence.readiness_before = before.readiness
    evidence.composer_before = before.composer
    evidence.modal_before = before.modal
    evidence.cursor_before = before.cursor
    evidence.cursor_from_backend = before.cursor_from_backend
    sent_at = time.time()
    with _transport_evidence(evidence, "send-payload"):
        _send_payload(handle, prepared, run_json=run_json, evidence=evidence)
    return _confirm_interactive_turn(
        handle,
        prepared,
        sent_at,
        before,
        run_json=run_json,
        confirm=confirm,
        ack_out_of_band=ack_out_of_band,
        evidence=evidence,
    )


@contextmanager
def _transport_evidence(evidence: DeliveryEvidence, step: str):
    """Turn a refused Orca call inside the delivery into an evidence-carrying delivery failure.

    A `terminal wait` or `terminal send` the host will not perform is exactly as much a delivery
    that did not happen as a pane that swallowed the prompt, and the caller persists both the same
    way. Without this the host's own exception escapes with nothing attached and the record keeps a
    count and a sentence — and, worse, whatever evidence an earlier failure happened to leave.

    `ValueError` is not caught: the two argument refusals this path raises are programming errors
    in the caller, not deliveries.
    """
    try:
        yield
    except (TuiDeliveryError, ValueError):
        raise
    except Exception as exc:
        evidence.reason = f"transport-refused-{step}"
        raise TuiDeliveryError(
            f"the {step} step of prompt delivery was refused (stage={evidence.stage}): {exc}",
            evidence=evidence,
        ) from None


def _send_payload(
    handle: str,
    prompt: PreparedAgentPrompt,
    *,
    run_json: RunJson,
    evidence: DeliveryEvidence,
    submit_only: bool = False,
) -> None:
    """Run the one provider-aware body/submit transport and merge its metadata-only receipt."""
    try:
        receipt = send_agent_prompt(
            handle, prompt, run_json=run_json, submit_only=submit_only
        )
    except AgentPromptTransportError as exc:
        _record_transport_receipt(evidence, exc.receipt)
        if exc.receipt.body_write_accepted:
            _advance(evidence, STAGE_PAYLOAD_WRITTEN)
        if exc.receipt.submit_count:
            evidence.attempts += 1
        evidence.reason = exc.reason
        raise TuiDeliveryError(
            f"the prompt transport refused {exc.reason} (stage={evidence.stage})", evidence=evidence
        ) from None
    _record_transport_receipt(evidence, receipt)
    if receipt.submit_count:
        evidence.attempts += 1
    if receipt.body_write_accepted:
        _advance(evidence, STAGE_PAYLOAD_WRITTEN)


def _record_transport_receipt(evidence: DeliveryEvidence, receipt: PromptTransportReceipt) -> None:
    """Accumulate the transport's body/submit facts without preserving prompt text."""
    evidence.transport_version = receipt.transport_version
    evidence.adapter = receipt.adapter
    evidence.framing = receipt.framing
    evidence.transport_policy = receipt.policy
    if receipt.body_write_count:
        evidence.body_write_accepted = receipt.body_write_accepted
        evidence.body_bytes_written += receipt.body_bytes_written
        evidence.body_write_count += receipt.body_write_count
    evidence.submit_write_accepted = receipt.submit_write_accepted
    evidence.submit_bytes_written += receipt.submit_bytes_written
    evidence.submit_count += receipt.submit_count
    # Compatibility telemetry still means the body write, not a successful delivery.
    if receipt.body_write_count:
        evidence.send_accepted = receipt.body_write_accepted
        evidence.bytes_written += receipt.body_bytes_written


def _confirm_interactive_turn(
    handle: str,
    prompt: PreparedAgentPrompt,
    sent_at: float,
    before: PaneProbe,
    *,
    run_json: RunJson,
    confirm: Callable[[float], bool] | None,
    ack_out_of_band: bool = False,
    evidence: DeliveryEvidence,
) -> DeliveryOutcome:
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    while time.monotonic() < deadline:
        if confirm is not None and confirm(sent_at):
            _advance(evidence, STAGE_TURN_OBSERVED)
            _advance(evidence, STAGE_ACKNOWLEDGED)
            evidence.turn_confirmed = True
            evidence.reason = ""
            return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)
        probe = probe_pane(handle, run_json=run_json)
        _record_probe(evidence, before, probe)
        if probe.readiness == READINESS_UNKNOWN:
            # Not a swallowed prompt and not a working head: the pane cannot be asked at all.
            # Guessing either way here would hide the failure the caller has to act on.
            evidence.reason = "pane-unprobeable"
            raise TuiDeliveryError(
                f"the pane could not be probed after the prompt was sent "
                f"(stage={evidence.stage}, resends={evidence.resends})",
                evidence=evidence,
            )
        if probe.readiness == READINESS_BUSY or (
            evidence.cursor_moved and not evidence.payload_left_in_composer
        ):
            # The pane went to work, or it printed something it had not printed before the send
            # and is no longer holding the payload. Either is a turn: the second one is how a head
            # that answered between two probes looks, and reading that as a pane which stayed idle
            # is what made a delivered wake report as a failure.
            _advance(evidence, STAGE_ENTER_ACCEPTED)
            _advance(evidence, STAGE_TURN_OBSERVED)
            evidence.turn_confirmed = True
            if ack_out_of_band:
                evidence.reason = ""
                return DeliveryOutcome(DELIVERY_ACCEPTED, evidence)
        if (
            probe.readiness != READINESS_BUSY
            and evidence.resends < TUI_DELIVERY_RETRIES
            and time.monotonic() >= next_resend_at
        ):
            # Ready or held in a dialog: either way the pane is not working on this prompt, so it
            # is entered again. A composer still holding the payload needs the Enter alone, which
            # is what carries a prompt past a dialog that swallowed it; a composer that is empty
            # with nothing having happened is a pane the payload never reached, so it is written
            # again. A pane whose screen cannot be read gets the bare Enter it always got.
            with _transport_evidence(evidence, "resend-payload"):
                _send_payload(
                    handle,
                    prompt,
                    run_json=run_json,
                    evidence=evidence,
                    submit_only=evidence.payload_left_in_composer or not probe.screen_read,
                )
            evidence.resends += 1
            next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
        time.sleep(max(TUI_DELIVERY_POLL_S, 0.01))
    evidence.reason = _failure_reason(evidence)
    raise TuiDeliveryError(
        f"interactive prompt delivery was not confirmed after {TUI_DELIVERY_TIMEOUT_S:.1f}s "
        f"(reason={evidence.reason}, stage={evidence.stage}, resends={evidence.resends})",
        evidence=evidence,
    )


def _record_probe(evidence: DeliveryEvidence, before: PaneProbe, probe: PaneProbe) -> None:
    """Fold one post-send probe into the attempt's evidence.

    The composer is compared against what it held before the send rather than against emptiness: a
    TUI paints its own hint text into an empty composer, and only a fingerprint that changed says
    the payload is the thing sitting there.
    """
    evidence.readiness_after = probe.readiness
    evidence.composer_after = probe.composer
    evidence.modal_after = probe.modal
    evidence.cursor_after = probe.cursor
    evidence.payload_left_in_composer = (
        probe.composer not in (COMPOSER_UNKNOWN, COMPOSER_EMPTY)
        and probe.composer != before.composer
    )
    if not evidence.payload_left_in_composer and evidence.stage == STAGE_PAYLOAD_WRITTEN and probe.screen_read:
        _advance(evidence, STAGE_ENTER_ACCEPTED)
    evidence.cursor_from_backend = probe.cursor_from_backend
    if (
        probe.cursor
        and before.cursor
        and probe.cursor_from_backend == before.cursor_from_backend
        and probe.cursor != before.cursor
    ):
        # Two positions of the same kind, and they differ: the pane printed. A backend cursor and
        # a tail digest are not comparable, so a probe that lost the cursor says nothing here.
        evidence.cursor_moved = True


def _failure_reason(evidence: DeliveryEvidence) -> str:
    """Name the stage the delivery stopped at, in the words the telemetry keeps."""
    if evidence.payload_left_in_composer:
        return "payload-left-in-composer"
    if evidence.stage == STAGE_TURN_OBSERVED:
        return "turn-observed-but-unconfirmed"
    if evidence.stage == STAGE_ENTER_ACCEPTED:
        return "enter-accepted-without-turn"
    return f"pane-stayed-{evidence.readiness_after or READINESS_UNKNOWN}"
