"""The pane operations interactive delivery and a head's lifecycle need, and Orca's answers to them.

Delivery asks a session manager for very little: write bytes into a pane, read the pane back, and
wait until it will accept input. Everything above this module is about what those answers mean,
and none of it is specific to Orca.

A head's own life needs more than delivery and needs it through the same seam: `spawn` opens a
pane, `stop` closes one, and both have to find a pane again after the session manager aliased the
handle it first gave out. So ``SessionHost`` extends ``PaneHost`` with them rather than a second
protocol being invented beside it.

Orca is the only session manager this product has, so ``OrcaPaneHost`` and ``OrcaSessionHost`` are
the defaults everywhere; what this seam buys is that its argument vectors exist in one file. What
it does not cover is worktree registration and removal, which belongs to a workspace, not a pane.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .tui_delivery_types import RunJson


class PaneHostError(RuntimeError):
    """A session manager that answered a pane operation with something unusable."""


class PaneSplitSourceMissing(PaneHostError):
    """The split anchor is absent from the host's renderer graph.

    A pane can remain addressable as a PTY after its UI node disappears. Orca uses this same
    refusal before and after attempting to create the child, so a caller must inventory panes
    before recovering with a standalone pane. Other split failures stay untyped and fail closed
    because they may have left a process behind.
    """


@dataclass(frozen=True)
class Pane:
    """One pane as the session manager named it: the handle to address, and its stable leaf.

    Orca can alias the handle it returned at create time while the leaf stays put, so the leaf is what
    a later tick re-finds the same pty by. A host with no such notion returns the handle alone.
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
    # The runtime pane the session manager named for this pty, `None` when it named none -- which
    # is what `orca terminal list` answers on the build measured on 2026-08-25: its entries carry
    # no `paneRuntimeId` at all. The symptom `paneRuntimeId: -1` recorded in
    # issue:84c0ae4f796f994a7c1d came from a different call, so this field is supplementary
    # evidence to be reported where a host does supply it, and never the basis of an answer about
    # what the window draws -- `RuntimeLayout` below is that basis. Advisory in exactly the sense
    # `connected` is: it says what the window shows, never whether a process is there.
    runtime_pane_id: int | None = None
    # Set only by the head-operation recovery path. Pane inventory never supplies it.
    fallback_reason: str = ""


@dataclass(frozen=True)
class RuntimeLayout:
    """Which ptys the session manager's renderer actually draws in one workspace.

    A pty can be listed, connected and writable while no runtime pane draws it: the tab exists in
    the model, the window shows nothing, and an operator reads the empty workspace as a head that
    never started (issue:84c0ae4f796f994a7c1d). The renderer's own answer to "what is drawn" is the
    visual-layout tree Orca returns beside the terminal list, so membership in that tree -- not a
    field of the terminal entry -- is what tells the two apart.

    Everything a caller needs to answer honestly is kept apart here. ``supported`` is false when
    the channel named no tree at all, ``known_workspace`` is false when it named trees but none
    for this workspace, and ``terminal_nodes`` counts the drawn ptys so that "the tree draws
    nothing" stays distinguishable from "the tree named no identity this pty can be compared by".
    None of those is a statement about a head.
    """

    supported: bool = False
    reason: str = ""
    known_workspace: bool = False
    # The identities the drawn nodes carry. `leafId` is the primary key on purpose: the session
    # manager can hand back a different handle alias for the same pty (dispatcher_state.py:132),
    # so a handle that does not match is not evidence of anything.
    leaves: frozenset[str] = frozenset()
    handles: frozenset[str] = frozenset()
    terminal_nodes: int = 0


@dataclass(frozen=True)
class WorkspaceInventory:
    """One workspace's ptys and, beside them, what its renderer draws.

    Both come from a single answer, so the two halves cannot disagree about which ptys existed at
    the moment they were read.
    """

    panes: tuple[Pane, ...] = ()
    layout: RuntimeLayout = RuntimeLayout()


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

    A head operation reaches the session manager through this and nothing else.
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
    """The installed session manager, reached through its public ``orca terminal`` CLI."""

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
        return self.run_json(
            [
                "orca",
                "terminal",
                "wait",
                "--terminal",
                handle,
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(timeout_ms),
                "--json",
            ]
        )


def _epoch_seconds(value: Any) -> float:
    """Orca times a pane's last output in milliseconds; a missing or unparsable one is no clock."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _runtime_pane_id(value: Any) -> int | None:
    """Read Orca's `paneRuntimeId`, keeping "the host said nothing" apart from "no pane".

    A session manager that never names the field leaves ``None``; only a value it did name becomes
    a number, because the whole point of the field is telling an unrendered pane apart from an
    unanswered question.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _panes_from_payload(payload: dict[str, Any]) -> list[Pane]:
    """The ptys of one `terminal list` answer, refusing a shape that names none.

    An inventory that cannot be read is not an empty worktree, and the difference decides whether a
    head is stopped or replaced. An answer this cannot parse says nothing about which panes exist,
    so it refuses rather than reporting none — the caller that only needs to pick a pane degrades
    that refusal into [] itself.
    """
    terminals = payload.get("terminals")
    if not isinstance(terminals, list):
        raise PaneHostError("orca terminal list returned an unsupported shape")
    return [
        Pane(
            handle=str(entry.get("handle") or ""),
            leaf=str(entry.get("leafId") or ""),
            title=str(entry.get("title") or ""),
            connected=entry.get("connected") is not False,
            last_output_at=_epoch_seconds(entry.get("lastOutputAt")),
            runtime_pane_id=_runtime_pane_id(entry.get("paneRuntimeId")),
        )
        for entry in terminals
        if isinstance(entry, dict)
    ]


def _runtime_layout(payload: dict[str, Any], workspace: str) -> RuntimeLayout:
    """What the renderer draws in ``workspace``, or why that could not be read.

    Three answers, kept apart because collapsing any two of them would reproduce the very error
    this exists to stop. A build that ignores or does not know the option leaves no
    ``visualLayouts`` key: unsupported. A channel that answered but named no tree for this
    workspace has said nothing about this workspace: not known. Only a tree that is actually here
    licenses reading a pty's absence from it as "nothing draws it".
    """
    layouts = payload.get("visualLayouts")
    if not isinstance(layouts, list):
        return RuntimeLayout(
            supported=False,
            reason=(
                "this session manager's terminal inventory carried no `visualLayouts`, so what "
                "its renderer draws could not be read"
            ),
        )
    trees = [
        entry
        for entry in layouts
        if isinstance(entry, dict) and _same_workspace(entry.get("worktreePath"), workspace)
    ]
    if not trees:
        return RuntimeLayout(
            supported=True,
            reason=(
                "the session manager's renderer named no layout tree for this workspace, so "
                "what it draws here could not be read"
            ),
        )
    nodes: list[dict[str, Any]] = []
    for entry in trees:
        _visual_terminal_nodes(entry.get("root"), nodes)
    leaves = {leaf for leaf in (_layout_leaf(node) for node in nodes) if leaf}
    handles = {str(node.get("handle") or "") for node in nodes}
    return RuntimeLayout(
        supported=True,
        known_workspace=True,
        reason="",
        leaves=frozenset(leaves),
        handles=frozenset(handle for handle in handles if handle),
        terminal_nodes=len(nodes),
    )


def _layout_leaf(node: dict[str, Any]) -> str:
    return str(node.get("leafId") or "") or _pane_key_leaf(node.get("paneKey"))


def _visual_terminal_nodes(node: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Every drawn terminal in a renderer tree, without spelling the tree's own grammar.

    Orca nests groups, tabs and split children under keys this module has no reason to know; what
    it does know is that a drawn pty is a node saying ``type: "terminal"``. Walking generically
    means a tree that grows a new kind of container keeps answering correctly instead of silently
    reporting every pty as undrawn.
    """
    if depth > 64:
        return
    if isinstance(node, dict):
        if node.get("type") == "terminal":
            out.append(node)
            return
        for value in node.values():
            if isinstance(value, (dict, list)):
                _visual_terminal_nodes(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _visual_terminal_nodes(item, out, depth + 1)


def _same_workspace(value: Any, workspace: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return os.path.abspath(os.path.expanduser(value)) == os.path.abspath(os.path.expanduser(workspace))


def _pane_key_leaf(value: Any) -> str:
    """Read Orca's stable leaf id from its create-time ``tabId:leafId`` pane key."""
    if not isinstance(value, str):
        return ""
    _tab, separator, leaf = value.partition(":")
    return leaf if separator and leaf else ""


@dataclass(frozen=True)
class OrcaSessionHost(OrcaPaneHost):
    """Orca's answer to the whole pane lifecycle, as its ``orca terminal`` CLI spells it."""

    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        result = self.run_json(
            [
                "orca",
                "terminal",
                "create",
                "--worktree",
                f"path:{workspace}",
                "--title",
                title,
                "--command",
                command,
                "--json",
            ]
        )
        terminal = result.get("terminal") if isinstance(result.get("terminal"), dict) else result
        handle = terminal.get("handle") or terminal.get("id") if isinstance(terminal, dict) else None
        if not isinstance(handle, str) or not handle:
            raise PaneHostError("orca did not return a terminal handle")
        return Pane(handle=handle, leaf=_pane_key_leaf(terminal.get("paneKey")))

    def split_pane(self, handle: str, command: str) -> Pane:
        try:
            result = self.run_json(
                [
                    "orca",
                    "terminal",
                    "split",
                    "--terminal",
                    handle,
                    "--direction",
                    "vertical",
                    "--command",
                    command,
                    "--json",
                ]
            )
        except Exception as exc:  # The injected runner owns its error type.
            if "terminal_split_source_not_found" in str(exc):
                raise PaneSplitSourceMissing("orca split source is absent from the renderer graph") from None
            raise
        split = result.get("split") if isinstance(result.get("split"), dict) else result
        opened = split.get("handle") if isinstance(split, dict) else None
        if not isinstance(opened, str) or not opened:
            raise PaneHostError("orca did not return a split terminal handle")
        return Pane(handle=opened, leaf=_pane_key_leaf(split.get("paneKey")))

    def rename_pane(self, handle: str, title: str) -> None:
        self.run_json(
            [
                "orca",
                "terminal",
                "rename",
                "--terminal",
                handle,
                "--title",
                title,
                "--json",
            ]
        )

    def close_pane(self, handle: str) -> None:
        self.run_json(["orca", "terminal", "close", "--terminal", handle, "--json"])

    def _list(self, workspace: str, *, with_layout: bool) -> dict[str, Any]:
        if not workspace:
            raise PaneHostError("terminal inventory needs a workspace")
        args = ["orca", "terminal", "list", "--worktree", f"path:{workspace}"]
        if with_layout:
            # The renderer tree is opt-in and every other caller pays nothing for it: a delivery
            # tick asking which pty to write into does not need to know what is drawn.
            args.append("--include-visual-layouts")
        args.append("--json")
        data = self.run_json(args)
        if isinstance(data, dict) and data.get("ok") is False:
            raise PaneHostError("orca terminal list failed")
        payload = (
            data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
        )
        if not isinstance(payload, dict):
            raise PaneHostError("orca terminal list returned an unsupported shape")
        return payload

    def panes(self, workspace: str) -> list[Pane]:
        return _panes_from_payload(self._list(workspace, with_layout=False))

    def workspace_inventory(self, workspace: str) -> WorkspaceInventory:
        """The ptys of one workspace and, from the same answer, the renderer tree that draws them.

        Asks for the visual layouts explicitly, because the terminal entries alone cannot say what
        is drawn. A session manager too old to know the option refuses the whole call, and refusing
        the inventory with it would report a live workspace as unreadable -- so that one case falls
        back to the plain listing and says, in the layout's own ``reason``, that the channel is not
        supported here. A refusal of the plain listing too is a refusal, and propagates.
        """
        try:
            payload = self._list(workspace, with_layout=True)
        except Exception as exc:  # The injected runner owns its error type.
            # Broad on purpose: what distinguishes "this build does not know the option" from "the
            # session manager is down" is not the exception, it is whether the plain listing below
            # answers. If it refuses too, that refusal is the one that propagates.
            return WorkspaceInventory(
                panes=tuple(self.panes(workspace)),
                layout=RuntimeLayout(
                    supported=False,
                    reason=(
                        "this session manager did not answer `terminal list "
                        f"--include-visual-layouts` ({str(exc)[:160]}), so what its renderer "
                        "draws could not be read"
                    ),
                ),
            )
        return WorkspaceInventory(
            panes=tuple(_panes_from_payload(payload)),
            layout=_runtime_layout(payload, workspace),
        )

    def stop_workspace(self, workspace: str) -> None:
        self.run_json(["orca", "terminal", "stop", "--worktree", f"path:{workspace}", "--json"])


def safe_command_label(args: Sequence[str]) -> str:
    """Describe one of the argument vectors above without retaining a prompt in an exception.

    A runner includes its label in failures and a failure record outlives the pane, so the label is
    redacted before a subprocess is started rather than scrubbed after an arbitrary provider error
    has been made durable. `send` deliberately passes the prompt as `--text`, the only argument here
    that can hold one. It lives beside the vectors, because which word carries a prompt is a fact
    about the CLI this module spells.
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

    Callers pass the runner they always passed and get Orca. A caller that has a session manager of
    its own passes ``host`` and the runner is never consulted.
    """
    if host is not None:
        return host
    if run_json is None:
        raise ValueError("interactive delivery needs either a pane host or a command runner")
    return OrcaPaneHost(run_json)


def session_host(run_json: RunJson | None = None, *, host: SessionHost | None = None) -> SessionHost:
    """The same resolution for a caller that opens and closes panes as well as writing into them."""
    if host is not None:
        return host
    if run_json is None:
        raise ValueError("a head's lifecycle needs either a session host or a command runner")
    return OrcaSessionHost(run_json)
