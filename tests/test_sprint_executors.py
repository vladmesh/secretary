"""A sprint's optional worker and reviewer pins: three states, and the one that is absence.

The observer's value is required and always there. These two are not, and the whole point of the
contract is that an absent field is a decision — the owner pinned nobody, and the sprint's observer
chooses per card — rather than a gap somebody fills with `role_defaults` further down the read path.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.data import export_board, init_layout, normalize_sprint_entity
from secretary.dispatcher_observer import render_observer_prompt
from secretary.restore import RestoreError, import_normalized_board
from secretary.sprint_observer import (
    REVIEWER_FIELD,
    WORKER_FIELD,
    executor_malformed,
    executor_pinned,
    executor_unset,
    head_choice,
    parse_executor,
)
from secretary.sprints import SprintReader, SprintWriter
from secretary.tasks import TaskAudit, TaskError, TaskWriter
from tests.fakes.sprints import (
    KEEP_THE_ISSUE_OPEN,
    ProductSprintKanboard,
    SprintFixture,
    _write_project_registry,
)
from tests.observer_identity import as_observer
from tests.restore_fixtures import _EmptyBoardsKanboard
from tests.sprint_close_fixtures import close_decisions

UNSET_BOTH = {"worker": executor_unset(), "reviewer": executor_unset()}


class ExecutorValueTests(unittest.TestCase):
    """The stored value itself, before any sprint carries one."""

    def test_a_profile_name_is_the_only_pinned_form(self) -> None:
        self.assertEqual(parse_executor("codex-observer"), executor_pinned("codex-observer"))

    def test_nothing_else_is_read_as_pinning_nothing(self) -> None:
        """A field somebody wrote badly is corruption, never the absent state.

        Reading `""` or `none` as "unset" would let a damaged row present itself as a sprint that
        deliberately left its observer free, which is the one confusion this value exists to prevent.
        """
        for stored in ("", " ", "none", " codex-observer", "codex-observer ", None, 7, {"profile": "x"}):
            with self.subTest(stored=stored):
                self.assertEqual(parse_executor(stored), executor_malformed())


class SprintExecutorPinTests(SprintFixture):
    """What a sprint entity carries, and what it refuses to carry."""

    def _row_metadata(self, reference: str) -> dict:
        row = next(row for row in self._sprint_rows() if row["reference"] == reference)
        return self.client.metadata[row["id"]]

    def _assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertEqual(self._sprint_rows(), [])

    def test_a_sprint_that_pins_neither_role_says_so_and_writes_no_field(self) -> None:
        created = self._create(goal="unpinned", reference="sprint:unpinned")
        self.assertEqual(created["sprint"]["executors"], UNSET_BOTH)

        stored = self._row_metadata("sprint:unpinned")
        self.assertNotIn(WORKER_FIELD, stored)
        self.assertNotIn(REVIEWER_FIELD, stored)
        # And the read is the same one the next process makes off the row.
        reader = SprintReader(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]
        self.assertEqual(reader.show("sprint:unpinned", include_cards=False)["executors"], UNSET_BOTH)

    def test_each_role_is_pinned_on_its_own(self) -> None:
        cases = {
            "sprint:worker-only": (
                {"worker": "codex-observer"},
                {"worker": executor_pinned("codex-observer"), "reviewer": executor_unset()},
            ),
            "sprint:reviewer-only": (
                {"reviewer": "claude-observer"},
                {"worker": executor_unset(), "reviewer": executor_pinned("claude-observer")},
            ),
            "sprint:both": (
                {"worker": "codex-observer", "reviewer": "claude-observer"},
                {
                    "worker": executor_pinned("codex-observer"),
                    "reviewer": executor_pinned("claude-observer"),
                },
            ),
        }
        for reference, (pins, expected) in cases.items():
            with self.subTest(reference=reference):
                created = self._create(goal=reference, reference=reference, **pins)
                self.assertEqual(created["sprint"]["executors"], expected)
                self.writer.close(
                    role="po",
                    actor="operator",
                    reference=reference,
                    decisions=KEEP_THE_ISSUE_OPEN,
                )

        stored = self._row_metadata("sprint:both")
        self.assertEqual(stored[WORKER_FIELD], "codex-observer")
        self.assertEqual(stored[REVIEWER_FIELD], "claude-observer")

    def test_a_profile_the_registry_does_not_have_is_refused_before_the_row(self) -> None:
        with self.assertRaises(TaskError) as raised:
            self._create(goal="unknown head", reference="sprint:unknown", worker="codex-worker")
        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("codex-worker", raised.exception.message)
        self.assertIn("head registry", raised.exception.message)
        self._assert_nothing_was_written()

    def test_a_registry_that_cannot_be_read_is_a_refusal_and_never_a_pass(self) -> None:
        """The same rule the declared observer has: a claim nobody could check is not admitted."""
        (self.instance / "heads" / "heads.yaml").write_text("::: not yaml", encoding="utf-8")
        with self.assertRaises(TaskError) as raised:
            self._create(goal="broken registry", reference="sprint:broken", reviewer="claude-observer")
        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("head registry", raised.exception.message)
        self._assert_nothing_was_written()

    def test_none_and_the_empty_string_are_refused_rather_than_read_as_absence(self) -> None:
        for role, spelling in (
            ("worker", "none"),
            ("reviewer", "none"),
            ("worker", ""),
            ("reviewer", "   "),
        ):
            with self.subTest(role=role, spelling=spelling):
                with self.assertRaises(TaskError) as raised:
                    self._create(goal="no such answer", reference="sprint:refused", **{role: spelling})
                self.assertEqual(raised.exception.code, "validation")
                self.assertIn(role, raised.exception.message)
                self._assert_nothing_was_written()

    def test_status_carries_the_same_two_states_beside_the_observer(self) -> None:
        reference = self._create(goal="status", reference="sprint:status", worker="codex-observer")["sprint"][
            "ref"
        ]
        status = SprintReader(self.client, data_dir=self.tmp.name).status(  # type: ignore[arg-type]
            reference
        )
        self.assertEqual(
            status["executors"],
            {"worker": executor_pinned("codex-observer"), "reviewer": executor_unset()},
        )

    def test_the_cli_takes_both_pins_and_takes_neither(self) -> None:
        def create(reference: str, *pins: str) -> dict:
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch("secretary.sprint_commands.KanboardClient.for_instance", return_value=self.client),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(
                    [
                        "sprint",
                        "create",
                        "--role",
                        "po",
                        "--data-dir",
                        self.tmp.name,
                        "--instance",
                        str(self.instance),
                        "--goal",
                        reference,
                        "--product",
                        "secretary",
                        "--issue",
                        "issue:open",
                        "--project",
                        "secretary",
                        "--ref",
                        reference,
                        "--request-id",
                        f"cli-{reference}",
                        "--observer",
                        "codex-observer",
                        *pins,
                    ]
                )
            self.assertEqual((code, errors.getvalue()), (0, ""))
            return json.loads(output.getvalue())

        # The call every existing script makes, unchanged and still unconstrained.
        self.assertEqual(create("sprint:cli-plain")["sprint"]["executors"], UNSET_BOTH)
        self.writer.close(
            role="po",
            actor="operator",
            reference="sprint:cli-plain",
            decisions=KEEP_THE_ISSUE_OPEN,
        )
        pinned = create("sprint:cli-pinned", "--worker", "codex-observer", "--reviewer", "claude-observer")
        self.assertEqual(
            pinned["sprint"]["executors"],
            {"worker": executor_pinned("codex-observer"), "reviewer": executor_pinned("claude-observer")},
        )


class ObserverPromptExecutorTests(unittest.TestCase):
    """What the launch document tells the observer about who runs the cards."""

    def test_an_unpinned_role_reads_as_the_observer_s_choice(self) -> None:
        document = render_observer_prompt({"ref": "sprint:1", "executors": UNSET_BOTH})
        self.assertIn("## Executors", document)
        for role in ("worker", "reviewer"):
            self.assertIn(f"- {role}: not pinned.", document)
            self.assertIn(f"fixed no {role} profile", document)
            # What the section must never license: a sprint whose cards run without that role.
            self.assertIn(f"not a sprint that runs without a {role}", document)

    def test_a_pinned_role_names_the_profile_every_card_runs_on(self) -> None:
        document = render_observer_prompt(
            {
                "ref": "sprint:1",
                "executors": {
                    "worker": executor_pinned("codex-observer"),
                    "reviewer": executor_unset(),
                },
            }
        )
        self.assertIn("- worker: pinned to head profile `codex-observer`.", document)
        self.assertIn("- reviewer: not pinned.", document)

    def test_a_sprint_dict_that_carries_no_executors_still_prints_both_roles(self) -> None:
        """A caller holding an older shape gets the honest reading of it, not a crash."""
        document = render_observer_prompt({"ref": "sprint:1"})
        self.assertIn("- worker: not pinned.", document)
        self.assertIn("- reviewer: not pinned.", document)


class SprintCardExecutorTests(SprintFixture):
    """Cutting a card under a pin, and cutting one where there is none."""

    def _tasks(self) -> TaskWriter:
        return TaskWriter(self.client, data_dir=self.tmp.name)  # type: ignore[arg-type]

    def _card(self, sprint: str, request_id: str, **kwargs) -> dict:
        return self._tasks().create(
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="work",
            target="ready",
            sprint=sprint,
            request_id=request_id,
            **kwargs,
        )["task"]

    def test_a_card_of_a_pinned_sprint_runs_the_pinned_profiles(self) -> None:
        reference = self._create(goal="pinned", worker="codex-observer", reviewer="claude-observer")[
            "sprint"
        ]["ref"]

        # Naming the pinned profile is accepted, and so is naming nothing: the pin is what the
        # card is written with either way, so it stays readable on the card as it always was.
        stated = self._card(reference, "stated", head="codex-observer", review_head="claude-observer")
        self.assertEqual(stated["routing"]["head_override"], "codex-observer")
        self.assertEqual(stated["routing"]["review_head_override"], "claude-observer")

        silent = self._card(reference, "silent")
        self.assertEqual(silent["routing"]["head_override"], "codex-observer")
        self.assertEqual(silent["routing"]["review_head_override"], "claude-observer")

    def test_a_card_asking_for_another_profile_is_refused_by_name(self) -> None:
        reference = self._create(goal="pinned", worker="codex-observer")["sprint"]["ref"]
        before = len(self._tasks().reader.list(sprint=reference))

        with self.assertRaises(TaskError) as raised:
            self._card(reference, "other-worker", head="claude-observer")
        self.assertEqual(raised.exception.code, "sprint_executor_pinned")
        self.assertIn(reference, raised.exception.message)
        self.assertIn("codex-observer", raised.exception.message)
        self.assertIn("claude-observer", raised.exception.message)
        self.assertEqual(len(self._tasks().reader.list(sprint=reference)), before)

        # A recreation after a rework is the same create and is held to the same pin.
        seeded = self._card(reference, "seed")
        with self.assertRaises(TaskError) as recreated:
            self._card(
                reference,
                "reslice",
                head="claude-observer",
                seed_ref=seeded["ref"],
                supersedes=seeded["ref"],
            )
        self.assertEqual(recreated.exception.code, "sprint_executor_pinned")

    def test_the_reviewer_pin_is_held_on_its_own(self) -> None:
        reference = self._create(goal="reviewer pinned", reviewer="claude-observer")["sprint"]["ref"]

        # The unpinned role stays free, including the profile the other role is pinned to.
        card = self._card(reference, "free-worker", head="codex-observer")
        self.assertEqual(card["routing"]["head_override"], "codex-observer")
        self.assertEqual(card["routing"]["review_head_override"], "claude-observer")

        with self.assertRaises(TaskError) as raised:
            self._card(reference, "other-reviewer", review_head="codex-observer")
        self.assertEqual(raised.exception.code, "sprint_executor_pinned")
        self.assertIn("reviewer", raised.exception.message)

    def test_a_sprint_that_pins_nothing_adds_no_check(self) -> None:
        reference = self._create(goal="unpinned")["sprint"]["ref"]
        for index, (head, review) in enumerate(
            (("codex-observer", "claude-observer"), ("claude-observer", "codex-observer"), ("", ""))
        ):
            with self.subTest(head=head, review=review):
                card = self._card(reference, f"free-{index}", head=head, review_head=review)
                self.assertEqual(card["routing"]["head_override"], head or None)
                self.assertEqual(card["routing"]["review_head_override"], review or None)

    def test_a_pin_that_cannot_be_read_stops_the_card_instead_of_being_ignored(self) -> None:
        reference = self._create(goal="corrupt pin")["sprint"]["ref"]
        row = next(row for row in self._sprint_rows() if row["reference"] == reference)
        self.client.metadata[row["id"]][WORKER_FIELD] = "  "

        with self.assertRaises(TaskError) as raised:
            self._card(reference, "corrupt")
        self.assertEqual(raised.exception.code, "sprint_executor_unreadable")
        self.assertIn(reference, raised.exception.message)
        self.assertEqual(
            [event["kind"] for event in TaskAudit(self.tmp.name).events(reference=reference)],
            ["created"],
        )


class SprintExecutorRecoveryTests(unittest.TestCase):
    """The pins through the entity's own recovery path: export, parity, restore.

    A durable field that a checkpoint drops is not durable. The window this closes is the one
    between the export and the restore, where a pin the owner set came back as "the observer
    chooses" — the substitution the whole contract is written against.
    """

    def _round_trip(self, **pins: str) -> tuple[dict, dict, Path, Path]:
        """Seed one closed sprint with these pins, export it, restore it into an empty backend."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source_data, target_data = root / "source-data", root / "target-data"
        init_layout(source_data)
        init_layout(target_data)
        source = ProductSprintKanboard()
        instance = _write_project_registry(root, "secretary")
        writer = SprintWriter(source, data_dir=source_data, instance=instance)  # type: ignore[arg-type]
        reference = writer.create(
            role="po",
            actor="operator",
            goal="recovered",
            reference="sprint:recovered",
            product="secretary",
            issues=["issue:open"],
            projects=["secretary"],
            observer=head_choice("codex-observer"),
            request_id="seed-create",
            **pins,
        )["sprint"]["ref"]
        with as_observer(reference):
            TaskWriter(source, data_dir=source_data).create(  # type: ignore[arg-type]
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="linked",
                target="ready",
                sprint=reference,
                request_id="seed-card",
            )
        writer.close(
            role="po",
            actor="operator",
            reference=reference,
            request_id="seed-close",
            decisions=close_decisions(writer, reference),
        )
        export_board(
            source_data,
            instance_dir=instance,
            reader=mock.Mock(export=mock.Mock(return_value=[])),
            sprint_client=source,
        )
        for name in ("cards.json", "sprints.json"):
            shutil.copy(source_data / "board" / name, target_data / "board" / name)
        exported = json.loads((target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        client = _EmptyBoardsKanboard()
        import_normalized_board(target_data, client=client, instance=instance)  # type: ignore[arg-type]
        restored = SprintReader(client, data_dir=target_data).show(reference)  # type: ignore[arg-type]
        return exported["sprints"][0], restored, target_data, instance

    def test_absence_comes_back_as_absence(self) -> None:
        record, restored, _, _ = self._round_trip()
        # The record of a sprint that pins nobody carries neither key, so an export taken before
        # these fields existed and one taken after are the same bytes.
        self.assertNotIn("worker", record)
        self.assertNotIn("reviewer", record)
        self.assertEqual(restored["executors"], UNSET_BOTH)

    def test_each_pin_comes_back_as_the_profile_it_was(self) -> None:
        for pins, expected in (
            ({"worker": "codex-observer"}, {"worker": "codex-observer"}),
            ({"reviewer": "claude-observer"}, {"reviewer": "claude-observer"}),
            (
                {"worker": "codex-observer", "reviewer": "claude-observer"},
                {"worker": "codex-observer", "reviewer": "claude-observer"},
            ),
        ):
            with self.subTest(pins=pins):
                record, restored, _, _ = self._round_trip(**pins)
                self.assertEqual({key: record[key] for key in expected}, expected)
                for role in ("worker", "reviewer"):
                    self.assertEqual(
                        restored["executors"][role],
                        executor_pinned(expected[role]) if role in expected else executor_unset(),
                    )
                # The recovered row is one the guard still holds: its cards keep the pin.
                self.assertNotIn(
                    "unset",
                    json.dumps({role: restored["executors"][role] for role in expected}),
                )

    def test_the_recovered_row_normalizes_back_to_the_record_it_came_from(self) -> None:
        """Parity is what makes the restore report success, so it has to compare the pins too."""
        record, restored, _, _ = self._round_trip(worker="codex-observer")
        self.assertEqual(normalize_sprint_entity(restored), record)

    def test_a_pin_that_is_not_a_profile_stops_the_restore_before_the_first_write(self) -> None:
        """Recovering corruption as "the owner pinned nobody" is the one repair nobody asked for."""
        record, _, target_data, instance = self._round_trip(worker="codex-observer")
        payload = {"version": 1, "sprints": [{**record, "worker": ""}]}
        (target_data / "board" / "sprints.json").write_text(json.dumps(payload), encoding="utf-8")
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "worker pin is not a head profile name"):
            import_normalized_board(target_data, client=client, instance=instance)  # type: ignore[arg-type]
        # Nothing of either set was written: the refusal is the preflight, not the sprint step.
        self.assertEqual(client.tasks, [])


class CardEditExecutorTests(SprintFixture):
    """`task edit` is the second door onto the card's two profiles, and the same guard holds it."""

    def _card(self, sprint: str, request_id: str, **kwargs) -> dict:
        return TaskWriter(self.client, data_dir=self.tmp.name).create(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            project="secretary",
            task_type="code",
            title="work",
            target="ready",
            sprint=sprint,
            request_id=request_id,
            **kwargs,
        )["task"]

    def _edit(self, reference: str, request_id: str, **kwargs) -> dict:
        return TaskWriter(self.client, data_dir=self.tmp.name).edit(  # type: ignore[arg-type]
            role="observer",
            actor="observer",
            reference=reference,
            request_id=request_id,
            **kwargs,
        )["task"]

    def test_an_edit_may_restate_the_pin_and_may_not_replace_it(self) -> None:
        reference = self._create(goal="pinned", worker="codex-observer")["sprint"]["ref"]
        card = self._card(reference, "card")["ref"]

        edited = self._edit(card, "same", head="codex-observer")
        self.assertEqual(edited["routing"]["head_override"], "codex-observer")

        with self.assertRaises(TaskError) as raised:
            self._edit(card, "other", head="claude-observer")
        self.assertEqual(raised.exception.code, "sprint_executor_pinned")
        self.assertIn(reference, raised.exception.message)
        self.assertIn("codex-observer", raised.exception.message)
        self.assertEqual(
            self._edit(card, "title-only", title="renamed")["routing"]["head_override"],
            "codex-observer",
        )

    def test_an_edit_cannot_clear_the_pin_off_the_card(self) -> None:
        """Clearing the override would hand the card back to `role_defaults`, pin and all."""
        reference = self._create(goal="pinned", reviewer="claude-observer")["sprint"]["ref"]
        card = self._card(reference, "card")["ref"]

        edited = self._edit(card, "cleared", review_head="")
        self.assertEqual(edited["routing"]["review_head_override"], "claude-observer")

    def test_an_unpinned_sprint_edits_exactly_as_before(self) -> None:
        reference = self._create(goal="unpinned")["sprint"]["ref"]
        card = self._card(reference, "card", head="codex-observer")["ref"]

        edited = self._edit(card, "free", head="claude-observer", review_head="codex-observer")
        self.assertEqual(edited["routing"]["head_override"], "claude-observer")
        self.assertEqual(edited["routing"]["review_head_override"], "codex-observer")
        self.assertIsNone(self._edit(card, "cleared", head="")["routing"]["head_override"])


if __name__ == "__main__":
    unittest.main()
