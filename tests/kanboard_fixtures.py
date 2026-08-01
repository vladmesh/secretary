from __future__ import annotations


class OfflineKanboard:
    """Zero-network stand-in for `secretary.status.KanboardClient()`.

    A worker, reviewer or operator shell that inherits a live installation's
    `KANBOARD_*` variables must not turn the unit suite into a client of that
    board (secretary-1026: `test_status.py` alone cost ~231s doing exactly
    that against a live Kanboard). `tests/__init__.py` patches
    `secretary.status.KanboardClient` to this before any test module is
    imported, so `collect_status`'s sprint read reports "no sprint board"
    instead of dialing out, regardless of what the environment holds. A test
    with sprint content of its own uses `tests.test_dispatcher.FakeKanboard`
    and patches the same seam locally, the same way the rest of the suite
    overrides other host-facing seams.
    """

    def call(self, method: str, **params: object) -> object:
        if method == "getProjectByName":
            return None
        if method == "getAllTasks":
            return []
        return None
