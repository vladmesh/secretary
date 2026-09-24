"""Which backend a head's life is lived through, as a closed vocabulary of one name.

A profile's own choice, and orthogonal to its adapter: what a head *is* — the CLI it runs and the
effort it runs at — and what *holds* it — a supervisor of this product's own — are two independent
facts. Today the second list has one entry, and a profile may still say so explicitly.

It lives here, beside `local_pty_head`, rather than inside the `head` package for one reason: that
package is backend-independent by construction and names no session manager, which is what makes
its contract suite runnable with no backend installed. `head.command.validate_launch_shape` imports
these names for the one check every reader of a registry goes through, exactly as it imports the
adapters and efforts it validates against; a registry validated against a second copy of this list
would be a registry that can load and then fail when the backend is built.

There is deliberately no second name and no free text. A profile naming anything else — including
`orca-legacy`, which it could name until A20 step 2 — is refused when the registry is read, so a
head refused when the table loads and a head refused when it is raised are refused by one rule.

`orca-legacy` survives here only as a *record* marker: a durable `HeadRun` written while heads
were Orca panes says it, or says nothing, and still has to load. Such a record is reported as
legacy and is never launched, delivered to or given a backend.
"""

from __future__ import annotations

#: The local-pty path: a supervisor of this product's own holds the head's pty and its journal.
LOCAL_PTY_RUNTIME = "local-pty"
HEAD_RUNTIMES = (LOCAL_PTY_RUNTIME,)

#: What an absent `runtime` key means in a head *profile*.
DEFAULT_HEAD_RUNTIME = LOCAL_PTY_RUNTIME

#: Not a runtime: the name a durable record carries when its head was an Orca pane. No profile may
#: name it and no backend is built for it; it is read only to say a record is legacy.
ORCA_LEGACY_RUNTIME = "orca-legacy"
#: What an absent `runtime` means in a *durable record*: a `HeadRun` written before heads had a
#: choice of backend, or a spec rebuilt from a record that never named one. Every such head was an
#: Orca pane, so a record keeps meaning exactly what it meant when it was written: a legacy one.
RECORD_RUNTIME_WHEN_ABSENT = ORCA_LEGACY_RUNTIME


def is_legacy_runtime(name: str) -> bool:
    """Whether a runtime name read off a record marks a legacy Orca record (absence included)."""
    return (name or RECORD_RUNTIME_WHEN_ABSENT) == ORCA_LEGACY_RUNTIME
