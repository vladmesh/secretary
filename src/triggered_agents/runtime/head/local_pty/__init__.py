"""The process substrate a second head backend will stand on, and nothing above it.

This package owns one thing: a head process the product *itself* owns — started in its own
session and process group on its own pty, held by a supervisor that outlives the dispatcher tick
that launched it, addressable afterwards over a Unix socket, and narrating itself into a
versioned append-only journal.

It is a substrate, not a backend. There is no `HeadRuntime` here, no wiring into the dispatcher
and no profile that selects it. `LocalPtyHeadRuntime` — the six verbs of
`..runtime.HeadRuntime` expressed on top of this — is a separate piece of work, and the only
thing this package owes it is that each verb has something to stand on:

  * `deliver` is the socket's bounded input, `observe` is its bounded output plus `status`,
    `attach` is its bounded attach, `request_drain` is `drain`, `stop` is `stop`, and `start` is
    `client.spawn_head`.

## The route, in the order the decisions were made

**Does the head need an owner process at all?** Yes. Something has to hold the pty master for the
head's whole life, bound what it keeps of the head's output, refuse an oversized input by naming
the limit, reap the head and write down how it ended. All of that is a process that is alive
between dispatcher ticks; none of it is a thing a file or a signal can do.

**Then why not a systemd unit per head?** The product already manages host units through
`secretary.host_apply`, so this is the route with the most existing machinery behind it. It is
refused on one hard constraint: `host_apply`'s real host writes unit files into
`SYSTEM_UNIT_DIR` (`/etc/systemd/system`) through `sudo` and then reconciles. Bringing a head up
would then require a host reconcile and a root-owned file write, and rolling the canary back would
stop being a change of one profile value — which is a Definition of Done item of the sprint that
this substrate is being built for. A user unit (`systemd --user`) escapes the root-owned file but
not the second lifecycle authority: the head's life would then be owned by a unit manager the
dispatcher does not control, and `run.exited` would have to be recovered from journald rather than
written by whoever actually reaped the process.

**Then why not bare `setsid` plus a double fork, with no supervisor at all?** Because a detached
process is not an owned one. Double-forking a head under `setsid` gives it its own session
cheaply, and then: nobody holds the pty master, so the head's output goes to a file that grows
without bound or to nothing at all; nobody can refuse an oversized input, because there is no
reader on the other end of the input path to refuse with a named limit; nobody can bound
concurrent attach; and nobody reaps the head, so its exit status is lost and `run.exited` can only
ever be inferred from the pid being gone. The supervisor is what turns "a process that survives"
into "a process we own".

**So: a plain child process of the launcher, made independent.** `client.spawn_head` starts an
intermediate in a new session, the intermediate forks the supervisor and exits, and the launcher
reaps the intermediate immediately. The supervisor is therefore never a child of the dispatcher
tick — it cannot become a zombie held by a launcher that has gone away, and it is reparented to
init while staying addressable through its socket and its pid file. The head itself is forked onto
a pty by the supervisor, which puts it in a session of its own with the pty as its controlling
terminal, so a signal sent to the dispatcher's process group cannot reach it.

**And the terminal it is given is a non-canonical one.** This is the decision with the least room in
it. A pty's default line discipline is canonical: it buffers a line,
caps that line at 4095 bytes and discards the rest with no error, no blocking and no sign to the
writer. A substrate that declares a 64 KiB input limit on top of that discipline has not declared a
limit at all — the real one is 4095 bytes, unnamed and silent, which is exactly the shape of the
legacy wound (`issue:d9d049eaad39d02bbb1e`) this backend exists not to repeat. The alternative
considered was to declare the smaller number honestly and refuse at it; it is refused because the
product has already broken on a limit of that order, and because the substrate owns the pty and can
therefore make its declared limit the true one. In non-canonical mode the kernel answers a full
buffer with `EAGAIN` instead of a silent truncation, so a delivery of any size up to the limit
arrives whole. The mode is set on the pty before the head exists, which closes the window between
`exec` and an interactive adapter's own `termios` call; an adapter that wants a different mode
still sets one for itself.

**And a delivery is admitted, not awaited.** The last decision of the route, and the one this card
made twice. A payload at the input limit does not fit in a pty's buffer, so it can only move as
fast as the head reads it — and the first answer to that was to finish the write inside the request
handler, so that the answer and the journal both described bytes that had really landed. The
accounting was right and the shape was wrong: a single-threaded loop that waits for the head inside
a request handler stops answering *everybody* for as long as the head is slow, which makes a
supervisor that cannot say what it is doing — the one thing this substrate exists to prevent. So
the accounting stays and the waiting goes: `input` accepts or refuses within the loop's own tick,
the loop writes the payload as the terminal takes it, `input.accepted` is written when the bytes
land and counts only those, and how far a delivery got is state (`status`, and the journal) that a
caller reads when it wants to. Answerability is older than completeness: a supervisor that admits a
delivery is unfinished is better than one that cannot be asked.

## Identity is the existing launch identity

There is no second identity scheme. The head's command is wrapped by
`..command.with_pid_heartbeat`, exactly as an Orca-launched head's is, so the record under
`head.pid` is written by the head's own process (`$$` plus `exec`) and carries `pid`, `boot_id`,
`proc_starttime_ticks`, `run_id`, `role` and `task`. `secretary.dispatcher_watchdog`'s reader
classifies it with no change at all.
"""

from __future__ import annotations

from .client import (
    HeadHandle,
    LocalPtyError,
    LocalPtySpawnError,
    SupervisorClient,
    spawn_head,
)
from .journal import (
    DRAIN_REQUESTED,
    EVENT_KINDS,
    INPUT_ACCEPTED,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_TAIL_BYTES,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    TURN_FINISHED,
    TURN_STARTED,
    JournalError,
    JournalReadResult,
    JournalWriter,
    read_events,
    read_tail,
)
from .protocol import (
    ATTACH_MAX_CLIENTS,
    CONNECTION_MAX_CLIENTS,
    DELIVERY_STATES,
    FRAME_MAX_BYTES,
    INPUT_DELIVERY_SECONDS,
    INPUT_MAX_BYTES,
    OUTPUT_BUFFER_BYTES,
    ProtocolError,
    socket_path_for,
)

__all__ = [
    "ATTACH_MAX_CLIENTS",
    "CONNECTION_MAX_CLIENTS",
    "DELIVERY_STATES",
    "DRAIN_REQUESTED",
    "EVENT_KINDS",
    "FRAME_MAX_BYTES",
    "HeadHandle",
    "INPUT_ACCEPTED",
    "INPUT_DELIVERY_SECONDS",
    "INPUT_MAX_BYTES",
    "JOURNAL_SCHEMA_VERSION",
    "JOURNAL_TAIL_BYTES",
    "JournalError",
    "JournalReadResult",
    "JournalWriter",
    "LocalPtyError",
    "LocalPtySpawnError",
    "OUTPUT_BUFFER_BYTES",
    "PROVIDER_PROGRESSED",
    "ProtocolError",
    "RUN_EXITED",
    "RUN_STARTED",
    "RUN_STOPPING",
    "SupervisorClient",
    "TURN_FINISHED",
    "TURN_STARTED",
    "read_events",
    "read_tail",
    "socket_path_for",
    "spawn_head",
]
