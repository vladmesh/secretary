"""Which backend a head's life is lived through, as a closed vocabulary of two names.

A profile's own choice, and orthogonal to its adapter: what a head *is* — the CLI it runs and the
effort it runs at — and what *holds* it — an Orca pane, or a supervisor of this product's own — are
two independent facts, and any combination of the two lists is a head this product can launch.

It lives here, beside `orca_legacy_head` and `local_pty_head`, rather than inside the `head`
package for one reason: that package is backend-independent by construction and names no session
manager, which is what makes its contract suite runnable with no Orca installed. One of these
names is an Orca backend's, so spelling it in there would make that property false — and
`test_head_operations.BackendIndependenceTests` says so. `head.command.validate_launch_shape`
imports these two names for the one check every reader of a registry goes through, exactly as it
imports the adapters and efforts it validates against; a registry validated against a second copy
of this list would be a registry that can load and then fail when the backend is built.

There is deliberately no third name and no free text. A profile naming anything else is refused
when the registry is read, so a head refused when the table loads and a head refused when it is
raised are refused by one rule.
"""
from __future__ import annotations

#: The Orca path this product has always run: a head is a pane, and Orca owns its process.
ORCA_LEGACY_RUNTIME = "orca-legacy"
#: The local-pty path: a supervisor of this product's own holds the head's pty and its journal.
LOCAL_PTY_RUNTIME = "local-pty"
HEAD_RUNTIMES = (ORCA_LEGACY_RUNTIME, LOCAL_PTY_RUNTIME)

#: What an absent `runtime` means. Every registry written before this key existed is a registry of
#: Orca-legacy heads, so absence has to keep meaning exactly what those registries already did.
DEFAULT_HEAD_RUNTIME = ORCA_LEGACY_RUNTIME
