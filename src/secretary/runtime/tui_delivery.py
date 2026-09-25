"""What a prompt delivery into a live interactive head left behind, as values a record can keep.

The delivery itself belongs to the head backend (`local_pty_head` and its supervisor). This module
is the vocabulary every reader of a delivery shares: the stages a delivery can reach, the readiness
and pre-delivery states a refusal can name, `DeliveryEvidence` as it is persisted beside a head,
and the one derivation of whether the composer accepted the prompt. Durable records written by the
Orca pane delivery (removed in secretary-1725) carry the same fields, so they read back unchanged.

It lives in `runtime` because both sides need it and only one may import the other: the
dispatcher already reads this package, and the triggered-agents tick cannot read `secretary`
back. Nothing here knows about boards, roles or sessions, and nothing here reaches a terminal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .agent_prompt_transport import AGENT_PROMPT_TRANSPORT_VERSION, TRANSPORT_POLICY

# What one delivery attempt achieved. `accepted` means the head took the prompt into a turn while
# the caller's own proof of delivery is expected to arrive later, outside this call.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"

# Transport acceptance proves only that bytes entered the terminal, not that a turn began.
STAGE_NONE = "none"
STAGE_PAYLOAD_WRITTEN = "payload_written"
STAGE_ENTER_ACCEPTED = "enter_accepted"
STAGE_TURN_OBSERVED = "turn_observed"
STAGE_ACKNOWLEDGED = "acknowledged"

# What the composer was holding, as an answer that carries no prompt text. `unknown` is a pane
# whose screen could not be read or has no composer marker on it; the delivery then falls back to
# readiness alone, which is what this path had before the fingerprint existed.
COMPOSER_UNKNOWN = "unknown"
COMPOSER_EMPTY = "empty"

# Whether a head could take a prompt. `blocked` is a head held in a dialog: not ready for a prompt,
# and not working on one either. `unknown` is a probe that failed, which is not a busy head.
READINESS_READY = "ready"
READINESS_BUSY = "busy"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"
# Evidence states for a refused readiness wait: a stale terminal binding and an unavailable
# transport, which recovery must not confuse with a head that was found busy.
READINESS_UNAVAILABLE = "unavailable"
READINESS_STALE_HANDLE = "stale_handle"

# What the screen showed *before* the head could take a prompt at all: states in which a TUI is
# quiescent yet swallows every keystroke, so "ready" and "sendable" disagree.
PRE_DELIVERY_NONE = ""
# Codex's `Update available!` modal (issue:e4d6f307).
PRE_DELIVERY_UPDATE_MODAL = "update-modal"
# `Starting MCP servers` / `tab to queue message`: the composer queues what it is given instead of
# submitting it (issue:2fdac531). Only ever observed after the write, in `pre_delivery_after`.
PRE_DELIVERY_STARTING = "starting"
# A screen shaped like a dialog that was not recognised. Nothing is typed at it.
PRE_DELIVERY_UNKNOWN_DIALOG = "unknown-dialog"

# What could be established about sendability before the first byte was written. There is no
# `established` value: nothing a terminal asserts before a write proves a live, idle composer, so
# a pre-write answer is either a recognised dialog or this, and the post-write receipt is what a
# delivery rests on.
SENDABILITY_UNESTABLISHED = "unestablished"
SENDABILITY_DIALOG_REFUSED = "dialog-refused"

# Whether the composer accepted this pointer. `unobserved` is a carrier that never reached the
# delivery boundary at all — a bring-up that failed before a prompt was sent — and it is neither
# a receipt nor a refusal.
DELIVERY_RECEIPT_ACCEPTED = "accepted"
DELIVERY_RECEIPT_REFUSED = "refused"
DELIVERY_RECEIPT_UNOBSERVED = "unobserved"


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
    provider_source_state: str = ""
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
            "provider_source_state": self.provider_source_state,
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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def payload_fingerprint(prompt: str) -> tuple[int, str]:
    """The size and hash of a payload, which is all of it that is ever recorded."""
    raw = (prompt or "").encode("utf-8", "replace")
    return len(raw), _digest(prompt or "")


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
