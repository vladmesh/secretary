"""The three pane operations interactive delivery needs, and Orca's answer to them.

Delivery asks a session manager for very little: write bytes into a pane, read the pane back, and
wait until it will accept input.  Everything above this module is about what those answers mean —
readiness classification, composer residue, framing, the body/submit pair — and none of it is
specific to Orca.

Orca is nonetheless the only session manager this product has, so ``OrcaPaneHost`` is the default
everywhere.  What this seam buys is that its argument vectors exist in one file: a second
implementation is a class with these three methods, not a search for ``"orca"`` through the
delivery path.  It deliberately stops at delivery.  Worktree registration, pane creation and pane
teardown are a larger and differently-shaped dependency (`docs/ARCHITECTURE.md`), and pretending
this covers them would misreport how portable the product is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .tui_delivery_types import RunJson


class PaneHost(Protocol):
    """What a session manager must answer for one live interactive pane."""

    def send(self, handle: str, text: str, *, enter: bool) -> Any:
        """Write ``text`` into the pane, optionally following it with a submission."""

    def read(self, handle: str, *, limit: int | None = None) -> Any:
        """Return the pane's retained tail and its own output cursor."""

    def wait_idle(self, handle: str, *, timeout_ms: int) -> Any:
        """Block until the pane reports it will accept input, or refuse."""


@dataclass(frozen=True)
class OrcaPaneHost:
    """The installed session manager, reached through its public ``orca terminal`` CLI.

    The runner is injected rather than built here because its callers already own how a subprocess
    is spawned, timed out and redacted; this type owns only which arguments that runner is given.
    """

    run_json: RunJson

    def send(self, handle: str, text: str, *, enter: bool) -> Any:
        args = ["orca", "terminal", "send", "--terminal", handle, "--text", text]
        if enter:
            args.append("--enter")
        args.append("--json")
        return self.run_json(args)

    def read(self, handle: str, *, limit: int | None = None) -> Any:
        args = ["orca", "terminal", "read", "--terminal", handle]
        if limit is not None:
            args += ["--limit", str(limit)]
        args.append("--json")
        return self.run_json(args)

    def wait_idle(self, handle: str, *, timeout_ms: int) -> Any:
        return self.run_json([
            "orca", "terminal", "wait",
            "--terminal", handle,
            "--for", "tui-idle",
            "--timeout-ms", str(timeout_ms),
            "--json",
        ])


def pane_host(run_json: RunJson | None = None, *, host: PaneHost | None = None) -> PaneHost:
    """Resolve the host a delivery call should use.

    Callers pass the runner they always passed and get Orca.  A caller that has a session manager
    of its own passes ``host`` and the runner is never consulted, which is what makes the argument
    vectors above replaceable rather than merely tidy.
    """
    if host is not None:
        return host
    if run_json is None:
        raise ValueError("interactive delivery needs either a pane host or a command runner")
    return OrcaPaneHost(run_json)
