"""`LocalPtyHeadRuntime`: the six verbs over a head this product owns the process of.

The second backend of `HeadRuntime`, standing on the local-pty substrate
(`head.local_pty`): a supervisor in its own session holding the head's pty, a Unix socket that
answers without ever waiting for the head, and a versioned journal that says what happened. The
verbs, the receipts and the order of the critical section are the legacy backend's — nothing here
means something different by `HEAD_BUSY`, `HEAD_DRAINING`, `HEAD_ALIVE` or `HEAD_GONE`, and the
contract suite is run against both so that it cannot start to.

What changes is what the backend can honestly do, and it is exactly the two verbs Orca could not:

  * **`attach`** is real. The substrate hands out a stream, bounded by its own attach limit, and
    detaching is closing a socket: it does not touch the head and it does not lose its output,
    because what the head printed while nobody was attached is still in the supervisor's buffer;
  * **`request_drain`** is real in the sense this backend can prove. The drain reaches the process
    that owns the head — its supervisor closes admission for it, writes `drain.requested` into the
    head's own journal, and refuses every later `input` on the socket by name — and the receipt
    only says `head_signalled` once `status` has been read back and confirms it. The agent process
    itself is still told nothing, because no wind-down protocol exists on either side of that pty
    and inventing one would mean typing into a head that is mid-turn; `DRAIN_HEAD_NOT_SIGNALLED`
    on the receipt's reason says which of the two happened, so the difference stays legible.

**Delivery is where this backend is not the legacy one, and the difference is load-bearing.** On
the substrate `ok` from `input` means *admitted*: the payload was taken on and the supervisor's
loop writes it into the pty as fast as the head reads. A `deliver` that returned `HEAD_OK` on that
answer would be reporting an intention as an arrival, which is the wound the substrate closed one
layer down and this one would re-open. So `deliver` follows the payload and then **decides what
became of it once**, in `_delivery_report`, out of `status`'s own delivery record and the journal's
`input.accepted` with its two byte counts and its `complete` flag. That decision is a name, it
travels on the report, and every branch below reads it by that name:

  * `DELIVERY_ARRIVED` — all of it landed: `HEAD_OK`, `delivery_state` `complete`, and a
    `DeliveryOutcome` carrying the delivered byte count;
  * `DELIVERY_LEFT_A_PREFIX` — it ended part-way: `HEAD_ALIVE` (or `HEAD_GONE` when the head went
    with it), `delivery_state` `stalled` or `failed`, with what did land. Fatal, for the reason
    below;
  * `DELIVERY_LANDED_NOTHING` — it ended and the kernel took not one byte: the terminal is exactly
    as it was, no turn was started, and the head is worth delivering to again;
  * `DELIVERY_UNESTABLISHED` — it was offered, and what became of it **cannot be established**.
    Three ways, and they differ in which witnesses there were to ask:
    the supervisor stopped answering after admitting it and the journal has no record of it
    either; the substrate ran past its own delivery bound without ending it, and the journal has
    nothing either; or the supervisor stopped answering *as the payload was being offered*, before
    it named a delivery — and there the journal is not asked at all, because a journal record is
    matched on the delivery id and no id was ever handed back to match one on. Reported as
    `delivery_state` `unknown` with the last counts anybody could establish, and fatal.

Four outcomes, and there is no fifth. A delivery's state is never inferred from a boolean predicate
over the byte counts, and never inferred from *which exception arrived*: an admitted payload
followed by an unreachable supervisor is not a refusal at admission — a refusal means the terminal
was never touched, and here it provably may have been.

**The runtime's wait is derived from the substrate's own bound, and that is the cut of this card.**
secretary-1465 gave this runtime a `delivery_timeout` of its own, independent of the substrate's
`delivery_seconds`, and every red round of that card grew out of the one thing that permitted:
a runtime allowed to stop watching a delivery *before* the substrate had finished it. From that
permission came a fifth outcome for a delivery still in flight, a register of unfinished
deliveries to carry it on, a second place that asked witnesses what had become of one, and a wrong
reading of a transient refusal in that second place. None of it exists here, because the
permission does not: the wait is the bound the substrate declared for **this** delivery — the one
the head was raised with, read back off the delivery the supervisor admitted — plus a named grace
(`delivery_grace`, which can only ever extend it). By construction there is no configuration in
which this runtime stops watching first. A head that needs a longer reception has **one** number
raised, `delivery_seconds` at `start`, and the runtime's wait follows it.

So a delivery still in flight when that derived wait runs out is not a delivery that is going
well — it is a substrate that has run past a bound it declared and did not end its own delivery.
Nobody witnessed an ending, so there is none to report: it is `DELIVERY_UNESTABLISHED`, and fatal
for the same reason every unestablished fate is. There is exactly one place that asks the
witnesses what a delivery did, and it is the one that carries the delivery: `_follow`.

**A prefix left behind is fatal to the run, and that is a decision rather than an omission.** A
payload that reached the head's terminal in part leaves a prefix that nothing can take back: the
head has already read some of it, so flushing the pty's input queue (`TCIFLUSH` discards only what
the head has *not* read) repairs nothing, and admission re-opening would let the next payload land
against that fragment and be read as one line with it. Silently gluing them is the one thing this
must not do. So `DELIVERY_LEFT_A_PREFIX` closes admission here **and** on the substrate, and every
later `deliver` for that head is refused `HEAD_DRAINING` naming the reason. `DELIVERY_UNESTABLISHED`
closes admission for the same reason and a weaker one: a delivery whose fate is unknown *may* have
left a prefix, and admission left open over a maybe-prefix is exactly the glue this forbids.

**What `HEAD_OK` / `DELIVERY_ARRIVED` means, and it is stronger than the byte count.** It means
the head received *this payload as its own message* — not that these bytes were written. Because
the delivery is always watched to its end, "all of it landed" is the whole payload including the
newline that ends the line, so a receipt that is true about its own bytes cannot be false about the
message they joined. A partial arrival is never `ok`, and it is never followed by a silent second
admission over what it left.

**A refusal the substrate stated is never thrown away.** Every reader of a frame — `status`,
`input`, `drain`, `attach` — tests `ok` before it believes a word of the contents, and a refusal
the supervisor stated to a question asked *before* a payload was offered classifies as a refusal
**before** the offer rather than as an unknown fate after one. Both of the substrate's bounds —
its connection bound and its attach bound — are transient and self-clearing, so they are
`HEAD_BUSY`: never `_fatal`, never a closed admission, never a drain. Reading such a frame for its
contents without believing its `ok` was how a live, idle, merely popular head came to be closed for
the rest of this runtime's life. The one reader that does not test `ok` is `_ask_to_stop`, which
believes nothing it reads: the outcome of a stop is decided by the launch identity.

**Ordering of the journal against a drain this runtime sends.** The drain that a fatal delivery
sends can only be written after that delivery's own `input.accepted`, because the state is only
fatal once the substrate has said the delivery ended — and the supervisor writes the journal record
before it answers that. The one exception is `DELIVERY_UNESTABLISHED`, where the supervisor is not
answering at all: if it recovers, its `drain.requested` can precede the `input.accepted` of the
payload it was still carrying. That ordering is given up knowingly — closing admission over a
payload whose fate cannot be established outranks the order of two records — and it is the honest
remainder of observation 2 of the substrate card's review rather than a claim that it cannot happen.

**The turn, the epoch and the admission outlive the process that granted them** (secretary-1479).
The production dispatcher is a systemd timer: every tick is a new process, so a runtime built at
the start of one holds an empty `HeadActivity` and used to say, about a head another tick had
drained, that it admitted work. Three promises died at that boundary — a drain that refuses the
next turn, a running turn that is not interrupted, and a rotation that happens when the last lease
closes — and all three are now recovered rather than remembered, from the two witnesses this
backend already owns and with nothing new stored:

  * **the supervisor**, in one request. `status` already carries `alive`, `draining`, `stopping`,
    `turn_open`, `turn` and `journal_seq`; it is the head's present tense and it is asked first;
  * **its journal**, when the supervisor is gone, read as a bounded tail of
    `local_pty.JOURNAL_TAIL_BYTES` and replayed in sequence order. A supervised role's run
    directory is reused and the journal is append-only across incarnations, so a full read per tick
    is a cost that grows with the head's history; `run.started` inside the window is what keeps a
    previous incarnation's drain or turn from answering for this one.

`_rehydrate` does it inside the same lock and the same critical section as the verb it serves —
**every critical section, and once within each**, because a snapshot taken by an earlier verb is
not this decision's snapshot and comparing against one is how a conditional stop came to kill a
turn another tick had opened in between. The cost obligation 3 bounds is held at one status
request per critical section by passing that one answer around (`_Probe`) rather than by
remembering it. Rehydration only ever moves this object in the direction the head has already
gone: the epoch is raised and never lowered, a turn is adopted and never granted over one that is
held, admission is closed and never re-opened.

**The activity epoch of a head on this backend is its journal sequence, and it is nothing else** —
strictly increasing, recovered from the file when the journal is opened, and therefore a number one
tick can hand out and another can compare. No verb here synthesises one: `start`, `deliver`,
`observe`, `request_drain` and `stop` all return a sequence some witness actually stated, and a
witness that could state none leaves the epoch where it was. `ticks` stays what it was — a
diagnostic, moved by `noted`, and never a quiescence decision.

**And unknown is neither freedom nor a lease.** A head whose run directory holds debris that
neither witness can read has its admission closed, so no new turn is admitted on an unknown; that
includes a journal tail that cannot account for its own shape — torn, unreadable, out of order, or
begun mid-history — because "I did not see a drain" is not "there was none". It is given no lease,
because a fabricated one would be a fence only the head itself could ever lift.

**A head that has positively ended is closed too, and that is what makes it rotatable.** Closing
admission is not a fence; a lease is, and the dead are given none. `rotation_ready` means "takes no
more work and holds no turn", so a head whose supervisor says it exited, or whose launch identity
says its process is dead, can only answer that truthfully once admission is shut. Nothing stands
between it and its replacement: a bring-up is decided by the launch identity alone, and `start`
drops what this runtime concluded about the incarnation that ended before a new one takes its id.
It is the same asymmetry secretary-1468 and secretary-1478 drew: only positive evidence acts.

**Liveness is the launch identity, and there is no second scheme.** `head.identity.
head_process_status` — the reader the watchdog already applies, unchanged, and re-exported to the
control plane under its old name — is passed in by whoever builds this runtime, exactly as
`stop_if_quiescent` takes `head_process_alive` from its caller on the legacy backend. It is a
constructor argument rather than an import so that every builder of this runtime is on the same
reader, and because inventing a second pid-file reader here is the failure this argument exists to
prevent.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .head import local_pty
from .head.local_pty import protocol
from .head.operations import (
    HeadNudgeFailed,
    HeadOperationError,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    NudgePointer,
)
from .head.run import HeadRun, StopInitiator, new_run_id
from .head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    OBSERVE_NO_ADDRESS,
    AttachReceipt,
    DeliverReceipt,
    DrainReceipt,
    HeadActivity,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
    TurnLease,
)
from .head.spec import HeadSpec
from .head.task_ref import TaskRef
from .tui_delivery import (
    DELIVERY_CONFIRMED,
    NUDGE_FILE_MODE,
    READINESS_BUSY,
    READINESS_READY,
    DeliveryEvidence,
    DeliveryOutcome,
    payload_fingerprint,
)

#: What a supervisor that cannot be spoken to raises. `LocalPtyError` is the substrate's own
#: refusal — no socket, a connection the supervisor closed — and a bare `OSError` is the same fact
#: arriving from the kernel instead: a supervisor that exits mid-request resets the connection.
#: Both mean "this head could not be reached", and telling them apart here would only let one of
#: them escape as an exception where the other became a receipt.
_UNREACHABLE = (local_pty.LocalPtyError, OSError)

#: How the launch-identity reader is called. The shape of `secretary.dispatcher_watchdog`'s
#: `head_process_status`, which is the one this runtime is meant to be given.
IdentityReader = Callable[..., Mapping[str, Any]]

#: Why an observation of a supervised head says what it says. Tokens, for the same reason the
#: pane-flavoured ones on the boundary are: callers route on them.
#: The run directory holds nothing addressable — no socket and no journal.
OBSERVE_NO_RUN_DIRECTORY = "no_run_directory"
#: The socket did not answer, and the head's launch identity says its process is still alive. The
#: head is somebody's to account for; it is emphatically not a head that has ended.
OBSERVE_SUPERVISOR_UNREACHABLE = "supervisor_unreachable"
#: The head's own process has ended, by the supervisor's answer or by the launch identity.
OBSERVE_HEAD_EXITED = "head_exited"
#: The socket answered something this runtime cannot read as a status. Not an answer about the head.
OBSERVE_STATUS_UNREADABLE = "status_unreadable"

#: What became of one delivery. The closed vocabulary this backend decides in exactly one place —
#: `_delivery_report` — and that every branch afterwards reads by name. It is a value on the report
#: rather than a predicate anybody can re-derive, because a re-derivation is what every defect of
#: this file's first cut was: one read "still going" out of a boolean over the byte counts, another
#: read "nothing landed" out of which exception had arrived.
#: Four names, and there is deliberately no fifth for "still landing": the wait is derived from the
#: substrate's own bound, so a delivery this runtime is watching is one the substrate has not
#: finished, and one it has stopped watching is one the substrate said it had.
#: All of it reached the head's terminal.
DELIVERY_ARRIVED = "arrived"
#: It ended part-way. Fatal: the prefix on the terminal cannot be taken back.
DELIVERY_LEFT_A_PREFIX = "left_a_prefix"
#: It ended and the kernel took nothing. The terminal is as it was and the head is still worth
#: delivering to.
DELIVERY_LANDED_NOTHING = "landed_nothing"
#: It was offered and what became of it could not be established: neither witness could say, or
#: the substrate ran past the delivery bound it declared without ending it, or it went unanswered
#: before a delivery id existed to ask the journal about — all the same fact, that nobody
#: witnessed an ending. Fatal, because a fate that cannot be established may be a prefix.
DELIVERY_UNESTABLISHED = "unestablished"
#: The outcomes after which this runtime hands the head no more work, named as a set rather than
#: recomputed at each site.
FATAL_DELIVERY_OUTCOMES = frozenset({DELIVERY_LEFT_A_PREFIX, DELIVERY_UNESTABLISHED})

#: `delivery_state` on the receipt for a delivery whose fate could not be established. The
#: substrate's own four states say what a delivery *did*; this fifth one says that nobody could be
#: asked, which is a different fact and must not be reported as any of the four.
DELIVERY_STATE_UNKNOWN = "unknown"

#: Why a `deliver` says what it says, beside the delivery state it carries.
DELIVER_NOT_ADMITTED = "not_admitted"
DELIVER_STALLED = "delivery_stalled"
DELIVER_FAILED = "delivery_failed"
DELIVER_UNESTABLISHED = "delivery_unestablished"
DELIVER_PREFIX_IS_FATAL = "partial_delivery_closed_this_head"
DELIVER_UNKNOWN_IS_FATAL = "unestablished_delivery_closed_this_head"
#: A head this runtime will hand no more work because a delivery left an irreversible prefix on its
#: terminal. The reason travels on every later refusal, so the caller learns why rather than only
#: that it was refused.
DRAIN_AFTER_PARTIAL_DELIVERY = (
    "a delivery reached this head's terminal in part and could not be taken back: admission is "
    "closed rather than re-opened over the fragment it left"
)
#: The same gate for the same reason, over a delivery whose fate nobody could establish: admission
#: is not re-opened over a payload that may have left a prefix.
DRAIN_AFTER_UNESTABLISHED_DELIVERY = (
    "this head was given a payload and what became of it could not be established: admission is "
    "closed rather than re-opened over bytes that may be sitting on its terminal"
)
#: What a drain on this backend really does, said once so that every receipt can carry it.
DRAIN_HEAD_SIGNALLED = (
    "the process that owns this head was told: its supervisor closed admission, wrote "
    "drain.requested into the head's journal, and refuses every later input by name"
)
DRAIN_HEAD_NOT_SIGNALLED = (
    "this runtime hands the head no more work, but its supervisor could not be told, so the head's "
    "own socket would still admit a payload from somebody else"
)

#: The named grace this runtime adds to the substrate's own delivery bound, and the whole of what
#: it decides about waiting. It can only ever *extend* the wait: the bound is the substrate's, read
#: back off the delivery it admitted, and this is the slack that covers the round trips around it —
#: the answer that carried the bound, the polls, a loaded host. There is no way to spell a wait
#: shorter than the substrate's own, which is the property this card is a recut for.
DELIVERY_GRACE_SECONDS = 5.0

#: The bound this runtime derives its wait from when the substrate admitted a delivery without
#: declaring one on it. Still the substrate's own number — the default `INPUT_DELIVERY_SECONDS`
#: that a supervisor started without `--delivery-seconds` uses — rather than a second knob here.
UNDECLARED_DELIVERY_BOUND = protocol.INPUT_DELIVERY_SECONDS

#: Why a stop-if-quiescent refused. The same two tokens the legacy backend uses, because the
#: refusals mean the same thing and a caller must not have to tell the backends apart to read them.
STOP_TURN_IN_FLIGHT = "turn_in_flight"
STOP_ACTIVITY_SINCE = "activity_since_expected_epoch"

#: Why a bring-up was refused before anything was spawned. Two refusals, two tokens, because they
#: are made by two different witnesses and a caller must not have to tell them apart by reading a
#: sentence: `START_TURN_IN_FLIGHT` is this runtime's own outstanding turn — in-process, and true
#: only for a head *this* object handed a turn to — while `START_HEAD_ALREADY_UP` is the head's own
#: launch identity on disk, which is what a runtime constructed a tick later has instead of a memory.
START_TURN_IN_FLIGHT = "turn_in_flight"
START_HEAD_ALREADY_UP = "head_already_up"

#: How long `stop` waits for a signalled head to actually be gone before it says it could not be
#: confirmed. Above the supervisor's own escalation grace, so a head that only dies to `SIGKILL`
#: is still seen to die rather than reported as a stop that failed.
STOP_CONFIRM_SECONDS = 10.0
_CONFIRM_POLL_SECONDS = 0.05

#: Where the durable answer about one head came from, when this runtime asked for it (secretary-1479).
#: Five values, because a caller — and every branch below — has to be able to tell "the head says it
#: is idle" from "nobody could say anything about it" from "a live supervisor declined to be asked
#: right now", and no two of those three may ever be one token.
#: The supervisor answered on its socket: the live, authoritative source, and one request.
REHYDRATED_FROM_SUPERVISOR = "supervisor"
#: The supervisor is gone or unreachable and its journal answered instead, from a bounded tail.
REHYDRATED_FROM_JOURNAL = "journal"
#: There is no head to say anything about: the run directory holds neither socket nor journal. Not
#: an unknown — the absence of everything a head leaves behind is a positive answer, exactly as it
#: is for the launch identity, and a bring-up or a delivery against it is refused by the verb
#: itself rather than fenced out here.
REHYDRATED_ABSENT = "absent"
#: A head's debris exists and neither witness could say what state it is in. Fail-closed: this is
#: the one that closes admission (obligation 2 of secretary-1479).
REHYDRATED_UNKNOWN = "unknown"
#: A live supervisor answered, and what it answered was one of the bounds it clears by itself —
#: `connection_limit`, `attach_limit`. It is a refusal *of this caller* and it is not a fact about
#: the head, so it is neither of the two above: not a durable answer to act on, and emphatically
#: not an unknown to fail closed over. A head at a bound is left exactly as it was — admission
#: untouched, no lease adopted, the epoch where it stood — and the question is worth asking again
#: the moment somebody else lets go. Collapsing this into `REHYDRATED_UNKNOWN` is how a live,
#: undrained, merely popular head came to be answered `HEAD_DRAINING` for good.
REHYDRATED_TRANSIENT = "transient"

#: Why admission is closed for a head this runtime never handed anything to. Both travel as the
#: reason on the `HEAD_DRAINING` receipt a later `deliver` gets, so the caller learns which of the
#: two it met rather than only that it was refused.
DELIVER_DRAINED_BEFORE_THIS_RUNTIME = (
    "a drain was requested for this head before this runtime existed, and the head's own "
    "supervisor or journal still says so: it takes no more work"
)
DELIVER_STATE_UNKNOWN = (
    "this head left a run directory behind and neither its supervisor nor its journal could say "
    "what state it is in: a new turn is refused rather than admitted on an unknown"
)
DELIVER_HEAD_ENDED = (
    "this head's own process has ended, by its supervisor's answer or by its launch identity: it "
    "takes no more work, and it holds no turn, so it is ready to be replaced"
)
#: Why admission is closed for a head *this* runtime drained itself. It is written down before the
#: drain's own rehydration runs, so that the note a later `deliver` reads names the drain that
#: actually happened rather than borrowing the wording of one that predates this process.
DELIVER_DRAINED_BY_THIS_RUNTIME = (
    "a drain was requested for this head, and the head's own supervisor or journal says so: it "
    "takes no more work"
)

#: The subject a turn adopted across a tick boundary carries. The turn was granted by a process
#: that is gone, so the caller it was granted for cannot be named — and inventing one would put a
#: false name into the refusal every later delivery reads.
ADOPTED_TURN_SUBJECT = "a caller from a previous tick"


class LocalPtyRuntimeError(RuntimeError):
    """This runtime was built in a shape that could not describe a head truthfully."""


@dataclass(frozen=True)
class AttachedStream:
    """A live head's stream, as a caller is handed one, and the address it was joined at.

    The client is the caller's to close, and closing it is the whole of detaching: it is not a
    message to the supervisor, it does not touch the head, and the output that arrives while
    nobody holds a stream is still in the supervisor's buffer for the next caller.
    """

    client: local_pty.SupervisorClient
    socket_path: str
    backlog: bytes = b""
    dropped_bytes: int = 0
    total_bytes: int = 0

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class DeliveryReport:
    """What one delivery did to the head's terminal: the decision, and the numbers behind it.

    `outcome` is the whole point of this object. It is one of the four names in this module's
    vocabulary, it is decided once by `_delivery_report`, and nothing downstream re-derives it: a
    caller — or a branch in this file — asks *which outcome this is*, never "did more than zero
    bytes land and was it not complete", which is the predicate that made a delivery still in
    flight look like one that had stalled.

    `state` is what the receipt carries as `delivery_state`: the substrate's own state verbatim,
    or `unknown` when the fate could not be established. `written` is what the kernel took from the
    supervisor and `offered` is what the caller handed over — always both, never one. `journalled`
    says whether the journal's own `input.accepted` was found for this delivery, so a reader can
    tell a fact corroborated in two places from one that only `status` could give.

    `floor` is the journal sequence this delivery was offered after, and it is what bounds which of
    the journal's `input.accepted` records may answer for it. Delivery ids restart at 1 in every
    supervisor incarnation while the journal is append-only across all of them, so without the
    floor a record from a previous incarnation of a reused run directory could answer for this
    payload.
    """

    outcome: str
    state: str
    written: int
    offered: int
    delivery_id: int = 0
    journalled: bool = False
    floor: int = 0
    detail: str = ""
    #: The highest journal sequence this delivery's watch actually saw, which is what the head's
    #: activity epoch is raised to afterwards. Never below `floor`: that one was read from the
    #: supervisor before the payload was offered.
    seq: int = 0

    @property
    def fatal(self) -> bool:
        """Whether this outcome closes the head, read from the outcome and from nothing else."""
        return self.outcome in FATAL_DELIVERY_OUTCOMES


def _delivery_report(
    *,
    state: str,
    written: int,
    offered: int,
    established: bool,
    delivery_id: int = 0,
    journalled: bool = False,
    floor: int = 0,
    detail: str = "",
    seq: int = 0,
) -> DeliveryReport:
    """Decide what became of one delivery. The only place in this backend that decides it.

    `established` is whether a witness — the supervisor's `status`, or the head's journal where
    there was a delivery id to match a record on — actually said what the delivery **ended as**. A
    delivery that was offered and then went unwitnessed is `DELIVERY_UNESTABLISHED`, and it is
    deliberately not folded into any of the four states the substrate has words for: "I could not find out" is not "nothing landed", and
    reporting it as the latter is what let a payload be written straight behind a fragment that had
    landed.

    A state of `in_flight` is the same fact wearing the substrate's own word. It can only be read
    here once the wait derived from the substrate's bound has run out, which means the substrate
    went past a bound it declared without ending its own delivery: nobody witnessed an ending, so
    there is none to report. Folding it in here rather than giving it an outcome of its own is what
    keeps "the delivery has not finished" from being a thing this runtime can return — the state
    that grew a register, a settlement and a second place to ask witnesses.
    """
    if not established or state == protocol.DELIVERY_IN_FLIGHT:
        return DeliveryReport(
            outcome=DELIVERY_UNESTABLISHED,
            state=DELIVERY_STATE_UNKNOWN,
            written=written,
            offered=offered,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=detail,
            seq=max(seq, floor),
        )
    if state == protocol.DELIVERY_COMPLETE and written >= offered:
        outcome = DELIVERY_ARRIVED
    elif written > 0:
        outcome = DELIVERY_LEFT_A_PREFIX
    else:
        outcome = DELIVERY_LANDED_NOTHING
    return DeliveryReport(
        outcome=outcome,
        state=state,
        written=written,
        offered=offered,
        delivery_id=delivery_id,
        journalled=journalled,
        floor=floor,
        detail=detail,
        seq=max(seq, floor),
    )


class LocalPtyHeadRuntime:
    """The six verbs over a head whose process, terminal and journal this product owns.

    `root` is where run directories live. It is deliberately a short path: a Unix socket address is
    bounded at about a hundred bytes, and a workspace path with a run id under it does not fit —
    `protocol.socket_path_for` refuses rather than failing opaquely later.

    `head_process_status` is the launch-identity reader, and it is required. There is no reading of
    a head's process this runtime invents for itself: the product has exactly one scheme for that —
    `pid`, `boot_id`, `proc_starttime_ticks` written by the head's own shell — and exactly one
    reader of it, `head.identity.head_process_status`, which `secretary.dispatcher_watchdog`
    re-exports for the control plane. Passing it in rather than importing it keeps every builder of
    this runtime — the dispatcher, and the mechanical-role driver in `runtime/dispatch.py` — on
    that one reader instead of on a scheme of its own.

    `connect_timeout` is what it says and nothing more: how long a supervisor that has not spoken
    on this connection yet may take to speak. It bounds reaching a head — the connect, and the
    single question each of `observe`, `attach`, `request_drain` and `stop` asks — and it is
    deliberately *not* a number that can shorten the watching of a delivery: the moment a delivery
    is admitted, `_put` rebounds the connection from the bound the substrate declared for that
    delivery, so every `status` inside `_follow` is bounded by the substrate's number plus the
    grace rather than by this one. It is named separately from "how long to wait for a delivery"
    because it answers a different question, and the two were one number only for as long as the
    second one had no answer of its own.

    There is no `delivery_timeout`. How long this runtime watches a delivery is not something it
    is told; it is **derived**, per delivery, from the bound the substrate declared on the delivery
    it admitted — the head's own `delivery_seconds`, the one `start` passed to the supervisor —
    plus `delivery_grace`, which can only ever extend it. A head that needs a longer reception has
    one number raised and the other follows it, and there is no configuration in which this runtime
    stops watching a delivery before the substrate has finished it. That is the whole cut of
    secretary-1466: the independent second knob is what made "the delivery is still going" a thing
    a verb could return, and everything that grew to carry such a delivery — a register, a
    settlement, a second place asking witnesses — is gone with it.

    **One lock for every head of this runtime, and the limitation that buys is named rather than
    hidden.** `deliver`, `request_drain`, `stop` and `stop_if_quiescent` are the four things that
    can contradict each other about one head, so they run one at a time under a lock this object
    owns — the legacy backend's arrangement, and it is per runtime there too. What is different
    here is how long the lock is held: a verb on the legacy backend is one session-manager call,
    while `deliver` holds this lock for the whole reception — up to the substrate's own
    `delivery_seconds` plus `delivery_grace`. So a slow delivery to one head delays `observe`,
    `attach`, `stop` and `deliver` for every *other* head this runtime holds, for that long.

    It is accepted here, and these are the terms:

      * **nothing is delayed today.** The dispatcher constructs its backends per tick process and
        drives every verb from that tick's single thread, so there is no second caller in existence
        to be made to wait. The delay is a property of a concurrency this product does not yet
        have;
      * **the hold is bounded, and by a number no profile can raise.** `delivery_seconds` is an
        argument of `start`, not a registry key: per-profile runtime selection (secretary-1467) did
        not give a profile one. Every head therefore comes up on the substrate's default of
        `protocol.INPUT_DELIVERY_SECONDS`, so the worst hold is that plus `DELIVERY_GRACE_SECONDS`
        — fifteen seconds — which is under one dispatcher tick;
      * **when it stops being acceptable**, which is the half a limitation is worth naming for. Two
        thresholds, either one of which is enough: a second caller — a thread, an operator command
        sharing one runtime object with a tick, a dispatcher that drives two heads at once — or a
        `delivery_seconds` a profile can raise. The moment a head's reception can be configured
        past a tick's own period, one head's delivery can starve another head's `stop`, and a stop
        that cannot run is how a head gets a second one opened beside it.

    The repair, when either threshold is crossed, is the one this docstring is standing in for: a
    lock per head held across the verb, with this runtime-wide lock reduced to the bookkeeping
    `HeadActivity` and `_fatal` need — they are shared across heads and `HeadActivity` explicitly
    locks nothing of its own, so the per-head lock cannot simply replace this one.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        head_process_status: IdentityReader,
        activity: HeadActivity | None = None,
        spawn: Callable[..., local_pty.HeadHandle] = local_pty.spawn_head,
        connect_timeout: float = 5.0,
        delivery_grace: float = DELIVERY_GRACE_SECONDS,
        delivery_poll: float = 0.02,
        stop_timeout: float = STOP_CONFIRM_SECONDS,
    ) -> None:
        if not callable(head_process_status):
            raise LocalPtyRuntimeError(
                "this runtime is given the product's launch-identity reader; it does not invent a "
                "second way to ask whether a head's process is alive"
            )
        if delivery_grace < 0:
            raise LocalPtyRuntimeError(
                "the grace over the substrate's delivery bound only ever extends the wait; a "
                "negative one would be the independent second knob this backend does not have"
            )
        self.root = Path(root)
        self.activity = activity or HeadActivity()
        self._identity = head_process_status
        self._spawn = spawn
        self._connect_timeout = connect_timeout
        self._delivery_grace = float(delivery_grace)
        self._delivery_poll = delivery_poll
        self._stop_timeout = stop_timeout
        # Reentrant, so `stop_if_quiescent` can perform `stop`.
        self._lock = threading.RLock()
        # Heads whose terminal holds an unfinished payload's prefix. Kept beside the activity
        # rather than inside it because it is this backend's fact, not the boundary's: no other
        # backend can leave a head in this state, and a caller reads it as the reason on a receipt.
        self._fatal: dict[str, str] = {}
        # Why a rehydrated admission is closed, for the refusal a later `deliver` hands back. Kept
        # beside `_fatal` and read after it: a terminal holding a prefix is the more specific fact.
        self._admission_notes: dict[str, str] = {}

    def delivery_wait_for(self, substrate_bound: float) -> float:
        """How long this runtime watches a delivery the substrate bounded at `substrate_bound`.

        The one place the wait is computed, and it is a function of the substrate's number rather
        than of anything this runtime was configured with: the grace is added, never subtracted, so
        the answer is always strictly longer than the bound it is derived from. A delivery the
        substrate admitted without declaring a bound on it is waited out at the substrate's own
        default, which is still the substrate's number and not a second one kept here.
        """
        bound = float(substrate_bound) if substrate_bound > 0 else UNDECLARED_DELIVERY_BOUND
        return bound + self._delivery_grace

    # -- the six verbs ------------------------------------------------------------------------

    def start(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        title: str,
        pointer: NudgePointer | None = None,
        run_id: str = "",
        role: str = "",
        run: HeadRun | None = None,
        subject: str = "",
        rows: int = 24,
        cols: int = 80,
        quiet_seconds: float | None = None,
        delivery_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
        **ignored: Any,
    ) -> StartReceipt:
        """Bring one head up under a supervisor of its own, and point it at its task.

        `title` is what Orca puts on a pane and this backend has no pane, so it is accepted and
        unused rather than refused: the caller above the boundary hands both backends the same
        arguments, and a verb that rejected one of them would make the boundary a lie.

        A bring-up that names a run this runtime is already holding a turn for is refused before
        anything is started, exactly as on the legacy backend: the invariant belongs to the
        boundary, not to a convention its callers keep.

        That refusal is made twice, by two witnesses, and both are made before anything is spawned.
        The turn lease is this runtime's own memory, and it can only ever answer for the heads this
        object started; the production dispatcher is a systemd timer, so every tick is a *new*
        process whose activity is empty by construction and for which a head brought up a tick ago
        never existed. `_already_up` is the second witness, and it is the head's own launch identity
        on disk — the one fact about a head that outlives the process that started it.
        """
        del title, ignored
        with self._lock:
            claimed = run.run_id if run is not None else (run_id or "")
            if claimed:
                held = self.activity.lease(claimed)
                if held is not None:
                    return StartReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this runtime is already running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} on run {claimed}: a bring-up over it "
                            "would claim a head that is already up"
                        ),
                        evidence={"refusal": START_TURN_IN_FLIGHT, "run_id": claimed},
                        epoch=self.activity.epoch(claimed),
                        lease=held,
                    )
                already_up = self._already_up(
                    claimed,
                    run,
                    spec=spec,
                    workspace=workspace,
                    task_ref=task_ref,
                    role=role,
                )
                if already_up is not None:
                    return already_up
            identity = claimed or new_run_id()
            try:
                handle = self._spawn(
                    root=self.root,
                    run_id=identity,
                    role=role or (run.role if run is not None else ""),
                    task=task_ref.ref or task_ref.document,
                    command=command,
                    cwd=workspace,
                    rows=rows,
                    cols=cols,
                    quiet_seconds=quiet_seconds,
                    delivery_seconds=delivery_seconds,
                    env=env,
                )
            except local_pty.LocalPtySpawnError as exc:
                return StartReceipt(
                    status=_spawn_status(exc),
                    run=run,
                    reason=str(exc),
                    failure=_spawn_failure(exc, run),
                    evidence={"reason": exc.reason, "detail": exc.detail},
                    epoch=self.activity.epoch(identity),
                )
            live = (run or HeadRun(
                run_id=identity, spec=spec, workspace=workspace, task_ref=task_ref, role=role,
            )).rebound(str(handle.socket_path), leaf=identity)
            live = _with_pid_file(live, str(handle.pid_file))
            # A new supervisor over a new pty is a new incarnation, so whatever this runtime had
            # concluded about the one that ended under this id — an admission closed because it
            # was dead or unreadable, a terminal holding somebody's prefix — is about a terminal
            # that no longer exists. It is dropped here rather than left to fence the head that
            # was just brought up. The epoch is not synthesised with it: it is raised to what the
            # head's own journal is at, which is the same scale the next tick will read.
            self.activity.forget(identity)
            self._fatal.pop(identity, None)
            self._admission_notes.pop(identity, None)
            self.activity.noted(identity)
            epoch = self._durable_epoch(live)
            if pointer is None:
                return StartReceipt(
                    status=HEAD_OK,
                    run=live,
                    epoch=epoch,
                    rotation_ready=self.activity.rotatable(identity),
                )
            lease = self.activity.grant(identity, subject or "head-launch")
            try:
                report, refusal = self._put(live, pointer, subject or "head-launch")
            except BaseException:
                # Whatever went wrong, the turn this bring-up handed itself is not running: a
                # lease left behind here would refuse every later delivery to a head nobody is
                # working on. `deliver` has guarded this since it was written; this one had not.
                self.activity.release(identity)
                raise
            if refusal is not None or report is None or report.outcome != DELIVERY_ARRIVED:
                # A bring-up whose prompt did not reach the head is a bring-up that left a head
                # nobody has given a task to. It is ended here rather than handed back running,
                # and which of the two refusals this is depends on whether that stop was confirmed
                # — the same distinction `spawn` draws between an aborted and a failed bring-up.
                self.activity.release(identity)
                return self._abandon_bring_up(live, report, refusal, epoch)
            self.activity.noted(identity)
            return StartReceipt(
                status=HEAD_OK,
                run=live.working(),
                delivery=_outcome_of(live, pointer, report, subject or "head-launch"),
                epoch=self.activity.advance_to(identity, report.seq),
                lease=lease,
                rotation_ready=self.activity.rotatable(identity),
            )

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, subject: str = "", **ignored: Any
    ) -> DeliverReceipt:
        """Put one prompt in front of a running head, and say what became of the bytes.

        The refusals before anything is written are the legacy backend's, and they mean the same
        things: `HEAD_DRAINING` is a head this runtime hands no more work — a drain was requested,
        or a partial delivery closed it — and `HEAD_BUSY` is a turn this runtime handed out that is
        still running. Neither is a queue.

        What follows the admission is this backend's own, and it is the reason `ok` can be trusted
        here: the payload is followed **to its end** — the wait is the substrate's own bound for
        this delivery plus the named grace, so there is no way for this verb to return while the
        substrate is still writing — what became of it is decided once as one of the four names in
        this module's vocabulary, and the receipt carries that decision as its status, its
        `delivery_state` and its two byte counts. `ok` is only ever `DELIVERY_ARRIVED`, and it
        means the head received *this payload as its own message* rather than that these bytes were
        written. See the module docstring for which of the outcomes close this head and why.
        """
        del ignored
        with self._lock:
            # Inside the lock and before the first of the two refusals is decided: what a previous
            # tick's drain closed, and what turn a previous tick handed out, are facts about this
            # head that this object was constructed without. Reading them anywhere else — outside
            # the lock, or after `admits` has already answered — would be reading them at a
            # different moment from the decision they are made for.
            # One successful status frame for the whole of this section (obligation 3): the same
            # answer rehydrates this object, decides whether a lease it adopts is still running,
            # and is the pre-offer frame `_put` would otherwise ask the supervisor for a second
            # time. If this first attempt itself fails, `_Probe` carries exactly one consumable
            # recovery attempt; there is no third request in the section.
            _, probe = self._section_probe(run)
            self._rehydrate(run, probe)
            if not self.activity.admits(run.run_id):
                # First, and before the turn is looked at: a head this runtime hands no more work
                # is `HEAD_DRAINING` whatever else is true of it — a drain was requested, or an
                # earlier delivery left something on its terminal — and answering `HEAD_BUSY` for
                # such a head would tell a caller to come back to one that will never take a
                # payload again.
                return DeliverReceipt(
                    status=HEAD_DRAINING,
                    run=run,
                    reason=(
                        self._fatal.get(run.run_id)
                        or self._admission_notes.get(run.run_id)
                        or DELIVER_NOT_ADMITTED
                    ),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            held = self.activity.lease(run.run_id)
            if held is not None:
                running = self._turn_still_running(run, probe)
                if running:
                    return DeliverReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this head is running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} ({running}): one head runs one turn, "
                            "and this delivery is not queued behind it"
                        ),
                        epoch=self.activity.epoch(run.run_id),
                        lease=held,
                    )
                self.activity.release(run.run_id)
            lease = self.activity.grant(run.run_id, subject or "head-nudge")
            try:
                report, refusal = self._put(run, pointer, subject or "head-nudge", probe)
            except BaseException:
                self.activity.release(run.run_id)
                raise
            if refusal is not None:
                # Refused at admission: nothing of this attempt reached the terminal, so the turn
                # it would have started is handed back.
                self.activity.release(run.run_id)
                return DeliverReceipt(
                    status=refusal.status,
                    run=run,
                    reason=refusal.reason,
                    failure=refusal.failure,
                    evidence=refusal.evidence,
                    epoch=self.activity.epoch(run.run_id),
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            assert report is not None
            # The runtime-wide diagnostic moves; the epoch does not move with it. The epoch this
            # delivery hands back is the head's journal sequence as the delivery's own watch last
            # read it, so that the number a caller keeps is on the scale the next tick rehydrates
            # onto — and a watch that could read no sequence at all leaves it where it was rather
            # than adding one to a number of a different kind.
            self.activity.noted(run.run_id)
            epoch = self.activity.advance_to(run.run_id, report.seq)
            if report.outcome == DELIVERY_ARRIVED:
                return DeliverReceipt(
                    status=HEAD_OK,
                    run=run.working() if run.running else run,
                    delivery=_outcome_of(run, pointer, report, subject or "head-nudge"),
                    delivery_state=report.state,
                    delivered_bytes=report.written,
                    offered_bytes=report.offered,
                    evidence=report,
                    epoch=epoch,
                    lease=lease,
                )
            return self._delivery_that_did_not_arrive(run, report, lease, epoch)

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """What the substrate can say about this head now: its status, its journal, its process.

        Nothing is guessed. `busy` comes from the supervisor's own turn state and from the turn
        lease this runtime is holding, and stays `None` for every answer that is not one — a socket
        that did not answer is not a head that is idle. The epoch is the head's own journal
        sequence, which moves when the substrate wrote a record and not on the fact of having
        looked: looking twice at a quiet head reads the same number twice, which is exactly the
        fact a caller watching for progress needs to be able to read.

        The supervisor is asked **once**, and the same answer both rehydrates this runtime and
        supplies the observation (obligation 3). Asking twice would not only cost two requests; it
        would report a head as it was at one moment out of an epoch read at another.
        """
        with self._lock:
            address = self._address(run)
            probe = self._probe(address) if address is not None else None
            # From that one answer, so that the epoch, the turn and the admission this observation
            # reports are the head's own and not this object's ignorance of a head it did not
            # start — and are the head's own *at the moment this observation was made*.
            self._rehydrate(run, probe)
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            if address is None or probe is None:
                return _unobservable(run, OBSERVE_NO_ADDRESS, epoch, lease, rotatable)
            if not address.journal_path.exists() and not address.socket_path.exists():
                return _unobservable(run, OBSERVE_NO_RUN_DIRECTORY, epoch, lease, rotatable)
            if probe.error is not None:
                return self._unreachable(run, address, epoch, lease, rotatable, probe.error)
            status = probe.status
            if status is None:
                return _unobservable(
                    run, OBSERVE_STATUS_UNREADABLE, epoch, lease, rotatable, evidence=probe.answer,
                )
            if not status.get("alive"):
                # The supervisor reaped the head and says so. A dead head runs no turn, and the
                # lease this runtime may still be holding names a turn that ended with the process.
                self.activity.release(run.run_id)
                return ObserveReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_HEAD_EXITED,
                    evidence=status,
                    epoch=epoch,
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                    connected=False,
                    busy=False,
                )
            # `epoch` is already this status frame's own `journal_seq`: the rehydration above was
            # made out of this very answer. There is deliberately nothing else here — an output
            # counter this runtime turned into a local increment would leave the receipt carrying
            # a number on a scale no other tick has, which is what obligation 5 forbids.
            turn_open = bool(status.get("turn_open"))
            delivering = _in_flight(status)
            if lease is not None and not turn_open and not delivering:
                # The turn this runtime handed out has ended, and this is where it learns that.
                # It is also where a drained head becomes rotatable, because the last turn it was
                # holding has just closed.
                self.activity.release(run.run_id)
                lease = None
                rotatable = self.activity.rotatable(run.run_id)
            return ObserveReceipt(
                status=HEAD_OK,
                run=run,
                evidence=status,
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=True,
                readiness=READINESS_BUSY if (turn_open or delivering) else READINESS_READY,
                last_output_at=_last_event_at(address),
                busy=turn_open or delivering or lease is not None,
            )

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Take this head out of service, at this runtime *and* at the process that owns it.

        Two facts, and they are separate because a backend can own one without the other. This one
        owns both when the socket answers: admission closes here, and the supervisor closes it
        there — `drain.requested` in the head's own journal, and every later `input` refused by
        name — which is read back from `status` before `head_signalled` is claimed. When the socket
        cannot be reached the gate is still real locally and the receipt says exactly that instead.

        What a drain closes is admission, never the turn. A head that is mid-turn keeps running it
        and keeps its lease; nothing is interrupted, cancelled or written into its terminal. When
        that last turn closes the head is done, and `rotation_ready` says so.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a drain names who requested it")
        with self._lock:
            self.activity.close_admission(run.run_id)
            self._admission_notes.setdefault(run.run_id, DELIVER_DRAINED_BY_THIS_RUNTIME)
            signalled, evidence, seq, probe = self._close_substrate_admission(run, initiator)
            # Rehydrated out of that same read-back, and therefore *after* the drain rather than
            # before it. A drain must not lose the turn a previous tick granted — `rotation_ready`
            # on this receipt is a statement about that turn — and the read-back frame states it:
            # a drain closes admission and never a turn, so the frame taken after it says
            # everything the frame taken before it would have, one status request later in time
            # and none later in cost. This section spends exactly one (obligation 3), and it is
            # the one `head_signalled` is already claimed from.
            self._rehydrate(run, probe)
            # The sequence read back *after* the drain, so that the epoch on this receipt counts
            # the `drain.requested` record this verb just caused rather than the state before it.
            # It comes off the read-back `head_signalled` is already claimed from: no second
            # request, and no number of this runtime's own invention.
            self.activity.noted(run.run_id)
            return DrainReceipt(
                status=HEAD_OK if signalled else HEAD_ALIVE,
                run=run,
                reason=DRAIN_HEAD_SIGNALLED if signalled else DRAIN_HEAD_NOT_SIGNALLED,
                evidence=evidence,
                draining=True,
                head_signalled=signalled,
                epoch=self.activity.advance_to(run.run_id, seq),
                lease=self.activity.lease(run.run_id),
                rotation_ready=self.activity.rotatable(run.run_id),
            )

    def stop(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        signal_name: str = "TERM",
        **ignored: Any,
    ) -> StopReceipt:
        """End this head, and confirm the ending against the head's own launch identity.

        Unconditional, as on the legacy backend: a freeze, an operator taking a head down and a
        bring-up cleaning up after itself all mean "end this now" and must not be refused because
        the head happens to be mid-turn. A stop meant to happen only while the head is quiet asks
        for that by name, through `stop_if_quiescent`.

        The initiator is recorded on the run before the signal is sent, so a stop that outlives
        this process still names who began it. The confirmation is the launch identity going dead —
        not the socket disappearing, which says the supervisor let go and says nothing about the
        head.
        """
        del ignored
        with self._lock:
            finishing = run.finishing(initiator)
            address = self._address(run)
            if address is None:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason="this head has no run directory to address, so nothing could be stopped",
                    failure=HeadStopFailed("a head with no address cannot be stopped", run=finishing),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                )
            asked = self._ask_to_stop(address, initiator, signal_name)
            gone = self._await_head_gone(address, run)
            if not gone:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=(
                        f"this head was asked to stop and its process was still there "
                        f"{self._stop_timeout:g}s later"
                    ),
                    failure=HeadStopFailed(
                        "the head's process outlived the stop it was sent", run=finishing
                    ),
                    evidence=asked,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            # The last thing the head's own journal says, read before this runtime forgets the
            # head: a stop's receipt carries a sequence the next tick could still compare, and a
            # process-local increment here would be the very number obligation 5 rules out.
            self.activity.noted(run.run_id)
            epoch = self._durable_epoch(run)
            self.activity.forget(run.run_id)
            self._fatal.pop(run.run_id, None)
            self._admission_notes.pop(run.run_id, None)
            return StopReceipt(
                status=HEAD_OK, run=finishing.exited(), evidence=asked, epoch=epoch
            )

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Join a caller to this head's live stream, through the substrate's own bounded attach.

        The stream travels on `evidence` as an `AttachedStream`: the connected client, the output
        the head produced before this caller arrived, and how much of it the supervisor had already
        dropped. `handle` is what the attachment is addressed by — the socket — so a caller that
        can do something with an address still gets one when it is refused a stream.

        Detaching is closing that client. It is not a message, it does not reach the head, and the
        head's output goes on being buffered for whoever attaches next.
        """
        with self._lock:
            self._rehydrate(run)
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            address = self._address(run)
            if address is None or not address.socket_path.exists():
                return AttachReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_NO_RUN_DIRECTORY if address is not None else OBSERVE_NO_ADDRESS,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=run.handle,
                    leaf=run.leaf or run.run_id,
                )
            try:
                client = self._connect(address)
            except _UNREACHABLE as exc:
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            try:
                answer = client.attach()
            except _UNREACHABLE as exc:
                # Connected, and then nothing came back. A verb of this boundary answers with a
                # receipt for that too: an attachment that did not happen against a head that is
                # still the caller's to account for, classified by the head's process rather than
                # by the socket that dropped.
                client.close()
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            if not answer.get("ok"):
                client.close()
                error = str(answer.get("error") or "")
                return AttachReceipt(
                    status=_refusal_status(error),
                    run=run,
                    reason=error or "the supervisor refused the attachment",
                    evidence=answer,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            return AttachReceipt(
                status=HEAD_OK,
                run=run,
                evidence=AttachedStream(
                    client=client,
                    socket_path=str(address.socket_path),
                    backlog=bytes(answer.get("bytes_data") or b""),
                    dropped_bytes=int(answer.get("dropped_bytes") or 0),
                    total_bytes=int(answer.get("total_bytes") or 0),
                ),
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
            )

    # -- not a verb ---------------------------------------------------------------------------

    def stop_if_quiescent(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        expected_activity_epoch: int,
        head_process_alive: bool,
        signal_name: str = "TERM",
    ) -> StopReceipt:
        """End this head only while it is still quiet, with the check and the stop indivisible.

        The composition the legacy backend owes and this one owes identically, in the order
        secretary-1462 fixed and for the same reasons:

          1. **the head's epoch against the caller's**, first and before anything is probed. It
             moved, so the judgement that decided this head was finished has expired;
          2. **the head's process, by the fact the caller established.** A process established not
             alive makes any outstanding lease stale by definition — the turn it names ended when
             the process did — so the lease is closed and the supervisor is **not** asked. Liveness
             outranks terminal readiness here exactly as it does on Orca: a substrate that is
             answering about a head that is gone would still report the turn it last saw, and a
             signal that cannot tell "working" from "not there" cannot hold a veto over "not
             there";
          3. **only for a live process, the end of the turn**, read from the supervisor;
          4. **admission closed, then the stop**, with a refusal putting admission back exactly as
             it found it — including when an earlier drain had already closed it.

        `head_process_alive` is a required argument rather than something read here, for the same
        reason it is one on the legacy backend: it is the caller's own launch-identity evidence,
        the very reading that made it decide this head needed replacing, and re-deriving it here
        would answer a different question at a different moment.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a stop names who ended the head")
        with self._lock:
            # Inside the same lock and the same critical section as the comparison it serves, and
            # unconditionally — a snapshot an earlier verb of this object took is a snapshot of a
            # different moment, and comparing against one is how this stop came to kill the turn
            # a concurrent tick had opened after that verb ran. It is not a fifth step of the
            # order above and it is not a probe of the head's readiness: it is how this object
            # comes to hold the epoch and the turn that the process which granted them would have
            # held, so that step 1 compares two numbers on one scale instead of comparing a
            # caller's number against an empty — or a stale — memory.
            #
            # One status frame for this whole section (obligation 3): the answer that rehydrates
            # the epoch and the lease is the same answer step 3 reads the end of the turn from.
            # Two requests would also be two moments, and a lease adopted at one moment and tested
            # at another is the very comparison across time this rehydration exists to remove.
            _, probe = self._section_probe(run)
            self._rehydrate(run, probe)
            epoch = self.activity.epoch(run.run_id)
            if epoch != expected_activity_epoch:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=run,
                    reason=STOP_ACTIVITY_SINCE,
                    evidence={"expected_epoch": expected_activity_epoch, "epoch": epoch},
                    epoch=epoch,
                )
            held = self.activity.lease(run.run_id)
            if held is not None and not head_process_alive:
                self.activity.release(run.run_id)
                held = None
            if held is not None:
                running = self._turn_still_running(run, probe)
                if running:
                    return StopReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=STOP_TURN_IN_FLIGHT,
                        evidence=running,
                        epoch=epoch,
                        lease=held,
                    )
                self.activity.release(run.run_id)
            admitted = self.activity.admits(run.run_id)
            self.activity.close_admission(run.run_id)
            try:
                receipt = self.stop(run, initiator, signal_name=signal_name)
            except BaseException:
                if admitted:
                    self.activity.open_admission(run.run_id)
                raise
            if not receipt.ok and admitted:
                # Nothing was stopped, so nothing was taken out of service either.
                self.activity.open_admission(run.run_id)
            return receipt

    def forget_head(self, run_id: str) -> None:
        """Drop what this runtime remembers about a head somebody else's stop has ended."""
        if not run_id:
            return
        with self._lock:
            self.activity.forget(run_id)
            self._fatal.pop(run_id, None)
            self._admission_notes.pop(run_id, None)

    def activity_epoch(self, run: HeadRun) -> int:
        """This head's activity epoch, asked of the head rather than of this object's memory.

        The reader a caller uses when it is about to hand the epoch back to `stop_if_quiescent`
        and is not the process that granted it. `self.activity.epoch` answers out of memory, and
        memory is empty in a tick that has just started; this rehydrates first, under the same
        lock the conditional stop takes, so the number a tick reads and the number that tick's
        stop compares it against are on the same scale and describe the same head.
        """
        with self._lock:
            self._rehydrate(run)
            return self.activity.epoch(run.run_id)

    # -- what outlived the process that knew it -------------------------------------------------

    def _rehydrate(self, run: HeadRun, probe: _Probe | None = None) -> None:
        """Recover this head's turn, epoch and admission from the substrate that outlived them.

        The whole of secretary-1479, and it stores nothing new: the durable truth already exists
        in two places this backend owns, and until now nobody asked them.

          * **the supervisor**, over its socket, in one request — `alive`, `draining`, `stopping`,
            `turn_open`, `turn` and `journal_seq` are all on the answer `status` already gives;
          * **its journal**, when the supervisor is gone, read as a bounded tail
            (`local_pty.JOURNAL_TAIL_BYTES`). A supervised role's run directory is reused and its
            journal is append-only across incarnations, so reading the whole file every tick is a
            cost that grows with the head's history; the shape of the head *now* is in its last
            records, and `run.started` inside the window resets the derivation so that a previous
            incarnation's drain or turn cannot answer for this one.

        **Once per critical section, and every critical section.** Not once per head per process:
        that was a cache, and a cache is a snapshot whose freshness is decided by whoever happened
        to ask first rather than by the decision being made now. A conditional stop that compared
        an epoch rehydrated by an earlier verb compared a number the head had already moved past,
        and killed the turn another tick had opened in between. The bound of obligation 3 is kept
        where it belongs — **one status request per critical section** — by `_Probe`: a verb that
        needs the supervisor's answer for itself asks once and hands that answer to this.

        **Only in the direction the head already went.** The epoch is raised, never set; the turn
        is adopted, never granted over one that is held; admission is closed, never re-opened.
        Rehydration cannot invent activity, cannot evict a running turn and cannot put a head that
        somebody drained back into service.

        **A bound the substrate clears by itself is neither an answer nor an unknown.** Three
        classes of evidence and no collapsing between them: the supervisor's status frame or its
        journal act; a typed self-clearing refusal — `connection_limit`, `attach_limit` — is
        retried and leaves this head exactly as it was; and only a genuine silence fails closed.
        The middle one is a refusal of *this caller* by a supervisor that is alive and not
        draining, so treating it as the unknown below closed a live head's admission over a limit
        that clears the moment somebody lets go.

        **Unknown is not freedom, and it is not a lease either** (obligation 2). A head whose
        debris exists but whose state neither witness can state has its admission closed, so no
        new turn is admitted on an unknown; it is given no turn lease, because a fabricated lease
        would be a fence nothing could lift — it would make a head that is positively dead
        un-rotatable forever, and only the head itself ever writes the evidence that would clear
        it.

        **And a head that has positively ended is closed too, which is what makes it rotatable.**
        Closing admission over a dead head is not a fence: a fence is a lease, and the dead are
        given none. `rotatable` is "takes no more work and holds no turn", so a head whose
        supervisor says it exited — or whose launch identity says its process is dead — answers
        that truthfully only once the first half of it is closed. Its replacement is not held up
        by any of this, because a bring-up consults the launch identity and never admission
        (obligation 1), and `start` clears what this runtime believed about the incarnation that
        ended before the new one takes the same id.

        **Liveness is not asked here** (obligation 1). Whether a head's process is alive is
        `head_process_status` and stays `head_process_status`; the launch identity is consulted
        only for the one negative above, and this adds no second answer to that question.
        """
        run_id = run.run_id
        if not run_id:
            return
        address = self._address(run)
        if address is None:
            return
        state = self._durable_state(address, run_id, probe)
        if state.source in (REHYDRATED_ABSENT, REHYDRATED_TRANSIENT):
            # Nothing to recover from, and — for a bound the substrate clears by itself — nothing
            # this runtime is entitled to conclude either. The head is left exactly as it was:
            # admission untouched, no lease adopted, the epoch where it stood. Not even the
            # sequence moves, because a refusal frame carries none.
            return
        # The sequence first and unconditionally: it is the one thing a window that cannot account
        # for its shape still states truthfully, and the epoch is that number or it is nothing.
        self.activity.advance_to(run_id, state.seq)
        if state.source == REHYDRATED_UNKNOWN:
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_STATE_UNKNOWN)
            return
        if state.exited or self._identity_says_dead(address):
            # Positively ended. No lease is adopted — the journal's last word about a supervisor
            # killed mid-turn is `turn.started`, and believing it against a process that is gone
            # would hold the one lease nothing could ever release — and admission is closed, so
            # that the head this runtime can say nothing more about is one it can say is done.
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_HEAD_ENDED)
            return
        if state.draining:
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_DRAINED_BEFORE_THIS_RUNTIME)
        if state.turn_open:
            self.activity.adopt(
                run_id,
                TurnLease(
                    lease_id=f"{run_id}:turn-{state.turn}",
                    run_id=run_id,
                    subject=ADOPTED_TURN_SUBJECT,
                    granted_at_epoch=self.activity.epoch(run_id),
                ),
            )

    def _durable_epoch(self, run: HeadRun, probe: _Probe | None = None) -> int:
        """This head's epoch, raised to the journal sequence its own witnesses are at.

        The one way an epoch is ever produced on this backend (obligation 5). There is no second,
        process-local scale beside it: a receipt whose number was invented here is a number the
        next tick cannot compare to anything, and `stop_if_quiescent` would then be comparing a
        count of what one object did against a sequence the head wrote. A witness that cannot say
        a sequence leaves the epoch where it was — `advance_to` never lowers it — so the honest
        answer to "nobody could tell me" is the last thing somebody could.
        """
        address = self._address(run)
        if address is None:
            return self.activity.epoch(run.run_id)
        return self.activity.advance_to(
            run.run_id, self._durable_state(address, run.run_id, probe).seq
        )

    def _section_probe(self, run: HeadRun) -> tuple[_Address | None, _Probe | None]:
        """The successful-path status request, taken once at the top of a critical section.

        Its answer serves the rehydration, the turn question and the substrate call alike. A
        failed attempt carries one consumable recovery request into `_put`; `_Probe.spend_retry`
        makes a third request impossible even if another consumer later tries to recover too.
        `None` where there is no socket to ask — the journal answers those, and it costs no
        request.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return address, None
        return address, self._probe(address)

    def _probe(self, address: _Address) -> _Probe:
        """Ask the supervisor once what this head is doing, and keep whatever came of asking.

        On success this is the only status request in the section. Nothing answering gives the
        section one bounded recovery attempt, consumed by `_Probe.spend_retry` before it is made.
        It is a method rather than an inline `try` so that the three endings a socket has — an
        answer, a frame that is not one, and nothing at all — are separated in one place and read
        by name everywhere else.
        """
        try:
            with self._connect(address) as client:
                answer = client.status()
        except _UNREACHABLE as exc:
            return _Probe(error=exc, retry_available=True)
        if isinstance(answer, dict) and answer.get("ok"):
            return _Probe(status=answer)
        return _Probe(answer=answer)

    def _durable_state(
        self, address: _Address, run_id: str, probe: _Probe | None = None
    ) -> _DurableHead:
        """What the head itself still says about its turn, its admission and its epoch.

        The supervisor first and the journal second, in that order and never the other way round:
        a live supervisor is the head's present tense, while the journal is what it wrote down.
        The journal is not a fallback that can open a door the socket closed — a tail that cannot
        account for its own shape is an unknown here, and an unknown closes admission.

        `probe` is the answer a caller already has, when this critical section has already spent
        its one status request. Passing it is what keeps the cost of obligation 3 at one request
        for every verb that needs the supervisor's answer for itself as well.

        **Three classes of evidence, and none of them may become another.** A durable answer — this
        supervisor's status frame, or its journal — acts. A typed refusal the substrate clears by
        itself is retried and changes nothing: the journal is *not* consulted behind it, because a
        tail that cannot account for its shape would then answer for a supervisor that is alive and
        talking, and an unknown closes admission. And a genuine silence — no frame, or a frame this
        runtime cannot read — is the unknown that fails closed.
        """
        if not address.socket_path.exists() and not address.journal_path.exists():
            return _DurableHead(source=REHYDRATED_ABSENT)
        if probe is None and address.socket_path.exists():
            probe = self._probe(address)
        if probe is not None:
            if probe.status is not None:
                return _supervisor_state(probe.status)
            if probe.transient:
                return _DurableHead(source=REHYDRATED_TRANSIENT)
        return _journal_state(address, run_id)

    # -- the substrate ------------------------------------------------------------------------

    def _already_up(
        self,
        claimed: str,
        run: HeadRun | None,
        *,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        role: str,
    ) -> StartReceipt | None:
        """The refusal of a bring-up over a head whose own launch identity says it is running.

        `None` when there is nothing to refuse, and a receipt when there is: the caller returns it
        unchanged, so this decision is made in one place and is made *before* `_spawn`. Which is
        the whole point — a second supervisor started over a live head is not a state this backend
        recovers from by noticing afterwards.

        The evidence is the head's own launch-identity record and nothing else. Not the run
        directory, not the socket file, not the journal: all three are debris a dead head leaves
        behind, and a bring-up fenced out by debris is a card that never runs again. The record
        answers because it carries `boot_id` and `proc_starttime_ticks` beside the pid, so a
        record written before this host rebooted — or a pid the kernel has since handed to
        something else — is read as the dead head it describes rather than as a live one.

        **Only a positive live match refuses.** A record that is missing, half-written, malformed
        or unreadable is not evidence that a head is up, and this returns `None` for every one of
        them: the bring-up goes ahead. That direction is chosen deliberately and it is the
        asymmetry the two failures deserve. Refusing on unreadable evidence turns a corrupt file
        into a permanent fence around a run that nothing can lift, because nothing ever rewrites
        that file except the head this refusal is preventing. Proceeding on unreadable evidence
        risks a second supervisor — and that one is caught again downstream, by the run directory
        lock the supervisor takes and by its own `_refuse_a_second_head`, which reads the same
        record from inside the process that would be the second owner.

        The record read here is the **canonical** one, `root/run_id/head.pid`, and never the
        `pid_file` the caller handed in. A live head writes its launch identity where this backend
        told it to write it, which is that path and only that path; the `pid_file` on the run a
        bring-up arrives with is the dispatcher's own watchdog heartbeat, at a workspace path the
        tick has just *cleared* (`DispatcherHost._launch` drops it before every launch so a
        previous launch's pid cannot answer for this one). Asking that file whether a head is up
        gets the answer the clearing put there — nothing — no matter how alive the head is. So the
        subject is stripped of it before `_address` derives the address, which is exactly what
        `_address` does with a run that carries no `pid_file`: the derivation is the point, since
        the head this refuses over was started by a process that is gone. Only the admission reads
        the canonical path this way; `observe`, `stop`, `stop_if_quiescent` and `deliver` keep
        honouring the caller's `pid_file`, because for those verbs it is the dispatcher's own
        identity contract about a head it is already tracking, not a question about whether one
        exists.
        """
        subject = _with_pid_file(
            run if run is not None else HeadRun(
                run_id=claimed, spec=spec, workspace=workspace, task_ref=task_ref, role=role,
            ),
            "",
        )
        address = self._address(subject)
        if address is None or not self._process_alive(address, subject):
            return None
        return StartReceipt(
            status=HEAD_BUSY,
            run=run,
            reason=(
                f"run {claimed} already has a head up: its launch identity at "
                f"{address.pid_file} is a live match, so a bring-up here would put a second "
                "supervisor over a head that is already running"
            ),
            evidence={
                "refusal": START_HEAD_ALREADY_UP,
                "run_id": claimed,
                "pid_file": str(address.pid_file),
            },
            epoch=self.activity.epoch(claimed),
        )

    def _address(self, run: HeadRun) -> _Address | None:
        """Where this head is addressed, derived from the run id rather than remembered.

        Deriving is what makes this backend answer about a head a *later* dispatcher process asks
        about: the run directory is `root/run_id` and everything in it has a predictable name, so a
        runtime constructed a tick ago knows how to reach a head it never started.
        """
        if not run.run_id:
            return None
        try:
            run_dir = protocol.run_dir_for(self.root, run.run_id)
            socket_path = protocol.socket_path_for(run_dir)
        except protocol.ProtocolError:
            return None
        return _Address(
            run_dir=run_dir,
            socket_path=socket_path,
            journal_path=run_dir / protocol.JOURNAL_NAME,
            pid_file=Path(run.pid_file) if run.pid_file else run_dir / protocol.PID_FILE_NAME,
        )

    def _connect(self, address: _Address) -> local_pty.SupervisorClient:
        return local_pty.SupervisorClient.connect(
            address.socket_path, timeout=self._connect_timeout
        )

    def _process_alive(self, address: _Address, run: HeadRun) -> bool:
        """Whether the head's own process is alive, by the launch identity and nothing else.

        The expectation is handed over whole when the run carries one, because the reader treats a
        partial expectation as unprovable rather than as a wildcard — deliberately, and this must
        not work around it. A run with no role or task recorded is checked the only way that is
        left: the record has to be a live match on its own terms and name this run.
        """
        expected = {"run_id": run.run_id, "role": run.role, "task": _task_of(run)}
        if all(expected.values()):
            status = self._identity(str(address.pid_file), expected=expected)
            return bool(status.get("alive")) and bool(status.get("match"))
        status = self._identity(str(address.pid_file))
        record = status.get("record") or {}
        return (
            bool(status.get("alive"))
            and bool(status.get("match"))
            and str(record.get("run_id") or "") == run.run_id
        )

    def _identity_says_dead(self, address: _Address) -> bool:
        """Whether the launch identity positively says the head's process has ended.

        Positively: a record that cannot be read at all is not evidence of a dead head, and this
        answers `False` for it. The expectation is deliberately not passed here — the question is
        about the process the record names, and a mismatch is not a death.
        """
        return bool(self._identity(str(address.pid_file)).get("state") == "dead")

    def _put(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        probe: _Probe | None = None,
    ) -> tuple[DeliveryReport | None, _Refusal | None]:
        """Offer one payload and follow it until this backend can say what became of it.

        Two answers, and exactly one of them is ever not `None`. The line between them is
        **whether the payload can still be known not to have been offered**, and it is drawn
        deliberately rather than by where an exception happens to be caught. Before the request
        goes onto the socket, a supervisor that cannot be spoken to — or one that answers the
        question asked there with a refusal of its own — is a `_Refusal`: the head's terminal was
        provably never touched. From that moment on nothing is a refusal any more —
        every ending is a `DeliveryReport`, and the worst one of those can say is that what
        became of the payload could not be established.

        `probe` is what came of the status request the calling section already took. A successful
        frame is reused and no second request is made. If that attempt failed before producing a
        frame, the probe permits exactly one recovery request before the offer; its retry bit is
        consumed first, so no later consumer can make a third. The resulting frame has the same
        two roles: a stated refusal before anything was offered, or the journal sequence that
        floors the search for this delivery's own `input.accepted`.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return None, _Refusal(
                status=HEAD_GONE,
                reason="this head has no socket to deliver through",
                failure=HeadNudgeFailed("the head's supervisor can no longer be addressed"),
            )
        if probe is not None and probe.status is None and isinstance(probe.answer, Mapping):
            # A refusal the supervisor stated to this section's one question, and it was stated
            # before a byte of this payload existed on any socket — so it is a refusal *before*
            # the offer exactly as it would have been had this method asked it, and it is read by
            # the same classifier. At the connection bound that is `HEAD_BUSY`, and nothing here
            # is closed, drained or remembered as fatal.
            return None, _stated_refusal(probe.answer)
        payload = _payload_of(pointer)
        try:
            client = self._connect(address)
        except _UNREACHABLE as exc:
            return None, self._unreachable_refusal(address, run, exc)
        with client:
            # The journal's own sequence, read before this payload is offered, is the second
            # key that `input.accepted` records are matched on. Delivery ids restart at 1 in
            # every supervisor incarnation while the journal is append-only across all of
            # them, so an id alone would let a record from a previous incarnation of a reused
            # run directory answer for this delivery. Nothing has been offered yet at this
            # point, so a supervisor that cannot answer here is a refusal.
            #
            # The section's own frame is that reading when there is one — it was taken inside this
            # same lock, before this payload existed, and asking again would be the second status
            # request obligation 3 does not allow.
            if probe is not None and probe.status is not None:
                status: Mapping[str, Any] = probe.status
            else:
                if probe is not None and not probe.spend_retry():
                    assert probe.error is not None
                    return None, self._unreachable_refusal(address, run, probe.error)
                try:
                    status = client.status()
                except _UNREACHABLE as exc:
                    return None, self._unreachable_refusal(address, run, exc)
            if not status.get("ok"):
                # A refusal the supervisor *stated*, and the frame it stated it in is the whole of
                # this caller's news. At the connection bound the supervisor accepts the socket,
                # writes this frame and lets go without registering the connection or reading a
                # byte of any request — so the payload below was never offered and the head's
                # terminal was never touched. Believing the frame's contents without believing its
                # `ok` was how a live head at a self-clearing bound got closed for good: the write
                # that followed met `EPIPE` and became "admitted, then unanswerable", which is
                # fatal by design. It also left `floor` at 0, which is the bound this backend
                # introduced precisely so that a reused run directory's older records cannot
                # answer for a fresh delivery.
                return None, _stated_refusal(status)
            floor = int(status.get("journal_seq") or 0)
            try:
                answer = client.send_input(payload, subject=subject)
            except _UNREACHABLE as exc:
                # The request is on the socket and no answer came back. Whether the supervisor
                # read it and admitted the payload cannot be established from here, and "probably
                # not" is precisely the assumption that writes the next payload behind a fragment.
                # So this is an unestablished delivery and not a refusal, even though it is one
                # step earlier than the case below: the line is drawn at what can be established,
                # never at how far down the call the failure happened to be.
                #
                # The journal is not asked here, and that is not an omission: an `input.accepted`
                # record is matched on the delivery id, and no answer came back to carry one. This
                # is the one unestablished fate with only one witness, because it is the one that
                # happens before the second witness has anything to be asked about.
                return _delivery_report(
                    state=DELIVERY_STATE_UNKNOWN,
                    written=0,
                    offered=len(payload),
                    established=False,
                    floor=floor,
                    detail=f"the head's supervisor stopped answering as it was offered: {exc}",
                ), None
            if not answer.get("ok"):
                return None, _admission_refusal(answer)
            admitted = dict(answer.get("delivery") or {})
            # From here the connection is watching a delivery whose bound the substrate has just
            # declared, so its own answer bound is derived from that same number rather than left
            # at the connect bound. `connect_timeout` is how long a supervisor that has not spoken
            # yet may take to speak; it is not "how long this delivery may take", and leaving it
            # in force here would let a supervisor slower than it — but well inside the bound it
            # declared — be read as one that stopped answering, which is a fatal outcome. One
            # number governs the watching, and it is the substrate's.
            client.set_timeout(self.delivery_wait_for(_declared_bound(admitted)))
            return self._follow(address, client, admitted, len(payload), floor), None

    def _follow(
        self,
        address: _Address,
        client: local_pty.SupervisorClient,
        admitted: Mapping[str, Any],
        offered: int,
        floor: int,
    ) -> DeliveryReport:
        """Watch an admitted delivery to its end, and be the only place that asks what it did.

        **The wait is the substrate's own, not this runtime's.** `admitted` carries the bound the
        supervisor put on *this* delivery — the head's `delivery_seconds` — so the deadline here is
        that number plus the named grace, measured from after the admission answer came back and
        therefore strictly later than the supervisor's own. A delivery always leaves the in-flight
        state within its bound: it completes, it stalls at that bound, or it fails with the head.
        So this loop outlasts the substrate by construction, and there is no configuration that
        makes it return over a delivery still being written.

        The waiting is done here rather than through the client's own `wait_for_delivery` for one
        reason: that one answers a delivery it could not follow to the end by raising, and a caller
        then has to read the state of the delivery out of *which exception arrived*. The state
        travels as a value instead, so the polling is done where every ending can be turned into
        one — and this is the single point at which the witnesses are asked at all. There is no
        second one: nothing outside this loop ever asks what became of a delivery, because nothing
        outside it is ever left holding one.
        """
        delivery_id = int(admitted.get("id") or 0)
        last: Mapping[str, Any] = admitted
        # The highest journal sequence this watch has seen, starting at the one read before the
        # payload was offered. It is carried out on the report so that the epoch this delivery
        # moves is the head's own sequence rather than a count kept only in this process.
        seq = floor
        deadline = time.monotonic() + self.delivery_wait_for(
            _declared_bound(admitted)
        )
        while True:
            try:
                status = client.status()
            except _UNREACHABLE as exc:
                # Admitted, and then nobody left to ask. The journal is the other witness and it
                # is on this host's disk, so it is read before anything is concluded; only if it
                # has nothing about this delivery either is the fate reported as unestablished.
                return self._report_of(
                    address, last, offered, floor, established=False, detail=str(exc), seq=seq
                )
            if not status.get("ok"):
                if _is_transient_bound(status) and time.monotonic() < deadline:
                    # A bound the substrate clears by itself is not a witness saying anything
                    # about this delivery, and it is emphatically not an ending: the supervisor is
                    # alive and busy. There is time left on the substrate's own bound, so the
                    # question is simply asked again rather than answered out of a frame that
                    # declined it.
                    time.sleep(self._delivery_poll)
                    continue
                # A frame that declines the question is not an answer about this delivery, and
                # believing its (absent) `delivery` key would read a supervisor that refused as a
                # supervisor that said nothing had landed. It ends this watch the same way a
                # silent socket does, and for the same reason: the journal is the witness left.
                return self._report_of(
                    address, last, offered, floor,
                    established=False, detail=_refusal_detail(status), seq=seq,
                )
            seq = max(seq, int(status.get("journal_seq") or 0))
            delivery = status.get("delivery")
            if isinstance(delivery, dict) and int(delivery.get("id") or 0) == delivery_id:
                last = delivery
                if delivery.get("state") != protocol.DELIVERY_IN_FLIGHT:
                    return self._report_of(
                        address, last, offered, floor, established=True, seq=seq
                    )
            if time.monotonic() >= deadline:
                # Past the substrate's own bound, plus the grace, and the delivery it declared
                # that bound for has still not ended. That is not a delivery going well and it is
                # not one this runtime gave up on early — it is a substrate that overran a promise
                # it made, so nobody witnessed an ending and there is none to report. `established`
                # is `False` and the journal gets the last word; if it has nothing either, the
                # fate is unknown and closes the head, because bytes may be sitting on its
                # terminal.
                return self._report_of(
                    address, last, offered, floor,
                    established=False,
                    seq=seq,
                    detail=(
                        f"the substrate bounded this delivery at "
                        f"{_declared_bound(admitted):g}s and had not ended it "
                        f"{self.delivery_wait_for(_declared_bound(admitted)):g}s later"
                    ),
                )
            time.sleep(self._delivery_poll)

    def _report_of(
        self,
        address: _Address,
        delivery: Mapping[str, Any],
        offered: int,
        floor: int,
        *,
        established: bool,
        detail: str = "",
        seq: int = 0,
    ) -> DeliveryReport:
        """What reached the head's terminal, from `status` and corroborated by the journal.

        `status` is the authority while it answers and the journal is the second witness, in that
        order and not the other way round: a delivery of which not one byte landed is a real fact
        about a real payload, and the record of it is in `status` first. When `status` cannot be
        had at all the order reverses of necessity — the journal is then the only witness left,
        and a journalled delivery is an established one however the socket ended.
        """
        state = str(delivery.get("state") or protocol.DELIVERY_IN_FLIGHT)
        written = int(delivery.get("written_bytes") or 0)
        delivery_id = int(delivery.get("id") or 0)
        journalled = False
        for event in self._accepted_records(address, floor):
            if int(event.get("delivery") or 0) != delivery_id:
                continue
            journalled = True
            seq = max(seq, int(event.get("seq") or 0))
            # The journal counts what the kernel took, which is the same number `status` reports.
            # Where they can differ is time: the record is written when the delivery ends, so a
            # journal that has it is a journal that saw the end of it.
            written = int(event.get("bytes") or written)
            state = str(event.get("state") or state)
        return _delivery_report(
            state=state,
            written=written,
            offered=int(delivery.get("size_bytes") or offered),
            established=established or journalled,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=str(delivery.get("detail") or "") or detail,
            seq=seq,
        )

    def _accepted_records(self, address: _Address, floor: int) -> tuple[dict[str, Any], ...]:
        """The journal's `input.accepted` records written after this delivery was offered.

        Bounded by the journal's own sequence rather than matched on the delivery id alone. The id
        is unique within one supervisor incarnation and the journal outlives incarnations, so in a
        run directory that was ever reused the id by itself would let an old record answer for a
        new payload. A journal that cannot be read at all is not evidence of anything and says so
        by being empty.
        """
        try:
            events = local_pty.read_tail(address.journal_path).of_kind(local_pty.INPUT_ACCEPTED)
        except OSError:
            return ()
        return tuple(event for event in events if int(event.get("seq") or 0) > floor)

    def _unreachable_refusal(
        self, address: _Address, run: HeadRun, exc: Exception
    ) -> _Refusal:
        """A payload that was never admitted, because the supervisor could not be spoken to."""
        return _Refusal(
            status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            failure=HeadNudgeFailed(f"the head's supervisor could not be reached: {exc}"),
            evidence=str(exc),
        )

    def _delivery_that_did_not_arrive(
        self, run: HeadRun, report: DeliveryReport, lease: Any, epoch: int
    ) -> DeliverReceipt:
        """A delivery that was offered, ended, and did not arrive whole: the outcome it is.

        Every branch here is keyed on `report.outcome` — the name decided once, upstream — and on
        nothing else. All three are endings, because the wait was the substrate's own: there is no
        branch here for a delivery that has not finished, and that absence is the point. What each
        outcome costs the head:

          * `DELIVERY_LEFT_A_PREFIX` and `DELIVERY_UNESTABLISHED`: the head. Admission closes here
            and at the supervisor, the reason is remembered so that every later refusal carries
            it, and the lease is kept because the head has been given something and may be working
            on it;
          * `DELIVERY_LANDED_NOTHING`: the turn only. Nothing reached the terminal, so no turn was
            started and the lease is handed back; the head is still worth delivering to.
        """
        if report.fatal:
            self._close_head(run, report)
        elif report.outcome == DELIVERY_LANDED_NOTHING:
            self.activity.release(run.run_id)
            lease = None
        return DeliverReceipt(
            status=self._status_of(run, report),
            run=run,
            reason=self._reason_of(report),
            failure=HeadNudgeFailed(report.detail or report.outcome),
            evidence=report,
            delivery_state=report.state,
            delivered_bytes=report.written,
            offered_bytes=report.offered,
            epoch=epoch,
            lease=lease,
            rotation_ready=self.activity.rotatable(run.run_id),
        )

    def _close_head(self, run: HeadRun, report: DeliveryReport) -> None:
        """Hand this head no more work, here and at the process that owns it.

        One place rather than two branches of `deliver`, because a prefix on the terminal and a
        fate nobody could establish are the same fact about the same terminal — this runtime hands
        it no more work — and they must not be able to drift apart. The reason differs and travels
        on every later refusal; the consequence does not.

        It is one place for `start` as well as for `deliver`, and that is the whole of the rule:
        the payload a bring-up delivers is a payload like any other, so a launch prompt that left a
        prefix or went unwitnessed closes the head here rather than in a second version of this
        rule on the abandon path. `_abandon_bring_up` calls it before it stops the head.
        """
        self._fatal[run.run_id] = (
            DRAIN_AFTER_PARTIAL_DELIVERY
            if report.outcome == DELIVERY_LEFT_A_PREFIX
            else DRAIN_AFTER_UNESTABLISHED_DELIVERY
        )
        self.activity.close_admission(run.run_id)
        self._close_substrate_admission(
            run,
            StopInitiator(
                actor="local-pty-runtime",
                reason=(
                    DELIVER_PREFIX_IS_FATAL
                    if report.outcome == DELIVERY_LEFT_A_PREFIX
                    else DELIVER_UNKNOWN_IS_FATAL
                ),
            ),
        )

    def _status_of(self, run: HeadRun, report: DeliveryReport) -> str:
        """`HEAD_GONE` only for a head established to have ended; `HEAD_ALIVE` for the rest.

        A delivery that failed says the head's terminal closed under it, which is the head ending.
        An unestablished delivery says nothing about the head at all, so the launch identity is
        asked — and answers `HEAD_ALIVE` for every reading that is not a positive death, because a
        head that cannot be read about is emphatically not a head that has gone.
        """
        if report.state == protocol.DELIVERY_FAILED:
            return HEAD_GONE
        if report.outcome == DELIVERY_UNESTABLISHED:
            address = self._address(run)
            if address is not None and self._identity_says_dead(address):
                return HEAD_GONE
        return HEAD_ALIVE

    def _reason_of(self, report: DeliveryReport) -> str:
        """Why this delivery says what it says: the outcome first, the substrate's state after."""
        reason = (
            DELIVER_UNESTABLISHED
            if report.outcome == DELIVERY_UNESTABLISHED
            else {
                protocol.DELIVERY_STALLED: DELIVER_STALLED,
                protocol.DELIVERY_FAILED: DELIVER_FAILED,
            }.get(report.state, DELIVER_STALLED)
        )
        if report.outcome == DELIVERY_LEFT_A_PREFIX:
            return f"{reason}: {DRAIN_AFTER_PARTIAL_DELIVERY}"
        if report.outcome == DELIVERY_UNESTABLISHED:
            return f"{reason}: {DRAIN_AFTER_UNESTABLISHED_DELIVERY}"
        return reason

    def _abandon_bring_up(
        self,
        run: HeadRun,
        report: DeliveryReport | None,
        refusal: _Refusal | None,
        epoch: int,
    ) -> StartReceipt:
        """End a head whose prompt never arrived, and say what the ending left behind.

        A bring-up whose launch prompt ended fatally closes admission exactly as a later delivery
        does, and through the same call: `_close_head` is the one place that rule lives, so `start`
        and `deliver` cannot come to disagree about what a fatal outcome costs a head. It matters
        precisely when the stop below does not confirm — the head is then still there, with a
        prefix of its launch prompt possibly on its terminal, and it must not be delivered to
        again. A confirmed stop makes the question moot: `stop` forgets the head, admission and
        fatal reason with it.
        """
        detail = refusal.reason if refusal is not None else (report.detail if report else "")
        if report is not None and report.fatal:
            self._close_head(run, report)
        stopped = self.stop(
            run, StopInitiator(actor="head-launch", reason="the head was never given its task")
        )
        message = f"this head came up and its prompt did not reach it: {detail}"
        if stopped.ok:
            return StartReceipt(
                status=HEAD_GONE,
                run=stopped.run,
                reason=message,
                failure=HeadSpawnFailed(message),
                evidence=report or (refusal.evidence if refusal is not None else None),
                epoch=epoch,
            )
        return StartReceipt(
            status=HEAD_ALIVE,
            run=stopped.run,
            reason=f"{message}; the head it left behind could not be stopped",
            failure=HeadSpawnAborted(message, run=stopped.run),
            evidence=report or (refusal.evidence if refusal is not None else None),
            epoch=epoch,
        )

    def _close_substrate_admission(
        self, run: HeadRun, initiator: StopInitiator
    ) -> tuple[bool, Any, int, _Probe | None]:
        """Tell the process that owns this head to take no more input, and read the answer back.

        `head_signalled` is claimed from `status` rather than from the request having been sent:
        the drain is only true of the head once the supervisor says it is draining. The third
        value is that same frame's `journal_seq`, so the caller's epoch is the head's own sequence
        after the drain was written and not a number derived anywhere else; zero means the
        read-back said nothing, and `advance_to` leaves the epoch alone for it.

        The fourth is that read-back as this file's one status value, handed back so the caller's
        rehydration is made out of it instead of dialling for a second frame. It is `None` for
        every ending where no status was read at all — no socket, a refused drain, a supervisor
        that stopped answering — and the caller then asks for itself, which is still one request
        for the section.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return False, OBSERVE_NO_RUN_DIRECTORY, 0, None
        try:
            with self._connect(address) as client:
                answer = client.drain(initiator.actor or "dispatcher")
                if not answer.get("ok"):
                    return False, answer, 0, None
                status = client.status()
                if not status.get("ok"):
                    # The drain was accepted and the read-back was declined. `head_signalled` is
                    # claimed from what `status` says, so a frame that says nothing about draining
                    # cannot support the claim — and the refusal it does carry is the evidence.
                    return False, status, 0, _Probe(answer=status)
                return (
                    bool(status.get("draining")),
                    answer,
                    int(status.get("journal_seq") or 0),
                    _Probe(status=status),
                )
        except _UNREACHABLE as exc:
            return False, str(exc), 0, None

    def _ask_to_stop(
        self, address: _Address, initiator: StopInitiator, signal_name: str
    ) -> Any:
        """Ask the supervisor to end its head. A supervisor that is gone is not a failure here.

        A head whose supervisor has died is exactly the case where the confirmation below matters:
        nothing was asked, and whether the head is gone is still decided by its launch identity.

        This is the one reader in this file that does not test `ok` before it returns, and that is
        the point of it rather than an omission: the answer is carried as evidence and is never
        believed by anything. A refusal, an unknown op and a supervisor that died mid-request all
        mean the same thing here — the stop was not taken by the socket — and `_await_head_gone`
        decides the outcome from the launch identity either way.
        """
        try:
            with self._connect(address) as client:
                return client.stop(initiator.actor or "dispatcher", signal_name)
        except _UNREACHABLE as exc:
            return {"ok": False, "error": OBSERVE_SUPERVISOR_UNREACHABLE, "detail": str(exc)}

    def _await_head_gone(self, address: _Address, run: HeadRun) -> bool:
        """Wait for the head's own process to be gone, by the identity that named it.

        Not the socket disappearing: that says the supervisor let go, which is a fact about the
        supervisor. A head whose identity record was never written — a bring-up stopped before its
        shell got that far — is answered by the journal's `run.exited` instead, because there is
        nothing else that could ever say yes.
        """
        deadline = time.monotonic() + self._stop_timeout
        while True:
            if self._identity_says_dead(address):
                return True
            if not address.pid_file.exists() and _has_exited(address):
                return True
            if time.monotonic() >= deadline:
                return self._identity_says_dead(address) or (
                    not address.pid_file.exists() and _has_exited(address)
                )
            time.sleep(_CONFIRM_POLL_SECONDS)

    def _turn_still_running(self, run: HeadRun, probe: _Probe | None = None) -> str:
        """Whether the turn this runtime holds a lease for is still running, and how it knows.

        The supervisor is asked rather than assumed: a lease granted three ticks ago and never seen
        to close is stale knowledge, and refusing every later delivery on it would strand the head.
        A supervisor that cannot be reached is not permission — "I could not tell" is a refusal the
        caller is told about, not a prompt written over a running turn.

        `probe` is this critical section's one status frame, and every caller inside a section that
        already took one passes it: the rehydration that adopted the lease and the question of
        whether that lease's turn is still open are two readings of one answer, not two questions,
        and asking twice cost the second request obligation 3 does not allow.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return "its supervisor can no longer be addressed to ask whether the turn ended"
        if probe is None:
            probe = self._probe(address)
        if probe.error is not None:
            return f"its supervisor could not be asked whether the turn ended ({probe.error})"
        status = probe.status
        if status is None:
            return "its supervisor answered nothing this runtime can read as a turn"
        if not status.get("alive"):
            # The head's process has ended, so the turn it was running ended with it.
            return ""
        if _in_flight(status):
            return "a payload is still being written into its terminal"
        return "its supervisor still shows the turn open" if status.get("turn_open") else ""

    def _unreachable(
        self,
        run: HeadRun,
        address: _Address,
        epoch: int,
        lease: Any,
        rotatable: bool,
        exc: Exception,
    ) -> ObserveReceipt:
        """A socket that did not answer, classified by the head's process rather than by the socket.

        The two are different facts and the product has killed live heads by collapsing them: a
        supervisor that died leaves a head running and orphaned, and reporting that as a head that
        has gone is how its replacement is opened beside it.
        """
        if self._identity_says_dead(address):
            # A lease outstanding on a process that no longer exists is stale by definition: the
            # turn it names ended when the process did.
            self.activity.release(run.run_id)
            return ObserveReceipt(
                status=HEAD_GONE,
                run=run,
                reason=OBSERVE_HEAD_EXITED,
                evidence=str(exc),
                epoch=epoch,
                lease=None,
                rotation_ready=self.activity.rotatable(run.run_id),
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=False,
                busy=False,
            )
        return ObserveReceipt(
            status=HEAD_ALIVE,
            run=run,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            evidence=str(exc),
            epoch=epoch,
            lease=lease,
            rotation_ready=rotatable,
            handle=str(address.socket_path),
            leaf=run.leaf or run.run_id,
            connected=False,
            busy=None,
        )


@dataclass(frozen=True)
class _DurableHead:
    """What outlived the process that granted this head's turn, as one value.

    `source` is what said it, and it is on the value rather than derived from which fields are
    filled in, because "the head is not draining" and "nobody could tell me whether it is" are two
    answers and this backend is not allowed to spell them the same way.

    `seq` is the head's journal sequence — strictly increasing, recovered from the file when the
    journal is opened, and therefore the same scale in every tick. It is what the activity epoch is
    raised to, which is the whole of obligation 5: a number a process hands out and another process
    can compare.
    """

    source: str
    seq: int = 0
    draining: bool = False
    turn_open: bool = False
    turn: int = 0
    exited: bool = False


def _journal_state(address: _Address, run_id: str) -> _DurableHead:
    """This head's shape, derived from a bounded tail of the journal its supervisor left behind.

    The derivation is a replay in sequence order rather than a search for the newest record of
    each kind, because the two disagree exactly where it matters: a `turn.started` with no
    `turn.finished` after it is an open turn, and the same record followed by `run.exited` is not —
    it is a turn that ended with the process, and a head whose turn ended that way is rotatable.
    `run.started` resets everything, so a run directory that was reused cannot have a previous
    incarnation's drain or turn answer for this one.

    A window with nothing in it for this run is `REHYDRATED_UNKNOWN`, and the caller fails closed
    on it.

    **So is a window that cannot account for itself** (secretary-1479). `read_tail` says four
    separate things about what it handed back — the last record was cut off, some records could
    not be read, the sequence did not increase, the window began mid-history — and every one of
    them means the same thing to *this* derivation: an event that would have closed admission may
    be the record that was torn, the record that was unreadable, or the record that scrolled out
    of the window. A replay that ignores them announces "this head is not draining" on the
    strength of not having seen the drain, which is the one sentence a fail-closed reader is not
    allowed to say. The sequence such a window ends on is still true and is still carried, because
    a number the journal actually wrote is comparable whatever else was lost; what is refused is
    the *shape*, and the caller closes admission on it and invents no lease.
    """
    try:
        result = local_pty.read_tail(address.journal_path)
    except OSError:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    events = [event for event in result.events if str(event.get("run_id") or "") == run_id]
    if not events:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    seq = int(events[-1].get("seq") or 0)
    if result.truncated_tail or result.malformed or result.partial_head or not result.ordered:
        return _DurableHead(source=REHYDRATED_UNKNOWN, seq=seq)
    draining = turn_open = exited = False
    turn = 0
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == local_pty.RUN_STARTED:
            draining = turn_open = exited = False
            turn = 0
        elif kind == local_pty.TURN_STARTED:
            turn_open = True
            turn = int(event.get("turn") or turn)
        elif kind == local_pty.TURN_FINISHED:
            turn_open = False
        elif kind in (local_pty.DRAIN_REQUESTED, local_pty.RUN_STOPPING):
            draining = True
        elif kind == local_pty.RUN_EXITED:
            exited = True
            turn_open = False
    return _DurableHead(
        source=REHYDRATED_FROM_JOURNAL,
        seq=seq,
        draining=draining,
        turn_open=turn_open,
        turn=turn,
        exited=exited,
    )


def _supervisor_state(status: Mapping[str, Any]) -> _DurableHead:
    """What one `status` frame the supervisor answered says about this head's durable shape."""
    return _DurableHead(
        source=REHYDRATED_FROM_SUPERVISOR,
        seq=int(status.get("journal_seq") or 0),
        # A supervisor that is stopping this head is one that has already closed its admission —
        # it writes `drain.requested` before `run.stopping` — and reading only `draining` would
        # miss the half-second between the two.
        draining=bool(status.get("draining")) or bool(status.get("stopping")),
        turn_open=bool(status.get("turn_open")),
        turn=int(status.get("turn") or 0),
        exited=not bool(status.get("alive")),
    )


@dataclass
class _Probe:
    """A section's bounded question to the supervisor, and everything it answered.

    A value rather than three call sites, because "the supervisor said this", "the socket did not
    answer" and "the socket answered something that is not a status" are three different facts and
    every one of them decides something different. A successful path gets one request. A request
    that failed before returning a frame gets one recovery attempt, whose bit is consumed before
    the request is made; a third request is therefore unavailable by construction.
    """

    #: The frame the supervisor answered, when it answered one this runtime can read.
    status: Mapping[str, Any] | None = None
    #: What came back instead, when something did and it was not a readable status.
    answer: Any = None
    #: What went wrong on the socket, when nothing came back at all.
    error: BaseException | None = None
    #: The one recovery request a failed initial attempt may still spend.
    retry_available: bool = False

    def spend_retry(self) -> bool:
        """Consume the failed attempt's sole retry before anybody can make the request."""
        if not self.retry_available:
            return False
        self.retry_available = False
        return True

    @property
    def transient(self) -> bool:
        """Whether the refusal that came back is one the substrate clears by itself.

        The line between class 2 and class 3 of secretary-1479, drawn on the name the supervisor
        put in the frame rather than on the fact that this runtime got no state out of it. A live
        supervisor at its connection bound says `connection_limit` and means "not you, not now";
        reading that as "nobody can say what this head is" is how a bound that clears in
        milliseconds came to close a head's admission for the life of the process.
        """
        return isinstance(self.answer, Mapping) and _is_transient_bound(self.answer)


@dataclass(frozen=True)
class _Address:
    """Everything about one head that is derived from its run directory."""

    run_dir: Path
    socket_path: Path
    journal_path: Path
    pid_file: Path


@dataclass(frozen=True)
class _Refusal:
    """A delivery refused before anything reached the terminal."""

    status: str
    reason: str
    failure: HeadOperationError | None = None
    evidence: Any = None


def _refusal_status(error: str) -> str:
    """Which status a refusal the supervisor *stated* is, by what it refused with.

    One mapping for every verb that is refused before it asked for anything, because the fact is
    the same one whichever verb met it: the supervisor said no, and it said no with a name.

    Only a head the supervisor says has gone is `HEAD_GONE`. Both bounds — the attach limit and
    the connection limit — are refusals worth making again the moment somebody else lets go, and
    reporting a live head sitting at one of them as a head that ended is exactly the collapse
    ("alive looks dead") this sprint exists to remove. Anything else the supervisor can refuse
    with is an answer *from* a live supervisor about a live head, so it is `HEAD_ALIVE` too: what
    was asked did not happen, and the head is still the caller's to account for.
    """
    if _is_transient_bound({"error": error}):
        return HEAD_BUSY
    if error == protocol.ERROR_HEAD_GONE:
        return HEAD_GONE
    return HEAD_ALIVE


def _declared_bound(admitted: Mapping[str, Any]) -> float:
    """The bound the substrate put on the delivery it just admitted, in seconds.

    Read off the delivery record the supervisor answered with, so it is the bound of *this*
    delivery on *this* head — the `delivery_seconds` the head was raised with — rather than
    anything this runtime remembers or was configured with. A record that declares none is answered
    with the substrate's own default, which is the number a supervisor started without
    `--delivery-seconds` uses; it is still one knob, owned one layer down.
    """
    try:
        bound = float(admitted.get("timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        return UNDECLARED_DELIVERY_BOUND
    return bound if bound > 0 else UNDECLARED_DELIVERY_BOUND


def _is_transient_bound(answer: Mapping[str, Any]) -> bool:
    """Whether a refusal the supervisor stated is one of the bounds it clears by itself.

    The connection bound and the attach bound are refusals of a *caller*, made by a live
    supervisor about a live head, and they stop holding the moment somebody else lets go. Neither
    is ever a fact about a delivery, a turn or a head's life, which is why every reader that meets
    one answers `HEAD_BUSY` and none of them closes anything.
    """
    return str(answer.get("error") or "") in (
        protocol.ERROR_CONNECTION_LIMIT,
        protocol.ERROR_ATTACH_LIMIT,
    )


def _refusal_detail(answer: Mapping[str, Any]) -> str:
    """What a supervisor's refusal frame says, in the order a reader wants it: detail, then name."""
    return (
        str(answer.get("detail") or "")
        or str(answer.get("error") or "")
        or "the head's supervisor answered nothing this runtime can read"
    )


def _stated_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """A refusal the supervisor stated to a question asked *before* any payload was offered.

    The second half of this backend's rule about delivery state, and it is the half a stated
    refusal makes the difference for: state is never invented, and a state the substrate stated is
    never thrown away. A frame that is not `ok` is the supervisor declining the question — the
    connection bound is the reachable one, and it is a bound the substrate itself treats as normal
    and self-clearing — so it classifies as a refusal *before* the offer, never as an unknown fate
    after one. Reading such a frame for its contents and going on to write is how a head nothing
    had touched came to be closed for the rest of this runtime's life.
    """
    error = str(answer.get("error") or "")
    detail = _refusal_detail(answer)
    return _Refusal(
        status=_refusal_status(error),
        reason=detail,
        failure=HeadNudgeFailed(detail),
        evidence=answer,
    )


def _admission_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """The substrate's own refusal of a payload, as the status the boundary already has for it.

    Every one of these left the head's terminal exactly as it was, which is why none of them is
    `HEAD_OK` with a delivery attached and why the turn is handed back for all of them.
    """
    error = str(answer.get("error") or "")
    detail = str(answer.get("detail") or error)
    if _is_transient_bound(answer):
        # The connection this offer was made on met a bound of the substrate rather than the head:
        # the supervisor wrote the refusal and let go without reading the request, so nothing was
        # offered. It is the same fact `_refusal_status` names for a question refused before an
        # offer, and it is `HEAD_BUSY` here for the same reason — a limit that clears itself is
        # never a head that ended, was drained, or is anything but worth asking again.
        return _Refusal(HEAD_BUSY, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_DRAINING:
        return _Refusal(HEAD_DRAINING, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_HEAD_GONE:
        return _Refusal(HEAD_GONE, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_INPUT_IN_FLIGHT:
        # Somebody else's payload holds the floor. Worth making again once it lands, and never
        # interleaved with it.
        return _Refusal(HEAD_BUSY, detail, HeadNudgeFailed(detail), answer)
    # An oversized payload and everything else: the head is untouched and still the caller's.
    return _Refusal(HEAD_ALIVE, detail, HeadNudgeFailed(detail), answer)


def _payload_of(pointer: NudgePointer) -> bytes:
    """One pointer as the bytes a head's terminal receives: the line, and the Enter that sends it."""
    return (pointer.text + "\n").encode("utf-8")


def _outcome_of(
    run: HeadRun, pointer: NudgePointer, report: DeliveryReport, subject: str
) -> DeliveryOutcome:
    """The delivery evidence for a payload that provably reached the head's terminal.

    `confirmed` rather than `accepted`, and it is the stronger word on purpose: on this backend the
    proof is the supervisor's own count of the bytes the kernel took, corroborated by the journal,
    rather than a session manager's report that a send was accepted.
    """
    payload_bytes, payload_hash = payload_fingerprint(pointer.text)
    evidence = DeliveryEvidence(
        handle=run.handle,
        subject=subject,
        payload_bytes=payload_bytes,
        payload_sha256=payload_hash,
        delivery_mode=NUDGE_FILE_MODE if pointer.document else "",
        document_path=pointer.document,
        adapter=run.spec.adapter,
        body_write_accepted=True,
        body_bytes_written=report.written,
        body_write_count=1,
        send_accepted=True,
        bytes_written=report.written,
        attempts=1,
        turn_confirmed=True,
        reason=report.detail,
    )
    return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)


def _spawn_status(exc: local_pty.LocalPtySpawnError) -> str:
    """Which of the four things a refused bring-up left behind.

    A timeout and an `already_running` refusal both mean something may be running that this
    bring-up did not account for, which is `HEAD_ALIVE` — the same distinction `HeadSpawnAborted`
    draws on the legacy path, and for the same reason: treating it as a failure is how live heads
    get a second head opened beside them.
    """
    if exc.reason in ("timeout", "already_running"):
        return HEAD_ALIVE
    return HEAD_GONE


def _spawn_failure(
    exc: local_pty.LocalPtySpawnError, run: HeadRun | None
) -> HeadOperationError:
    if _spawn_status(exc) == HEAD_ALIVE and run is not None:
        return HeadSpawnAborted(str(exc), run=run)
    return HeadSpawnFailed(str(exc))


def _with_pid_file(run: HeadRun, pid_file: str) -> HeadRun:
    """The same run, holding the launch-identity record its head writes."""
    if run.pid_file == pid_file:
        return run
    payload = run.to_json()
    payload["pid_file"] = pid_file
    return HeadRun.from_json(payload)


def _task_of(run: HeadRun) -> str:
    return run.task_ref.ref or run.task_ref.document


def _in_flight(status: Mapping[str, Any]) -> bool:
    delivery = status.get("delivery")
    return (
        isinstance(delivery, dict)
        and delivery.get("state") == protocol.DELIVERY_IN_FLIGHT
    )


def _has_exited(address: _Address) -> bool:
    """Whether the journal says the head's process ended.

    A bounded tail, like every journal read this backend makes: `run.exited` is the last thing
    written about an incarnation, so a window at the end of the file is where it is if it is
    anywhere. A window that has scrolled past an older incarnation's exit answers `False`, which is
    the safe direction — this is only ever asked to *confirm* that a head is gone.
    """
    return bool(local_pty.read_tail(address.journal_path).of_kind(local_pty.RUN_EXITED))


def _last_event_at(address: _Address) -> float:
    """The head's own clock, as the newest thing its journal has to say about it."""
    events = local_pty.read_tail(address.journal_path).events
    return float(events[-1].get("at") or 0.0) if events else 0.0


def _unobservable(
    run: HeadRun, reason: str, epoch: int, lease: Any, rotatable: bool, *, evidence: Any = None
) -> ObserveReceipt:
    """An observation this backend could not make, said as that and not as an answer."""
    return ObserveReceipt(
        status=HEAD_UNSUPPORTED,
        run=run,
        reason=reason,
        evidence=evidence,
        epoch=epoch,
        lease=lease,
        rotation_ready=rotatable,
        handle=run.handle,
        leaf=run.leaf or run.run_id,
    )
