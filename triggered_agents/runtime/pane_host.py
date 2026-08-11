"""The pane operations interactive delivery and a head's lifecycle need, and Orca's answers to them.

Delivery asks a session manager for very little: write bytes into a pane, read the pane back, and
wait until it will accept input.  Everything above this module is about what those answers mean —
readiness classification, composer residue, framing, the body/submit pair — and none of it is
specific to Orca.

A head's own life needs more than delivery, and it needs it through the same seam: `spawn` opens a
pane, `stop` closes one, and both have to find a pane again after the session manager aliased the
handle it first gave out.  Those verbs used to exist only as ``orca terminal`` argument vectors
inside the dispatcher, which is what made the three head operations impossible to run without Orca.
So ``SessionHost`` extends ``PaneHost`` with them rather than a second protocol being invented
beside it: one live pane is one thing, and a head that is delivered to and later closed would
otherwise have to hold two objects and hope they name the same pty.

Orca is nonetheless the only session manager this product has, so ``OrcaPaneHost`` and
``OrcaSessionHost`` are the defaults everywhere.  What this seam buys is that its argument vectors
exist in one file: a second implementation is a class with these methods, not a search for
``"orca"`` through the delivery and bring-up paths.  What it still does not cover is worktree
registration and removal — a larger and differently-shaped dependency (`docs/ARCHITECTURE.md`) that
belongs to a workspace rather than to a pane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from .tui_delivery_types import RunJson


class PaneHostError(RuntimeError):
    """A session manager that answered a pane operation with something unusable."""


@dataclass(frozen=True)
class Pane:
    """One pane as the session manager named it: the handle to address, and its stable leaf.

    Orca can alias the handle it returned at create time while the leaf stays put, so the leaf is
    what a later tick re-finds the same pty by.  A host that has no such notion returns the handle
    alone and every lookup then goes by handle, which is what a backend with stable handles means.
    """

    handle: str
    leaf: str = ""
    # The label the session manager currently shows for this pane. Weak identity on purpose: an
    # interactive head overwrites it with its own OSC sequence seconds after launch, so it is only
    # ever a fallback for a pane whose handle and leaf were never persisted.
    title: str = ""
    # Whether the session manager still has a live connection to this pane. Only an inventory
    # answers it; a pane just created is connected by construction. Callers use it to pick a pane,
    # never to decide a head is dead — a disconnected pane is one nothing can be typed into, which
    # is not the same fact as a process that exited.
    connected: bool = True
    # When this pane last printed, in epoch seconds, or 0.0 when the session manager did not say.
    # Advisory work liveness for a caller that watches a head for progress; the pid heartbeat is
    # what answers whether a process is there.
    last_output_at: float = 0.0


class PaneHost(Protocol):
    """What a session manager must answer for one live interactive pane."""

    def send(self, handle: str, text: str, *, enter: bool) -> Any:
        """Write ``text`` into the pane, optionally following it with a submission."""

    def read(self, handle: str, *, limit: int | None = None) -> Any:
        """Return the pane's retained tail and its own output cursor."""

    def wait_idle(self, handle: str, *, timeout_ms: int) -> Any:
        """Block until the pane reports it will accept input, or refuse."""


class SessionHost(PaneHost, Protocol):
    """The pane's whole life: everything `PaneHost` answers, plus opening and closing one.

    A head operation reaches the session manager through this and nothing else, so an
    implementation of these methods is the whole of what a backend-independent head run needs.
    """

    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        """Open a pane in ``workspace`` running ``command``, labelled ``title``."""

    def split_pane(self, handle: str, command: str) -> Pane:
        """Open a pane running ``command`` beside an existing one."""

    def rename_pane(self, handle: str, title: str) -> None:
        """Relabel a pane, or refuse."""

    def close_pane(self, handle: str) -> None:
        """Close one pane, or refuse."""

    def panes(self, workspace: str) -> Sequence[Pane]:
        """Every pane the session manager currently has in ``workspace``."""

    def stop_workspace(self, workspace: str) -> None:
        """Stop every pane of ``workspace``, for a caller that can no longer name one."""


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


def _epoch_seconds(value: Any) -> float:
    """Orca times a pane's last output in milliseconds; a missing or unparsable one is no clock."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _pane_key_leaf(value: Any) -> str:
    """Read Orca's stable leaf id from its create-time ``tabId:leafId`` pane key."""
    if not isinstance(value, str):
        return ""
    _tab, separator, leaf = value.partition(":")
    return leaf if separator and leaf else ""


@dataclass(frozen=True)
class OrcaSessionHost(OrcaPaneHost):
    """Orca's answer to the whole pane lifecycle, as its ``orca terminal`` CLI spells it.

    These argument vectors were the dispatcher's private ones (`_create_terminal`, `_split_pane`,
    the terminal inventory and the by-worktree stop). They are here so that the operations above
    them hold no opinion about which session manager is installed, and so the dispatcher's own
    lifecycle paths and the head operations issue one set of commands rather than two.
    """

    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        result = self.run_json([
            "orca", "terminal", "create",
            "--worktree", f"path:{workspace}",
            "--title", title,
            "--command", command,
            "--json",
        ])
        terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else result
        handle = (
            terminal.get("handle") or terminal.get("id") if isinstance(terminal, dict) else None
        )
        if not isinstance(handle, str) or not handle:
            raise PaneHostError("orca did not return a terminal handle")
        return Pane(handle=handle, leaf=_pane_key_leaf(terminal.get("paneKey")))

    def split_pane(self, handle: str, command: str) -> Pane:
        result = self.run_json([
            "orca", "terminal", "split",
            "--terminal", handle,
            "--direction", "vertical",
            "--command", command,
            "--json",
        ])
        split = result.get("split") if isinstance(result.get("split"), dict) else result
        opened = split.get("handle") if isinstance(split, dict) else None
        if not isinstance(opened, str) or not opened:
            raise PaneHostError("orca did not return a split terminal handle")
        return Pane(handle=opened, leaf=_pane_key_leaf(split.get("paneKey")))

    def rename_pane(self, handle: str, title: str) -> None:
        self.run_json([
            "orca", "terminal", "rename", "--terminal", handle, "--title", title, "--json",
        ])

    def close_pane(self, handle: str) -> None:
        self.run_json(["orca", "terminal", "close", "--terminal", handle, "--json"])

    def panes(self, workspace: str) -> list[Pane]:
        if not workspace:
            raise PaneHostError("terminal inventory needs a workspace")
        data = self.run_json(
            ["orca", "terminal", "list", "--worktree", f"path:{workspace}", "--json"]
        )
        if isinstance(data, dict) and data.get("ok") is False:
            raise PaneHostError("orca terminal list failed")
        payload = data.get("result") if isinstance(data.get("result"), dict) else data
        terminals = payload.get("terminals") if isinstance(payload, dict) else None
        # An inventory that cannot be read is not an empty worktree, and the difference decides
        # whether a head is stopped or replaced. An answer this cannot parse says nothing about
        # which panes exist, so it refuses rather than reporting none — the caller that only needs
        # to pick a pane degrades that refusal into [] itself.
        if not isinstance(terminals, list):
            raise PaneHostError("orca terminal list returned an unsupported shape")
        return [
            Pane(
                handle=str(entry.get("handle") or ""),
                leaf=str(entry.get("leafId") or ""),
                title=str(entry.get("title") or ""),
                connected=entry.get("connected") is not False,
                last_output_at=_epoch_seconds(entry.get("lastOutputAt")),
            )
            for entry in terminals
            if isinstance(entry, dict)
        ]

    def stop_workspace(self, workspace: str) -> None:
        self.run_json(["orca", "terminal", "stop", "--worktree", f"path:{workspace}", "--json"])


def safe_command_label(args: Sequence[str]) -> str:
    """Describe one of the argument vectors above without retaining a prompt in an exception.

    A runner includes its label in failures, and a failure record outlives the pane, so the label
    has to be redacted before a subprocess is started rather than scrubbed after an arbitrary
    provider error has been made durable. `send` deliberately passes the prompt as `--text`, which
    is the only argument here that can hold one.

    This lives beside the vectors rather than with the runner that is handed them: which word of an
    `orca terminal` call carries a prompt is a fact about the CLI this module spells, and a redactor
    kept anywhere else is a redactor that goes stale the next time a vector changes. It reads a
    vector rather than building one, so a runner for another session manager passes its own through
    and gets it back unchanged.
    """
    args = list(args)
    if args[:3] != ["orca", "terminal", "send"]:
        return " ".join(args)
    safe: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            safe.append("<prompt-redacted>")
            redact_next = False
        else:
            safe.append(arg)
            redact_next = arg == "--text"
    return " ".join(safe)


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


def session_host(
    run_json: RunJson | None = None, *, host: SessionHost | None = None
) -> SessionHost:
    """The same resolution for a caller that opens and closes panes as well as writing into them."""
    if host is not None:
        return host
    if run_json is None:
        raise ValueError("a head's lifecycle needs either a session host or a command runner")
    return OrcaSessionHost(run_json)
