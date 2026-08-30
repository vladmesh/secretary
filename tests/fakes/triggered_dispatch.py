from __future__ import annotations

from triggered_agents.runtime.pane_host import Pane


class FakeSessionHost:
    """A session manager for a test: it records what it was asked and answers what it was given.

    The scheduler reaches Orca through `SessionHost` and nothing else (secretary-1416), so a test
    of what a tick does to its terminals is a test against one of these — no subprocess, no `orca`
    on the box, and every pane verb observable as the call it is rather than as an argument vector
    somebody has to re-parse.
    """

    def __init__(
        self,
        *,
        panes: tuple[Pane, ...] = (),
        screens: tuple[str, ...] = (),
        idle: bool = True,
        wait_error: BaseException | None = None,
        list_error: BaseException | None = None,
    ) -> None:
        self._panes = list(panes)
        self._screens = list(screens)
        self._idle = idle
        self._wait_error = wait_error
        self._list_error = list_error
        self.sends: list[str] = []
        self.enters: list[bool] = []
        self.waits: list[tuple[str, int]] = []
        self.reads: list[tuple[str, int | None]] = []
        self.opened: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        self.stopped: list[str] = []

    # PaneHost
    def send(self, handle: str, text: str, *, enter: bool) -> dict:
        self.sends.append(text)
        self.enters.append(enter)
        return {"send": {"accepted": True, "bytesWritten": len(text.encode()) + (1 if enter else 0)}}

    def read(self, handle: str, *, limit: int | None = None) -> dict:
        self.reads.append((handle, limit))
        screen = (
            self._screens.pop(0) if len(self._screens) > 1 else (self._screens[0] if self._screens else "")
        )
        return {"terminal": {"tail": screen.splitlines()}}

    def wait_idle(self, handle: str, *, timeout_ms: int) -> dict:
        self.waits.append((handle, timeout_ms))
        if self._wait_error is not None:
            raise self._wait_error
        return {"wait": {"satisfied": self._idle}}

    # SessionHost
    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.opened.append((workspace, title, command))
        pane = Pane(handle=f"term-{len(self.opened)}", leaf=f"leaf-{len(self.opened)}", title=title)
        self._panes.append(pane)
        return pane

    def split_pane(self, handle: str, command: str) -> Pane:
        raise AssertionError("the scheduler never splits a pane")

    def rename_pane(self, handle: str, title: str) -> None:
        raise AssertionError("the scheduler never renames a pane")

    def close_pane(self, handle: str) -> None:
        self.closed.append(handle)
        self._panes = [pane for pane in self._panes if pane.handle != handle]

    def panes(self, workspace: str) -> list[Pane]:
        if self._list_error is not None:
            raise self._list_error
        return list(self._panes)

    def stop_workspace(self, workspace: str) -> None:
        self.stopped.append(workspace)
        self._panes = []
