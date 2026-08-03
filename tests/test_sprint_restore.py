"""Sprint entities through export, checkpoint and restore (secretary-819).

Recovery used to rebuild the Pipeline cards of a sprint and drop the sprint itself:
goal, Definition of Done, repositories, status, budget, current task, resume and every
record to the entity lived only on the live board. These pin the whole path: a filled
closed sprint is exported, restored into a separate empty backend, and compared field by
field against its source.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.data import export_board, init_layout, normalize_sprint_entity
from secretary.restore import RestoreError, import_normalized_board, restore_findings, restore_state
from secretary.sprint_observer import (
    forget_migration_state,
    head_choice,
    none_choice,
    strict_reader_active,
)
from secretary.sprints import (
    SprintReader,
    SprintWriter,
    ensure_sprint_board,
    sprint_admission_lock,
)
from secretary.tasks import TaskWriter

from tests.restore_fixtures import _EmptyBoardsKanboard
from tests.test_sprints import ProductSprintKanboard, _write_project_registry
from tests.observer_identity import as_observer


def _root(name: str) -> str:
    """A declared repository root as the row stores it: canonical and absolute.

    Admission refuses to resolve a stored root itself, so an export standing in for rows
    this installation wrote carries the canonical form too.
    """
    return str(Path(name).resolve())


CARD_EXPORT = {
    "id": 12, "reference": "secretary-12", "title": "Linked card", "description": "card body",
    "column": "Ready", "swimlane": "", "position": 1, "task_type": "code", "project": "secretary",
    "metadata": {
        "record_type": "task", "complexity": "standard", "family_preference": "auto",
    },
    "comments": [],
}
RESUME = {
    "selected_step": "restore the entity", "selected_why": "the checkpoint carries it",
    "rejected_alternatives": "recreate it by hand", "current_task": "secretary-12",
    "dod_state": "tests pending", "next_safe_step": "run the suite",
    "recorded_at": "2026-07-20T00:00:00Z",
}


class SprintRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_data = self.root / "source-data"
        self.target_data = self.root / "target-data"
        init_layout(self.source_data)
        init_layout(self.target_data)
        self.source = ProductSprintKanboard()
        self.instance = _write_project_registry(self.root, "secretary", "secretary-instance")
        self.ref = self._seed_closed_sprint()
        self._export()

    def _seed_closed_sprint(self) -> str:
        writer = SprintWriter(  # type: ignore[arg-type]
            self.source, data_dir=self.source_data, instance=self.instance,
        )
        ref = writer.create(
            role="po", actor="operator", goal="Ship sprint entities into recovery",
            definition_of_done="restore rebuilds the entity", reference="sprint:entity",
            repositories=["secretary", "secretary-instance"], product="secretary",
            issues=["issue:open"], projects=["secretary", "secretary-instance"],
            observer=head_choice("codex-observer"), request_id="seed-create",
        )["sprint"]["ref"]
        with as_observer(ref):
            card = TaskWriter(self.source, data_dir=self.source_data).create(  # type: ignore[arg-type]
                # The sprint holds `secretary`, so its own observer is the writer of its cards.
                role="observer", actor="observer", project="secretary", task_type="code",
                title="linked", target="ready", sprint=ref, request_id="seed-card",
            )["task"]
        writer.comment(role="po", actor="operator", reference=ref, body="first note", request_id="seed-comment")
        writer.record_budget(role="po", actor="operator", reference=ref, event_type="red_ci", request_id="seed-budget")
        writer.set_current_task(
            role="po", actor="operator", reference=ref, task_reference=card["ref"], request_id="seed-current",
        )
        writer.resume(role="po", actor="operator", reference=ref, entry=RESUME, request_id="seed-resume")
        writer.close(role="po", actor="operator", reference=ref, request_id="seed-close")
        return ref

    def _pipeline_command(self) -> list[str]:
        """A stand-in for `triggered_agents pipeline export`; cards are not the subject here."""
        script = self.root / "fake_pipeline.py"
        script.write_text("print(%r)" % json.dumps([CARD_EXPORT]), encoding="utf-8")
        return [sys.executable, str(script)]

    def _export(self) -> None:
        export_board(self.source_data, command=self._pipeline_command(), sprint_client=self.source)
        # Only the normalized export travels; the target backend starts from nothing else.
        for name in ("cards.json", "sprints.json"):
            shutil.copy(self.source_data / "board" / name, self.target_data / "board" / name)

    def _exported_sprint(self) -> dict:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        return payload["sprints"][0]

    def _restore(self, client: object | None = None) -> tuple[object, int]:
        client = client or _EmptyBoardsKanboard()
        return client, import_normalized_board(
            self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
        )

    def _carry_audit_log(self) -> None:
        """Bring the source's audit log along, as the real checkpoint does.

        `state/board/events.ndjson` is checkpoint canon and recovery materializes it back. The
        fixture's own `_export` copies only the normalized card and sprint JSON, so a test that
        needs the log — recovering an open row's head, or the migration's completion event — puts
        it there explicitly.
        """
        shutil.copy(
            self.source_data / "board" / "events.ndjson",
            self.target_data / "board" / "events.ndjson",
        )
        forget_migration_state(self.target_data)

    def _record_observer_launch(self, head: str) -> None:
        events = self.source_data / "board" / "events.ndjson"
        with events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event_id": "evt_launch_" + head.replace("-", "_"), "schema_version": 1,
                "occurred_at": "2026-07-20T00:00:00Z",
                "actor": {"role": "dispatcher", "id": "dispatcher"},
                "kind": "observer_launched", "outcome": "success", "task_id": "",
                "ref": self.ref,
                "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
                "request_id": "req-launch-" + head, "payload": {"head": head, "launches": 1},
            }, sort_keys=True) + "\n")

    def test_export_carries_the_sprint_set_next_to_the_cards(self) -> None:
        summary = json.loads((self.source_data / "board" / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["card_count"], 1)
        self.assertEqual(summary["sprint_count"], 1)
        lines = (self.source_data / "board" / "sprints.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["reference"] for line in lines], [self.ref])
        exported = self._exported_sprint()
        self.assertEqual(exported["status"], "closed")
        self.assertEqual(
            exported["repositories"], [_root("secretary"), _root("secretary-instance")],
        )
        self.assertEqual(exported["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(exported["current_task"], "secretary-26")
        self.assertEqual(exported["resume"]["selected_step"], RESUME["selected_step"])
        self.assertEqual([comment["text"] for comment in exported["comments"]],
                         ["[po]\nfirst note", "[sprint:resume]\n" + RESUME["selected_step"]])

    def test_closed_sprint_is_rebuilt_field_by_field_in_an_empty_backend(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        exported = self._exported_sprint()
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["goal"], exported["goal"])
        self.assertEqual(live["definition_of_done"], exported["definition_of_done"])
        self.assertEqual(live["repositories"], exported["repositories"])
        self.assertEqual(live["product"], "secretary")
        self.assertEqual(live["issues"], exported["issues"])
        self.assertEqual(live["reservations"], exported["reservations"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(live["budget"]["by_type"], exported["budget"]["by_type"])
        self.assertEqual(live["budget"]["total"], 1)
        self.assertEqual(live["current_task"], exported["current_task"])
        self.assertEqual(live["resume"], exported["resume"])
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in exported["comments"]],
        )
        # The entity came back on a new Kanboard row, so its own dates describe the
        # recovery; the dates it was restored from stay readable and compare exactly.
        self.assertEqual(live["audit"]["source"], exported["audit"])
        self.assertNotEqual(live["audit"]["created_at"], exported["audit"]["created_at"])
        self.assertEqual(normalize_sprint_entity(live), exported)
        self.assertEqual(restore_state(self.target_data)["sprint_count"], 1)
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")
        self.assertEqual(restore_findings(self.target_data), ["memory index has not been rebuilt",
                                                             "managed reconcile has not been applied"])

    def test_the_observer_declaration_survives_a_round_trip(self) -> None:
        client, _ = self._restore()

        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["observer"], head_choice("codex-observer"))
        self.assertEqual(self._exported_sprint()["observer"], head_choice("codex-observer"))

    def test_an_invalid_observer_value_stops_the_restore_before_the_first_write(self) -> None:
        """Validated as a set, before the Pipeline cards, not at the sprint step that follows them.

        Sprint entities are written after every card, so validating them where they are written
        would leave a fully restored card board behind the refusal.
        """
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["observer"] = {"kind": "default"}
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "not one of the tagged forms"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        board = ensure_sprint_board(client)  # type: ignore[arg-type]
        self.assertEqual([task for task in client.tasks if task["project_id"] == board], [])
        # The Pipeline card of the same export is untouched too: nothing of either set was written.
        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]
        self.assertEqual(
            [method for method, _ in client.calls if method in {"createTask", "saveTaskMetadata"}],  # type: ignore[attr-defined]
            [],
        )

    def _mark_migrated(self, data_dir: Path) -> None:
        """Put the migration's completion event in the log this recovery travels with."""
        events = data_dir / "board" / "events.ndjson"
        events.parent.mkdir(parents=True, exist_ok=True)
        with events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "event_id": "evt_migration_done", "schema_version": 1,
                "occurred_at": "2026-08-02T00:00:00Z",
                "actor": {"role": "steward", "id": "observer-migration"},
                "kind": "observer_migration_completed", "outcome": "success",
                "task_id": "", "ref": "",
                "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
                "request_id": "observer-migration-completed:test",
                "payload": {"inventory_digest": "test", "rows": 1},
            }, sort_keys=True) + "\n")
        forget_migration_state(data_dir)

    def test_a_migrated_export_missing_an_observer_is_refused(self) -> None:
        """After the migration a row without the field is a corrupt export, not an old one."""
        self._mark_migrated(self.target_data)
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0].pop("observer")
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        with self.assertRaisesRegex(RestoreError, "completed the observer migration"):
            import_normalized_board(self.target_data, client=_EmptyBoardsKanboard())  # type: ignore[arg-type]

    def _pre_migration_open_export(self) -> None:
        """A checkpoint from before the cutover: an open row that declares nothing."""
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0].pop("observer")
        payload["sprints"][0]["status"] = "open"
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        forget_migration_state(self.target_data)

    def test_a_pre_migration_open_row_is_republished_with_its_recovered_head(self) -> None:
        """Recovery may neither publish an open observer-less row nor invent a choice.

        It does not have to do either: the checkpoint carries the same durable lifecycle log the
        migration recovers a head from, so the head this sprint actually ran is recoverable by the
        migration's own rule and is written before the reference publishes the row.

        The installation still comes back tolerant — no completion event was written and the closed
        rows are still the cutover's work — but its one open row carries an executable value.
        """
        self._record_observer_launch("claude-observer")
        self._export()
        self._pre_migration_open_export()
        self._carry_audit_log()

        client, _ = self._restore()

        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["observer"], head_choice("claude-observer"))
        self.assertEqual(live["status"], "open")
        self.assertFalse(strict_reader_active(self.target_data))
        # Written with the fields, so the row was never readable open declaring nothing.
        order = [
            method for method, params in client.calls  # type: ignore[attr-defined]
            if (method == "saveTaskMetadata" and "sprint_observer" in dict(params["values"]))
            or (method == "updateTask" and params.get("reference") == self.ref)
        ]
        self.assertEqual(order[:2], ["saveTaskMetadata", "updateTask"])

    def test_a_pre_migration_open_row_with_nothing_to_recover_is_refused(self) -> None:
        """The narrow refusal: no successful launch anywhere in the log this checkpoint carries."""
        self._pre_migration_open_export()
        self._carry_audit_log()
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "no successful observer launch"):
            import_normalized_board(  # type: ignore[arg-type]
                self.target_data, client=client, instance=self.instance,
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

    def test_a_recovered_head_the_registry_no_longer_has_is_refused(self) -> None:
        self._record_observer_launch("retired-observer")
        self._export()
        self._pre_migration_open_export()
        self._carry_audit_log()

        with self.assertRaisesRegex(RestoreError, "not a profile of this installation"):
            self._restore()

    def test_a_declared_head_the_registry_no_longer_has_is_refused(self) -> None:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = {"kind": "head", "profile": "retired-observer"}
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "not a profile of this installation"):
            import_normalized_board(  # type: ignore[arg-type]
                self.target_data, client=client, instance=self.instance,
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

    def test_a_second_disaster_keeps_the_migrated_strict_state(self) -> None:
        """The checkpoint of a migrated installation recovers a migrated installation."""
        self._mark_migrated(self.target_data)
        first, _ = self._restore()
        export_board(self.target_data, command=self._pipeline_command(), sprint_client=first)
        second_data = self.root / "second-migrated"
        shutil.copytree(self.target_data, second_data)
        forget_migration_state(second_data)

        second = _EmptyBoardsKanboard()
        import_normalized_board(second_data, client=second)  # type: ignore[arg-type]

        live = SprintReader(second, data_dir=second_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["observer"], head_choice("codex-observer"))
        self.assertTrue(strict_reader_active(second_data))
        self.assertFalse((second_data / "sprints" / "observer-strict.json").exists())

    def test_an_open_row_is_refused_when_its_export_carries_provenance(self) -> None:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = {
            "kind": "historical", "profile": None, "source": "migration_unknown",
        }
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

        with self.assertRaisesRegex(RestoreError, "may not carry migration provenance"):
            self._restore()

    def _two_open_rows(self, **overrides: object) -> dict:
        """An export carrying the seeded row open, plus a second open row beside it."""
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        second = dict(payload["sprints"][0]) | {"reference": "sprint:collision"} | overrides
        payload["sprints"].append(second)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _set_open_sprint_limit(self, value: int) -> None:
        (self.instance / "instance.yaml").write_text(
            f"open_sprint_limit: {value}\n", encoding="utf-8",
        )

    def test_restore_refuses_an_export_of_open_sprints_admission_would_have_refused(self) -> None:
        """Restore reproduces rows one by one, so the set is judged once, before the first write.

        Otherwise an archive is the way around admission: two open sprints sharing a
        product, a reservation, a repository tree and an observer head would come back
        exactly as the rules refuse to create them.
        """
        self._two_open_rows()
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "not admissible"):
            import_normalized_board(
                self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]
        self.assertEqual(
            [method for method, _ in client.calls if method in {"createTask", "saveTaskMetadata"}],  # type: ignore[attr-defined]
            [],
        )

    def test_restore_at_the_pilot_limit_judges_the_open_set_by_the_same_rules(self) -> None:
        """Two open rows come back only when they satisfy every rule `create` enforces."""
        self._set_open_sprint_limit(2)
        for name, overrides, message in (
            (
                "product",
                {
                    "product": "secretary", "reservations": ["other"],
                    "repositories": [_root("other")], "observer": none_choice(),
                },
                "needs a different product",
            ),
            (
                "reservation",
                {"product": "other", "repositories": [_root("other")], "reservations": ["secretary"]},
                "already reserved by an open sprint",
            ),
            (
                "repository",
                {
                    "product": "other", "reservations": ["other"],
                    "repositories": [_root("secretary/nested")], "observer": none_choice(),
                },
                "overlaps",
            ),
            (
                "observer",
                {
                    "product": "other", "reservations": ["other"], "repositories": [_root("other")],
                    "observer": head_choice("codex-observer"),
                },
                "one-observer ceiling",
            ),
        ):
            with self.subTest(collision=name):
                self.setUp()
                self._set_open_sprint_limit(2)
                self._two_open_rows(**overrides)
                client = _EmptyBoardsKanboard()

                with self.assertRaisesRegex(RestoreError, message):
                    import_normalized_board(
                        self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
                    )

                self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

        self.setUp()
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other", reservations=["other"], repositories=[_root("other")],
            observer=none_choice(),
        )
        client, _ = self._restore()

        reader = SprintReader(client, data_dir=self.target_data)  # type: ignore[arg-type]
        self.assertEqual(
            sorted(sprint["ref"] for sprint in reader.list(statuses={"open"})),
            ["sprint:collision", "sprint:entity"],
        )

    def test_restore_refuses_an_open_row_whose_root_is_not_canonical(self) -> None:
        """An archive is not a way to publish an open row admission could not judge.

        A relative root names a different tree from every process that reads it, so the
        set check refuses it rather than resolving it against the working directory
        recovery happens to run in.
        """
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other", reservations=["other"], repositories=["../elsewhere"],
            observer=none_choice(),
        )
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(
            RestoreError,
            "repository root '../elsewhere', which is not an absolute path",
        ):
            import_normalized_board(
                self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

    def test_restore_refuses_a_lone_open_row_whose_root_is_not_canonical(self) -> None:
        """One open row is the reachable shape: pre-fix creates could only make one.

        An export of an installation that ran before roots were canonicalized carries a
        single open sprint declaring `.`.  Judging it only against the other open sprints
        would inspect nothing at all here, and recovery would publish the row.
        """
        self._set_open_sprint_limit(2)
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["repositories"] = ["."]
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(
            RestoreError, "repository root '.', which is not an absolute path",
        ):
            import_normalized_board(
                self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

    def _legacy_open_row_beside_the_seeded_one(self) -> None:
        """The seeded row open, plus an open row from before sprints owned a product.

        The legacy reference sorts after the seeded one, so it is the candidate the set
        check judges second: the order that used to let it through.
        """
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        payload["sprints"][0]["observer"] = none_choice()
        legacy = dict(payload["sprints"][0]) | {
            "reference": "sprint:z-legacy", "repositories": ["separate-repository"],
        }
        for field in ("product", "issues", "reservations"):
            legacy.pop(field, None)
        payload["sprints"].append(legacy)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_restore_refuses_a_second_open_row_that_declares_no_product(self) -> None:
        """Ownership absence is refused on whichever side of the comparison it lands.

        A pre-ownership row is a valid export, and nothing proves it disjoint from the
        sprint beside it. Judging only the already-admitted side made the answer depend
        on which reference sorted first, so the same pair was refused one way round and
        restored the other.
        """
        self._set_open_sprint_limit(2)
        self._legacy_open_row_beside_the_seeded_one()
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "sprint:z-legacy: this sprint declares no product"):
            import_normalized_board(
                self.target_data, client=client, instance=self.instance,  # type: ignore[arg-type]
            )

        self.assertEqual(client.tasks, [])  # type: ignore[attr-defined]

    def test_restore_holds_the_admission_lock_from_its_check_to_its_write(self) -> None:
        """Recovery publishes open sprints, so it admits a set and must serialize like one.

        A `create` that read the board between restore's check and its write would see no
        restored sprint, admit itself, and leave the installation holding both.
        """
        self._set_open_sprint_limit(2)
        self._two_open_rows(
            product="other", reservations=["other"], repositories=[_root("other")],
            observer=none_choice(),
        )
        blocked: list[str] = []
        contenders: list[threading.Thread] = []

        def contender_blocked() -> bool:
            """Whether a second admission on this data dir has to wait for the restore."""
            entered = threading.Event()

            def acquire() -> None:
                with sprint_admission_lock(self.target_data):
                    entered.set()

            thread = threading.Thread(target=acquire, daemon=True)
            contenders.append(thread)
            thread.start()
            self.addCleanup(thread.join, 5)
            return not entered.wait(0.2)

        import secretary.restore as restore_module

        check = restore_module._check_restored_admission
        publish = restore_module._import_sprints

        def checked(*args: object, **kwargs: object) -> object:
            blocked.append("check:%s" % contender_blocked())
            return check(*args, **kwargs)  # type: ignore[arg-type]

        def published(*args: object, **kwargs: object) -> object:
            blocked.append("publish:%s" % contender_blocked())
            return publish(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(restore_module, "_check_restored_admission", checked), \
                mock.patch.object(restore_module, "_import_sprints", published):
            client, _ = self._restore()

        self.assertEqual(blocked, ["check:True", "publish:True"])
        reader = SprintReader(client, data_dir=self.target_data)  # type: ignore[arg-type]
        self.assertEqual(
            sorted(sprint["ref"] for sprint in reader.list(statuses={"open"})),
            ["sprint:collision", "sprint:entity"],
        )
        # And it is released again: the installation is not left unable to admit anything.
        for thread in list(contenders):
            thread.join(5)
        self.assertFalse(contender_blocked())

    def test_an_open_row_is_never_published_without_its_observer(self) -> None:
        """Status and observer are on the row before the reference makes it readable."""
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "open"
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        client, _ = self._restore()

        order = [
            method for method, params in client.calls  # type: ignore[attr-defined]
            if (method == "saveTaskMetadata" and "sprint_observer" in dict(params["values"]))
            or (method == "updateTask" and params.get("reference") == self.ref)
        ]
        self.assertEqual(order[:2], ["saveTaskMetadata", "updateTask"])

    def test_checkpoint_without_sprint_ownership_restores_as_it_was(self) -> None:
        """A sprint closed before a sprint owned a Product keeps every path working.

        The export of such an entity has no product, issues or reservations at all, and
        recovery must neither refuse it nor invent the fields on the restored row.
        """
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        legacy = payload["sprints"][0]
        for field in ("product", "issues", "reservations"):
            legacy.pop(field)
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps({"version": 1, "sprints": [legacy]}), encoding="utf-8"
        )

        client, _ = self._restore()

        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        # Neither the row, nor the view of it, nor the next checkpoint of it gains a
        # field the entity never had.
        for field in ("product", "issues", "reservations"):
            self.assertNotIn(field, live)
            self.assertNotIn(field, normalize_sprint_entity(live))
        self.assertEqual(live["goal"], legacy["goal"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")

        board = ensure_sprint_board(client)  # type: ignore[arg-type]
        row = next(task for task in client.tasks if task["project_id"] == board)  # type: ignore[attr-defined]
        self.assertEqual(
            sorted(key for key in client.metadata[row["id"]] if key.startswith("sprint_")),  # type: ignore[attr-defined]
            [
                "sprint_budget", "sprint_current_task", "sprint_definition_of_done", "sprint_goal",
                "sprint_observer", "sprint_repositories", "sprint_resume", "sprint_source_audit",
                "sprint_status",
            ],
        )
        # Its own export is stable: a second checkpoint of the restored entity still
        # carries no ownership, so the next recovery compares equal again.
        export_board(self.target_data, command=self._pipeline_command(), sprint_client=client)
        again = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        for field in ("product", "issues", "reservations"):
            self.assertNotIn(field, again["sprints"][0])

    def test_ownership_a_legacy_entity_never_had_fails_the_parity_gate(self) -> None:
        """Parity compares whether a field is there, not only what it holds.

        A target that writes `sprint_product=""` for an entity whose export carries no
        product is a lossy metadata write. Reading both sides back as `""` would let it
        through, and the legacy entity would silently gain fields nobody wrote.
        """
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        legacy = payload["sprints"][0]
        for field in ("product", "issues", "reservations"):
            legacy.pop(field)
        (self.target_data / "board" / "sprints.json").write_text(
            json.dumps({"version": 1, "sprints": [legacy]}), encoding="utf-8"
        )
        client = _EmptyBoardsKanboard()
        original = client.call

        def gains_empty_ownership(method: str, **params: object) -> object:
            if method == "saveTaskMetadata" and "sprint_source_audit" in dict(params["values"]):  # type: ignore[arg-type]
                values = dict(params["values"]) | {  # type: ignore[arg-type]
                    "sprint_product": "", "sprint_issues": "[]", "sprint_reservations": "[]",
                }
                return original(method, task_id=params["task_id"], values=values)
            return original(method, **params)

        with mock.patch.object(client, "call", side_effect=gains_empty_ownership):
            with self.assertRaisesRegex(RestoreError, "sprint parity check failed"):
                import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        state = restore_state(self.target_data)
        self.assertEqual(state["sprint_parity"], "failed")
        self.assertEqual(state["sprints"], "failed")

    def test_pipeline_cards_still_restore_alongside_the_entities(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        restored = [task for task in client.tasks if task["reference"] == "secretary-12"]  # type: ignore[attr-defined]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restore_state(self.target_data)["board_parity"], "complete")

    def test_repeated_restore_creates_one_entity_and_no_duplicate_records(self) -> None:
        client, _ = self._restore()
        namespace = restore_state(self.target_data)["restore_namespace"]

        # The retry meets the durable audit its own first pass wrote, and stays on the
        # same namespace: the entities that audit names are the ones this backend holds.
        self._restore(client)

        self.assertEqual(restore_state(self.target_data)["restore_namespace"], namespace)
        sprint_board = ensure_sprint_board(client)  # type: ignore[arg-type]
        entities = [task for task in client.tasks if task["project_id"] == sprint_board]  # type: ignore[attr-defined]
        self.assertEqual([task["reference"] for task in entities], [self.ref])
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in self._exported_sprint()["comments"]],
        )

    def test_second_disaster_restores_from_the_checkpoint_of_the_first_recovery(self) -> None:
        """A recovered instance is itself recoverable.

        The restore audit is durable, so the next checkpoint ships it as canon. Meeting
        those request ids again on a backend that holds nothing used to short-circuit
        every write as already committed and fail reading the entity nobody created.
        """
        first, _ = self._restore()
        # The checkpoint of the recovered instance: its own export, its own audit.
        export_board(self.target_data, command=self._pipeline_command(), sprint_client=first)
        second_data = self.root / "second-data"
        shutil.copytree(self.target_data, second_data)

        second = _EmptyBoardsKanboard()
        self.assertEqual(import_normalized_board(second_data, client=second), 1)  # type: ignore[arg-type]

        self.assertNotEqual(
            restore_state(second_data)["restore_namespace"],
            restore_state(self.target_data)["restore_namespace"],
        )
        live = SprintReader(second, data_dir=second_data).show(self.ref)  # type: ignore[arg-type]
        exported = self._exported_sprint()
        self.assertEqual(live["goal"], exported["goal"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(live["repositories"], exported["repositories"])
        self.assertEqual(live["budget"]["by_type"], exported["budget"]["by_type"])
        self.assertEqual(live["current_task"], exported["current_task"])
        self.assertEqual(live["resume"], exported["resume"])
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in exported["comments"]],
        )
        self.assertEqual(normalize_sprint_entity(live), exported)
        self.assertEqual(restore_state(second_data)["sprint_parity"], "complete")
        self.assertEqual(
            [task["reference"] for task in second.tasks if task["reference"] == self.ref], [self.ref]  # type: ignore[attr-defined]
        )

    def test_parity_failure_leaves_recovery_incomplete_with_a_named_error(self) -> None:
        client = _EmptyBoardsKanboard()
        original = client.call

        def lossy(method: str, **params: object) -> object:
            # Only the rewrite of the exported fields is lossy: the create of the row
            # verifies its own metadata and would refuse before parity is ever reached.
            # The dropped field is the reservations rather than the status, because
            # status and observer now land with the create, so that the row is never
            # published in a shape nobody chose. Ownership is written only here, so it is
            # what a lossy rewrite can still lose.
            if method == "saveTaskMetadata" and "sprint_source_audit" in dict(params["values"]):  # type: ignore[arg-type]
                values = {k: v for k, v in dict(params["values"]).items() if k != "sprint_reservations"}  # type: ignore[arg-type]
                return original(method, task_id=params["task_id"], values=values)
            return original(method, **params)

        with mock.patch.object(client, "call", side_effect=lossy):
            with self.assertRaisesRegex(RestoreError, "sprint parity check failed"):
                import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        state = restore_state(self.target_data)
        self.assertEqual(state["sprint_parity"], "failed")
        self.assertEqual(state["sprints"], "failed")
        self.assertIn("sprint restore parity failed", restore_findings(self.target_data))

    def test_recovery_interrupted_before_the_sprint_step_reports_it_unfinished(self) -> None:
        # Doctor treats a restore state with no sprint key as one that predates sprint
        # entities. A recovery that started under this build records the step from the
        # first live write, so an interruption cannot be read as nothing left to do.
        with mock.patch("secretary.restore._import_sprints", side_effect=RestoreError("stopped")):
            with self.assertRaisesRegex(RestoreError, "stopped"):
                import_normalized_board(self.target_data, client=_EmptyBoardsKanboard())  # type: ignore[arg-type]

        self.assertEqual(restore_state(self.target_data)["sprints"], "pending")
        self.assertIn("sprint restore is incomplete", restore_findings(self.target_data))

    def test_foreign_sprint_on_the_target_board_stops_the_restore(self) -> None:
        (self.target_data / "board" / "cards.json").write_text(
            json.dumps({"version": 1, "cards": []}), encoding="utf-8"
        )
        client = _EmptyBoardsKanboard()
        SprintWriter(client, data_dir=self.target_data).restore_create(  # type: ignore[arg-type]
            goal="someone else's sprint", reference="sprint:foreign", request_id="foreign",
        )

        with self.assertRaisesRegex(RestoreError, "sprint board is not empty"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

    def test_invalid_sprint_export_is_refused_before_any_live_write(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "archived"
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "invalid status"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        self.assertEqual(client.tasks, [])

    def test_export_without_a_sprint_board_leaves_the_target_untouched(self) -> None:
        (self.target_data / "board" / "sprints.json").unlink()
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        self.assertFalse(
            any(
                method == "createProject" and params.get("name") == "Secretary sprints"
                for method, params in client.calls  # type: ignore[attr-defined]
            )
        )
        self.assertEqual(restore_state(self.target_data)["sprint_count"], 0)


if __name__ == "__main__":
    unittest.main()
