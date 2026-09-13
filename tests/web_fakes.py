"""Recording layers and a minimal system snapshot for tests of `secretary.web.app.WebApp`."""

from __future__ import annotations

from typing import Any

from secretary.webproto.reads import health_summary


class Recording:
    """A layer that records every call and answers with the document it was given per operation.

    An answer that is an exception is raised instead, so a test can make one operation refuse.
    """

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def operation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, {"args": args, **kwargs}))
            answer = self.answers.get(name, {"kind": name})
            if isinstance(answer, Exception):
                raise answer
            return answer

        return operation


def system_snapshot() -> dict[str, Any]:
    """The smallest snapshot the dashboard renders: an installation with nothing in flight."""
    source = {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-13T12:00:00Z",
    }
    return {
        "observed_at": "2026-09-13T12:00:00Z",
        "installation": {
            "instance": "/i",
            "name": "test",
            "data_dir": "/d",
            "health": {"source": source, "status": health_summary({})},
        },
        "projects": {"source": source, "items": []},
        "tasks": {"source": source, "items": []},
        "agents": {"source": source, "items": []},
    }
