"""The restore's card initialization against an in-process model of the PostgreSQL card store.

The recovery drill of ummanu-41 stopped on production's `personal_site-198`: a closed card in the
`personal_site` lane, on a project id the registry has since retired for `personal-site`. Every
write the restore sent was taken; what failed was the proof that reads them back. The card's export
carries `"model": ""`, and the store, which clears a key it is handed empty, reads that key back
absent.

`StoreModel` answers the calls `restore_cards_batched` makes the way `SqlCardClient` does: the
virtual lane table is the sorted set of lane names, a lane id is a position in it, and
`saveTaskMetadata` sorts each key into the same column, counter, link or bag treatment, from the
store's own key tables. It refuses what the store refuses on these calls. The real store is
`tests/test_snapshot_recovery_postgres.py`'s, in CI.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ummanu.board import sql_cards
from ummanu.board.sql_cards import SqlCardError
from ummanu.restore import _ensure_restore_swimlanes, _normalized_cards, _restore_card_order
from ummanu.task_restore import close_restored_cards_batched, restore_cards_batched
from ummanu.tasks import TaskError

BOARD = sql_cards.BOARD_ID
COLUMNS = dict(sql_cards.BOARD_COLUMNS)

#: `cards/0001/00001509.json` of production's checkpoint, less its comments.
RETIRED = {
    "closed": True,
    "column": "Done",
    "comments": [],
    "date_moved": 1785832418,
    "description": "## Цель\n\nПривести docker-сборку сервисов в порядок.\n",
    "fields": {
        "base_branch": "",
        "blocked_by": "",
        "claim": "personal_site-198-1782988235",
        "effective_head": "claude-sonnet",
        "effective_review_head": "codex-terra-high",
        "head": "claude-sonnet",
        "project": "personal_site",
        "review_head": "",
        "slug": "",
        "task_type": "code",
    },
    "id": 360,
    "metadata": {
        "claim": "personal_site-198-1782988235",
        "complexity": "standard",
        "family_preference": "auto",
        "head": "claude-sonnet",
        "model": "",
        "project": "personal_site",
        "record_type": "task",
        "task_type": "code",
    },
    "position": 1,
    "reference": "personal_site-198",
    "swimlane": "personal_site",
    "title": "Docker-гигиена: .dockerignore + непривилегированная dev-стадия бэкенда",
}

#: A live card on the id that replaced it: its lane is the one `_matching_swimlane` would also
#: accept for `personal_site` if the exact name were not there.
CURRENT = {
    "column": "Ready",
    "comments": [],
    "description": "",
    "fields": {"project": "personal-site", "task_type": "code"},
    "metadata": {"project": "personal-site", "record_type": "task", "task_type": "code"},
    "position": 1,
    "reference": "personal-site-1",
    "swimlane": "personal-site",
    "title": "Current site card",
}


class StoreModel:
    """`SqlCardClient`'s answers to the restore's card calls, without PostgreSQL."""

    instance_dir = "."

    def __init__(self) -> None:
        self.lanes: list[str] = []
        self.rows: dict[int, dict[str, Any]] = {}
        self.next_key = 1

    # The client's two entry points: one call, and a batch that runs call by call.
    def call(self, method: str, **params: Any) -> Any:
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise SqlCardError(f"the board store does not serve {method}")
        return handler(**params)

    def call_batch(self, calls: Any) -> list[Any]:
        return [self.call(method, **dict(arguments)) for method, arguments in calls]

    # --- lanes: a sorted virtual table, an id is a position in it ----------------------------------
    def _lane_names(self) -> list[str]:
        named = {row["lane"] for row in self.rows.values() if row["lane"]}
        return sorted(named | set(self.lanes))

    def _lane_id(self, name: str | None) -> int:
        lanes = self._lane_names()
        return lanes.index(name) + 1 if name in lanes else 0

    def _lane_name(self, identifier: Any) -> str | None:
        lanes = self._lane_names()
        index = int(identifier or 0)
        return lanes[index - 1] if 1 <= index <= len(lanes) else None

    def _rpc_getActiveSwimlanes(self, *, project_id: int) -> list[dict[str, Any]]:
        return [{"id": index, "name": name} for index, name in enumerate(self._lane_names(), start=1)]

    def _rpc_addSwimlane(self, *, project_id: int, name: str) -> Any:
        if name in self._lane_names():
            return False
        self.lanes.append(name)
        return self._lane_id(name)

    # --- cards ---------------------------------------------------------------------------------
    def _row(self, task_id: Any) -> dict[str, Any]:
        row = self.rows.get(int(task_id))
        if row is None:
            raise SqlCardError(f"no card carries transport key {task_id}")
        return row

    def _rpc_createTask(
        self,
        *,
        project_id: int,
        title: str,
        description: str = "",
        column_id: int = 1,
        swimlane_id: int = 0,
        reference: str = "",
    ) -> int:
        if not reference:
            raise SqlCardError("the board store identifies a card by its reference (§9)")
        key, self.next_key = self.next_key, self.next_key + 1
        self.rows[key] = {
            "reference": reference,
            "title": title,
            "description": description or "",
            "column_id": int(column_id),
            "position": 1,
            "lane": self._lane_name(swimlane_id),
            "archived": False,
            "columns": {},
            "bag": {},
        }
        return key

    def _rpc_getAllTasks(self, *, project_id: int, status_id: int) -> list[dict[str, Any]]:
        return [
            {
                "id": key,
                "reference": row["reference"],
                "title": row["title"],
                "description": row["description"],
                "column_id": row["column_id"],
                "position": row["position"],
                "swimlane_id": self._lane_id(row["lane"]),
                "is_active": 0 if row["archived"] else 1,
            }
            for key, row in sorted(self.rows.items())
            if (status_id == 1) != row["archived"]
        ]

    def _rpc_moveTaskPosition(
        self, *, project_id: int, task_id: int, column_id: int, position: int, swimlane_id: int = 0
    ) -> bool:
        row = self._row(task_id)
        row["column_id"] = int(column_id)
        row["position"] = max(1, int(position))
        lane = self._lane_name(swimlane_id)
        if lane is not None:
            row["lane"] = lane
        return True

    def _rpc_closeTask(self, *, task_id: int) -> bool:
        self._row(task_id)["archived"] = True
        return True

    # --- metadata: `_rpc_saveTaskMetadata` and `_card_metadata`, key by key ------------------------
    def _rpc_saveTaskMetadata(self, *, task_id: int, values: dict[str, Any]) -> bool:
        row = self._row(task_id)
        declared = sql_cards._text(values.get("record_type"))
        if declared not in {"", "task"}:
            raise SqlCardError(f"card {row['reference']} is a task; it cannot carry record_type {declared!r}")
        columns, bag = row["columns"], row["bag"]
        for key, raw in values.items():
            text = sql_cards._text(raw)
            if (
                key in sql_cards._METADATA_COLUMNS
                or key in sql_cards._METADATA_LINKS
                or (key == sql_cards._METADATA_TIMESTAMP[0])
            ):
                # The column (or satellite rows) holds a value; an empty one is remembered in the bag.
                columns[key] = text
                if text:
                    bag.pop(key, None)
                else:
                    bag[key] = ""
            elif key == sql_cards._METADATA_FLAG[0]:
                columns[key] = "1" if sql_cards.live_impact_flag(text) else ""
                bag.pop(key, None)
            elif key in sql_cards._METADATA_COUNTERS:
                columns[key] = text if text.isdigit() and int(text) else ""
            elif text:
                bag[key] = text
            else:
                # An empty value for a key only the bag carries clears it.
                bag.pop(key, None)
        return True

    def _rpc_getTaskMetadata(self, *, task_id: int) -> dict[str, str]:
        row = self._row(task_id)
        meta = {key: value for key, value in row["columns"].items() if value}
        meta.update(row["bag"])
        meta["record_type"] = "task"
        return meta


class _Audit:
    """The restore's staged obligations, held the way `SqlTaskAudit` holds them."""

    def __init__(self) -> None:
        self.staged: dict[str, dict[str, Any]] = {}

    def committed_event(self, request_id: str) -> None:
        return None

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.staged.get(request_id))

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        self.staged[request_id] = copy.deepcopy(event)

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        self.staged.pop(request_id, None)

    @staticmethod
    def require_claim(existing: dict[str, Any], **_claim: Any) -> None:
        return None


def _exported(cards: list[dict[str, Any]], registered: set[str]) -> list[dict[str, Any]]:
    """The cards as restore reads them from the materialized export, against `registered`."""
    with tempfile.TemporaryDirectory() as raw:
        board = Path(raw) / "board"
        board.mkdir()
        (board / "cards.json").write_text(json.dumps({"version": 1, "cards": cards}), encoding="utf-8")
        return _normalized_cards(Path(raw), registered_project_ids=registered)


def _restore(store: StoreModel, cards: list[dict[str, Any]]) -> SimpleNamespace:
    writer = SimpleNamespace(client=store, audit=_Audit())
    ordered = sorted(cards, key=_restore_card_order)
    swimlanes = {lane["id"]: lane["name"] for lane in store.call("getActiveSwimlanes", project_id=BOARD)}
    columns, swimlanes = _ensure_restore_swimlanes(store, BOARD, COLUMNS, swimlanes, ordered)
    restore_cards_batched(
        writer,
        ordered,
        board_id=BOARD,
        columns=columns,
        swimlanes=swimlanes,
        existing={},
        request_prefix="restore:test:",
    )
    live = {row["reference"]: row for row in store.call("getAllTasks", project_id=BOARD, status_id=1)}
    close_restored_cards_batched(store, ordered, live, board_id=BOARD)
    return writer


def _placed(store: StoreModel, reference: str) -> dict[str, Any]:
    """Where the store holds a card, in the export's terms."""
    key, row = next((key, row) for key, row in store.rows.items() if row["reference"] == reference)
    return {
        "column": COLUMNS[row["column_id"]],
        "swimlane": row["lane"],
        "position": row["position"],
        "closed": row["archived"],
        "metadata": store.call("getTaskMetadata", task_id=key),
    }


class RetiredProjectCardTests(unittest.TestCase):
    def test_a_closed_card_on_a_retired_project_id_restores_as_exported(self) -> None:
        cards = _exported([copy.deepcopy(RETIRED), copy.deepcopy(CURRENT)], registered={"personal-site"})
        store = StoreModel()

        writer = _restore(store, cards)

        restored = _placed(store, "personal_site-198")
        # The placement is the export's: its own lane, not the current id's look-alike one.
        self.assertEqual(
            {key: restored[key] for key in ("column", "swimlane", "position", "closed")},
            {"column": "Done", "swimlane": "personal_site", "position": 1, "closed": True},
        )
        self.assertEqual(_placed(store, "personal-site-1")["swimlane"], "personal-site")
        self.assertEqual(sorted(store._lane_names()), ["personal-site", "personal_site"])
        # Every value the export carries is on the row; the empty `model` reads back as no value.
        exported = {key: value for key, value in RETIRED["metadata"].items() if value}
        self.assertEqual({k: v for k, v in restored["metadata"].items() if k in exported}, exported)
        self.assertNotIn("model", restored["metadata"])
        self.assertEqual(restored["metadata"]["project"], "personal_site")
        # Both obligations were proved initialized.
        revisions = {event["ref"]: event["backend"]["revision"] for event in writer.audit.staged.values()}
        self.assertEqual(revisions, {"personal_site-198": "initialized", "personal-site-1": "initialized"})

    def test_a_value_the_store_did_not_keep_is_still_incomplete(self) -> None:
        """Empty and absent are one value; a non-empty value the row lacks is not."""
        cards = _exported([copy.deepcopy(RETIRED)], registered={"personal-site"})
        store = StoreModel()
        save = store._rpc_saveTaskMetadata

        def losing_claim(*, task_id: int, values: dict[str, Any]) -> bool:
            return save(task_id=task_id, values={k: v for k, v in values.items() if k != "claim"})

        store._rpc_saveTaskMetadata = losing_claim  # type: ignore[method-assign]

        with self.assertRaises(TaskError) as raised:
            _restore(store, cards)

        self.assertEqual(
            raised.exception.message,
            "board parity check failed: restored-card initialization is incomplete for personal_site-198",
        )

    def test_the_model_refuses_what_the_store_refuses(self) -> None:
        store = StoreModel()
        key = store.call("createTask", project_id=BOARD, title="t", column_id=1, reference="ummanu-1")
        with self.assertRaises(SqlCardError) as raised:
            store.call("saveTaskMetadata", task_id=key, values={"record_type": "issue"})
        self.assertEqual(
            raised.exception.message, "card ummanu-1 is a task; it cannot carry record_type 'issue'"
        )
        with self.assertRaises(SqlCardError):
            store.call("createTask", project_id=BOARD, title="t", column_id=1)


if __name__ == "__main__":
    unittest.main()
