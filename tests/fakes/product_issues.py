from __future__ import annotations

from tests.fakes.tasks import WriteKanboard


class ProductBoard(WriteKanboard):
    """Kanboard fixture with the Product/Issue layout and real status filtering."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks[0]["id"] = 12

    def call(self, method: str, **params: object) -> object:
        if method == "getColumns":
            return [
                {"id": 1, "title": "Issues"},
                {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"},
                {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"},
                {"id": 6, "title": "Done"},
            ]
        if method == "getAllTasks":
            self.calls.append((method, params))
            status = params.get("status_id")
            if status == 1:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) != 0]
            if status == 0:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) == 0]
            return []
        return super().call(method, **params)
