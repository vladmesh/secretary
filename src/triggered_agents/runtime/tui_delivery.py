"""The product's one way to put a prompt in front of a live interactive head.

Every head that runs as a TUI is brought up with a command that carries no prompt, so something
has to type it into the pane afterwards. `terminal send` succeeding only means Orca accepted
keystrokes: a pane can still be painting, working, or holding a dialog that swallows what it was
given. One readiness classification, one resend policy, one confirmation boundary, one failure.

It lives in `runtime` because both sides need it and only one may import the other: the
dispatcher already reads this package, and the triggered-agents tick cannot read `secretary`
back. Nothing here knows about boards, roles or sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
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
from .pane_host import PaneHost
from .pane_host import pane_host as resolve_pane_host
from .prompt_document import NUDGE_FILE_MODE
from .tui_delivery_types import RunJson

TUI_IDLE_TIMEOUT_MS = int(
    os.environ.get("SECRETARY_TUI_IDLE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_TIMEOUT_MS", "60000"))
)
# Orca decides `tui-idle` from the pane's agent status and, failing that, from a quiescence window
# it polls. A probe shorter than that window would report every quiet pane as busy, so it is set
# above both rather than tuned for the fastest answer.
TUI_IDLE_PROBE_TIMEOUT_MS = int(
    os.environ.get(
        "SECRETARY_TUI_IDLE_PROBE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_PROBE_TIMEOUT_MS", "6000")
    )
)
TUI_DELIVERY_RETRIES = int(
    os.environ.get("SECRETARY_TUI_DELIVERY_RETRIES", os.environ.get("TA_TUI_DELIVERY_RETRIES", "2"))
)
TUI_DELIVERY_TIMEOUT_S = float(
    os.environ.get("SECRETARY_TUI_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12"))
)
TUI_DELIVERY_POLL_S = float(
    os.environ.get("SECRETARY_TUI_DELIVERY_POLL_S", os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25"))
)
TUI_DELIVERY_RESEND_GRACE_S = float(
    os.environ.get(
        "SECRETARY_TUI_DELIVERY_RESEND_GRACE_S", os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1")
    )
)
# How long a pane that is not yet able to take a prompt is given to become able to. A Codex head
# that is still starting its MCP servers is the ordinary case and it clears on its own; a head that
# does not clear inside this window is a head the caller has to replace, not one to keep typing at.
TUI_PRE_DELIVERY_TIMEOUT_S = float(
    os.environ.get(
        "SECRETARY_TUI_PRE_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_PRE_DELIVERY_TIMEOUT_S", "45")
    )
)
TUI_PRE_DELIVERY_POLL_S = float(
    os.environ.get("SECRETARY_TUI_PRE_DELIVERY_POLL_S", os.environ.get("TA_TUI_PRE_DELIVERY_POLL_S", "1"))
)
# How many times the one known modal is answered before the head is given up on. Two, because the
# answer either takes or the screen is not the modal this code recognises.
TUI_MODAL_ANSWER_ATTEMPTS = int(
    os.environ.get("SECRETARY_TUI_MODAL_ANSWER_ATTEMPTS", os.environ.get("TA_TUI_MODAL_ANSWER_ATTEMPTS", "2"))
)
# Screen evidence is bounded before digesting and never retained as content.
TUI_FINGERPRINT_LIMIT = 4000
# Limit composer reads to the screen bottom; unbounded history can contain stale markers.
TUI_COMPOSER_READ_LINES = int(
    os.environ.get("SECRETARY_TUI_COMPOSER_READ_LINES", os.environ.get("TA_TUI_COMPOSER_READ_LINES", "24"))
)
# How much of the payload has to be found in the composer for the composer to count as holding it.
# Short enough to survive the TUI wrapping the line it painted, long enough that no hint text,
# footer or spinner ever matches it by accident.
TUI_COMPOSER_PAYLOAD_PROBE = 48
TUI_COMPOSER_PAYLOAD_PROBE_MIN = 12


_WAIT_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"([a-z_]+)"')
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# The prompt markers the interactive heads of this product paint: Codex's `›` and Claude's `❯`.
COMPOSER_MARKERS = ("›", "❯")
# Codex may replace a large paste with a placeholder; classify that state explicitly.
_PASTE_RE = re.compile(r"pasted?\s+content", re.IGNORECASE)


# What one delivery attempt achieved. `accepted` means the pane took the prompt into a turn while
# the caller's own proof of delivery is expected to arrive later, outside this call.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"

# Transport acceptance proves only that bytes entered the pane, not that a turn began.
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
# These are evidence states for a refused readiness wait, not answers returned by
# `terminal_readiness`: a stale terminal binding and an unavailable transport both make that
# probe unknown, but recovery must not confuse either one with a pane Orca actually found busy.
READINESS_UNAVAILABLE = "unavailable"
READINESS_STALE_HANDLE = "stale_handle"

# What a pane is doing *before* it can take a prompt at all, read from the screen rather than from
# Orca. Orca answers readiness from the pane's agent status and a quiescence window, and a TUI
# sitting on its own update dialog or still starting its MCP servers is quiescent: `tui-idle`
# satisfied, nothing working, and every keystroke swallowed. So "ready" is Orca's answer to a
# different question than "sendable", and these are the states that make the two disagree.
PRE_DELIVERY_NONE = ""
# `✨ Update available! 0.152.0 -> 0.152.1 … 1. Update now 2. Skip 3. Skip until next version.`
# It ate 51 minutes of a reviewer's round on issue:e4d6f307.
PRE_DELIVERY_UPDATE_MODAL = "update-modal"
# `Starting MCP servers`, and the footer that goes with it: while a Codex head is starting, the
# composer queues what it is given instead of submitting it (`tab to queue message`). Orca called
# that pane `tui-idle/ready` and the TASK pointer sat in it for 80 minutes on issue:2fdac531.
#
# This is a *post-write* observation and nothing else. Measured against the real backend on this
# host, the pane paints nothing before the write that names it: see `SENDABILITY_UNESTABLISHED`.
# Once the pointer is in the composer Codex paints `tab to queue message` there and this state is
# observed — `tests/fixtures/panes/codex-mid-startup-holding-pointer.json` is that read — which is
# why it stays named, recorded in `pre_delivery_after`, and never claimed before a byte is written.
PRE_DELIVERY_STARTING = "starting"
# A screen shaped like a dialog that this code does not recognise. Nothing is typed at it.
PRE_DELIVERY_UNKNOWN_DIALOG = "unknown-dialog"
# The pre-delivery states that are a dialog holding the pane, as opposed to a phase it leaves.
# Only these gate the write: they are the ones the pane paints where the code can see them.
PRE_DELIVERY_DIALOGS = (PRE_DELIVERY_UPDATE_MODAL, PRE_DELIVERY_UNKNOWN_DIALOG)

# What the boundary could establish about sendability before writing the first byte.
#
# There is no `established` value here, and its absence is the finding rather than an omission. On
# Orca plus Codex nothing the backend asserts before a write says "this composer is live and idle".
# Measured on this host on 2026-09-03 across a real ~24s startup window, held open by a throwaway
# `CODEX_HOME` whose only difference is one MCP server whose command is `sleep`:
#
#       * Orca answered `tui-idle` satisfied with no `blockedReason` on all sixteen probes taken
#         across the window — the card exists because that answer is about a different question;
#       * its output cursor did not advance at all (`nextCursor` 20 on every read for the whole
#         window), so a quiescence test cannot tell a starting pane from a settled one;
#       * the startup status is painted as a character-by-character redraw whose fragments spell no
#         phrase contiguously, so no pattern names it either — thirteen reads over that window with
#         an empty composer classified as nothing at all.
#
# So a pre-write answer is either a dialog this code recognised, or this: not established. Saying
# that is the honest boundary; inferring readiness from a regex that did not match is not. What
# protects the pipeline is the post-write receipt, which was verified in the same session — the
# pointer written into that starting composer was positively found still sitting in it.
SENDABILITY_UNESTABLISHED = "unestablished"
SENDABILITY_DIALOG_REFUSED = "dialog-refused"

# How the known modal was settled, kept apart from whether the prompt was then received.
MODAL_NOT_PRESENT = "not-present"
MODAL_ANSWERED_SKIP = "answered-skip"
MODAL_REFUSED_UNKNOWN = "refused-unknown"
# A screen that matched the known modal's words without being the frame the pane is painting. No
# keystroke is authorised by a pattern alone, so this is a refusal and not an answer.
MODAL_REFUSED_NOT_ON_SCREEN = "refused-not-on-screen"
MODAL_UNRESOLVED = "unresolved"

# Whether the composer accepted this pointer. `unobserved` is a carrier that never reached the
# delivery boundary at all — a bring-up that failed before a prompt was sent — and it is neither
# a receipt nor a refusal.
DELIVERY_RECEIPT_ACCEPTED = "accepted"
DELIVERY_RECEIPT_REFUSED = "refused"
DELIVERY_RECEIPT_UNOBSERVED = "unobserved"

# The one keystroke this product ever sends at a dialog: Codex's own third choice, "Skip until
# next version". Upgrading is a separate, explicit action and never something a delivery performs
# to get past a screen, so choices 1 and 2 are not reachable from here.
CODEX_UPDATE_MODAL_SKIP_CHOICE = "3"

_UPDATE_MODAL_RE = re.compile(r"update available", re.IGNORECASE)
_UPDATE_MODAL_CHOICE_RE = re.compile(r"skip until next version|update now", re.IGNORECASE)
_STARTING_RE = re.compile(r"starting mcp servers|tab to queue message", re.IGNORECASE)
# Codex's own modal footer. A screen carrying it is holding a choice, whatever the choice is.
_DIALOG_FOOTER_RE = re.compile(r"press enter to continue", re.IGNORECASE)


def live_screen(screen: str) -> str:
    """What the pane is painting now, as against everything it printed earlier and kept.

    Orca's `terminal read` answers with a pane's retained raw output, not a rendered screen, and a
    TUI redraws in place: every frame it ever painted is still in that text, so a status line
    outlives by minutes the state it announced. Measured against the real backend on this host, a
    started, idle Codex pane — model resolved, both MCP servers up, composer painting its own hint —
    still carries its `Starting MCP servers` line in the tail, with the output cursor no longer
    moving. Classifying that text as a screen makes a ready head permanently
    un-sendable, which is `tests/fixtures/panes/codex-started-idle.json`.

    The one thing in that text that says where the newest frame begins is the prompt marker the TUI
    paints once per frame, so the live screen is what follows the last one. A screen carrying no
    marker at all is one where nothing is painting a composer — a dialog owning the terminal — and
    the bounded end of the tail is then the best answer available.
    """
    text = strip_ansi(screen or "")
    if not text:
        return ""
    held = composer_region(text)
    if held is not None:
        return held
    return " ".join(text[-TUI_FINGERPRINT_LIMIT:].split())


def classify_pre_delivery(screen: str, *, readiness: str = READINESS_READY) -> str:
    """Which pre-delivery state this screen is in, or `PRE_DELIVERY_NONE` for a sendable pane.

    Asked of `live_screen` and never of the whole retained tail, because this is a question about
    what is showing now and the tail is a record of everything that ever showed. The known states
    are named positively and everything else that is dialog-shaped — Orca naming a `blockedReason`,
    or Codex's own `Press enter to continue` footer under a screen none of the known patterns match
    — is `PRE_DELIVERY_UNKNOWN_DIALOG`, which is a refusal and never a guess.

    A screen that could not be read is not a dialog: it costs the delivery this evidence and leaves
    it on readiness alone, exactly as it was before this classification existed. Orca's own
    `blockedReason` is the one exception, because that is an answer rather than a missing one.

    Naming a state is not authority to type at it. Whether a recognised dialog is still the frame
    the pane is painting is `dialog_is_live`, deliberately a separate question.
    """
    text = live_screen(screen)
    if not text:
        return PRE_DELIVERY_UNKNOWN_DIALOG if readiness == READINESS_BLOCKED else PRE_DELIVERY_NONE
    if _UPDATE_MODAL_RE.search(text) and _UPDATE_MODAL_CHOICE_RE.search(text):
        return PRE_DELIVERY_UPDATE_MODAL
    if _STARTING_RE.search(text):
        return PRE_DELIVERY_STARTING
    if readiness == READINESS_BLOCKED or _DIALOG_FOOTER_RE.search(text):
        return PRE_DELIVERY_UNKNOWN_DIALOG
    return PRE_DELIVERY_NONE


def dialog_is_live(screen: str, *, readiness: str = READINESS_READY) -> bool:
    """Whether a dialog is the frame this pane is painting, which is what authorises a keystroke.

    Kept apart from the pattern match on purpose. `classify_pre_delivery` answers *which* screen
    this is; this answers *whether it is still showing*, and only the second may authorise a key.
    A pattern added later therefore cannot inherit the authorisation by matching text that has
    scrolled into history — the hazard being that a settled update modal's words stay in the tail
    (`tests/fixtures/panes/codex-settled-update-modal.json`), and a `3` typed at that pane is not
    a dismissal but a bare prompt submitted to the provider, immediately before its task pointer.

    Orca naming a `blockedReason` is an answer about now, so it counts on its own. Otherwise the
    dialog is live when its own footer — the last thing a dialog paints — is in the live screen.
    Anything less is not proof, and the fail-closed answer to no proof is no keystroke.
    """
    if readiness == READINESS_BLOCKED:
        return True
    region = live_screen(screen)
    return bool(region) and _DIALOG_FOOTER_RE.search(region) is not None


def delivery_receipt_state(carrier: Any) -> str:
    """Whether the composer accepted the pointer this evidence was taken for.

    The one question the rest of the product asks of a delivery record, asked in one place so that
    a launch, a recovery and an adoption cannot answer it differently. A live pid, a writable pane
    and Orca's own `accepted`/`bytesWritten` are deliberately not consulted: they are what used to
    be mistaken for delivery.

    `unobserved` is a carrier the delivery boundary never produced — a bring-up that failed before
    a prompt existed — and only evidence carrying a `stage` is the boundary's own.
    """
    evidence = getattr(carrier, "evidence", carrier)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or "stage" not in evidence:
        return DELIVERY_RECEIPT_UNOBSERVED
    if bool(evidence.get("payload_left_in_composer")):
        # Positive, prompt-specific proof that this pointer is still unsent. Determinate.
        return DELIVERY_RECEIPT_REFUSED
    if bool(evidence.get("turn_confirmed")):
        return DELIVERY_RECEIPT_ACCEPTED
    return DELIVERY_RECEIPT_REFUSED


@dataclass
class DeliveryEvidence:
    """What one delivery attempt saw, in a form that can be persisted beside the head.

    Everything here is an identifier, a bounded classification or a digest. The prompt is represented
    by its size and its hash and never by its text: these records outlive the head they were taken on.
    """

    handle: str = ""
    subject: str = ""
    stage: str = STAGE_NONE
    payload_bytes: int = 0
    payload_sha256: str = ""
    # How the head was given its task. `nudge-file` is the protocol rule: the pane received a
    # bounded line naming a document and the content never entered the terminal, so `payload_bytes`
    # here is the size of that line rather than the size of the task. The path is kept because it
    # is the run's own pointer to what the head was asked to do; the document's text is not, here
    # or anywhere else in this record. An empty mode is a delivery that carried its own content.
    delivery_mode: str = ""
    document_path: str = ""
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
    # The typed outcome of a readiness wait that failed before any pane probe or write could be
    # made.  Empty historical evidence is deliberately not busy; `delivery_readiness_state`
    # reads it as unknown.  The normal before/after fields continue to describe probes made once
    # a wait succeeded.
    readiness_state: str = ""
    readiness_before: str = ""
    readiness_after: str = ""
    composer_before: str = COMPOSER_UNKNOWN
    composer_after: str = COMPOSER_UNKNOWN
    payload_left_in_composer: bool = False
    modal_before: bool = False
    modal_after: bool = False
    # Which pre-delivery state the pane was found in, if any, and how the known modal was settled.
    # These three are the modal-resolution half of the telemetry and say nothing about receipt.
    pre_delivery_before: str = PRE_DELIVERY_NONE
    pre_delivery_after: str = PRE_DELIVERY_NONE
    # What the boundary could establish about sendability before it wrote the first byte. Never
    # "established": on this backend nothing asserts a live idle composer pre-write, so this says
    # either that a dialog refused the write or that sendability was not established and the
    # receipt is what the delivery rests on. A reader must not mistake the second for a proof.
    sendability: str = ""
    modal_resolution: str = ""
    modal_answers: int = 0
    # The provider-binding half: the caller's own criterion — what the provider wrote down about
    # the turn — answered yes. `turn_confirmed` beside it is what the pane showed. Neither implies
    # the other, and "delivered" is not one bit.
    provider_bound: bool = False
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
            "delivery_mode": self.delivery_mode,
            "document_path": self.document_path,
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
            "readiness_state": self.readiness_state,
            "readiness_before": self.readiness_before,
            "readiness_after": self.readiness_after,
            "composer_before": self.composer_before,
            "composer_after": self.composer_after,
            "payload_left_in_composer": self.payload_left_in_composer,
            "modal_before": self.modal_before,
            "modal_after": self.modal_after,
            "pre_delivery_before": self.pre_delivery_before,
            "pre_delivery_after": self.pre_delivery_after,
            "sendability": self.sendability,
            "modal_resolution": self.modal_resolution,
            "modal_answers": self.modal_answers,
            "provider_bound": self.provider_bound,
            # Derived, and kept in the record so a reader of a persisted receipt does not have to
            # re-derive it: modal resolution, delivery receipt and provider binding, side by side.
            "delivery_receipt": self.receipt,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "cursor_moved": self.cursor_moved,
            "cursor_from_backend": self.cursor_from_backend,
            "reason": self.reason,
        }

    @property
    def receipt(self) -> str:
        """Whether the composer accepted the pointer, as `delivery_receipt_state` answers it.

        The three stored fields are handed over rather than `self`, because `to_json` publishes
        this derivation and asking it for the whole record here would be a cycle.
        """
        return delivery_receipt_state(
            {
                "stage": self.stage,
                "payload_left_in_composer": self.payload_left_in_composer,
                "turn_confirmed": self.turn_confirmed,
            }
        )

    @classmethod
    def from_json(cls, payload: Any) -> DeliveryEvidence:
        if not isinstance(payload, dict):
            return cls()
        fields = cls()
        for name, value in payload.items():
            # Only the stored fields are restored. `to_json` also publishes derived keys, and a
            # record that ever carried one under the property's own name must be inert here rather
            # than an AttributeError raised on a read-only property.
            if name not in cls.__dataclass_fields__:
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
    """The delivery verdict a caller compares, carrying the evidence that produced it."""

    evidence: DeliveryEvidence

    def __new__(cls, value: str, evidence: DeliveryEvidence) -> DeliveryOutcome:
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

    The cursor is Orca's `nextCursor` and it is the authority on whether the pane printed anything;
    the tail is not, because the retained window is bounded and repaint-heavy TUI output can leave
    it identical across a turn. `cursor_known` is false only when the runtime answered without one,
    and then a digest of the tail stands in.
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
    # Whether this probe found the delivery's own payload sitting in the composer. A probe taken
    # without a payload to look for answers false, which is what it can honestly say.
    holds_payload: bool = False
    # Which pre-delivery state the screen showed, if any. Not asked of a pane Orca found busy: a
    # working head is the busy path's answer, and its own footer says `tab to queue message` while
    # it works.
    pre_delivery: str = PRE_DELIVERY_NONE
    # Whether a dialog is the frame the pane is painting. Separate from `pre_delivery` because it
    # is the only thing that authorises a keystroke, and a classification is not that.
    dialog_live: bool = False

    @property
    def modal(self) -> bool:
        """Whether this pane is held in a dialog, by either of the two things that can say so."""
        return self.readiness == READINESS_BLOCKED or self.pre_delivery in PRE_DELIVERY_DIALOGS

    @property
    def sendable(self) -> bool:
        """Whether anything the backend showed forbids writing into this pane.

        Read the name carefully: this is not "the composer is live and idle", because nothing on
        this backend answers that before a write. It is the weaker, honest question — is a dialog
        holding the pane — and the delivery records `SENDABILITY_UNESTABLISHED` when the answer is
        no, rather than claiming a readiness it did not observe. A pane still starting its MCP
        servers passes this test on Orca plus Codex, and the receipt is what catches it.
        """
        return self.readiness != READINESS_BLOCKED and self.pre_delivery not in PRE_DELIVERY_DIALOGS


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def payload_fingerprint(prompt: str) -> tuple[int, str]:
    """The size and hash of a payload, which is all of it that is ever recorded."""
    raw = (prompt or "").encode("utf-8", "replace")
    return len(raw), _digest(prompt or "")


def read_pane(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    limit: int | None = None,
) -> PaneRead:
    """One pane read: the tail, ANSI stripped, and the session manager's output cursor with it.

    A pane that cannot be read is not a failure here: it costs the delivery its composer and cursor
    evidence and leaves it on readiness alone. `limit` bounds the retained output for a caller that
    reads a panel rather than a position; delivery itself passes none.
    """
    try:
        data = resolve_pane_host(run_json, host=host).read(handle, limit=limit)
    except Exception:
        return PaneRead()
    terminal = (
        data.get("terminal") if isinstance(data, dict) and isinstance(data.get("terminal"), dict) else data
    )
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


def read_pane_text(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    limit: int | None = None,
) -> str:
    """The pane's text alone, for callers that read a screen rather than a position."""
    return read_pane(handle, run_json=run_json, host=host, limit=limit).text


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def composer_region(screen: str) -> str | None:
    """The text after the last prompt marker a TUI paints — Codex's `›`, Claude's `❯`.

    Whitespace-collapsed, bounded, and `None` when the screen carries no marker at all. Only a
    screen read from the bottom of the pane makes this the composer: given the retained history a
    marker can be anywhere in the window, and what follows it is then the transcript.
    """
    if not screen:
        return None
    marker = max(screen.rfind(char) for char in COMPOSER_MARKERS)
    if marker < 0:
        return None
    return " ".join(screen[marker + 1 :][:TUI_FINGERPRINT_LIMIT].split())


def composer_fingerprint(screen: str) -> str:
    """What the composer is holding, as a classification, a length and a digest.

    Evidence for a reader of the record, not a decision: a TUI paints its own hint text, its footer
    and a ticking spinner into the same region, so this fingerprint differs between two probes of a
    pane whose composer never changed. The text is hashed, never kept.
    """
    held = composer_region(screen)
    if held is None:
        return COMPOSER_UNKNOWN
    if not held:
        return COMPOSER_EMPTY
    kind = "paste" if _PASTE_RE.search(held) else "text"
    return f"{kind}:{len(held)}:{_digest(held)}"


def composer_holds_payload(screen: str, payload: str) -> bool:
    """Whether the composer is still holding this payload, asked as a positive question.

    This is the failure the delivery boundary exists for, so it is looked for rather than inferred:
    the composer holds the payload when the TUI covered it with a paste placeholder, or when the
    payload's own opening words are sitting in that region. Both are statements about this prompt.

    It used to be inferred instead — any composer fingerprint that was not empty and differed from
    the one taken before the send. On a Codex pane that is true of every probe ever taken: the hint
    text, the model footer and the `Working (12s)` counter all live after the marker and all change
    between two reads. That inference is what reported 62 delivered observer wakes out of 62 as
    `payload-left-in-composer` and cost the head 14 replacements in a day.
    """
    held = composer_region(screen)
    if not held:
        return False
    if _PASTE_RE.search(held):
        return True
    probe = " ".join(strip_ansi(payload or "").split())[:TUI_COMPOSER_PAYLOAD_PROBE]
    return len(probe) >= TUI_COMPOSER_PAYLOAD_PROBE_MIN and probe in held


def output_cursor(read: PaneRead) -> tuple[str, bool]:
    """Where the pane's output has got to, and whether that came from the backend.

    Orca's own cursor is used whenever it answers with one, opaque and unparsed: it advances when the
    pane printed, which a retained tail does not have to. A digest of that tail is only what stands
    in when the runtime returns no cursor at all.
    """
    if read.cursor_known:
        return f"orca:{read.cursor}", True
    if not read.text:
        return "", False
    marker = max(read.text.rfind(char) for char in COMPOSER_MARKERS)
    output = read.text[:marker] if marker >= 0 else read.text
    output = output[-TUI_FINGERPRINT_LIMIT:]
    return f"tail:{len(output)}:{_digest(output)}", False


def probe_pane(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    payload: str = "",
) -> PaneProbe:
    """Readiness, composer and output cursor in one look, for the evidence of one attempt.

    The read is bounded to the pane's last rows, and what comes back is still retained output
    rather than a rendered screen: Orca keeps raw bytes and a TUI redraws in place, so old frames
    sit in that text beside the newest one. Every question here that is about *now* — the composer,
    the pre-delivery state, whether a dialog is live — therefore goes through the live-screen
    region rather than the whole answer. `payload` is the prompt this delivery is carrying, and it
    is only ever compared against the screen: the probe keeps a classification and a digest of it,
    never its text.
    """
    readiness = terminal_readiness(handle, run_json=run_json, host=host)
    read = read_pane(handle, run_json=run_json, host=host, limit=TUI_COMPOSER_READ_LINES)
    cursor, from_backend = output_cursor(read)
    return PaneProbe(
        readiness=readiness,
        composer=composer_fingerprint(read.text),
        cursor=cursor,
        cursor_from_backend=from_backend,
        screen_read=bool(read.text),
        holds_payload=composer_holds_payload(read.text, payload),
        pre_delivery=(
            PRE_DELIVERY_NONE
            if readiness == READINESS_BUSY
            else classify_pre_delivery(read.text, readiness=readiness)
        ),
        dialog_live=(readiness != READINESS_BUSY and dialog_is_live(read.text, readiness=readiness)),
    )


def _advance(evidence: DeliveryEvidence, stage: str) -> None:
    if _STAGE_ORDER.index(stage) > _STAGE_ORDER.index(evidence.stage):
        evidence.stage = stage


def wait_for_tui_idle(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    timeout_ms: int | None = None,
) -> None:
    """Wait until the session manager reports the pane ready for input; a refusal reaches the caller.

    This is also what a freshly created head is given to come up in: a TUI paints, reads its config
    and answers the readiness probe well after the pty exists.
    """
    resolve_pane_host(run_json, host=host).wait_idle(
        handle, timeout_ms=TUI_IDLE_TIMEOUT_MS if timeout_ms is None else timeout_ms
    )


def terminal_readiness(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    timeout_ms: int | None = None,
) -> str:
    """Ask whether the pane is ready for input, and answer in three states, not two.

    The runtime derives it from the pane's own agent status and falls back to a quiescence window, so
    no screen is read here. `READINESS_BUSY` is a pane that is working, which Orca reports as a
    satisfied-false answer or a failed command carrying `code: timeout`. A pane it names a
    `blockedReason` for is `READINESS_BLOCKED` — held in a dialog, so a prompt sent to it went
    nowhere. `READINESS_UNKNOWN` is the probe itself failing and must not be read as a busy head.
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


def delivery_readiness_state(carrier: Any) -> str:
    """Return the typed readiness state carried by a failed delivery, conservatively.

    A persisted evidence record predating `readiness_state` did not observe this refusal, so it is
    unknown rather than busy: only a current failed `tui-idle` wait that parsed one of Orca's working
    answers earns the no-replacement treatment.
    """
    evidence = getattr(carrier, "evidence", carrier)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if isinstance(evidence, dict):
        state = str(evidence.get("readiness_state") or "")
    else:
        state = str(getattr(evidence, "readiness_state", "") or "")
    if state in {READINESS_BUSY, READINESS_BLOCKED, READINESS_UNAVAILABLE, READINESS_STALE_HANDLE}:
        return state
    return READINESS_UNKNOWN


def _refused_wait_readiness(exc: Exception) -> str:
    """Classify a `terminal wait` the host refused, from the body Orca printed with it.

    The CLI exits non-zero both for a condition it could not satisfy and for a failure, and the host
    turns the two into the same exception, so the answer is in the text rather than in the outcome:

          * a `wait` object saying `satisfied: false` is a pane Orca found working or blocked behind
            a dialog. That is busy, and busy waits for readiness;
          * `code: timeout` is the same condition not being met before the probe's own deadline;
          * anything else, an unreadable body included, is a probe that was never answered.
    """
    body = _json_object(str(exc))
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    wait = result.get("wait") if isinstance(result, dict) else None
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    code = _wait_error_code(body, exc)
    return READINESS_BUSY if code == "timeout" else READINESS_UNKNOWN


def _wait_error_code(body: dict[str, Any], exc: Exception) -> str:
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if not code:
        # A body too damaged to parse can still carry its code in the text.
        codes = _WAIT_ERROR_CODE_RE.findall(str(exc))
        code = codes[-1] if codes else ""
    return code


def _refused_wait_evidence_state(exc: Exception) -> str:
    """Keep the three refusal classes separate in durable delivery evidence."""
    body = _json_object(str(exc))
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    wait = result.get("wait") if isinstance(result, dict) else None
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    code = _wait_error_code(body, exc)
    if code == "timeout":
        return READINESS_BUSY
    if code == "terminal_handle_stale":
        return READINESS_STALE_HANDLE
    return READINESS_UNAVAILABLE


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
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    adapter: str = "",
    confirm: Callable[[float], bool] | None = None,
    ack_out_of_band: bool = False,
    subject: str = "",
    document_path: str = "",
    before_send: Callable[[], None] | None = None,
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
    `tui-idle` satisfied the whole time, because a pane holding a composer really is idle. So a send
    that reports bytes and a pane that reports idle are not delivery; the payload found in the
    composer and the pane's own output position are.

    `document_path` says the prompt is a nudge at a task document rather than the task itself. It
    changes nothing about how the four stages are observed and everything about what the evidence
    means: the payload fingerprint is then the fingerprint of a pointer.

    Callers pass `confirm`, their own criterion for stage 4. A caller whose proof arrives later sets
    `ack_out_of_band` and gets `DELIVERY_ACCEPTED` once stage 3 is evidenced. A caller may set both,
    and then either one is enough. They are not two readings of one thing: `confirm` is what the
    provider wrote down about the turn, stage 3 is what the pane showed, and either on its own is a
    prompt that landed. A caller that holds both proofs — the observer wake does — has no reason to
    be refused because the weaker of them was the one that could not see.

    The verdict comes back with the evidence of the attempt attached, and so does the failure,
    including a failure of the transport itself.
    """
    if confirm is None and not ack_out_of_band:
        raise ValueError("interactive delivery requires a confirmation criterion")
    payload_bytes, payload_hash = payload_fingerprint(prompt)
    evidence = DeliveryEvidence(
        handle=handle,
        subject=subject,
        payload_bytes=payload_bytes,
        payload_sha256=payload_hash,
        delivery_mode=NUDGE_FILE_MODE if document_path else "",
        document_path=document_path,
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
        wait_for_tui_idle(handle, run_json=run_json, host=host)
    if before_send is not None:
        # Retained workers stay frozen until the exact delivery path has observed a ready pane.
        # A busy wait therefore does not turn into a SIGCONT followed by a stop-and-replacement
        # recovery; once ready, activation remains immediately before the existing send path.
        with _transport_evidence(evidence, "activate-head"):
            before_send()
    # Nothing is written into a pane that is not yet able to take a prompt. This is the step that
    # separates "Orca says ready" from "sendable", and it runs before the first byte.
    before = _settle_pre_delivery(
        handle, run_json=run_json, host=host, payload=prepared.text, evidence=evidence
    )
    evidence.readiness_before = before.readiness
    evidence.composer_before = before.composer
    evidence.modal_before = before.modal
    evidence.cursor_before = before.cursor
    evidence.cursor_from_backend = before.cursor_from_backend
    sent_at = time.time()
    with _transport_evidence(evidence, "send-payload"):
        _send_payload(handle, prepared, run_json=run_json, host=host, evidence=evidence)
    return _confirm_interactive_turn(
        handle,
        prepared,
        sent_at,
        before,
        run_json=run_json,
        host=host,
        confirm=confirm,
        ack_out_of_band=ack_out_of_band,
        evidence=evidence,
    )


def _settle_pre_delivery(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    payload: str,
    evidence: DeliveryEvidence,
) -> PaneProbe:
    """Answer what can be answered before the first byte, or refuse. Returns the probe sent against.

    This step exists for dialogs, and it says so rather than pretending to more:

          * the one known modal is answered with its own documented "Skip until next version"
            choice, a bounded number of times, and readiness is proved again afterwards. Two
            conditions have to hold and they are asked separately: the screen is the modal this
            code knows, and that modal is the frame the pane is painting. Upgrading is not
            something a delivery does to get past a dialog, so the other two choices do not exist
            here;
          * anything else that is dialog-shaped is a typed refusal with no keystrokes at all. A
            screen this code does not recognise is a screen it cannot answer, and guessing at one
            is how a head ends up agreeing to something nobody asked it. A window that expires is
            the same refusal: the caller's answer to a pane a dialog will not let go of is to
            replace the head, not to keep waiting while the card reports progress;
          * everything else is recorded as `SENDABILITY_UNESTABLISHED` and written into. That is
            not a claim that the composer is live and idle — nothing this backend offers before a
            write says that, and the constant carries the measurement — it is the honest statement
            that no dialog was seen and the delivery now rests on its receipt.

    What used to be here and is deliberately gone: a bounded wait-out of `starting`. A head still
    bringing its MCP servers up paints nothing before the write that the code can read, so waiting
    for that state to clear was waiting on a signal that never arrives, and treating its absence as
    readiness was inferring a fact from a regex that did not match. `pre_delivery_before` still
    records whatever *was* seen; it is evidence, not a gate.
    """
    deadline = time.monotonic() + max(TUI_PRE_DELIVERY_TIMEOUT_S, 0)
    probe = probe_pane(handle, run_json=run_json, host=host, payload=payload)
    evidence.pre_delivery_before = probe.pre_delivery
    evidence.readiness_before = probe.readiness
    while True:
        if probe.sendable:
            evidence.modal_resolution = MODAL_ANSWERED_SKIP if evidence.modal_answers else MODAL_NOT_PRESENT
            evidence.sendability = SENDABILITY_UNESTABLISHED
            return probe
        evidence.sendability = SENDABILITY_DIALOG_REFUSED
        if probe.pre_delivery == PRE_DELIVERY_UNKNOWN_DIALOG or probe.readiness == READINESS_BLOCKED:
            evidence.modal_resolution = MODAL_REFUSED_UNKNOWN
            evidence.reason = "unknown-dialog"
            raise TuiDeliveryError(
                "the pane is holding a dialog this code does not recognise; nothing was typed at it "
                f"(readiness={probe.readiness})",
                evidence=evidence,
            )
        if probe.pre_delivery == PRE_DELIVERY_UPDATE_MODAL:
            if not probe.dialog_live:
                # The words are there and the dialog is not. Whatever matched has scrolled into
                # history, so there is nothing on this screen a keystroke could answer and typing
                # one would submit a bare `3` to the provider instead. This condition is asked of
                # the probe rather than of the pattern precisely so that no pattern can ever be
                # the whole authorisation for a key.
                evidence.modal_resolution = MODAL_REFUSED_NOT_ON_SCREEN
                evidence.reason = "modal-not-on-screen"
                raise TuiDeliveryError(
                    "the known update modal matched text the pane is no longer painting; "
                    "nothing was typed at it",
                    evidence=evidence,
                )
            if evidence.modal_answers >= max(TUI_MODAL_ANSWER_ATTEMPTS, 0):
                evidence.modal_resolution = MODAL_UNRESOLVED
                evidence.reason = f"pre-delivery-{PRE_DELIVERY_UPDATE_MODAL}"
                raise TuiDeliveryError(
                    "the known update modal did not clear after "
                    f"{evidence.modal_answers} documented Skip answers",
                    evidence=evidence,
                )
            with _transport_evidence(evidence, "answer-update-modal"):
                resolve_pane_host(run_json, host=host).send(
                    handle, CODEX_UPDATE_MODAL_SKIP_CHOICE, enter=True
                )
            evidence.modal_answers += 1
            # Readiness is proved again before the prompt is written, exactly as it is on the way in.
            with _transport_evidence(evidence, "wait-for-readiness"):
                wait_for_tui_idle(handle, run_json=run_json, host=host)
        if time.monotonic() >= deadline:
            evidence.modal_resolution = evidence.modal_resolution or MODAL_UNRESOLVED
            evidence.reason = f"pre-delivery-{probe.pre_delivery or READINESS_BLOCKED}"
            raise TuiDeliveryError(
                f"the pane never left its dialog within {TUI_PRE_DELIVERY_TIMEOUT_S:.1f}s "
                f"(state={probe.pre_delivery or probe.readiness})",
                evidence=evidence,
            )
        time.sleep(max(TUI_PRE_DELIVERY_POLL_S, 0.01))
        probe = probe_pane(handle, run_json=run_json, host=host, payload=payload)
        evidence.readiness_before = probe.readiness
        if probe.pre_delivery != PRE_DELIVERY_NONE:
            # The state the refusal will name is the one still holding the pane.
            evidence.pre_delivery_before = probe.pre_delivery


@contextmanager
def _transport_evidence(evidence: DeliveryEvidence, step: str):
    """Turn a refused Orca call inside the delivery into an evidence-carrying delivery failure.

    A `terminal wait` or `terminal send` the host will not perform is exactly as much a delivery that
    did not happen as a pane that swallowed the prompt. `ValueError` is not caught: the two argument
    refusals this path raises are programming errors in the caller, not deliveries.
    """
    try:
        yield
    except (TuiDeliveryError, ValueError):
        raise
    except Exception as exc:
        if step == "wait-for-readiness":
            state = _refused_wait_evidence_state(exc)
            evidence.readiness_state = state
            if state in {READINESS_BUSY, READINESS_BLOCKED}:
                evidence.readiness_before = state
            evidence.reason = f"readiness-{state}"
        else:
            evidence.reason = f"transport-refused-{step}"
        raise TuiDeliveryError(
            f"the {step} step of prompt delivery was refused (stage={evidence.stage}): {exc}",
            evidence=evidence,
        ) from None


def _send_payload(
    handle: str,
    prompt: PreparedAgentPrompt,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    evidence: DeliveryEvidence,
    submit_only: bool = False,
) -> None:
    """Run the one provider-aware body/submit transport and merge its metadata-only receipt."""
    try:
        receipt = send_agent_prompt(handle, prompt, run_json=run_json, host=host, submit_only=submit_only)
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
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    confirm: Callable[[float], bool] | None,
    ack_out_of_band: bool = False,
    evidence: DeliveryEvidence,
) -> DeliveryOutcome:
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    while time.monotonic() < deadline:
        # A normal caller's criterion has always taken precedence over pane evidence.  Keep that
        # launch/worker/reviewer contract intact.  An out-of-band acknowledgement is different:
        # it expressly lets pane evidence accept a delivery, so first reject the one direct,
        # prompt-specific proof that this payload is still unsent.
        if not ack_out_of_band and confirm is not None and confirm(sent_at):
            # The criterion is answered before this iteration takes its own look, so the composer
            # evidence in hand is the previous iteration's. Take one more before the receipt is
            # derived from it: on the ordinary submit-only resend path the pointer sat in the
            # composer until a bare Enter sent it, and keeping that probe's positive
            # `payload_left_in_composer` would make `delivery_receipt_state` read a delivery that
            # succeeded as a determinate refusal — a wrongly refused adoption and an unnecessary
            # head replacement in exactly the crash window this boundary is here for.
            _record_probe(
                evidence,
                before,
                probe_pane(handle, run_json=run_json, host=host, payload=prompt.text),
            )
            _advance(evidence, STAGE_TURN_OBSERVED)
            _advance(evidence, STAGE_ACKNOWLEDGED)
            evidence.turn_confirmed = True
            evidence.provider_bound = True
            evidence.reason = ""
            return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)
        probe = probe_pane(handle, run_json=run_json, host=host, payload=prompt.text)
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
        # A provider turn cannot override proof that this prompt remains in the composer.
        if not evidence.payload_left_in_composer and confirm is not None and confirm(sent_at):
            _advance(evidence, STAGE_TURN_OBSERVED)
            _advance(evidence, STAGE_ACKNOWLEDGED)
            evidence.turn_confirmed = True
            evidence.provider_bound = True
            evidence.reason = ""
            return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)
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
            # Re-send only when no evidence accounts for the payload; otherwise use bare Enter.
            unaccounted_for = (
                probe.screen_read
                and not evidence.payload_left_in_composer
                and not evidence.cursor_moved
                and not evidence.turn_confirmed
            )
            with _transport_evidence(evidence, "resend-payload"):
                _send_payload(
                    handle,
                    prompt,
                    run_json=run_json,
                    host=host,
                    evidence=evidence,
                    submit_only=not unaccounted_for,
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

    Whether the payload is still in the composer is the probe's own positive finding — the payload
    was looked for and found — and not a difference between two fingerprints. The fingerprints stay
    in the record for a reader, and `before` is still what the output cursor is compared against.
    """
    evidence.readiness_after = probe.readiness
    evidence.composer_after = probe.composer
    evidence.modal_after = probe.modal
    evidence.pre_delivery_after = probe.pre_delivery
    evidence.cursor_after = probe.cursor
    evidence.payload_left_in_composer = probe.holds_payload
    if (
        not evidence.payload_left_in_composer
        and evidence.stage == STAGE_PAYLOAD_WRITTEN
        and probe.screen_read
    ):
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
        # A pane that went to work is not a pane held before delivery, whatever its screen also
        # showed. Naming the pre-delivery state ahead of this reported a genuine unconfirmed turn
        # as `pre-delivery-starting`, which is the telemetry an operator acts on.
        return "turn-observed-but-unconfirmed"
    if evidence.stage == STAGE_ENTER_ACCEPTED:
        return "enter-accepted-without-turn"
    if evidence.pre_delivery_after:
        # Nothing accounts for the prompt and the pane is in a state that cannot take one: name
        # the state, because that is what is holding the delivery.
        return f"pre-delivery-{evidence.pre_delivery_after}"
    return f"pane-stayed-{evidence.readiness_after or READINESS_UNKNOWN}"
