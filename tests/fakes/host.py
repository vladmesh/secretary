from __future__ import annotations

from collections.abc import Callable

from triggered_agents.runtime.pane_host import Pane, PaneSplitSourceMissing


class FakeSessionHost:
    """A session manager with no Orca behind it: panes are rows in a list.

    It answers every method of `SessionHost` and records what it was asked, so a test can say both
    "the head came up" and "it came up through the host rather than around it".
    """

    def __init__(self) -> None:
        self.panes_by_workspace: dict[str, list[Pane]] = {}
        self.commands: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.sent: list[tuple[str, str, bool]] = []
        self.closed: list[str] = []
        self.refuse_close = False
        self.split_source_missing = False
        self.next_handle = 1
        # A test that cares *when* the session manager was reached, not only whether it was.
        self.on_call: Callable[[str], None] | None = None

    def _note(self, name: str) -> None:
        if self.on_call is not None:
            self.on_call(name)

    # lifecycle ------------------------------------------------------------
    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.calls.append(("open_pane", workspace, title))
        self._note("open_pane")
        pane = Pane(handle=f"term:{self.next_handle}", leaf=f"leaf:{self.next_handle}")
        self.next_handle += 1
        self.panes_by_workspace.setdefault(workspace, []).append(pane)
        self.commands[pane.handle] = command
        return pane

    def split_pane(self, handle: str, command: str) -> Pane:
        self.calls.append(("split_pane", handle))
        self._note("split_pane")
        if self.split_source_missing:
            raise PaneSplitSourceMissing("split source vanished")
        workspace = self._workspace_of(handle)
        pane = Pane(handle=f"term:{self.next_handle}", leaf=f"leaf:{self.next_handle}")
        self.next_handle += 1
        self.panes_by_workspace.setdefault(workspace, []).append(pane)
        self.commands[pane.handle] = command
        return pane

    def rename_pane(self, handle: str, title: str) -> None:
        self.calls.append(("rename_pane", handle, title))
        self._note("rename_pane")

    def close_pane(self, handle: str) -> None:
        self.calls.append(("close_pane", handle))
        self._note("close_pane")
        if self.refuse_close:
            raise RuntimeError("tab_not_found")
        self.closed.append(handle)
        for workspace, panes in self.panes_by_workspace.items():
            self.panes_by_workspace[workspace] = [pane for pane in panes if pane.handle != handle]

    def panes(self, workspace: str) -> list[Pane]:
        self.calls.append(("panes", workspace))
        self._note("panes")
        return list(self.panes_by_workspace.get(workspace, []))

    def stop_workspace(self, workspace: str) -> None:
        self.calls.append(("stop_workspace", workspace))
        self._note("stop_workspace")
        self.panes_by_workspace[workspace] = []

    # delivery -------------------------------------------------------------
    def send(self, handle: str, text: str, *, enter: bool):
        self.calls.append(("send", handle))
        self._note("send")
        self.sent.append((handle, text, enter))
        return {"accepted": True, "bytesWritten": len(text.encode())}

    def read(self, handle: str, *, limit: int | None = None):
        self._note("read")
        return {"terminal": {"tail": ["› "], "nextCursor": f"c{len(self.sent)}"}}

    def wait_idle(self, handle: str, *, timeout_ms: int):
        self._note("wait_idle")
        return {"satisfied": True}

    def reincarnate(self, handle: str) -> str:
        """Give a pane a new handle while its pty — and so its leaf — stays exactly where it is."""
        for workspace, panes in self.panes_by_workspace.items():
            for index, pane in enumerate(panes):
                if pane.handle == handle:
                    fresh = Pane(handle=f"{handle}:alias", leaf=pane.leaf)
                    panes[index] = fresh
                    return fresh.handle
        raise AssertionError(f"no pane {handle}")

    def _workspace_of(self, handle: str) -> str:
        for workspace, panes in self.panes_by_workspace.items():
            if any(pane.handle == handle for pane in panes):
                return workspace
        return ""
