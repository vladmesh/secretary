from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from secretary import tasks
from secretary.board.card_transitions import CARD_TRANSITIONS
from secretary.board.done_retention import close_old_done
from secretary.board.host import TransitionRequest
from secretary.board.kanboard import KanboardBoardHost
from secretary.board.models import Actor, CardState, EntityKind, Event, RelatedRefs
from secretary.board.sql_audit import SqlTaskAudit
from secretary.board.steward_reports import StewardReportBoard
from secretary.board.transitions import TRANSITIONS, transition_for
from secretary.board_transport import BoardTransport
from secretary.cli import main
from secretary.data import export_board, init_layout
from secretary.dispatch.state import claim_mismatch
from secretary.restore import import_normalized_board
from secretary.routing_journal import (
    HeadRun,
    attempts,
    head_run_from_profile,
    routing_head_snapshot_from_launch,
    routing_payload,
)
from secretary.sprints import refresh_active_sprint_projects
from secretary.tasks import (
    _STATE_BY_COLUMN,
    ArtifactOwnershipTaskError,
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    is_significant_observer_event,
    specification_revision,
    standing_decision,
)
from tests.fakes.sprints import SprintKanboard
from tests.fakes.tasks import empty_seed, reader_seed, writer_seed
from tests.observer_identity import as_observer, bind_observer, unbound_observer
from tests.sql_backend_fixtures import CardStoreCase, ensure_sprint_row
from triggered_agents.runtime.head import HeadRun as LifecycleHeadRun
from triggered_agents.runtime.head import HeadSpec, TaskRef

CARD_STATES = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")


@contextlib.contextmanager
def open_sprint(ref: str = "sprint:test", project: str = "secretary"):
    """Stand in for the open sprint every Ready card needs.

    These tests are about the create and audit path; the sprint link is a precondition of a
    create on the board, and the guard behind it is covered in tests/test_sprints.py.

    The caller is bound to the same sprint, because the observer creating a card linked to it is
    that sprint's own head; an unbound caller is refused before the create path is reached.
    """
    sprint = {"ref": ref, "status": "open", "repositories": [project], "reservations": [project]}
    with mock.patch("secretary.sprints.SprintReader.show", return_value=sprint), as_observer(ref):
        yield ref


# The sprint the assessment fixture's card belongs to.
SPRINT = "sprint:1031"


class FakeSprintReader:
    """The sprint board as the task writer's reservation guard reads it: one open sprint."""

    def __init__(self, sprint: dict[str, object]) -> None:
        self.sprint = sprint

    def list(self, **kwargs: object) -> list[dict[str, object]]:
        return [self.sprint]

    def show(self, reference: str, **kwargs: object) -> dict[str, object]:
        if reference != self.sprint["ref"]:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.sprint


class BoardFixture:
    """The board as a fixture, in the vocabulary both backends answer.

    Every helper here speaks either the card client's own protocol — the one `TaskReader` and
    `TaskWriter` speak to Kanboard and to the store alike — or the reader's normalized card.
    None of them reaches into a fake's rows, its metadata map or its RPC log, which is what used
    to pin ninety writer cases to one backend (`KANBOARD_FIXTURE_ONLY` in
    tests/test_tasks_sql_backend.py).  A case built on these runs unchanged on both.

    `self.rpc` is the transport log, and it is the test's own rather than a fake's: it records
    what the product asked the *client interface* for.  That makes it truthful on either backend,
    but it does not make it the right thing to assert: **where the fact is an effect, assert the
    effect.**  A refused write is `assertBoardUnchanged`, a scrubbed comment is the comment the
    reader returns, an archive is the card reading as closed — each is stronger than a call count
    and backend-neutral by construction rather than by argument.  A count survives only where the
    claim *is* the absence of a call and no state distinguishes it; there are five such cases and
    each says so at the assertion.  Anything whose subject is Kanboard's wire behaviour — the
    batch log below, the order two writes were issued in — is named in `KANBOARD_ONLY`
    (tests/test_tasks_sql_backend.py).
    """

    #: Neither client selects a card by the project id; both take it because the RPC carries one.
    PROJECT_ID = 7

    #: The methods that change the board.  "Does not write" is asserted against these.
    WRITE_METHODS = (
        "createTask",
        "updateTask",
        "moveTaskPosition",
        "saveTaskMetadata",
        "createComment",
        "closeTask",
        "addSwimlane",
    )

    def record_board_calls(self) -> None:
        """Wrap the client's own entry point, so the log is the product's calls and not a fake's."""
        self.rpc: list[tuple[str, dict]] = []
        #: The client's own entry point, before the log wraps it.  The fixture's arranging calls
        #: go through this one, so `self.rpc` holds the product's transport and only that.
        self._board = self.client.call

        def call(method: str, /, **params: object) -> object:
            self.rpc.append((method, dict(params)))
            return self._board(method, **params)

        self.client.call = call  # type: ignore[method-assign]

        self.rpc_batches: list[list[tuple[str, dict]]] = []
        batched = self.client.call_batch

        def call_batch(calls):
            batch = [(method, dict(params)) for method, params in calls]
            self.rpc_batches.append(batch)
            return batched(batch)

        self.client.call_batch = call_batch  # type: ignore[method-assign]

    # --- transport ------------------------------------------------------------------

    def board_calls(self, method: str) -> list[dict]:
        return [params for name, params in self.rpc if name == method]

    def board_call_count(self, method: str) -> int:
        return len(self.board_calls(method))

    def board_batches(self) -> list[list[tuple[str, dict]]]:
        """The batched reads the product posted — a Kanboard-only observation.

        Both clients serve `call_batch`, so an assertion over this log *passes* on either backend,
        and that is exactly why it must not be read as parity.  A Kanboard batch is one JSON-RPC
        round trip and the economy is the point; `SqlCardClient.call_batch` is
        `[self.call(...) for ...]`, one batch because there is no round trip, so the same
        assertion proves nothing there.  Every case that uses this is named in `KANBOARD_ONLY`
        (tests/test_tasks_sql_backend.py).  What *is* portable is the other log, `self.rpc`:
        which methods of the client interface the product invoked, and how many times.
        """
        return self.rpc_batches

    def board_writes(self) -> list[str]:
        return [name for name, _params in self.rpc if name in self.WRITE_METHODS]

    @contextlib.contextmanager
    def _off_the_record(self):
        """A fixture read the transport log does not see.

        `self.rpc` answers "what did the *product* ask the client for", and a case that asserts
        it saw no call at all must not be defeated by the assertion's own read.
        """
        rpc, batches = self.rpc, self.rpc_batches
        self.rpc, self.rpc_batches = [], []
        try:
            yield
        finally:
            self.rpc, self.rpc_batches = rpc, batches

    # --- reading the card through the product's own read path ------------------------

    def board_snapshot(self) -> dict[str, dict]:
        """Every card the board holds, live and archived, as `TaskReader` returns it.

        The effect-shaped answer to "nothing was written": a refused operation leaves this
        equal to what it was, and any write at all — a column, a metadata key, a comment, a new
        card — changes it.  Stronger than counting the calls that did not happen, and true or
        false on either backend by construction.
        """
        with self._off_the_record():
            return self.board_read().restore_snapshot()

    def assertBoardUnchanged(self, before: dict[str, dict]) -> None:
        self.assertEqual(self.board_snapshot(), before)

    def board_read(self) -> TaskReader:
        raise NotImplementedError

    def card(self, reference: str) -> dict:
        return self.board_read().show(reference)

    def card_state(self, reference: str) -> str:
        return str(self.card(reference)["state"])

    def card_comments(self, reference: str) -> list[str]:
        return [str(comment["body"]) for comment in self.card(reference)["comments"]]

    def card_extension(self, reference: str, key: str) -> object:
        return (self.card(reference).get("extensions") or {}).get("kanboard", {}).get(key)

    def assertCardCarriesNoMetadata(self, reference: str) -> None:
        """Nothing was stamped on the card: the model's own metadata fields are all unset."""
        card = self.card(reference)
        self.assertEqual(card["project"], "")
        self.assertEqual(card["type"], "")
        self.assertIsNone(card["record_type"])
        self.assertIsNone(card["claim"]["worker"])
        self.assertIsNone(card["sprint"])
        # The swimlane is the board's own placement, not a metadata key anybody stamped.
        self.assertEqual(
            set((card.get("extensions") or {}).get("kanboard", {})) - {"swimlane"}, set()
        )

    def card_exists(self, reference: str) -> bool:
        try:
            self.card(reference)
        except TaskError:
            return False
        return True

    # --- arranging the board ---------------------------------------------------------

    def backend_row(self, reference: str) -> dict:
        row = self._board("getTaskByReference", project_id=self.PROJECT_ID, reference=reference)
        assert isinstance(row, dict), f"no card carries {reference}"
        return row

    def backend_id(self, reference: str) -> int:
        return int(self.backend_row(reference)["id"])

    def column_ids(self) -> dict[str, int]:
        """The column each state is on *this* board: the two backends number them differently."""
        return {
            state: int(column["id"])
            for column in self._board("getColumns", project_id=self.PROJECT_ID)
            if (state := _STATE_BY_COLUMN.get(str(column["title"])))
        }

    def lane_id(self, name: str) -> int:
        for lane in self._board("getActiveSwimlanes", project_id=self.PROJECT_ID):
            if str(lane["name"]) == name:
                return int(lane["id"])
        return 0

    def place_card(self, reference: str, state: str, position: int = 1) -> None:
        row = self.backend_row(reference)
        self._board(
            "moveTaskPosition",
            project_id=self.PROJECT_ID,
            task_id=int(row["id"]),
            column_id=self.column_ids()[state],
            position=position,
            swimlane_id=int(row.get("swimlane_id") or 0),
        )

    def set_card_metadata(self, reference: str, **values: object) -> None:
        self._board("saveTaskMetadata", task_id=self.backend_id(reference), values=dict(values))

    def clear_card_metadata(self, reference: str, *keys: str) -> None:
        """Both backends read an empty value as no value, which is what a removed key means."""
        self.set_card_metadata(reference, **{key: "" for key in keys})

    def add_card(
        self,
        *,
        reference: str,
        title: str,
        state: str = "issues",
        description: str = "",
        lane: str | None = "Secretary",
        archived: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> int:
        task_id = int(
            self._board(
                "createTask",
                project_id=self.PROJECT_ID,
                title=title,
                description=description,
                column_id=self.column_ids()[state],
                swimlane_id=self.lane_id(lane) if lane else 0,
                reference=reference,
            )
        )
        if metadata:
            self._board("saveTaskMetadata", task_id=task_id, values=dict(metadata))
        if archived:
            self._board("closeTask", task_id=task_id)
        return task_id

    def archive_card(self, reference: str) -> None:
        self._board("closeTask", task_id=self.backend_id(reference))

    @contextlib.contextmanager
    def open_sprint(self, ref: str = "sprint:test", project: str = "secretary"):
        """The open sprint every Ready card needs, and the row `tasks.sprint_ref` refers to (§3.3).

        The guard reads `SprintReader`, which the module-level helper mocks; the store makes
        `tasks.sprint_ref` a foreign key, so the row is there as well.
        """
        self.client.ensure_sprint(ref)
        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            open_sprint(ref, project) as sprint,
        ):
            yield sprint

    def add_comment(self, reference: str, body: str) -> None:
        self._board("createComment", task_id=self.backend_id(reference), content=body)

    def remove_card(self, reference: str) -> None:
        """Delete a card outright: the one verb the card protocol does not carry (`closeTask` archives)."""
        self.client.remove_card(reference)

    # --- injecting the faults a board can have ---------------------------------------

    @staticmethod
    def refusal(method: str) -> TaskError:
        return TaskError("backend_error", f"the board refused the {method} write", 1)

    @contextlib.contextmanager
    def board_refuses(self, method: str, error: TaskError | None = None):
        """The board rejects one method outright: nothing of it is applied."""
        served = self.client.call

        def call(name: str, /, **params: object) -> object:
            if name == method:
                # The attempt reached the board and was refused there, so the transport log
                # holds it exactly as it holds the calls that succeed.
                self.rpc.append((name, dict(params)))
                raise error or self.refusal(method)
            return served(name, **params)

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def board_refuses_once(self, method: str):
        """The board rejects the first call of one method and serves every later one."""
        served = self.client.call
        refused = False

        def call(name: str, /, **params: object) -> object:
            nonlocal refused
            if name == method and not refused:
                refused = True
                self.rpc.append((name, dict(params)))
                raise self.refusal(method)
            return served(name, **params)

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def board_loses_reply(self, method: str):
        """The write lands and its reply does not: the caller cannot know it happened."""
        served = self.client.call

        def call(name: str, /, **params: object) -> object:
            result = served(name, **params)
            if name == method:
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def board_drops_the_call_after(self, method: str):
        """The next round trip after `method` is lost, which says nothing about `method` itself."""
        served = self.client.call
        armed = False

        def call(name: str, /, **params: object) -> object:
            nonlocal armed
            if armed:
                armed = False
                raise TaskError("backend_unavailable", "the board is unavailable", 1)
            result = served(name, **params)
            if name == method:
                armed = True
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield

    @contextlib.contextmanager
    def board_moves_the_card_after(self, reference: str, state: str):
        """Another writer moves the card onward between the move and the read back."""
        served = self.client.call
        raced = False

        def call(name: str, /, **params: object) -> object:
            nonlocal raced
            result = served(name, **params)
            if name == "moveTaskPosition" and not raced:
                raced = True
                row = self._board(
                    "getTaskByReference", project_id=self.PROJECT_ID, reference=reference
                )
                self._board(
                    "moveTaskPosition",
                    project_id=self.PROJECT_ID,
                    task_id=int(row["id"]),
                    column_id=self.column_ids()[state],
                    position=1,
                    swimlane_id=int(row.get("swimlane_id") or 0),
                )
            return result

        with mock.patch.object(self.client, "call", side_effect=call):
            yield


class TaskReaderTests(BoardFixture, CardStoreCase):
    """The reader's contract, over a real card store seeded with ``reader_seed``."""

    def board_client(self):
        return self.card_store(reader_seed())

    def setUp(self) -> None:
        self.client = self.board_client()
        self.record_board_calls()
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]

    def board_read(self) -> TaskReader:
        return self.reader

    def test_list_normalizes_and_filters_deterministically(self) -> None:
        result = self.reader.list(states={"ready"}, project="secretary")

        self.assertEqual([task["ref"] for task in result], ["secretary-468"])
        task = result[0]
        self.assertEqual(task["claim"], {"worker": "codex-terra", "claimed_at": None})
        self.assertEqual(task["retry"], {"same": 2, "switched": 0, "heads": ["codex-terra", "claude-opus"]})
        self.assertEqual(task["routing"]["complexity"], "standard")
        self.assertEqual(task["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(task["extensions"]["kanboard"], {"steward_report": "1", "swimlane": "Secretary"})
        self.assertNotIn("comments", task)

    def test_list_names_the_card_identity_of_its_backend(self) -> None:
        """§9 of docs/BOARD_STORE.md: the identity is `tasks.board_key` (`task_postgres_<key>`)."""
        task = self.reader.list(states={"ready"}, project="secretary")[0]
        self.assertEqual(task["id"], "task_postgres_12")
        self.assertEqual(task["audit"]["backend"]["kind"], "postgres")

    def test_show_preserves_comments_and_legacy_defaults(self) -> None:
        task = self.reader.show("old-1")

        self.assertEqual(task["project"], "")
        self.assertEqual(task["type"], "")
        self.assertIsNone(task["blocked_by"])
        self.assertEqual(task["position"], 0)
        self.assertEqual(task["routing"]["family_preference"], "auto")
        self.assertEqual(task["comments"][0]["marker"], "report:done")
        self.assertEqual(task["comments"][0]["created_at"], "2024-07-03T09:47:00Z")

    def test_show_reports_missing_task(self) -> None:
        with self.assertRaisesRegex(TaskError, "not found") as raised:
            self.reader.show("missing")
        self.assertEqual(raised.exception.code, "not_found")


class TaskCliTests(CardStoreCase):
    def test_backend_error_never_echoes_credentials(self) -> None:
        """A transport failure is `backend_unavailable` and carries none of the token.

        The installation is named and is one this test built: unnamed, the command resolves
        `DEFAULT_INSTANCE` — `~/secretary-instance`, which on the appliance host is the *live*
        installation, whose transport and card backend this suite must never read (secretary-1622).
        The ambient `KANBOARD_*` are still exported while it runs, because "not a source of transport
        configuration" is part of what this case is about (secretary-1026); the credential that must
        not be echoed is the one in the transport file the named instance owns.
        """
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            transport = instance / "board-transport.env"
            transport.write_text(
                "KANBOARD_URL=https://board.invalid/token\n"
                "KANBOARD_API_USER=user\nKANBOARD_API_TOKEN=super-secret\n",
                encoding="utf-8",
            )
            transport.chmod(0o600)
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "KANBOARD_URL": "https://board.invalid/token",
                        "KANBOARD_API_USER": "user",
                        "KANBOARD_API_TOKEN": "super-secret",
                    },
                    clear=False,
                ),
                mock.patch("secretary.tasks.urllib.request.urlopen", side_effect=OSError("super-secret")),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(["task", "list", "--instance", str(instance)])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")
        self.assertNotIn("super-secret", errors.getvalue())

    def test_missing_runtime_configuration_is_json_error(self) -> None:
        # The instance is named explicitly and points at an empty directory. Clearing the
        # environment is not enough on its own: `DEFAULT_INSTANCE` is `Path.home()/secretary-instance`
        # resolved at import, so on the appliance host itself an unnamed run resolves the live
        # installation and reads the production board.
        output, errors = io.StringIO(), io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(["task", "show", "--ref", "secretary-468", "--instance", tmp])

        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

    def test_reads_are_bound_to_the_named_installation(self) -> None:
        """`task list`/`task show` take an instance like every write command does.

        They used to take none at all, so `_instance` fell through to the home default and no
        flag or variable could move them: a process bound to one installation read another's
        board. On the appliance host that other board is production (secretary-1026's class of
        accident, arriving through the home default rather than through ambient credentials).
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {}, clear=True):
                for argv in (
                    ["task", "list", "--instance", tmp],
                    ["task", "show", "--ref", "secretary-468", "--instance", tmp],
                ):
                    errors = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                        code = main(argv)
                    self.assertEqual(code, 1, argv)
                    self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

            # The variable is the same source the write commands already honour.
            with mock.patch.dict("os.environ", {"SECRETARY_INSTANCE": tmp}, clear=True):
                errors = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                    code = main(["task", "list"])

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "backend_unavailable")

    def test_create_passes_kind_review_and_live_impact_through(self) -> None:
        """secretary-1638: `--type infra`, `--review` and `--live-impact` reach the writer as given."""
        for extra, review, live_impact in (
            ([], "", False),
            (["--review", "skipped", "--live-impact"], "skipped", True),
        ):
            writer = mock.Mock()
            writer.return_value.create.return_value = {"action": "created"}
            with (
                tempfile.TemporaryDirectory() as tmp,
                mock.patch("secretary.task_commands.TaskWriter", writer),
                mock.patch("secretary.task_commands.card_client"),
                mock.patch.dict("os.environ", {}, clear=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = main(
                    ["task", "create", "--role", "po", "--instance", tmp, "--data-dir", tmp,
                     "--project", "secretary", "--type", "infra", "--title", "T", *extra]
                )
            self.assertEqual(code, 0)
            kwargs = writer.return_value.create.call_args.kwargs
            self.assertEqual(
                (kwargs["task_type"], kwargs["review"], kwargs["live_impact"]), ("infra", review, live_impact)
            )

    def test_show_renders_kind_review_and_live_impact(self) -> None:
        client = self.card_store(reader_seed())
        client.save_metadata(12, {"task_type": "research", "review": "skipped", "live_impact": "1"})
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("secretary.task_commands.card_client", return_value=client),
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(output),
        ):
            code = main(["task", "show", "--ref", "secretary-468", "--instance", tmp])
        self.assertEqual(code, 0)
        card = json.loads(output.getvalue())
        self.assertEqual((card["type"], card["review"], card["live_impact"]), ("research", "skipped", True))
        self.assertNotIn("review", card.get("extensions", {}).get("kanboard", {}))

    def test_create_rejects_codex_mode_for_non_codex_head_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "heads").mkdir()
            (root / "instance.yaml").write_text(
                "version: 1\nname: test\ndata_dir: /tmp/data\n", encoding="utf-8"
            )
            (root / "heads" / "heads.yaml").write_text(
                "profiles:\n  claude-opus:\n    adapter: claude\nrole_defaults:\n  new_card: claude-opus",
                encoding="utf-8",
            )
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(
                    [
                        "task",
                        "create",
                        "--role",
                        "po",
                        "--instance",
                        str(root),
                        "--project",
                        "secretary",
                        "--type",
                        "code",
                        "--title",
                        "T",
                        "--head",
                        "claude-opus",
                        "--codex-mode",
                        "tui",
                    ]
                )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("requires a Codex worker head", error["message"])

    def test_create_rejects_codex_mode_exec_before_the_registry_is_even_read(self) -> None:
        """`--codex-mode exec` names a launch shape the product removed.

        It is refused with that reason, before the instance registry is opened and long before any
        board call: the CLI must never accept a mode that would then have to be silently launched
        as something else. The instance path here does not exist, so reaching the registry read at
        all would fail with a different error.
        """
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "create",
                    "--role",
                    "po",
                    "--instance",
                    "/nonexistent-instance",
                    "--project",
                    "secretary",
                    "--type",
                    "code",
                    "--title",
                    "T",
                    "--codex-mode",
                    "exec",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("interactive TUI only", error["message"])

    def test_archive_cli_reads_reason_file_and_closes_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            reason = root / "reason.md"
            reason.write_text("backlog cleanup\n", encoding="utf-8")
            client = self.card_store(writer_seed(), instance_dir=tmp)
            client.save_metadata(12, claim="")
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch("secretary.task_commands.card_client", return_value=client),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                code = main(
                    [
                        "task",
                        "archive",
                        "--role",
                        "po",
                        "--ref",
                        "secretary-468",
                        "--data-dir",
                        str(data_dir),
                        "--reason-file",
                        str(reason),
                        "--request-id",
                        "archive-cli",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "archived")
        self.assertEqual(client.row(12)["is_active"], 0)


class KanboardClientTests(unittest.TestCase):
    def test_rpc_error_is_sanitized(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"error":{"message":"super-secret"}}'
        response.__enter__.return_value = response
        with mock.patch("secretary.tasks.urllib.request.urlopen", return_value=response):
            client = KanboardClient(
                BoardTransport("https://board.invalid", "user", "super-secret"),
                Path.cwd(),
            )
            with self.assertRaises(TaskError) as raised:
                client.call("getAllTasks", project_id=1)
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertNotIn("super-secret", raised.exception.message)


class TaskWriterTests(BoardFixture, CardStoreCase):
    """The writer's contract, over a real card store seeded with ``writer_seed``."""

    def board_client(self):
        return self.card_store(writer_seed(), instance_dir=self.tmpdir.name)

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.client = self.board_client()
        self.record_board_calls()
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def board_read(self) -> TaskReader:
        return self.writer.reader

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_forbidden_role_does_not_write(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.report(role="reviewer", actor="r", reference="secretary-468", kind="done", body="")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertBoardUnchanged(before)
        # The claim the effect cannot carry: the guard refused before the board was touched at
        # all, not even for a read.  There is no card state that distinguishes "read and
        # refused" from "refused without reading", so the absence of the call is the assertion.
        self.assertEqual(self.rpc, [])

    def test_stale_transition_does_not_write(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(role="po", actor="p", reference="secretary-468", target="ready", reason="")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertBoardUnchanged(before)

    def test_generic_create_keeps_in_progress_closed_to_steward_reports(self) -> None:
        self.assertNotIn("steward_report", inspect.signature(self.writer.create).parameters)
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "only a steward report") as raised:
            self.writer.create(
                role="steward",
                actor="dispatch",
                project="secretary",
                task_type="research",
                title="not an accounting artifact",
                target="in_progress",
                slug="not-a-report",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertBoardUnchanged(before)

    def test_steward_report_create_is_audited_directly_in_progress_and_replays(self) -> None:
        self.add_card(
            reference="secretary-700", title="Archived high-water mark", state="done", archived=True
        )

        result = self.writer.create_steward_report(
            actor="dispatch",
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120000",
            request_id="steward-report-create",
        )

        self.assertEqual(result["task"]["ref"], "secretary-701")
        self.assertEqual(result["task"]["state"], "in_progress")
        # The whole card, compared exactly, and not a list of fields that happen to match: the
        # released case compared the complete metadata map, so an extra key the create starts
        # writing has to fail here.  Every metadata key reaches the reader — the model's own keys
        # as named fields, everything else in `extensions.kanboard` — so the exhaustive claim
        # survives the move intact.  Three keys are dropped by name because they are the board's
        # identity and placement rather than anything the create stamped, and §9 spells two of
        # them differently on the two backends.
        report = self.card("secretary-701")
        for backend_owned in ("id", "audit", "position"):
            report.pop(backend_owned)
        self.assertEqual(
            report,
            {
                "ref": "secretary-701",
                "title": "steward: hourly sweep",
                "description": "",
                "state": "in_progress",
                "closed": False,
                "project": "secretary",
                "type": "research",
                "blocked_by": None,
                "claim": {"worker": "steward-sweep-20260830-120000", "claimed_at": None},
                "routing": {
                    "complexity": "standard",
                    "family_preference": "auto",
                    "head_override": None,
                    "review_head_override": None,
                    "resolved_worker_family": None,
                    "resolved_worker_head": None,
                    "resolved_review_family": None,
                    "resolved_review_head": None,
                    "routing_reason": None,
                    "quota_snapshot_at": None,
                    "codex_launch_mode": None,
                },
                "workspace": {
                    "slug": "steward-sweep-20260830-120000",
                    "base_branch": None,
                    "seed_ref": None,
                    "supersedes": None,
                },
                "retry": {"same": 0, "switched": 0, "heads": []},
                "sprint": None,
                "record_type": "task",
                # secretary-1638: a research card's stored review choice defaults to skipped.
                "review": "skipped",
                "live_impact": False,
                "extensions": {
                    "kanboard": {
                        "record_type": "task",
                        "steward_report": "1",
                        "swimlane": "Secretary",
                    }
                },
                "comments": [],
            },
        )
        # A card created directly in In progress and one created in Issues and then moved read
        # the same afterwards, so this too is a claim only the call log can carry.
        self.assertEqual(self.board_call_count("moveTaskPosition"), 0)
        after_create = self.board_snapshot()
        event = self.writer.audit.committed_event("steward-report-create")
        assert event is not None
        self.assertTrue(event["payload"]["steward_report"])

        replay = self.writer.create_steward_report(
            actor="dispatch",
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120000",
            request_id="steward-report-create",
        )
        self.assertTrue(replay["replayed"])
        self.assertBoardUnchanged(after_create)

    def test_steward_cannot_close_an_ordinary_in_progress_card(self) -> None:
        self.place_card("secretary-468", "in_progress")
        self.clear_card_metadata("secretary-468", "steward_report")
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "only for its own report") as raised:
            self.writer.move(
                role="steward",
                actor="dispatch",
                reference="secretary-468",
                target="done",
                reason="close",
                request_id="ordinary-steward-close",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertBoardUnchanged(before)

    def test_steward_can_close_its_in_progress_report(self) -> None:
        self.place_card("secretary-468", "in_progress")
        result = self.writer.move(
            role="steward",
            actor="dispatch",
            reference="secretary-468",
            target="done",
            reason="sweep complete",
            request_id="report-steward-close",
        )
        self.assertEqual(result["task"]["state"], "done")

    def test_steward_report_adapter_is_structural_task_reader_writer_composition(self) -> None:
        board = StewardReportBoard(self.writer.reader, self.writer, actor="dispatch")
        reference = board.create_report(
            project="secretary",
            title="steward: hourly sweep",
            slug="steward-sweep-20260830-120002",
        )

        self.assertEqual(board.in_progress_reports(project="secretary")[0]["reference"], reference)
        board.move_report(reference=reference, target="done", reason="sweep complete")
        self.assertEqual(self.writer.reader.show(reference)["state"], "done")

    def test_kanboard_host_executes_every_declared_card_edge_through_the_typed_canon(self) -> None:
        host = self.writer.board_host
        for index, declaration in enumerate(TRANSITIONS[EntityKind.CARD].values()):
            self.place_card("secretary-468", declaration.source.value)
            result = host.transition(
                TransitionRequest(
                    EntityKind.CARD,
                    "secretary-468",
                    declaration.target,
                    Actor("po", "operator"),
                    "registry contract",
                    RelatedRefs(("sprint:1031",)),
                    f"host-edge-{index}",
                )
            )
            self.assertEqual(result.entity.state, declaration.target)
            self.assertEqual(result.event.kind, declaration.event_kind)
            self.assertEqual(
                (result.event.source_state, result.event.target_state),
                (declaration.source.value, declaration.target.value),
            )
        self.assertEqual(len(host.canon.events(ref="secretary-468")), len(TRANSITIONS[EntityKind.CARD]))

    def test_the_typed_event_is_staged_exactly_once_before_the_column_effect(self) -> None:
        """Staging is a precondition of the effect, and the committed event is that same record."""
        self.place_card("secretary-468", "in_progress")
        staged: list[dict | None] = []
        real_call = self.client.call

        def call(method: str, /, **params: object) -> object:
            if method == "moveTaskPosition":
                staged.append(self.writer.audit.pending_event("staged-once"))
            return real_call(method, **params)

        with mock.patch.object(self.client, "call", call):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="staged-once",
            )

        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]["record_type"], "board.protocol_event")
        self.assertEqual(staged[0]["transition"], {"source": "in_progress", "target": "validate"})
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(
            self.writer.audit.committed_event("staged-once")["event_id"],
            staged[0]["event_id"],
        )

    def _pending_typed_move(self, request_id: str, target: str = "ready") -> int:
        """Leave the supported post-effect failure: the column moved, its event did not commit."""
        self.place_card("secretary-468", "in_progress")
        with mock.patch.object(self.writer.audit, "append", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.move(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    target=target,
                    reason="",
                    request_id=request_id,
                )
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], target)
        # The cleanup this edge owes the board runs inside the transition, so it is already
        # complete when only the commit fails.
        self.assertIsNone(self.card("secretary-468")["claim"]["worker"])
        return self.board_call_count("moveTaskPosition")

    def test_a_refused_card_edge_stages_no_typed_event(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(
                role="po",
                actor="p",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="refused-edge",
            )

        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertBoardUnchanged(before)
        self.assertIsNone(self.writer.audit.event("refused-edge"))
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(self.writer.audit.events(), [])

    def _released_move_record(self, request_id: str, **payload: object) -> dict[str, object]:
        """One generic `moved` record shaped exactly as the released writer wrote it."""
        return {
            "event_id": f"evt_{request_id}",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00+00:00",
            "actor": {"role": "dispatcher", "id": "d"},
            "kind": "moved",
            "outcome": "success",
            "task_id": self.writer.reader.show("secretary-468")["id"],
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "r1"},
            "request_id": request_id,
            "payload": dict(payload),
        }

    def test_generic_pending_contender_cannot_be_published_as_a_typed_transition(self) -> None:
        """A released record owns its request id, and the typed request may not borrow it."""
        self.place_card("secretary-468", "in_progress")
        contender = self._released_move_record("contended-transition", to="validate")
        contender["event_id"] = "legacy-contender"
        self.writer.audit.stage("contended-transition", contender)
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "another operation or payload") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="validate",
                reason="submit",
                request_id="contended-transition",
            )

        self.assertEqual(raised.exception.code, "validation")
        pending = self.writer.audit.pending_event("contended-transition")
        self.assertEqual(pending["event_id"], "legacy-contender")
        self.assertNotIn("record_type", pending)
        self.assertBoardUnchanged(before)

    def test_a_released_generic_move_id_still_replays_after_the_migration(self) -> None:
        """The upgrade does not turn a pre-migration move id into a validation error.

        Dispatcher move ids are deterministic per attempt, so an attempt spanning the upgrade
        re-issues one. It has to answer as the released replay it is, not as a typed request.
        """
        self.place_card("secretary-468", "validate")
        released = self._released_move_record(
            "released-move",
            **{
                "from": "in_progress",
                "to": "validate",
                "reason_sha256": hashlib.sha256(b"submit").hexdigest(),
            },
        )
        self.writer.audit.stage("released-move", released)
        self.writer.audit.append("released-move", released)
        before = self.board_snapshot()

        replayed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="validate",
            reason="submit",
            request_id="released-move",
        )

        self.assertEqual(replayed["action"], "moved")
        self.assertIs(replayed["replayed"], True)
        self.assertEqual(replayed["event_id"], "evt_released-move")
        self.assertBoardUnchanged(before)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_a_released_pending_generic_move_is_finished_by_its_released_cleanup(self) -> None:
        """The other released half: a pending generic move still completes its Ready reset."""
        self.place_card("secretary-468", "ready")
        released = self._released_move_record(
            "released-pending-move",
            **{"from": "in_progress", "to": "ready", "reason_sha256": None},
        )
        self.writer.audit.stage("released-pending-move", released)

        replayed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="released-pending-move",
        )

        self.assertIs(replayed["replayed"], True)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        # Recovery finished the Ready reset and moved nothing: the card is where the released
        # half-move already left it.
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertNotIn("record_type", self.writer.audit.events()[0])

    def test_retry_does_not_repeat_backend_write_or_event(self) -> None:
        result = self.writer.comment(
            role="worker", actor="w", reference="secretary-468", body="safe", request_id="same"
        )
        second = self.writer.comment(
            role="worker", actor="w", reference="secretary-468", body="safe", request_id="same"
        )
        self.assertEqual(result["event_id"], second["event_id"])
        self.assertEqual(sum("safe" in body for body in self.card_comments("secretary-468")), 1)
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_comment_scrubs_runtime_secret_before_board_and_audit(self) -> None:
        runtime = Path(self.tmpdir.name) / "external" / "runtime.env"
        secret = "opaque-token-value"
        url = "https://board.example.invalid/jsonrpc.php"
        runtime.parent.mkdir()
        runtime.write_text(f"KANBOARD_URL={url}\nKANBOARD_API_TOKEN={secret}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SECRETARY_RUNTIME_ENV_FILE": str(runtime)}):
            self.writer.comment(
                role="worker",
                actor="w",
                reference="secretary-468",
                body=f"Check {url}; token {secret}",
                request_id="scrubbed-comment",
            )

        content = self.card_comments("secretary-468")[-1]
        self.assertIn(url, content)
        self.assertNotIn(secret, content)
        self.assertIn("«REDACTED»:env-value", content)
        self.assertNotIn(secret, json.dumps(self.writer.audit.events()))

    def test_ordinary_long_text_is_preserved_for_board_protocol_text(self) -> None:
        ordinary = "build-attestation-" + "a" * 64

        self.writer.comment(
            role="worker",
            actor="w",
            reference="secretary-468",
            body=ordinary,
            request_id="ordinary-long-comment",
        )
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            classification="external_fact",
            body=ordinary,
            request_id="ordinary-long-report",
        )
        self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="red",
            body=ordinary,
            request_id="ordinary-long-verdict",
        )

        comments = self.card_comments("secretary-468")
        self.assertTrue(all(ordinary in content for content in comments[-3:]))

    def test_custom_catalog_value_is_scrubbed_before_a_board_comment(self) -> None:
        secret = "custom-catalogued-credential"
        with mock.patch("secretary.secret_store.redaction_values", return_value=(secret,)):
            self.writer.comment(
                role="worker",
                actor="w",
                reference="secretary-468",
                body=secret,
                request_id="custom-catalog-scrub",
            )

        content = self.card_comments("secretary-468")[-1]
        self.assertNotIn(secret, content)
        self.assertIn("«REDACTED»:env-value", content)

    def test_backend_failure_removes_uncommitted_pending_record(self) -> None:
        with (
            self.board_refuses("createComment"),
            self.assertRaisesRegex(TaskError, "refused"),
        ):
            self.writer.comment(role="worker", actor="w", reference="secretary-468", body="safe")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_edit_is_po_only_and_requires_a_change(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.edit(role="worker", actor="w", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertBoardUnchanged(before)
        # As in test_forbidden_role_does_not_write: both guards refuse before the board is
        # touched at all, and only the empty call log says so.
        self.assertEqual(self.rpc, [])

        with self.assertRaisesRegex(TaskError, "requires a new") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)
        self.assertEqual(self.rpc, [])

    def test_edit_refuses_active_states(self) -> None:
        self.place_card("secretary-468", "in_progress")
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "Ready or Blocked") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-468", description="new spec")
        self.assertEqual(raised.exception.code, "edit_forbidden")
        self.assertBoardUnchanged(before)
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_edit_updates_spec_and_routing_and_writes_audit(self) -> None:
        before = self.card("secretary-468")
        old_description = str(before["description"])

        result = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="revised spec",
            head="codex-terra",
            review_head="claude-opus",
            request_id="edit-once",
        )

        self.assertEqual(result["action"], "edited")
        self.assertEqual(result["task"]["description"], "revised spec")
        # The exhaustive half of what the two board writes used to say: the description and the
        # two routing overrides changed, and the card is otherwise the card it was.  Reading it
        # back proves more than the calls did — a field written and then overwritten by another
        # write would still be caught here.
        after = self.card("secretary-468")
        self.assertEqual(
            {key: value for key, value in after.items() if key != "audit"},
            {
                **{key: value for key, value in before.items() if key != "audit"},
                "description": "revised spec",
                "routing": {
                    **before["routing"],
                    "head_override": "codex-terra",
                    "review_head_override": "claude-opus",
                },
            },
        )
        event = self.writer.audit.events()[0]
        self.assertEqual(event["kind"], "edited")
        payload = event["payload"]
        self.assertEqual(payload["description_sha256"], hashlib.sha256(b"revised spec").hexdigest())
        self.assertEqual(
            payload["description_sha256_was"], hashlib.sha256(old_description.encode()).hexdigest()
        )
        self.assertIsNone(payload["title_sha256"])
        self.assertEqual(payload["head"], "codex-terra")
        self.assertEqual(payload["review_head"], "claude-opus")
        self.assertEqual(
            specification_revision(self.writer.audit.events("secretary-468"), "revised spec"),
            event["event_id"],
        )

    def test_edit_retry_does_not_repeat_backend_write(self) -> None:
        first = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit"
        )
        second = self.writer.edit(
            role="po", actor="operator", reference="secretary-468", description="v2", request_id="same-edit"
        )
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(self.card("secretary-468")["description"], "v2")
        # A repeated write of the same description leaves the same card, so the only observation
        # of "it did not write twice" is the count of the product's own calls.
        self.assertEqual(self.board_call_count("updateTask"), 1)

    def test_archive_is_po_only_and_requires_reason(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "not permitted") as raised:
            self.writer.archive(role="worker", actor="w", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertBoardUnchanged(before)

        with self.assertRaisesRegex(TaskError, "non-empty reason") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason=" ")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_archive_refuses_live_work_or_active_claim(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "active claim") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "live_work")
        self.assertBoardUnchanged(before)

        self.clear_card_metadata("secretary-468", "claim")
        self.place_card("secretary-468", "validate")
        parked = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")
        self.assertEqual(raised.exception.code, "live_work")
        self.assertBoardUnchanged(parked)

    def test_archive_closes_card_and_writes_audit(self) -> None:
        self.clear_card_metadata("secretary-468", "claim")

        result = self.writer.archive(
            role="po",
            actor="operator",
            reference="secretary-468",
            reason="backlog cleanup",
            request_id="archive-once",
        )

        self.assertEqual(result["action"], "archived")
        self.assertTrue(self.card("secretary-468")["closed"])
        # One reason comment, stored, and no second one.  The order in which the two board
        # writes were issued is the subject of
        # test_archive_retry_after_failed_comment_recreates_reason_before_close, which is
        # Kanboard-only for exactly that reason.
        self.assertEqual(
            [body for body in self.card_comments("secretary-468") if body.startswith("[archive]")],
            ["[archive]\nbacklog cleanup"],
        )
        event = self.writer.audit.events()[0]
        self.assertEqual(event["kind"], "archived")
        self.assertEqual(event["payload"].keys(), {"reason_sha256"})
        self.assertNotIn("secretary-468", [task["ref"] for task in self.writer.reader.list()])

    def test_archive_refuses_dispatcher_record_after_claim_was_cleared(self) -> None:
        self.clear_card_metadata("secretary-468", "claim")
        state_dir = Path(self.tmpdir.name) / "dispatcher"
        state_dir.mkdir()
        (state_dir / "production-state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "phase": "production",
                    "records": {
                        "secretary-468": {
                            "worker": "worker-secretary-468",
                            "workspace": "/home/dev/orca/workspaces/secretary/468-archive",
                            "handle": "terminal-1",
                            "review_handle": "review-1",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "live dispatcher work") as raised:
            self.writer.archive(
                role="po",
                actor="operator",
                reference="secretary-468",
                reason="cleanup",
                request_id="archive-live-dispatcher-record",
            )

        self.assertEqual(raised.exception.code, "live_work")
        self.assertBoardUnchanged(before)

    def test_restore_comment_retry_uses_digest_occurrence_not_history_index(self) -> None:
        self.add_comment("secretary-468", "first")
        with self.board_loses_reply("createComment"):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.restore_comment(
                    reference="secretary-468",
                    body="second",
                    occurrence=0,
                    request_id="restore-second-lost-reply",
                )
        self.writer.restore_comment(
            reference="secretary-468",
            body="second",
            occurrence=0,
            request_id="restore-second-lost-reply",
        )
        self.assertEqual(self.card_comments("secretary-468"), ["first", "second"])

        with self.board_loses_reply("createComment"):
            with self.assertRaisesRegex(TaskError, "audit repair"):
                self.writer.restore_comment(
                    reference="secretary-468",
                    body="second",
                    occurrence=1,
                    request_id="restore-duplicate-lost-reply",
                )
        self.writer.restore_comment(
            reference="secretary-468",
            body="second",
            occurrence=1,
            request_id="restore-duplicate-lost-reply",
        )
        self.assertEqual(self.card_comments("secretary-468"), ["first", "second", "second"])

    def test_dispatcher_claim_stamps_metadata_moves_and_audits(self) -> None:
        self.clear_card_metadata("secretary-468", "claim")
        result = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            resolved_review_head="codex-reviewer",
            request_id="claim-once",
        )

        self.assertEqual(result["action"], "claimed")
        self.assertEqual(result["task"]["state"], "in_progress")
        claimed = self.card("secretary-468")
        self.assertEqual(claimed["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(claimed["routing"]["resolved_worker_head"], "codex")
        self.assertEqual(claimed["routing"]["resolved_review_head"], "codex-reviewer")
        event = self.writer.audit.events()[0]
        self.assertEqual(event["kind"], "card.started")
        self.assertEqual(event["transition"], {"source": "ready", "target": "in_progress"})

    def test_create_stores_codex_launch_mode_and_audits(self) -> None:
        with self.open_sprint() as sprint:
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                description="body",
                target="ready",
                reference="secretary-522",
                head="codex-extra",
                codex_launch_mode="tui",
                request_id="create-tui",
                sprint=sprint,
            )

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["task"]["ref"], "secretary-522")
        self.assertEqual(result["task"]["state"], "ready")
        self.assertEqual(result["task"]["routing"]["head_override"], "codex-extra")
        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(self.card("secretary-522")["routing"]["codex_launch_mode"], "tui")
        event = self.writer.audit.events()[0]
        self.assertEqual(event["kind"], "created")
        self.assertEqual(event["payload"]["codex_launch_mode"], "tui")
        self.assertEqual(event["payload"]["head"], "codex-extra")
        self.assertIn("title_sha256", event["payload"])

    BOUNDS = (
        "Probe the live queue.\n\n## Impact bounds\n\n### Allowed\nRead the staging queue.\n\n"
        "### Forbidden\nWrites to production.\n\n### Cleanup\nDrop the probe consumer.\n"
    )

    def stored_metadata(self, reference: str) -> dict:
        return dict(self._board("getTaskMetadata", task_id=self.backend_id(reference)) or {})

    def create_kind(self, reference: str, task_type: str, **fields: object) -> dict:
        with self.open_sprint() as sprint:
            return self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type=task_type,
                title=f"{task_type} card",
                reference=reference,
                request_id=f"create-{reference}",
                sprint=sprint,
                **fields,
            )

    def test_create_stores_each_kind_with_its_default_review(self) -> None:
        """secretary-1638: three kinds, and the review choice is stored per kind, not derived."""
        for number, (task_type, review) in enumerate(
            (("code", "required"), ("research", "skipped"), ("infra", "skipped")), start=530
        ):
            with self.subTest(task_type=task_type):
                result = self.create_kind(f"secretary-{number}", task_type)
                card = self.card(f"secretary-{number}")
                self.assertEqual(
                    (card["type"], card["review"], card["live_impact"]), (task_type, review, False)
                )
                self.assertEqual(result["task"]["review"], review)
                self.assertEqual(self.writer.audit.events(f"secretary-{number}")[0]["payload"]["review"], review)
        # The stored value is a board fact: clearing it is what makes a card legacy again.
        self.assertEqual(self.stored_metadata("secretary-531")["review"], "skipped")

    def test_a_legacy_card_with_no_stored_review_reads_as_required(self) -> None:
        card = self.card("secretary-468")
        self.assertNotIn("review", self.stored_metadata("secretary-468"))
        self.assertEqual((card["review"], card["live_impact"]), ("required", False))

    def test_create_accepts_a_review_override_in_both_directions(self) -> None:
        self.create_kind("secretary-540", "code", review="skipped")
        self.create_kind("secretary-541", "research", review="required")
        self.create_kind("secretary-542", "infra", review="required")
        self.assertEqual(
            [self.card(f"secretary-{number}")["review"] for number in (540, 541, 542)],
            ["skipped", "required", "required"],
        )

    def test_an_explicit_reviewer_head_does_not_decide_the_review(self) -> None:
        """Whether review runs is `--review` or the kind default; a reviewer head never changes it."""
        self.create_kind("secretary-543", "code", review_head="claude-opus")
        card = self.card("secretary-543")
        self.assertEqual((card["review"], card["routing"]["review_head_override"]), ("required", "claude-opus"))
        self.create_kind("secretary-544", "research", review="required", review_head="claude-opus")
        self.assertEqual(self.card("secretary-544")["review"], "required")

        before = self.board_snapshot()
        for task_type, fields in (
            ("code", {"review": "skipped"}),
            # No `--review`: research defaults to skipped, and a head the sprint does not pin contradicts it.
            ("research", {}),
        ):
            with self.subTest(task_type=task_type), self.assertRaisesRegex(TaskError, "review is skipped") as raised:
                self.create_kind("secretary-545", task_type, review_head="claude-opus", **fields)
            self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

        with self.assertRaisesRegex(TaskError, "review must be one of") as raised:
            self.create_kind("secretary-545", "code", review="sometimes")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_live_impact_is_refused_outside_research(self) -> None:
        before = self.board_snapshot()
        for task_type in ("code", "infra"):
            with self.subTest(task_type=task_type), self.assertRaisesRegex(TaskError, "research attribute") as raised:
                self.create_kind("secretary-545", task_type, live_impact=True, description=self.BOUNDS)
            self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_live_impact_research_needs_declared_impact_bounds(self) -> None:
        before = self.board_snapshot()
        for description, missing in (
            ("no bounds at all", "needs a '## Impact bounds' section"),
            ("## Impact bounds\n### Allowed\nstaging\n### Cleanup\nundo\n", "missing '### Forbidden'"),
            (
                "## Impact bounds\n### Allowed\nstaging\n### Forbidden\n\n### Cleanup\nundo\n",
                "empty '### Forbidden'",
            ),
            (
                "## Impact bounds\n### Allowed\nstaging\n### Forbidden\nprod\n## Notes\n### Cleanup\nundo\n",
                "missing '### Cleanup'",
            ),
        ):
            with self.subTest(missing=missing), self.assertRaisesRegex(TaskError, re.escape(missing)) as raised:
                self.create_kind("secretary-546", "research", live_impact=True, description=description)
            self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

        result = self.create_kind("secretary-546", "research", live_impact=True, description=self.BOUNDS)
        self.assertTrue(result["task"]["live_impact"])
        card = self.card("secretary-546")
        self.assertEqual((card["type"], card["review"], card["live_impact"]), ("research", "skipped", True))
        self.assertTrue(self.writer.audit.events("secretary-546")[0]["payload"]["live_impact"])
        # Without the flag a research card's description is its own business.
        self.create_kind("secretary-547", "research", description="no bounds needed")
        self.assertFalse(self.card("secretary-547")["live_impact"])

    def test_edit_cannot_strip_the_impact_bounds_of_a_live_impact_card(self) -> None:
        self.create_kind("secretary-548", "research", live_impact=True, description=self.BOUNDS)
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "needs a '## Impact bounds' section") as raised:
            self.writer.edit(role="po", actor="operator", reference="secretary-548", description="bounds gone")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

        revised = self.BOUNDS.replace("Read the staging queue.", "Read the staging queue twice.")
        self.create_kind("secretary-547", "research", description=self.BOUNDS)
        with self.open_sprint():
            self.writer.edit(role="observer", actor="observer", reference="secretary-548", description=revised)
            # A card without the flag keeps its free-form description.
            self.writer.edit(role="observer", actor="observer", reference="secretary-547", description="anything")
        self.assertEqual(self.card("secretary-548")["description"], revised)
        self.assertEqual(self.card("secretary-547")["description"], "anything")

    def test_the_audit_that_follows_the_client_sees_the_report_the_journal_never_gets(self) -> None:
        """What the dispatcher must read on this backend, and what it read on 2026-09-10.

        The worker's `report:done` is committed to `requests`/`board_events`; the file journal
        under the same data dir stays empty. A reader built from the data dir alone (`TaskAudit`)
        would wait for that report forever, which is how secretary-1614 was declared stalled.
        """
        # A done report is refused from a dirty checkout, so give the writer a clean one of its
        # own rather than whatever the test process was started in.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(
            ["git", *identity, "commit", "-q", "--allow-empty", "-m", "seed"],
            cwd=workspace,
            check=True,
        )
        self.writer.workspace = workspace
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="audit-follows-the-client",
        )
        audit = tasks.task_audit_for(self.client, self.tmpdir.name)
        self.assertIsInstance(audit, SqlTaskAudit)
        reported = audit.events("secretary-468", kind="reported")
        self.assertEqual([event["request_id"] for event in reported], ["audit-follows-the-client"])
        self.assertEqual(reported[0]["data"]["marker"], "report:done")
        self.assertFalse((Path(self.tmpdir.name) / "board" / "events.ndjson").exists())

    def restore_destination(self):
        """A fresh store holding only the project and sprint the restored cards name."""
        destination = self.card_store(empty_seed(), instance_dir=self.tmpdir.name)
        with destination.transaction():
            destination._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES ('secretary', true, true)"
            )
            ensure_sprint_row(destination, "sprint:test")
        return destination

    def test_kind_review_and_live_impact_survive_export_and_restore(self) -> None:
        self.create_kind("secretary-550", "research", live_impact=True, description=self.BOUNDS)
        self.create_kind("secretary-551", "infra", review="required")
        self.create_kind("secretary-552", "code", review="skipped")
        # The fixture's two legacy rows predate `record_type`; the export requires one.
        self.set_card_metadata("secretary-468", record_type="task")
        self.set_card_metadata("old-1", record_type="task", project="secretary", task_type="research")
        expected = {
            "old-1": ("research", "required", False),
            "secretary-468": ("code", "required", False),
            "secretary-550": ("research", "skipped", True),
            "secretary-551": ("infra", "required", False),
            "secretary-552": ("code", "skipped", False),
        }
        data_dir = Path(self.tmpdir.name) / "round-trip"
        init_layout(data_dir)
        export_board(
            data_dir, instance_dir=Path(self.tmpdir.name), reader=self.writer.reader, sprint_client=SprintKanboard()
        )
        exported = {
            card["reference"]: card
            for card in json.loads((data_dir / "board" / "cards.json").read_text(encoding="utf-8"))["cards"]
        }
        self.assertEqual(exported["secretary-550"]["metadata"]["live_impact"], "1")
        self.assertEqual(exported["secretary-551"]["metadata"]["review"], "required")
        self.assertNotIn("review", exported["secretary-468"]["metadata"])

        destination = self.restore_destination()
        self.assertEqual(import_normalized_board(data_dir, client=destination), len(exported))
        restored = TaskReader(destination)
        self.assertEqual(
            {
                reference: (card["type"], card["review"], card["live_impact"])
                for reference in expected
                for card in [restored.show(reference)]
            },
            expected,
        )

    def test_edit_refuses_a_reviewer_head_on_a_skipped_card(self) -> None:
        self.create_kind("secretary-549", "infra")
        before = self.board_snapshot()
        # The sprint pins no reviewer, so any head the caller names contradicts `skipped`.
        with self.open_sprint(), self.assertRaisesRegex(TaskError, "review is skipped") as raised:
            self.writer.edit(role="observer", actor="observer", reference="secretary-549", review_head="claude-opus")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    @contextlib.contextmanager
    def pinned_sprint(self):
        """The open sprint with both executors pinned, as `sprint:1446` itself is."""
        from secretary.sprint_observer import executor_pinned

        with self.open_sprint() as ref:
            sprint = {
                "ref": ref,
                "status": "open",
                "repositories": ["secretary"],
                "reservations": ["secretary"],
                "executors": {"worker": executor_pinned("codex-worker"), "reviewer": executor_pinned("claude-review")},
            }
            with mock.patch("secretary.sprints.SprintReader.show", return_value=sprint):
                yield ref

    def create_in(self, sprint_context, reference: str, task_type: str, **fields: object) -> dict:
        role = str(fields.pop("role", "observer"))
        with sprint_context as sprint:
            self.writer.create(
                role=role,
                actor=role,
                project="secretary",
                task_type=task_type,
                title=f"{task_type} card",
                reference=reference,
                request_id=f"create-{reference}",
                sprint=sprint or "",
                **fields,
            )
        card = self.card(reference)
        return (card["review"], card["routing"]["head_override"] or None, card["routing"]["review_head_override"] or None)

    def test_a_sprint_reviewer_pin_decides_who_reviews_whatever_the_review_choice(self) -> None:
        """secretary-1638 round 3: the pin is applied as on base; `skipped` refuses only an unpinned head."""
        pinned = ("codex-worker", "claude-review")
        for reference, task_type, fields, expected in (
            ("secretary-560", "research", {}, ("skipped", *pinned)),
            ("secretary-561", "infra", {}, ("skipped", *pinned)),
            ("secretary-562", "code", {}, ("required", *pinned)),
            ("secretary-563", "research", {"review": "required"}, ("required", *pinned)),
            ("secretary-564", "research", {"review": "skipped", "review_head": "claude-review"}, ("skipped", *pinned)),
            ("secretary-565", "research", {"review_head": "claude-review"}, ("skipped", *pinned)),
        ):
            with self.subTest(reference=reference):
                self.assertEqual(self.create_in(self.pinned_sprint(), reference, task_type, **fields), expected)

        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "pins its reviewer") as raised:
            self.create_in(self.pinned_sprint(), "secretary-566", "research", review="skipped", review_head="other")
        self.assertEqual(raised.exception.code, "sprint_executor_pinned")
        self.assertBoardUnchanged(before)

    def test_without_a_sprint_skipped_refuses_any_explicit_reviewer_head(self) -> None:
        # A proposal in Issues is the create that names no sprint.
        no_sprint = contextlib.nullcontext(None)
        self.assertEqual(self.create_in(no_sprint, "secretary-567", "research", target="issues", role="retro"), ("skipped", None, None))
        self.assertEqual(self.create_in(no_sprint, "secretary-568", "code", target="issues", role="retro"), ("required", None, None))
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "review is skipped") as raised:
            self.create_in(no_sprint, "secretary-569", "research", target="issues", role="retro", review_head="claude-review")
        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_edit_of_a_skipped_card_in_a_pinned_sprint_keeps_the_pin(self) -> None:
        self.create_in(self.pinned_sprint(), "secretary-570", "infra")
        self.create_in(self.pinned_sprint(), "secretary-571", "code")
        self.set_card_metadata("secretary-570", review_head="")
        with self.pinned_sprint():
            # Empty head: the pin is written, exactly as on base.
            self.writer.edit(role="observer", actor="observer", reference="secretary-570", review_head="")
            self.assertEqual(self.card("secretary-570")["routing"]["review_head_override"], "claude-review")
            self.writer.edit(role="observer", actor="observer", reference="secretary-570", review_head="claude-review")
            self.assertEqual(self.card("secretary-570")["review"], "skipped")
            # A required card is untouched by the review choice.
            self.writer.edit(role="observer", actor="observer", reference="secretary-571", review_head="")
            self.assertEqual(self.card("secretary-571")["routing"]["review_head_override"], "claude-review")

        before = self.board_snapshot()
        with self.pinned_sprint(), self.assertRaisesRegex(TaskError, "pins its reviewer") as raised:
            self.writer.edit(role="observer", actor="observer", reference="secretary-570", review_head="other")
        self.assertEqual(raised.exception.code, "sprint_executor_pinned")
        self.assertBoardUnchanged(before)

    def test_auto_reference_enumeration_failure_writes_no_card(self) -> None:
        before = self.board_snapshot()
        original_call = self.client.call

        def invalid_task_list(method: str, **params: object) -> object:
            if method == "getAllTasks":
                return {"unexpected": "shape"}
            return original_call(method, **params)

        with (
            mock.patch.object(self.client, "call", side_effect=invalid_task_list),
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            self.open_sprint() as sprint,
            self.assertRaisesRegex(TaskError, "invalid task list") as raised,
        ):
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="No fallback",
                request_id="auto-reference-failure",
                sprint=sprint,
            )

        self.assertEqual(raised.exception.code, "backend_error")
        self.assertBoardUnchanged(before)

    def test_auto_reference_refuses_null_or_false_enumeration(self) -> None:
        before = self.board_snapshot()
        original_call = self.client.call

        for reply in (None, False):
            with (
                self.subTest(reply=reply),
                mock.patch.object(
                    self.client,
                    "call",
                    side_effect=lambda method, reply=reply, **params: (
                        reply if method == "getAllTasks" else original_call(method, **params)
                    ),
                ),
                mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
                self.open_sprint() as sprint,
                self.assertRaisesRegex(TaskError, "invalid task list") as raised,
            ):
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="No fallback",
                    request_id=f"null-reference-{reply}",
                    sprint=sprint,
                )

            self.assertEqual(raised.exception.code, "backend_error")
            self.assertBoardUnchanged(before)

    def test_create_passes_reference_to_atomic_backend_write(self) -> None:
        with (
            mock.patch("secretary.sprints.sprint_guard_index_initialized", return_value=True),
            self.open_sprint() as sprint,
        ):
            result = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Atomic reference",
                request_id="atomic-reference",
                sprint=sprint,
            )

        # The effect: the card exists under the reference the create answered with.
        self.assertEqual(self.card(result["task"]["ref"])["ref"], result["task"]["ref"])
        # And the claim that has no effect to read: the reference was never patched in by a
        # second write.  A card that was created blank and then updated reads exactly the same,
        # so the absence of the call is the only observation there is.  It is the product's own
        # call either way, so it holds on both backends.
        self.assertEqual(self.board_call_count("updateTask"), 0)

    def test_an_allocated_reference_clears_the_archived_rows_too(self) -> None:
        """An archived card keeps its reference for good, so the counter has to see it."""
        self.add_card(
            reference="secretary-1404",
            title="Archived",
            state="done",
            archived=True,
            metadata={"project": "secretary"},
        )

        with self.open_sprint() as sprint:
            created = self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Next in line",
                request_id="allocate-above-archived",
                sprint=sprint,
            )

        self.assertEqual(created["task"]["ref"], "secretary-1405")

    def test_an_allocated_reference_that_is_claimed_is_refused_not_written(self) -> None:
        """The claim check is what proves a reference free; allocation only proposes one.

        An enumeration that missed a row is simulated by allocating a reference the board already
        holds: the card must not be created under someone else's reference.
        """
        before = self.board_snapshot()
        with mock.patch.object(tasks, "next_project_reference", return_value="secretary-468"):
            with self.open_sprint() as sprint:
                with self.assertRaisesRegex(TaskError, "secretary-468 is already claimed") as raised:
                    self.writer.create(
                        role="observer",
                        actor="observer",
                        project="secretary",
                        task_type="code",
                        title="Collides",
                        request_id="allocated-collision",
                        sprint=sprint,
                    )

        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_explicit_reference_collision_is_still_refused(self) -> None:
        before = self.board_snapshot()
        with self.open_sprint() as sprint:
            with self.assertRaisesRegex(TaskError, "secretary-468 is already claimed") as raised:
                self.writer.create(
                    role="observer",
                    actor="observer",
                    project="secretary",
                    task_type="code",
                    title="Duplicate",
                    reference="secretary-468",
                    request_id="explicit-collision",
                    sprint=sprint,
                )

        self.assertEqual(raised.exception.code, "validation")
        self.assertBoardUnchanged(before)

    def test_ready_reset_preserves_codex_launch_mode(self) -> None:
        self.place_card("secretary-468", "in_progress")
        self.set_card_metadata("secretary-468", codex_launch_mode="tui")

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="retry",
            request_id="ready-preserves-mode",
        )

        self.assertEqual(result["task"]["routing"]["codex_launch_mode"], "tui")
        self.assertEqual(self.card("secretary-468")["routing"]["codex_launch_mode"], "tui")

    def test_create_rejects_invalid_codex_launch_mode_without_write(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "codex launch mode") as raised:
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="shell",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertBoardUnchanged(before)

    def test_create_rejects_the_retired_exec_launch_mode_without_write(self) -> None:
        """The service layer refuses it too, not only the command that usually calls it."""
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "codex launch mode must be tui") as raised:
            self.writer.create(
                role="observer",
                actor="observer",
                project="secretary",
                task_type="code",
                title="Launch mode",
                codex_launch_mode="exec",
            )

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertBoardUnchanged(before)

    def test_worker_create_ready_is_forbidden_without_backend_write(self) -> None:
        before = self.board_snapshot()
        with self.assertRaisesRegex(TaskError, "only proposals in Issues") as raised:
            self.writer.create(
                role="worker",
                actor="w",
                project="secretary",
                task_type="code",
                title="Continuation",
                target="ready",
            )

        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertBoardUnchanged(before)

    def _failed_claim(self, request_id: str, worker: str = "secretary-468-runtime") -> None:
        """A claim whose column move is refused: the whole attempt leaves nothing behind."""
        with (
            self.board_refuses("moveTaskPosition"),
            self.assertRaisesRegex(TaskError, "refused the moveTaskPosition") as raised,
        ):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker=worker,
                resolved_head="codex",
                request_id=request_id,
            )
        self.assertEqual(raised.exception.code, "backend_error")

    def test_a_failed_claim_move_leaves_neither_a_typed_event_nor_a_claim(self) -> None:
        """The claim write is inside the transition, so a refused move claims nothing.

        The released path wrote the claim metadata first and left a pending record for
        `reconcile` to finish by moving the card. Recovery may no longer repeat a move, so the
        claim is written only once the column effect is proven: a failed attempt has to be
        indistinguishable from one that never ran.
        """
        self.clear_card_metadata("secretary-468", "claim")

        self._failed_claim("claim-replay")

        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertIsNone(task["routing"]["resolved_worker_head"])

        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="claim-replay",
        )

        self.assertEqual(replayed["task"]["state"], "in_progress")
        self.assertEqual(replayed["task"]["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_reconcile_has_nothing_to_repeat_after_a_failed_claim_move(self) -> None:
        self.clear_card_metadata("secretary-468", "claim")
        self._failed_claim("claim-reconcile")

        self.assertEqual(self.writer.reconcile(), (0, 0))
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])

    def test_a_retry_after_a_failed_claim_move_still_meets_every_admission_guard(self) -> None:
        """A new attempt is a new admission, whoever it is: no attempt earns a skipped guard.

        The claimant id is derived from the card, so a retrying dispatcher computes the same
        worker id as the attempt that failed. That must not read as "already mine".
        """
        self.clear_card_metadata("secretary-468", "claim")
        self._failed_claim("claim-attempt-1")
        # Another code card of the same project is claimed before the retry.
        self.add_card(
            reference="secretary-999",
            title="Other code",
            state="in_progress",
            metadata={"project": "secretary", "task_type": "code", "claim": "other-worker"},
        )
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex-b",
                request_id="claim-attempt-2",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertBoardUnchanged(before)
        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "ready")
        self.assertIsNone(task["claim"]["worker"])
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_a_retry_after_a_failed_claim_move_records_the_head_it_asks_for(self) -> None:
        """The head a new attempt resolved is the head the card gets, not the failed one's.

        A dropped head is the disagreement `claim_mismatch` exists to catch, so a partial claim
        must not leave the next attempt launching against a head the dispatcher no longer holds.
        """
        self.clear_card_metadata("secretary-468", "claim")
        with (
            self.board_refuses("moveTaskPosition"),
            self.assertRaisesRegex(TaskError, "refused the moveTaskPosition"),
        ):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
                resolved_head="codex-a",
                request_id="claim-head-1",
            )

        claimed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex-b",
            request_id="claim-head-2",
        )

        self.assertIs(claimed["replayed"], False)
        self.assertEqual(claimed["task"]["state"], "in_progress")
        self.assertEqual(claimed["task"]["routing"]["resolved_worker_head"], "codex-b")
        self.assertNotIn(
            "resolved_head",
            claim_mismatch(claimed["task"], "secretary-468-runtime", "codex-b", ""),
        )

    def test_a_claim_on_a_held_card_is_refused_even_when_it_names_the_same_worker(self) -> None:
        """A live claim closes the door, and naming its holder is not a key to it."""
        held = str(self.card("secretary-468")["claim"]["worker"])
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "already claimed") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker=held,
                resolved_head="codex-b",
                request_id="claim-contender",
            )

        self.assertEqual(raised.exception.code, "claim_conflict")
        self.assertBoardUnchanged(before)
        self.assertEqual(self.card("secretary-468")["routing"]["head_override"], "codex-terra")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def _released_claim_record(self, request_id: str, **payload: object) -> dict[str, object]:
        """One generic `claimed` record shaped exactly as the released writer wrote it."""
        return {
            "event_id": f"evt_{request_id}",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00+00:00",
            "actor": {"role": "dispatcher", "id": "d"},
            "kind": "claimed",
            "outcome": "success",
            "task_id": self.writer.reader.show("secretary-468")["id"],
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "r1"},
            "request_id": request_id,
            "payload": dict(payload),
        }

    def _released_pending_claim(self, request_id: str) -> None:
        """The released half-claim: metadata written, column move still owed."""
        self.set_card_metadata("secretary-468", claim="secretary-468-runtime")
        self.set_card_metadata("secretary-468", resolved_head="codex")
        self.writer.audit.stage(
            request_id,
            self._released_claim_record(
                request_id,
                worker="secretary-468-runtime",
                resolved_head="codex",
                resolved_review_head=None,
                slug=None,
                base_branch=None,
                cap=3,
            ),
        )

    def test_a_released_pending_claim_is_still_finished_by_reconcile(self) -> None:
        """The released recovery survives the migration for the records that need it.

        A claim written before this migration wrote its metadata before the column move, so its
        pending record is an owed move. `reconcile` still completes it exactly as the released
        code did; only the typed path forbids recovery from moving a card.
        """
        self._released_pending_claim("released-pending-claim")

        self.assertEqual(self.writer.reconcile(), (1, 0))

        task = self.writer.reader.show("secretary-468")
        self.assertEqual(task["state"], "in_progress")
        self.assertEqual(task["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        record = self.writer.audit.events()[0]
        self.assertNotIn("record_type", record)
        self.assertEqual(record["kind"], "claimed")

    def test_a_released_pending_claim_id_replays_through_its_released_path(self) -> None:
        """Retrying that id is still the released claim, not a typed transition request."""
        self._released_pending_claim("released-claim-replay")

        replayed = self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="secretary-468-runtime",
            resolved_head="codex",
            request_id="released-claim-replay",
        )

        self.assertIs(replayed["replayed"], True)
        self.assertEqual(replayed["task"]["state"], "in_progress")
        self.assertEqual(replayed["task"]["claim"]["worker"], "secretary-468-runtime")
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_claim_rejects_project_code_capacity_without_write(self) -> None:
        self.clear_card_metadata("secretary-468", "claim")
        self.add_card(
            reference="secretary-999",
            title="Other code",
            state="in_progress",
            metadata={"project": "secretary", "task_type": "code", "claim": "other-worker"},
        )
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertBoardUnchanged(before)

    def test_claim_counts_a_parked_card_as_an_active_code_task(self) -> None:
        """A parked card holds a retained worker and its checkout: a second writer in the same
        project is as wrong there as it is in Validate."""
        self.clear_card_metadata("secretary-468", "claim")
        self.add_card(
            reference="secretary-999",
            title="Parked code",
            state="assessment",
            metadata={"project": "secretary", "task_type": "code", "claim": "other-worker"},
        )
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "one active code task") as raised:
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="secretary-468-runtime",
            )

        self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertBoardUnchanged(before)

    def test_archive_refuses_a_parked_card(self) -> None:
        """Assessment is a wait, not a resting place: the worker and workspace are still owned."""
        self.clear_card_metadata("secretary-468", "claim")
        self.place_card("secretary-468", "assessment")
        before = self.board_snapshot()

        with self.assertRaisesRegex(TaskError, "live worker or reviewer") as raised:
            self.writer.archive(role="po", actor="operator", reference="secretary-468", reason="cleanup")

        self.assertEqual(raised.exception.code, "live_work")
        self.assertBoardUnchanged(before)

    def test_reviewer_verdict_uses_review_marker(self) -> None:
        result = self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="green",
            body="ok",
            request_id="green",
        )

        self.assertEqual(result["action"], "verdict")
        self.assertEqual(self.card_comments("secretary-468")[-1], "[review:green]\nok")
        self.assertEqual(self.card("secretary-468")["comments"][-1]["marker"], "review:green")

    def test_validate_to_in_progress_rework_is_dispatcher_only(self) -> None:
        self.place_card("secretary-468", "validate")
        self.set_card_metadata("secretary-468", resolved_review_head="codex-reviewer")

        result = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="in_progress",
            reason="review:red",
            request_id="rework",
        )

        self.assertEqual(result["task"]["state"], "in_progress")
        self.assertIsNone(self.card("secretary-468")["routing"]["resolved_review_head"])

    def test_completed_ready_replay_does_not_reset_metadata_again(self) -> None:
        self.place_card("secretary-468", "in_progress")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="ready-done",
        )
        self.place_card("secretary-468", "validate")
        self.set_card_metadata("secretary-468", claim="codex-terra")
        self.set_card_metadata("secretary-468", resolved_head="codex-terra")
        self.set_card_metadata("secretary-468", resolved_review_head="codex-reviewer")
        self.set_card_metadata("secretary-468", retry_same="1")
        before = self.card("secretary-468")
        second = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="",
            request_id="ready-done",
        )

        self.assertEqual(second["task"]["state"], "validate")
        # Nothing was reset a second time: the whole card is what the fixture left, claim and
        # resolved heads and retry counters included.
        self.assertEqual(self.card("secretary-468"), before)
        self.assertEqual(self.card("secretary-468")["claim"]["worker"], "codex-terra")
        self.assertEqual(len(self.writer.audit.events()), 1)


class DoneRetentionTests(CardStoreCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        self.reader = TaskReader(self.client)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)
        self.client.move(12, "done")
        self.client.set_moved(12, 100)
        self.client.save_metadata(12, record_type="task", task_type="code")

    def cleanup(self, *, now: float = 100 + 14 * 86400 + 1) -> dict:
        return close_old_done(self.reader, self.writer, now=now, retention_days=14)

    def test_strict_threshold_missing_timestamp_and_deterministic_order(self) -> None:
        # A card reference ends in its number (§9), so the fixture's refs carry one.
        self.client.add_card(
            14, "a-old-1", title="old", state="done", project=None, metadata={"record_type": "task"}
        )
        self.client.set_moved(14, 100)
        self.client.add_card(
            15, "missing-1", title="missing", state="done", project=None, metadata={"record_type": "task"}
        )
        self.client.set_moved(15, None)
        result = self.cleanup()
        self.assertEqual(result["closed"], ["a-old-1", "secretary-468"])
        self.assertEqual(result["closed_count"], 2)
        self.assertEqual(self.client.row(15)["is_active"], 1)

        self.client.set_moved(15, 100)
        equal = close_old_done(self.reader, self.writer, now=100 + 14 * 86400, retention_days=14)
        self.assertEqual(equal["closed"], [])

    def test_reader_includes_only_active_done_execution_candidates(self) -> None:
        for key, reference, state, record_type, closed in (
            (14, "product-1", "done", "product", False),
            (15, "ready-1", "ready", "task", False),
            (16, "closed-1", "done", "task", True),
        ):
            self.client.add_card(
                key, reference, state=state, project=None, closed=closed,
                metadata={"record_type": record_type},
            )
            self.client.set_moved(key, 1)
        self.assertEqual(self.reader.done_retention_candidates(), [{"reference": "secretary-468", "date_moved": 100}])

    def test_product_or_issue_is_refused_without_close(self) -> None:
        for record_type in ("issue", "product"):
            self.client.save_metadata(12, record_type=record_type)
            with self.assertRaisesRegex(TaskError, "cannot be retired") as raised:
                self.writer.retire_done(
                    reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
                )
            self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_race_before_close_skips_changed_episode(self) -> None:
        original = self.client.call
        reads = 0

        def race(method: str, **params: object) -> object:
            nonlocal reads
            if method == "getAllTasks" and params.get("status_id") == 1:
                reads += 1
                if reads == 2:
                    self.client.set_moved(12, 200)
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=race):
            result = self.writer.retire_done(
                reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
            )
        self.assertTrue(result["skipped"])
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_race_before_close_skips_state_change(self) -> None:
        original = self.client.call
        reads = 0

        def race(method: str, **params: object) -> object:
            nonlocal reads
            if method == "getAllTasks" and params.get("status_id") == 1:
                reads += 1
                if reads == 2:
                    self.client.move(12, "ready")
            return original(method, **params)

        with mock.patch.object(self.client, "call", side_effect=race):
            result = self.writer.retire_done(
                reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
            )
        self.assertTrue(result["skipped"])
        self.assertFalse(any(method == "closeTask" for method, _params in self.client.calls))

    def test_replay_uses_episode_key_and_no_archive_comment(self) -> None:
        first = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
        )
        second = self.writer.retire_done(
            reference="secretary-468", expected_date_moved=100, cutoff=101, retention_days=14
        )
        self.assertTrue(first["retired"])
        self.assertTrue(second["skipped"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "closeTask"]), 1)
        self.assertFalse(any(call[0] == "createComment" for call in self.client.calls))
        event = self.writer.audit.events("secretary-468", kind="retired")[0]
        self.assertEqual(event["actor"]["role"], "retro")
        self.assertEqual(event["payload"]["expected_date_moved"], 100)
        self.assertEqual(event["request_id"], tasks._done_retention_request_id(12, 100))


class AssessmentStateTests(CardStoreCase):
    """secretary-1025/1031: the durable wait between a reviewer verdict and the observer's decision.

    These pin the model: who may move a card in and out of the column, that the column
    round-trips through the state map, and that a card only leaves it on a decision somebody
    recorded.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)

    def reserve_project(
        self,
        *,
        card_sprint: str = SPRINT,
        project: str = "secretary",
        data_dir: str = "",
    ) -> None:
        """Put the card in an open sprint that reserves its project.

        That reservation is what entitles an observer to decide about the card, so the tests of
        the decision path set it up as the board would have it: the guard index and the live
        sprint row both naming the sprint the card is linked to.

        The caller is bound to the card's own sprint, which is the head that would be deciding
        here. A test about a caller from elsewhere binds its own.
        """
        bind_observer(self, card_sprint)
        self.client.save_metadata(12, sprint_ref=card_sprint)
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": [project]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(data_dir or self.tmpdir.name, reader)

    def test_column_order_and_state_map(self) -> None:
        self.assertEqual(_STATE_BY_COLUMN["Assessment"], "assessment")
        self.assertEqual(
            list(_STATE_BY_COLUMN),
            ["Issues", "Ready", "In progress", "Validate", "Assessment", "Blocked", "Done"],
        )

    def test_dispatcher_transitions_are_exact(self) -> None:
        """Pinned exactly: a later card must widen this table deliberately, not by accident."""
        self.assertEqual(
            CARD_TRANSITIONS["dispatcher"],
            {
                (CardState.READY, CardState.IN_PROGRESS),
                (CardState.IN_PROGRESS, CardState.VALIDATE),
                (CardState.IN_PROGRESS, CardState.BLOCKED),
                (CardState.IN_PROGRESS, CardState.READY),
                (CardState.VALIDATE, CardState.IN_PROGRESS),
                (CardState.VALIDATE, CardState.BLOCKED),
                (CardState.VALIDATE, CardState.DONE),
                (CardState.VALIDATE, CardState.ASSESSMENT),
                (CardState.ASSESSMENT, CardState.IN_PROGRESS),
                (CardState.ASSESSMENT, CardState.DONE),
                (CardState.ASSESSMENT, CardState.BLOCKED),
            },
        )

    def test_worker_and_reviewer_stay_out_of_assessment(self) -> None:
        self.assertEqual(CARD_TRANSITIONS["worker"], frozenset())
        self.assertEqual(CARD_TRANSITIONS["reviewer"], frozenset())
        for role in ("po", "observer"):
            self.assertIn((CardState.VALIDATE, CardState.ASSESSMENT), CARD_TRANSITIONS[role])
        self.assertIn((CardState.ASSESSMENT, CardState.READY), CARD_TRANSITIONS["po"])
        self.assertEqual(
            {edge for edge in CARD_TRANSITIONS["steward"] if CardState.ASSESSMENT in edge},
            {(CardState.ASSESSMENT, CardState.BLOCKED)},
        )

    def test_the_observer_takes_no_exit_out_of_assessment(self) -> None:
        """The observer decides; the dispatcher performs. A board move by the observer would be a
        release with nothing merged, so the authority matrix has no exit for it at all."""
        self.assertEqual(
            {edge for edge in CARD_TRANSITIONS["observer"] if edge[0] is CardState.ASSESSMENT}, set()
        )
        self.assertIn((CardState.VALIDATE, CardState.ASSESSMENT), CARD_TRANSITIONS["observer"])

    def test_writer_preserves_the_complete_legacy_role_by_edge_contract(self) -> None:
        """Exercise the public writer instead of proving a copied table equals the registry."""
        dispatcher = {
            ("ready", "in_progress"),
            ("in_progress", "validate"),
            ("in_progress", "blocked"),
            ("in_progress", "ready"),
            ("validate", "in_progress"),
            ("validate", "blocked"),
            ("validate", "done"),
            ("validate", "assessment"),
            ("assessment", "in_progress"),
            ("assessment", "done"),
            ("assessment", "blocked"),
        }
        steward = {
            ("blocked", "ready"),
            ("blocked", "done"),
            ("in_progress", "done"),
            ("ready", "blocked"),
            ("in_progress", "blocked"),
            ("validate", "blocked"),
            ("assessment", "blocked"),
        }

        def legacy_authorized(role: str, source: str, target: str) -> bool:
            if source == target:
                return False
            if role == "po":
                return True
            if role == "observer":
                return source != "assessment"
            if role == "dispatcher":
                return (source, target) in dispatcher
            if role == "steward":
                return (source, target) in steward
            return False

        with as_observer(SPRINT), mock.patch.object(self.writer, "_sprint_holds_project", return_value=True):
            for role in ("po", "dispatcher", "observer", "steward", "worker", "reviewer", "retro"):
                for source in CARD_STATES:
                    for target in CARD_STATES:
                        self.client.move(12, source)
                        try:
                            self.writer.move(
                                role=role,
                                actor=role,
                                reference="secretary-468",
                                target=target,
                                reason="authorization contract",
                                request_id=f"transition-contract-{role}-{source}-{target}",
                            )
                        except TaskError as exc:
                            admitted = exc.code != "transition_forbidden"
                        else:
                            admitted = True
                        self.assertEqual(
                            admitted, legacy_authorized(role, source, target), (role, source, target)
                        )

    def _park(self, request_id: str = "into-assessment") -> None:
        self.client.move(12, "validate")
        entered = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="assessment",
            reason="",
            request_id=request_id,
        )
        self.assertEqual(entered["task"]["state"], "assessment")
        self.assertEqual(self.client.state(12), "assessment")

    def _decide(self, kind: str, request_id: str = "") -> dict:
        if not self.client.metadata(12).get("sprint_ref"):
            self.reserve_project()
        return self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind=kind,
            body="the round converged",
            request_id=request_id or f"decision-{kind}",
        )

    def test_dispatcher_moves_a_card_into_and_out_of_assessment(self) -> None:
        self._park()
        self._decide("rework")

        left = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="in_progress",
            reason="",
            decision="rework",
            request_id="out-of-assessment",
        )
        self.assertEqual(left["task"]["state"], "in_progress")
        self.assertEqual(self.client.state(12), "in_progress")

    def test_a_release_with_no_recorded_decision_is_refused(self) -> None:
        """The seam's whole point: nothing acts on a parked card that nobody decided about."""
        self._park()

        with self.assertRaisesRegex(TaskError, "recorded decision") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                request_id="undecided-release",
            )

        self.assertEqual(raised.exception.code, "decision_required")
        self.assertEqual(self.client.state(12), "assessment")
        # The decision rule runs before the typed canon is touched at all: a refused release
        # leaves neither a staged event to recover nor a request id somebody has to release.
        self.assertIsNone(self.writer.audit.event("undecided-release"))
        self.assertEqual(self.writer.audit.status()["pending"], 0)

    def test_a_move_naming_a_decision_nobody_recorded_is_refused(self) -> None:
        """Carrying the word is not deciding: the audit is what the refusal reads."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="claimed-release",
            )

        self.assertEqual(self.client.state(12), "assessment")

    def test_a_decision_from_an_earlier_parking_does_not_release_a_later_one(self) -> None:
        """A decision is about the round it was written for, not about every later round."""
        self._park()
        self._decide("release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="first-release",
        )
        self._park(request_id="parked-again")

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="replayed-release",
            )

    def test_exact_decision_replay_survives_dispatcher_leaving_assessment(self) -> None:
        self._park()
        first = self._decide("release", request_id="replay-after-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-release",
        )

        replay = self._decide("release", request_id="replay-after-release")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(replay["task"]["state"], "done")
        self.assertEqual(
            len(
                [
                    comment
                    for comment in self.client.comments(12)
                    if comment["comment"] == "[decision:release]\nthe round converged"
                ]
            ),
            1,
        )

    def test_exact_decision_replay_uses_its_original_assessment_visit(self) -> None:
        self._park()
        first = self._decide("release", request_id="replay-after-later-visit")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-first-visit",
        )
        # A PO may return the Card to Validate before the dispatcher parks a
        # second Assessment visit.  The replay must still describe visit one.
        self.client.move(12, "validate")
        self._park(request_id="park-second-visit")

        replay = self._decide("release", request_id="replay-after-later-visit")

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(
            len(
                [
                    comment
                    for comment in self.client.comments(12)
                    if comment["comment"] == "[decision:release]\nthe round converged"
                ]
            ),
            1,
        )

    def test_unowned_decision_remains_refused_after_leaving_assessment(self) -> None:
        self._park()
        self._decide("release", request_id="first-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-first-release",
        )

        with self.assertRaisesRegex(TaskError, "only recorded on a card in Assessment") as raised:
            self._decide("release", request_id="unowned-after-release")

        self.assertEqual(raised.exception.code, "transition_forbidden")

    def test_mismatched_decision_replay_is_refused_before_assessment_admission(self) -> None:
        self._park()
        self._decide("release", request_id="mismatched-after-release")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="apply-mismatched-release",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._decide("rework", request_id="mismatched-after-release")

        self.assertEqual(raised.exception.code, "validation")

    def test_a_decision_is_recorded_on_the_card_and_in_the_audit(self) -> None:
        self._park()

        decided = self._decide("reslice")

        self.assertEqual(decided["action"], "decided")
        comment = decided["task"]["comments"][-1]
        self.assertEqual(comment["marker"], "decision:reslice")
        self.assertIn("the round converged", comment["body"])
        event = self.writer.audit.events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["kind"], "card.decided")
        self.assertEqual(event["data"]["decision"], "reslice")
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

    def test_rework_refuses_a_worker_requirement_for_a_dispatcher_gate_receipt(self) -> None:
        """issue:7360d39d4956435c9cc6: the gate receipt is not worker evidence."""
        self._park()
        self.reserve_project()
        body = "Repair the local implementation and report the focused regression coverage."
        request_id = "dispatcher-receipt-rework"

        with mock.patch("secretary.tasks.specification_revision", return_value="specification-revision-1"):
            with self.assertRaises(ArtifactOwnershipTaskError) as raised:
                self.writer.decide(
                    role="observer",
                    actor="observer",
                    reference="secretary-468",
                    kind="rework",
                    body=body,
                    protocol_prerequisites=("dispatcher_executed_exact_sha_gate_receipt",),
                    request_id=request_id,
                )

            # Retrying the same denied request neither creates a second audit fact nor changes the
            # card into a worker-blocked/external-fact outcome.
            with self.assertRaises(ArtifactOwnershipTaskError) as retried:
                self.writer.decide(
                    role="observer",
                    actor="observer",
                    reference="secretary-468",
                    kind="rework",
                    body=body,
                    protocol_prerequisites=("dispatcher_executed_exact_sha_gate_receipt",),
                    request_id=request_id,
                )

        self.assertEqual(raised.exception.code, "artifact_ownership_violation")
        self.assertEqual(retried.exception.code, "artifact_ownership_violation")
        self.assertIn("owned by dispatcher", raised.exception.message)
        self.assertIn("specification-revision-1", raised.exception.message)
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "assessment")
        self.assertFalse(
            any(comment.get("marker") == "decision:rework" for comment in self.client.comments(12))
        )
        refusals = self.writer.audit.events("secretary-468", kind="card.decision_refused")
        self.assertEqual(len(refusals), 1)
        refusal = refusals[0]["data"]
        self.assertEqual(refusal["code"], "artifact_ownership_violation")
        self.assertEqual(refusal["artifact_owner"], "dispatcher")
        self.assertEqual(refusal["requested_role"], "worker")
        self.assertEqual(refusal["protocol_prerequisites"], ["dispatcher_executed_exact_sha_gate_receipt"])
        self.assertEqual(refusal["specification_revision"], "specification-revision-1")
        self.assertNotIn("external_fact", str(refusal))

        # A corrected instruction uses the normal decision path on this same Assessment visit and
        # candidate. The refusal did not consume the decision's retry identity.
        corrected = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="rework",
            body="Repair the local implementation and report the focused regression coverage.",
            protocol_prerequisites=("worker_local_broad_check_receipt",),
            request_id=request_id,
        )
        self.assertFalse(corrected["replayed"])
        self.assertEqual(corrected["task"]["state"], "assessment")
        decisions = self.writer.audit.events("secretary-468", kind="card.decided")
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["data"]["assessment_visit"])
        self.assertEqual(decisions[0]["data"]["protocol_prerequisites"], ["worker_local_broad_check_receipt"])

    def test_normal_rework_with_reviewer_verdict_context_is_recordable(self) -> None:
        self._park()
        self.reserve_project()

        decided = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="rework",
            body="Do not obtain an executed exact-SHA gate receipt. Address each reviewer finding.",
            protocol_prerequisites=(),
            request_id="normal-reviewer-context-rework",
        )

        self.assertFalse(decided["replayed"])
        self.assertEqual(decided["task"]["state"], "assessment")
        self.assertEqual(
            self.writer.audit.events("secretary-468", kind="card.decision_refused"), []
        )
        self.assertEqual(self.writer.audit.events("secretary-468", kind="card.decided")[0]["data"]["decision"], "rework")

    def test_decide_cli_persists_declared_protocol_prerequisites(self) -> None:
        self._park()
        data_dir = Path(self.tmpdir.name) / "cli"
        self.reserve_project(data_dir=str(data_dir))
        reason = Path(self.tmpdir.name) / "decision.md"
        reason.write_text("repair the local implementation\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "decide",
                    "--ref",
                    "secretary-468",
                    "--role",
                    "observer",
                    "--kind",
                    "rework",
                    "--protocol-prerequisite",
                    "worker_local_broad_check_receipt",
                    "--reason-file",
                    str(reason),
                    "--data-dir",
                    str(data_dir),
                    "--request-id",
                    "cli-structured-rework",
                ]
            )

        self.assertEqual((code, errors.getvalue()), (0, ""))
        event = tasks.task_audit_for(self.client).events("secretary-468", kind="card.decided")[-1]
        self.assertEqual(event["data"]["body"], "repair the local implementation\n")
        self.assertEqual(event["data"]["protocol_prerequisites"], ["worker_local_broad_check_receipt"])

    def test_verdict_cli_uses_the_established_writer_path(self) -> None:
        body = Path(self.tmpdir.name) / "verdict.md"
        body.write_text("looks good\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "verdict",
                    "--ref",
                    "secretary-468",
                    "--role",
                    "reviewer",
                    "--kind",
                    "green",
                    "--body-file",
                    str(body),
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "cli-verdict"),
                    "--request-id",
                    "cli-verdict",
                ]
            )

        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["action"], "verdict")

    def test_assessment_visit_accepts_one_canonical_decision_across_delivery_retries(self) -> None:
        self._park()

        first = self._decide("release", request_id="decision-first-delivery")
        replay = self._decide("release", request_id="decision-retried-delivery")

        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        decisions = self.writer.audit.events("secretary-468", kind="decided")
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["data"]["assessment_visit"])
        with self.assertRaisesRegex(TaskError, "already has a release decision") as raised:
            self._decide("rework", request_id="decision-conflicting-delivery")
        self.assertEqual(raised.exception.code, "decision_already_recorded")

    def test_concurrent_assessment_decisions_have_one_canonical_winner(self) -> None:
        self._park()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]
        gate = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []

        def decide(kind: str) -> None:
            gate.wait()
            try:
                with as_observer(SPRINT):
                    outcomes.append(
                        (
                            kind,
                            self.writer.decide(
                                role="observer",
                                actor="observer",
                                reference="secretary-468",
                                kind=kind,
                                body=kind,
                                request_id="race-" + kind,
                            ),
                        )
                    )
            except TaskError as exc:
                outcomes.append((kind, exc))

        threads = [threading.Thread(target=decide, args=(kind,)) for kind in ("release", "rework")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        decisions = self.writer.audit.events("secretary-468", kind="decided")
        self.assertEqual(len(decisions), 1, outcomes)
        self.assertEqual(sum(isinstance(result, dict) for _kind, result in outcomes), 1)
        self.assertEqual(sum(isinstance(result, TaskError) for _kind, result in outcomes), 1)

    def test_concurrent_same_id_decision_mismatch_is_refused(self) -> None:
        self._park()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]
        gate = threading.Barrier(2)
        outcomes: list[object] = []

        def decide(body: str) -> None:
            gate.wait()
            try:
                outcomes.append(
                    self.writer.decide(
                        role="observer",
                        actor="observer",
                        reference="secretary-468",
                        kind="release",
                        body=body,
                        request_id="same-id-different-body",
                    )
                )
            except TaskError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=decide, args=(body,)) for body in ("first body", "different body")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(isinstance(result, dict) for result in outcomes), 1)
        refused = [result for result in outcomes if isinstance(result, TaskError)]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].code, "validation")
        self.assertIn("belongs to another operation", refused[0].message)

    def test_observer_wake_predicate_excludes_routine_and_self_card_events(self) -> None:
        refs = {"secretary-468"}

        def event(kind: str, *, actor: str, ref: str = "secretary-468", payload: dict | None = None) -> dict:
            return {
                "ref": ref,
                "kind": kind,
                "outcome": "success",
                "actor": {"role": actor},
                "payload": payload or {},
            }

        for routine in (
            event("created", actor="observer"),
            event("claimed", actor="dispatcher"),
            event("reported", actor="worker"),
            event("commented", actor="dispatcher"),
            event("moved", actor="dispatcher", payload={"to": "validate"}),
            event("moved", actor="dispatcher", payload={"from": "assessment", "to": "issues"}),
            event("routing", actor="dispatcher"),
            event("decided", actor="observer", payload={"decision": "release"}),
        ):
            self.assertFalse(
                is_significant_observer_event(
                    routine,
                    linked_refs=refs,
                    sprint_ref=SPRINT,
                )
            )
        for semantic in (
            event("moved", actor="dispatcher", payload={"to": "assessment"}),
            event("moved", actor="dispatcher", payload={"to": "blocked"}),
            event("moved", actor="dispatcher", payload={"to": "done"}),
            event("moved", actor="po", payload={"from": "in_progress", "to": "issues"}),
            event("moved", actor="po", payload={"from": "validate", "to": "issues"}),
            event("moved", actor="po", payload={"from": "assessment", "to": "issues"}),
            event("moved", actor="steward", payload={"from": "assessment", "to": "issues"}),
            event("budget_recorded", actor="dispatcher", ref=SPRINT),
            event("commented", actor="po", ref=SPRINT),
        ):
            self.assertTrue(
                is_significant_observer_event(
                    semantic,
                    linked_refs=refs,
                    sprint_ref=SPRINT,
                )
            )

    def test_observer_wake_predicate_reads_typed_transitions_by_their_own_shape(self) -> None:
        """The migrated representation carries the same semantics, in its own fields.

        The predicate is told which record it holds by ``record_type``; it never reads a
        transition out of a generic payload or an outcome out of a typed one.
        """
        refs = {"secretary-468"}

        def typed(source: CardState, target: CardState, *, actor: str = "dispatcher") -> dict:
            declaration = transition_for(EntityKind.CARD, source, target)
            return Event(
                f"board-event-{actor}-{source.value}-{target.value}",
                declaration.event_kind,
                EntityKind.CARD,
                "secretary-468",
                Actor(actor, actor),
                "why",
                datetime(2026, 8, 11, tzinfo=UTC),
                source_state=source.value,
                target_state=target.value,
            ).to_record(f"request-{actor}-{source.value}-{target.value}")

        for routine in (
            typed(CardState.READY, CardState.IN_PROGRESS),
            typed(CardState.IN_PROGRESS, CardState.VALIDATE),
            typed(CardState.VALIDATE, CardState.IN_PROGRESS),
            typed(CardState.IN_PROGRESS, CardState.READY),
            # The observer's own writes never wake it again, whatever the edge says.
            typed(CardState.VALIDATE, CardState.BLOCKED, actor="observer"),
        ):
            self.assertFalse(
                is_significant_observer_event(routine, linked_refs=refs, sprint_ref=SPRINT),
                routine,
            )
        for semantic in (
            typed(CardState.VALIDATE, CardState.ASSESSMENT),
            typed(CardState.IN_PROGRESS, CardState.BLOCKED),
            typed(CardState.ASSESSMENT, CardState.DONE),
            typed(CardState.IN_PROGRESS, CardState.ISSUES, actor="po"),
        ):
            self.assertTrue(
                is_significant_observer_event(semantic, linked_refs=refs, sprint_ref=SPRINT),
                semantic,
            )

    def test_a_decision_needs_a_parked_card_a_reason_and_a_permitted_role(self) -> None:
        self._park()
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="  ",
                request_id="empty-reason",
            )
        with self.assertRaisesRegex(TaskError, "decision must be one of"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="merge",
                body="ship it",
                request_id="unknown-kind",
            )
        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="worker-decision",
            )
        # The card leaves the column. Its project stays reserved by the observer's own sprint, so
        # what refuses this is the state and not the reservation.
        self.reserve_project()
        self.client.move(12, "validate")
        with self.assertRaisesRegex(TaskError, "only recorded on a card in Assessment"):
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="unparked-decision",
            )

    def test_a_blocked_escalation_out_of_assessment_needs_no_decision(self) -> None:
        """Blocked stays reachable without one: it is what rescues a card nobody decided about."""
        self._park()

        escalated = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="blocked",
            reason="the release could not land",
            request_id="parked-card-blocked",
        )

        self.assertEqual(escalated["task"]["state"], "blocked")

    def test_only_the_observer_decides(self) -> None:
        """One authority for the decision. A PO that has to intervene overrides visibly."""
        self._park()

        with self.assertRaisesRegex(TaskError, "role is not permitted"):
            self.writer.decide(
                role="po",
                actor="operator",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="po-decision",
            )

    def test_a_decision_moves_the_card_where_that_decision_goes(self) -> None:
        """A recorded release paired with a move back to In progress is a rework nobody decided."""
        self._park()
        self._decide("release")

        with self.assertRaisesRegex(TaskError, "release decision moves the card to done") as raised:
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="in_progress",
                reason="",
                decision="release",
                request_id="release-to-in-progress",
            )

        self.assertEqual(raised.exception.code, "decision_mismatch")
        self.assertEqual(self.client.state(12), "assessment")

    def test_the_undecided_exits_from_assessment_are_closed(self) -> None:
        """Ready, Validate and Issues all leave the column with nothing decided, and Ready also
        clears the claim, which is what would let a second worker start on a reviewed checkout."""
        self._park()

        for target in ("ready", "validate", "issues"):
            with self.assertRaises(TaskError) as raised:
                self.writer.move(
                    role="dispatcher",
                    actor="d",
                    reference="secretary-468",
                    target=target,
                    reason="",
                    request_id=f"dispatcher-bypass-{target}",
                )
            self.assertIn(raised.exception.code, {"decision_required", "transition_forbidden"})
        self.assertEqual(self.client.state(12), "assessment")

    def test_the_observer_may_not_perform_its_own_decision(self) -> None:
        """The observer records the decision; the dispatcher performs it.

        A matching decision is checkable, but a board move is not a release: the card would read
        Done with nothing merged, In progress with no worker relaunched. So every
        decision-carrying exit is refused to the observer, on its own sprint's card and with the
        decision standing on the card.
        """
        self._park()

        self._decide("release", request_id="decision-performed-by-observer")
        with self.assertRaises(TaskError) as raised:
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="observer-performs-release",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertIn("task decide", str(raised.exception))
        self.assertEqual(self.client.state(12), "assessment")

        # And the dispatcher performs the one canonical decision that is standing.
        performed = self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="done",
            reason="",
            decision="release",
            request_id="dispatcher-performs-release",
        )
        self.assertEqual(performed["task"]["state"], "done")

    def test_a_decision_needs_an_open_sprint_to_hold_the_project(self) -> None:
        """A decision is refused where no open sprint holds the card's project, the reservation
        `move` already checks. What it does not do is say who the caller is: see the test below.
        """
        self._park()
        # A bound caller, so what is being tested is the reservation and not the identity: this
        # observer is somebody's head, and the card it reaches for is held by no open sprint.
        bind_observer(self, SPRINT)

        with self.assertRaisesRegex(TaskError, "role is not permitted") as unheld:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="decision-without-a-sprint",
            )
        self.assertEqual(unheld.exception.code, "role_forbidden")

        # A card linked to another sprint than the one holding its project is refused too, and
        # refused as the reservation it crosses.
        self.reserve_project(card_sprint="sprint:1030")
        with self.assertRaises(TaskError) as other:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="ship it",
                request_id="decision-from-another-sprint",
            )
        self.assertEqual(other.exception.code, "sprint_write_forbidden")
        self.assertEqual(standing_decision(self.writer.audit.events("secretary-468")), "")

    def test_the_decision_guard_also_places_the_caller(self) -> None:
        """The other half of the guard: which sprint's observer is writing.

        Every observer process still runs as `--role observer --actor observer`, so the actor id
        places nobody. The sprint its head was launched for does: the card's own observer decides,
        and a head of another sprint is refused as the identity failure it is.
        """
        self._park()
        self.reserve_project()

        decided = self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="release",
            body="deciding from this card's own head",
            request_id="decision-from-its-own-head",
        )

        self.assertEqual(decided["action"], "decided")
        event = self.writer.audit.events("secretary-468", kind="decided")[-1]
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

        self._park(request_id="park-again")
        with as_observer("sprint:2000"), self.assertRaises(TaskError) as stranger:
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="deciding about a sprint I do not observe",
                request_id="decision-from-another-head",
            )
        self.assertEqual(stranger.exception.code, "observer_sprint_mismatch")
        denial = self.writer.audit.events("secretary-468", kind="sprint_guard_denied")[-1]
        self.assertEqual(denial["payload"]["code"], "observer_sprint_mismatch")
        self.assertEqual(denial["payload"]["sprint"], "sprint:2000")

        with self.assertRaises(TaskError) as unbound, unbound_observer():
            self.writer.decide(
                role="observer",
                actor="observer",
                reference="secretary-468",
                kind="release",
                body="deciding from a head nobody bound",
                request_id="decision-from-an-unbound-head",
            )
        self.assertEqual(unbound.exception.code, "observer_identity_unbound")

    def test_a_po_override_still_takes_a_parked_card_back_to_ready(self) -> None:
        """The escape hatch stays open, and it is recorded as the override it is."""
        self._park()

        requeued = self.writer.move(
            role="po",
            actor="operator",
            reference="secretary-468",
            target="ready",
            reason="taking this one back by hand",
            request_id="po-requeue",
        )

        self.assertEqual(requeued["task"]["state"], "ready")

    def test_a_po_override_takes_a_parked_card_to_the_decided_targets_too(self) -> None:
        """The escape hatch is the whole exit, not the two thirds of it that need nothing decided.

        A seam stuck with no observer to release it is exactly when an operator has to finish or
        return a parked card by hand, and Done and In progress are where it would send it. Only
        the dispatcher is held to a recorded decision, because only the dispatcher performs one.
        """
        self.reserve_project()
        for target, request_id in (("done", "po-release"), ("in_progress", "po-return")):
            self._park(request_id=f"{request_id}-park")

            moved = self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target=target,
                reason="finishing this one by hand",
                sprint_override=True,
                sprint_override_reason="no observer is coming back for it",
                request_id=request_id,
            )

            self.assertEqual(moved["task"]["state"], target)

    def test_a_po_move_out_of_assessment_still_checks_a_decision_it_names(self) -> None:
        """Not being held to a decision is not licence to invent one: a decision the PO passes is
        read against the card and its destination like anybody else's."""
        self._park()

        with self.assertRaisesRegex(TaskError, "no release decision is recorded"):
            self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target="done",
                reason="",
                decision="release",
                request_id="po-claimed-release",
            )
        self._decide("release")
        with self.assertRaises(TaskError) as mismatched:
            self.writer.move(
                role="po",
                actor="operator",
                reference="secretary-468",
                target="in_progress",
                reason="",
                decision="release",
                sprint_override=True,
                sprint_override_reason="stepping in on a reserved project",
                request_id="po-mismatched-release",
            )

        self.assertEqual(mismatched.exception.code, "decision_mismatch")
        self.assertEqual(self.client.state(12), "assessment")

    def test_worker_may_not_move_a_card_out_of_assessment(self) -> None:
        self.client.move(12, "assessment")
        with self.assertRaisesRegex(TaskError, "may not move") as raised:
            self.writer.move(
                role="worker",
                actor="w",
                reference="secretary-468",
                target="done",
                reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(call[0] == "moveTaskPosition" for call in self.client.calls))

    def test_steward_escalates_an_assessment_card_with_a_reason(self) -> None:
        self.client.move(12, "assessment")
        with self.assertRaisesRegex(TaskError, "non-empty reason"):
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="blocked",
                reason="",
            )
        escalated = self.writer.move(
            role="steward",
            actor="s",
            reference="secretary-468",
            target="blocked",
            reason="the observer never came back",
            request_id="assessment-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        self.assertEqual(self.writer.reader.show("secretary-468")["state"], "blocked")

    def _move_cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "move",
                    "--ref",
                    "secretary-468",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "data"),
                    *arguments,
                ]
            )
        return code, output.getvalue(), errors.getvalue()

    def test_cli_move_target_assessment_moves_the_card(self) -> None:
        """Criterion 3 spells this `--target`; `--to` is the same argument under another name."""
        self.client.move(12, "validate")
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--target",
            "assessment",
            "--request-id",
            "cli-target",
        )

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["action"], "moved")
        self.assertEqual(self.client.state(12), "assessment")

        # The way back out is the decision path, through the CLI as well: the writer checks
        # `--decision` against the audit, so the recorded decision has to come first.
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--to",
            "done",
            "--request-id",
            "cli-to-undecided",
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(errors)["error"]["code"], "decision_required")

        # The CLI writes its audit and its sprint guard index under its own data dir, so both the
        # decision and the reservation that authorizes it have to be set up there.
        self.reserve_project(data_dir=str(Path(self.tmpdir.name) / "data"))
        reason = Path(self.tmpdir.name) / "reason.md"
        reason.write_text("ship it", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            decided = main(
                [
                    "task",
                    "decide",
                    "--ref",
                    "secretary-468",
                    "--role",
                    "observer",
                    "--kind",
                    "release",
                    "--reason-file",
                    str(reason),
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "data"),
                    "--request-id",
                    "cli-decision",
                ]
            )
        self.assertEqual((decided, errors.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["action"], "decided")
        code, output, errors = self._move_cli(
            "--role",
            "dispatcher",
            "--to",
            "done",
            "--decision",
            "release",
            "--request-id",
            "cli-to",
        )
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.client.state(12), "done")

    def test_cli_move_target_assessment_is_refused_for_a_forbidden_role(self) -> None:
        self.client.move(12, "validate")
        code, output, errors = self._move_cli(
            "--role",
            "worker",
            "--target",
            "assessment",
            "--request-id",
            "cli-forbidden",
        )

        self.assertEqual((code, output), (3, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "transition_forbidden")
        self.assertEqual(self.client.state(12), "validate")

    def test_cli_choice_lists_accept_assessment_where_a_state_is_legal(self) -> None:
        """`list --state` and `move --to` take it; `create --state` still cannot open a card there."""
        choices = _task_state_choices()
        self.assertIn("assessment", choices[("list", "state")])
        self.assertIn("assessment", choices[("move", "to")])
        self.assertEqual(choices[("create", "state")], ("issues", "ready"))
        # One argument, two spellings: `--target` is not a second option with its own dest.
        self.assertEqual(sorted(_move_target_option_strings()), ["--target", "--to"])


def _move_target_option_strings() -> list[str]:
    """Every flag `task move` accepts for the destination state."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    move = task._subparsers._group_actions[0].choices["move"]  # type: ignore[union-attr]
    return [option for action in move._actions if action.dest == "to" for option in action.option_strings]


def _task_state_choices() -> dict[tuple[str, str], tuple[str, ...]]:
    """{(task subcommand, argument dest): its choices} for every state-valued task argument."""
    from secretary.task_commands import add_task_subcommands

    parser = argparse.ArgumentParser()
    add_task_subcommands(parser.add_subparsers(dest="command"))
    task = parser._subparsers._group_actions[0].choices["task"]  # type: ignore[union-attr]
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for name, sub in task._subparsers._group_actions[0].choices.items():  # type: ignore[union-attr]
        for action in sub._actions:
            if action.dest in {"state", "to"} and action.choices:
                found[(name, action.dest)] = tuple(action.choices)
    return found


_READ_METHODS = {
    "getProjectByName",
    "getColumns",
    "getActiveSwimlanes",
    "getTaskByReference",
    "getTaskMetadata",
    "getAllComments",
    "getTask",
}


class RoutingJournalTests(CardStoreCase):
    """secretary-716: the routing record is journal-only and must survive everything the board
    forgets: the reviewer head cleared on the way out of Validate, the routing block reset on the
    way back to Ready."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)
        self.audit = self.writer.audit

    def _run(self, role: str, head: str):
        return head_run_from_profile(
            role=role,
            head=head,
            head_source="role_default",
            profile={
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "extra",
                "resource": "openai-sub",
            },
            resources={"openai-sub": {"account": "openai-subscription"}},
        )

    def _payload(self, attempt: int, phase: str, *heads: tuple[str, str], outcome: str = "") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase=phase,
            heads=[self._run(role, head) for role, head in heads],
            outcome=outcome,
        )

    def test_routing_writes_the_journal_without_touching_the_board(self) -> None:
        self.writer.routing(
            role="dispatcher",
            actor="pilot",
            reference="secretary-468",
            payload=self._payload(1, "worker", ("worker", "codex")),
            request_id="routing-1",
        )

        events = self.audit.events("secretary-468", kind="routing")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["heads"][0]["head"], "codex")
        self.assertEqual(
            [call for call in self.client.calls if call[0] not in _READ_METHODS],
            [],
            "a routing record is telemetry; it must not mutate the card",
        )

    def test_repeated_routing_record_commits_once(self) -> None:
        for _ in range(2):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")),
                request_id="routing-1",
            )

        self.assertEqual(len(self.audit.events("secretary-468", kind="routing")), 1)

    def test_only_the_dispatcher_may_write_routing(self) -> None:
        with self.assertRaisesRegex(TaskError, "not permitted"):
            self.writer.routing(
                role="worker",
                actor="w",
                reference="secretary-468",
                payload=self._payload(1, "worker", ("worker", "codex")),
            )

    def test_routing_rejects_an_unknown_phase_and_an_empty_head_list(self) -> None:
        with self.assertRaisesRegex(TaskError, "unknown routing phase"):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload={"attempt": 1, "phase": "guess", "heads": [{"role": "worker"}]},
            )
        with self.assertRaisesRegex(TaskError, "at least one head"):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload={"attempt": 1, "phase": "worker", "heads": []},
            )

    def test_attempts_rebuild_the_pairs_and_their_outcomes(self) -> None:
        for attempt, outcome in ((1, "red"), (2, "green")):
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(attempt, "worker", ("worker", "codex")),
                request_id=f"routing-worker-{attempt}",
            )
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(attempt, "review", ("reviewer", "codex-reviewer")),
                request_id=f"routing-review-{attempt}",
            )
            self.writer.routing(
                role="dispatcher",
                actor="pilot",
                reference="secretary-468",
                payload=self._payload(
                    attempt,
                    "verdict",
                    ("worker", "codex"),
                    ("reviewer", "codex-reviewer"),
                    outcome=outcome,
                ),
                request_id=f"routing-verdict-{attempt}",
            )

        history = attempts(self.audit.events("secretary-468", kind="routing"))
        self.assertEqual([record.attempt for record in history], [1, 2])
        self.assertEqual([record.outcome for record in history], ["red", "green"])
        self.assertEqual([record.worker.head for record in history], ["codex", "codex"])
        self.assertEqual([record.reviewer.head for record in history], ["codex-reviewer"] * 2)

    def test_a_head_without_a_model_must_say_the_cli_resolved_it(self) -> None:
        """The blank-model guard: a record may only omit the model when it names the runtime that
        picked one, so `claude-default` can never be journalled as a silent empty string."""
        with self.assertRaisesRegex(ValueError, "unpinned model"):
            HeadRun(role="worker", head="claude-default", model_source="profile")

        unpinned = head_run_from_profile(
            role="reviewer",
            head="claude-default",
            head_source="card",
            profile={"adapter": "claude", "resource": "claude-sub"},
            resources={"claude-sub": {"account": "claude-subscription"}},
        )

        self.assertEqual((unpinned.model, unpinned.model_source), ("", "cli_default"))

    def test_a_codex_record_names_the_one_launch_mode_whatever_it_was_asked_for(self) -> None:
        """The journal records the mode the head actually ran in, and there is one.

        A profile that still pins the retired `exec`, and a card that still carries it, are both
        legacy routing data. Neither may put a mode in the journal that no bring-up on this
        product could have produced.
        """
        run = head_run_from_profile(
            role="worker",
            head="codex",
            head_source="card",
            profile={
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "resource": "openai-sub",
                "codex_mode": "exec",
            },
            resources={"openai-sub": {"account": "openai-subscription"}},
        )

        self.assertEqual(run.codex_mode, "tui")
        self.assertNotIn(
            "codex_mode",
            inspect.signature(head_run_from_profile).parameters,
            "no caller may hand the journal a launch mode of its own",
        )

    def test_an_old_journal_record_is_read_back_as_it_was_written(self) -> None:
        """History is read, not rewritten: an attempt that really ran one-shot still says so."""
        legacy = HeadRun.from_json(
            {
                "role": "worker",
                "head": "codex",
                "head_source": "card",
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "model_source": "profile",
                "effort": "default",
                "codex_mode": "exec",
                "resource": "openai-sub",
                "account": "openai-subscription",
            }
        )

        self.assertEqual(legacy.codex_mode, "exec")

    def test_claude_effort_is_part_of_the_routing_record(self) -> None:
        run = head_run_from_profile(
            role="reviewer",
            head="claude-opus-medium",
            head_source="card",
            profile={
                "adapter": "claude",
                "model": "opus",
                "effort": "medium",
                "resource": "claude-sub",
            },
            resources={"claude-sub": {"account": "claude-subscription"}},
        )

        self.assertEqual(run.effort, "medium")

    def test_head_run_round_trips_session_and_prompt_identity(self) -> None:
        run = HeadRun(
            role="worker",
            head="codex",
            adapter="codex",
            model="gpt-5.6-terra",
            model_source="profile",
            session_id="rollout-123",
            prompt_path="/workspaces/card/TASK.md",
            prompt_version="sha256:" + "a" * 64,
        )

        restored = HeadRun.from_json(run.to_json())

        self.assertEqual(restored, run)
        self.assertEqual(restored.session_id_reason, "")

    def test_launch_prompt_identity_does_not_reread_a_mutated_worker_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "TASK.md"
            original = b"first launch instruction\n"
            document.write_bytes(original)
            lifecycle = LifecycleHeadRun(
                run_id="worker-launch",
                spec=HeadSpec(profile_id="codex", adapter="codex"),
                workspace=tmp,
                task_ref=TaskRef.card("secretary-1517", document=str(document)),
                role="worker",
                fanout_policy={
                    "version": 1,
                    "state": "unknown",
                    "terminal_state": "unknown",
                    "events": [],
                    "prompt_identity": {
                        "path": str(document),
                        "version": "sha256:" + hashlib.sha256(original).hexdigest(),
                    },
                },
            )
            document.write_text("later rework instruction\n", encoding="utf-8")

            snapshot = routing_head_snapshot_from_launch(
                self._run("worker", "codex").to_json(), lifecycle_run=lifecycle.to_json()
            )

        self.assertIsInstance(snapshot, HeadRun)
        self.assertEqual(snapshot.prompt_path, str(document))
        self.assertEqual(snapshot.prompt_version, "sha256:" + hashlib.sha256(original).hexdigest())


class ReportDurabilityGateTests(CardStoreCase):
    """`report --kind done` refuses to run from a dirty workspace (secretary-653).

    The gate lives in the worker's own session so it can commit and retry, instead of
    learning from the dispatcher post-factum that the card went to blocked."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        self.workspace = Path(self.tmpdir.name) / "workspace"
        self.workspace.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "worker@example.invalid"],
            ["config", "user.name", "worker"],
        ):
            subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True)
        (self.workspace / "code.py").write_text("print(1)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "commit", "-qm", "work"], check=True, capture_output=True
        )
        self.writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(self.workspace),
        )

    def _report(self, kind: str, body: str = "ready") -> dict:
        classification = "external_fact" if kind == "blocked" else ""
        return self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind=kind,
            body=body,
            classification=classification,
        )

    def test_clean_workspace_reports_done(self) -> None:
        self.assertEqual(self._report("done")["action"], "reported")

    def test_dirty_workspace_is_refused_without_touching_the_board(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertNotEqual(caught.exception.exit_code, 0)
        self.assertIn("code.py", caught.exception.message)
        self.assertIn("commit", caught.exception.message)
        # The refusal depends on the card's kind (a research/infra card has no candidate to commit),
        # so the card is read first; nothing is written.
        self.assertEqual(
            [method for method, _params in self.client.calls if not method.startswith("get")], []
        )
        self.assertEqual(self.writer.audit.status(), {"ok": True, "pending": 0})

    def test_exact_done_report_replays_after_the_workspace_becomes_dirty(self) -> None:
        first = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="replay-after-dirt",
        )
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")

        replay = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="replay-after-dirt",
        )

        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["event_id"], first["event_id"])
        self.assertEqual(len(self.client.comments(12)), 1)

    def test_a_research_or_infra_done_report_does_not_require_a_committed_workspace(self) -> None:
        """No candidate is published for these kinds; report artifacts may sit uncommitted."""
        (self.workspace / ".secretary-report").mkdir()
        (self.workspace / ".secretary-report" / "report.md").write_text("findings\n", encoding="utf-8")
        infra = "## What was done\nRotated the key.\n\n## How to verify\n`ssh host true`\n"
        for kind, body in (("research", "findings"), ("infra", infra)):
            with self.subTest(kind=kind):
                self.client.save_metadata(12, task_type=kind)
                self.assertEqual(self._report("done", body)["action"], "reported")

    def test_a_research_done_report_without_its_report_file_is_refused_without_touching_the_board(self) -> None:
        self.client.save_metadata(12, task_type="research")
        (self.workspace / "report.md").write_text("findings at the wrong place\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done", "findings")
        self.assertEqual(caught.exception.code, "validation")
        self.assertIn("`.secretary-report/report.md`", caught.exception.message)
        self.assertEqual(
            [method for method, _params in self.client.calls if not method.startswith("get")], []
        )

    def test_an_infra_done_report_without_both_sections_is_refused(self) -> None:
        self.client.save_metadata(12, task_type="infra")
        for body in (
            "## What was done\nRotated the key.\n",
            "## How to verify\n`ssh host true`\n",
            "## What was done\n\n## How to verify\n`ssh host true`\n",
        ):
            with self.subTest(body=body), self.assertRaises(TaskError) as caught:
                self._report("done", body)
            self.assertEqual(caught.exception.code, "validation")
            self.assertIn("## How to verify", caught.exception.message)
        self.assertEqual(self.client.comments(12), [])

    def test_untracked_file_is_refused(self) -> None:
        (self.workspace / "scratch.py").write_text("print(3)\n", encoding="utf-8")
        with self.assertRaises(TaskError) as caught:
            self._report("done")
        self.assertEqual(caught.exception.code, "uncommitted")
        self.assertIn("scratch.py", caught.exception.message)

    def test_runtime_audit_tail_does_not_block_done(self) -> None:
        board = self.workspace / "secretary-data" / "board"
        board.mkdir(parents=True)
        (board / "events.ndjson").write_text("{}\n", encoding="utf-8")
        self.assertEqual(self._report("done")["action"], "reported")

    def test_blocked_report_is_not_gated(self) -> None:
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        self.assertEqual(self._report("blocked", body="stuck on the adapter")["action"], "reported")

    def test_non_git_workspace_is_not_gated(self) -> None:
        plain = Path(self.tmpdir.name) / "plain"
        plain.mkdir()
        writer = TaskWriter(
            self.client,  # type: ignore[arg-type]
            data_dir=str(Path(self.tmpdir.name) / "data"),
            workspace=str(plain),
        )
        result = writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(result["action"], "reported")

    def test_cwd_is_the_default_workspace(self) -> None:
        writer = TaskWriter(self.client, data_dir=str(Path(self.tmpdir.name) / "data"))  # type: ignore[arg-type]
        (self.workspace / "code.py").write_text("print(2)\n", encoding="utf-8")
        with mock.patch("secretary.tasks.Path.cwd", return_value=self.workspace):
            with self.assertRaises(TaskError) as caught:
                writer.report(role="worker", actor="w", reference="secretary-468", kind="done", body="ok")
        self.assertEqual(caught.exception.code, "uncommitted")


class BlockedContractTests(CardStoreCase):
    """Why a card is blocked, and what the observer did about it (secretary-1034).

    Both halves are recorded rather than left in prose: the worker names the kind of blocker
    it hit, and the observer's move out of Blocked carries the reason it moved.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        # A workspace outside git, so the done report's durability gate is not what these
        # tests are measuring.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmpdir.name,
            workspace=str(workspace),
        )

    def _events(self, kind: str) -> list[dict]:
        typed = {"reported": "card.reported", "verdict": "card.verdict", "decided": "card.decided"}
        return [event for event in self.writer.audit.events() if event["kind"] in {kind, typed.get(kind)}]

    def _reserve(self) -> None:
        bind_observer(self, SPRINT)
        self.client.save_metadata(12, sprint_ref=SPRINT)
        reader = FakeSprintReader({"ref": SPRINT, "status": "open", "reservations": ["secretary"]})
        patcher = mock.patch("secretary.sprints.SprintReader", return_value=reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        refresh_active_sprint_projects(self.tmpdir.name, reader)

    def test_a_blocked_report_without_a_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification") as raised:
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="blocked",
                body="the upstream API is down",
                request_id="blocked-unclassified",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])

    def test_an_unknown_classification_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "require --classification"):
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="blocked",
                body="stuck",
                classification="something_else",
                request_id="blocked-unknown",
            )
        self.assertEqual(self.client.calls, [])

    def test_each_classification_reaches_the_audit_and_the_card(self) -> None:
        for index, classification in enumerate(("external_fact", "wrong_task_definition")):
            with self.subTest(classification=classification):
                result = self.writer.report(
                    role="worker",
                    actor="w",
                    reference="secretary-468",
                    kind="blocked",
                    body="stuck on the adapter",
                    classification=classification,
                    request_id=f"blocked-{classification}",
                )
                self.assertEqual(result["action"], "reported")
                event = self._events("reported")[index]
                self.assertEqual(event["record_type"], "board.protocol_event")
                self.assertEqual(event["kind"], "card.reported")
                payload = event["data"]
                self.assertEqual(payload["marker"], "report:blocked")
                self.assertEqual(payload["classification"], classification)
                self.assertEqual(
                    payload["body_sha256"],
                    hashlib.sha256(b"stuck on the adapter").hexdigest(),
                )
                comment = self.client.comments(12)[-1]["comment"]
                self.assertTrue(comment.startswith("[report:blocked]\n"))
                self.assertIn(f"classification: {classification}", comment)
                self.assertIn("stuck on the adapter", comment)

    def test_a_blocked_report_is_a_single_backend_write(self) -> None:
        """Two writes could disagree; the comment and the audit event cannot."""
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            body="stuck",
            classification="external_fact",
            request_id="blocked-one-write",
        )
        written = [
            method
            for method, _ in self.client.calls
            if method.startswith(("create", "save", "move", "update"))
        ]
        self.assertEqual(written, ["createComment"])

    def test_a_done_report_carries_no_classification(self) -> None:
        result = self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="done",
            body="ready",
            request_id="done-no-classification",
        )
        self.assertEqual(result["action"], "reported")
        self.assertIsNone(self._events("reported")[0]["data"]["classification"])
        self.assertNotIn("classification:", self.client.comments(12)[-1]["comment"])
        with self.assertRaisesRegex(TaskError, "no classification") as raised:
            self.writer.report(
                role="worker",
                actor="w",
                reference="secretary-468",
                kind="done",
                body="ready",
                classification="external_fact",
                request_id="done-with-classification",
            )
        self.assertEqual(raised.exception.code, "validation")

    def test_the_cli_refuses_an_unclassified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the upstream API is down\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "report",
                    "--role",
                    "worker",
                    "--ref",
                    "secretary-468",
                    "--kind",
                    "blocked",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "cli"),
                    "--body-file",
                    str(body),
                    "--request-id",
                    "cli-blocked-unclassified",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")

    def test_the_cli_records_a_classified_blocked_report(self) -> None:
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("the card contradicts itself\n", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            code = main(
                [
                    "task",
                    "report",
                    "--role",
                    "worker",
                    "--ref",
                    "secretary-468",
                    "--kind",
                    "blocked",
                    "--classification",
                    "wrong_task_definition",
                    "--data-dir",
                    str(Path(self.tmpdir.name) / "cli"),
                    "--body-file",
                    str(body),
                    "--request-id",
                    "cli-blocked-classified",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue())["action"], "reported")
        comment = self.client.comments(12)[-1]["comment"]
        self.assertIn("classification: wrong_task_definition", comment)

    def test_an_observer_moving_a_card_out_of_blocked_must_say_why(self) -> None:
        self._reserve()
        self.client.move(12, "blocked")

        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason") as raised:
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="ready",
                reason="   ",
                request_id="observer-silent-disposition",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.state(12), "blocked")

        reason = "the upstream fix landed, the card is workable again"
        moved = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="ready",
            reason=reason,
            request_id="observer-disposition",
        )
        self.assertEqual(moved["task"]["state"], "ready")
        event = self._events("card.unblocked")[-1]
        self.assertEqual(event["transition"], {"source": "blocked", "target": "ready"})
        self.assertEqual(event["reason"], reason)
        # The sprint the card belongs to is related to its transition without the caller
        # having to say so, which is what keeps a sprint's own history complete.
        self.assertEqual(event["related_refs"], [SPRINT])
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})
        self.assertIn(reason, self.client.comments(12)[-1]["comment"])

        # Every exit is guarded, not just the requeue to Ready.
        self.client.move(12, "blocked")
        with self.assertRaisesRegex(TaskError, "out of Blocked requires a non-empty reason"):
            self.writer.move(
                role="observer",
                actor="observer",
                reference="secretary-468",
                target="in_progress",
                reason="",
                request_id="observer-silent-resume",
            )

    def test_the_observer_may_still_move_a_card_into_blocked_without_a_reason(self) -> None:
        """Only the exit is guarded here. The entry paths are unchanged."""
        self._reserve()
        self.client.move(12, "in_progress")
        moved = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="blocked",
            reason="",
            request_id="observer-into-blocked",
        )
        self.assertEqual(moved["task"]["state"], "blocked")

    def test_the_record_of_a_block_survives_the_card_leaving_blocked(self) -> None:
        """The classification is history, not card state: nothing on the card to go stale."""
        self._reserve()
        self.writer.report(
            role="worker",
            actor="w",
            reference="secretary-468",
            kind="blocked",
            body="the upstream API is down",
            classification="external_fact",
            request_id="blocked-before-requeue",
        )
        self.client.move(12, "blocked")
        requeued = self.writer.move(
            role="observer",
            actor="observer",
            reference="secretary-468",
            target="ready",
            reason="the upstream fix landed",
            request_id="observer-requeue",
        )
        self.assertEqual(requeued["task"]["state"], "ready")
        self.assertNotIn("blocked_classification", requeued["task"])
        self.assertNotIn("blocked_classification", self.client.metadata(12))
        self.assertEqual(self._events("reported")[0]["data"]["classification"], "external_fact")

    def test_the_steward_requirement_is_untouched(self) -> None:
        self.client.move(12, "in_progress")
        with self.assertRaisesRegex(TaskError, "this steward transition requires a non-empty reason"):
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="blocked",
                reason="",
            )
        escalated = self.writer.move(
            role="steward",
            actor="s",
            reference="secretary-468",
            target="blocked",
            reason="the head went silent",
            request_id="steward-escalation",
        )
        self.assertEqual(escalated["task"]["state"], "blocked")
        # And its own exit out of Blocked keeps the shape it had: Ready needs nothing, Done does.
        self.assertEqual(
            self.writer.move(
                role="steward",
                actor="s",
                reference="secretary-468",
                target="ready",
                reason="",
                request_id="steward-requeue",
            )["task"]["state"],
            "ready",
        )


class RequestIdOwnershipTests(CardStoreCase):
    """A request id owns the operation it committed (secretary-1060).

    A retained worker reused the previous round's report id while submitting the next
    round's body. The committed event was replayed, no comment was appended, and the
    caller was told the report succeeded, so the dispatcher waited for a marker that
    could never arrive. The id has to be refused, not replayed.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = self.card_store(writer_seed(), instance_dir=self.tmpdir.name)
        # Outside git, so the done report's durability gate is not what these tests measure.
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir()
        self.writer = TaskWriter(  # type: ignore[arg-type]
            self.client,
            data_dir=self.tmpdir.name,
            workspace=str(workspace),
        )

    def _events(self, request_id: str = "") -> list[dict]:
        recorded = self.writer.audit.events()
        if not request_id:
            return recorded
        return [event for event in recorded if event["request_id"] == request_id]

    def _comments(self, task_id: int = 12) -> list[str]:
        return [str(comment["comment"]) for comment in self.client.comments(task_id)]

    def _report(self, **overrides: object) -> dict:
        call = {
            "role": "worker",
            "actor": "w",
            "reference": "secretary-468",
            "kind": "done",
            "body": "first round",
            "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.report(**call)  # type: ignore[arg-type]

    def test_a_reused_report_id_with_another_body_is_refused(self) -> None:
        """The live shape of issue:df7d0778b26357e60046."""
        first = self._report()
        self.client.calls.clear()

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["event_id"], first["event_id"])
        self.assertEqual(len(self._comments()), 1)

    def test_the_same_report_under_the_same_id_stays_idempotent(self) -> None:
        first = self._report()
        second = self._report()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["action"], "reported")
        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(len(self._comments()), 1)

    def test_structured_output_tells_a_replay_from_an_accepted_write(self) -> None:
        self.assertIs(self._report()["replayed"], False)
        self.assertIs(self._report()["replayed"], True)

    def test_a_reused_report_id_with_another_kind_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", classification="external_fact")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["data"]["marker"], "report:done")
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_with_another_classification_is_refused(self) -> None:
        self._report(kind="blocked", body="stuck", classification="external_fact")

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(kind="blocked", body="stuck", classification="wrong_task_definition")

        self.assertEqual(self._events("round-1")[0]["data"]["classification"], "external_fact")
        self.assertEqual(len(self._comments()), 1)

    def test_a_reused_report_id_on_another_card_is_refused(self) -> None:
        self._report()

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report(reference="old-1")

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["ref"], "secretary-468")
        self.assertEqual(self._comments(13), [])

    def test_a_reused_id_from_another_write_is_refused(self) -> None:
        """The claim is over the operation, not only over the report vocabulary."""
        self.writer.comment(
            role="worker",
            actor="w",
            reference="secretary-468",
            body="a note",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report()

        self.assertEqual(self._events("round-1")[0]["kind"], "commented")
        self.assertEqual(len(self._comments()), 1)

    def _stage_pending_report(self, body: str) -> dict:
        """A report staged by a crashed attempt: written, never appended."""
        event = {
            "event_id": "evt_staged",
            "schema_version": 1,
            "occurred_at": "2026-08-03T00:00:00Z",
            "actor": {"role": "worker", "id": "w"},
            "kind": "reported",
            "outcome": "success",
            "task_id": "task_kanboard_12",
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1",
            "payload": {"marker": "report:done", "body_sha256": hashlib.sha256(body.encode()).hexdigest()},
        }
        self.writer.audit.stage("round-1", event)
        return event

    def test_a_pending_report_is_owned_by_its_id_too(self) -> None:
        self._stage_pending_report("first round")

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._report(body="third round, a different report entirely")

        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self._comments(), [])

    def test_a_generic_pending_report_cannot_be_replaced_by_a_typed_owner(self) -> None:
        staged = self._stage_pending_report("first round")

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._report()

        self.assertEqual(self.writer.audit.pending_event("round-1"), staged)
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(self._comments(), [])

    def test_a_reused_verdict_id_with_another_verdict_is_refused(self) -> None:
        self.writer.verdict(
            role="reviewer",
            actor="r",
            reference="secretary-468",
            kind="green",
            body="ok",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.verdict(
                role="reviewer",
                actor="r",
                reference="secretary-468",
                kind="red",
                body="the gate is red",
                request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["data"]["marker"], "review:green")
        self.assertEqual(len(self._comments()), 1)

    def test_the_cli_refuses_a_reused_report_id_with_exit_code_two(self) -> None:
        data_dir = str(Path(self.tmpdir.name) / "cli")
        body = Path(self.tmpdir.name) / "report.md"
        body.write_text("first round\n", encoding="utf-8")
        argv = [
            "task",
            "report",
            "--role",
            "worker",
            "--ref",
            "secretary-468",
            "--kind",
            "done",
            "--data-dir",
            data_dir,
            "--body-file",
            str(body),
            "--request-id",
            "cli-round-1",
        ]
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch("secretary.task_commands.card_client", return_value=self.client),
            mock.patch("secretary.tasks.workspace_dirt", return_value=[]),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(argv), 0)
            body.write_text("third round, a different report entirely\n", encoding="utf-8")
            code = main(argv)

        self.assertEqual(code, 2)
        self.assertIs(json.loads(output.getvalue().splitlines()[0])["replayed"], False)
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "validation")
        self.assertEqual(len(self._comments()), 1)


class TypedMarkerRecoveryTests(RequestIdOwnershipTests):
    """The three migrated marker families share one staged typed transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.writer._guard_sprint_write = lambda **_kwargs: {}  # type: ignore[method-assign]
        self.writer._sprint_holds_project = lambda _project: True  # type: ignore[method-assign]

    def _write(self, family: str, request_id: str, body: str = "complete typed reason") -> dict:
        if family == "report":
            return self.writer.report(
                role="worker",
                actor="worker",
                reference="secretary-468",
                kind="done",
                body=body,
                request_id=request_id,
            )
        if family == "verdict":
            return self.writer.verdict(
                role="reviewer",
                actor="reviewer",
                reference="secretary-468",
                kind="green",
                body=body,
                request_id=request_id,
            )
        self.client.move(12, "assessment")
        return self.writer.decide(
            role="observer",
            actor="observer",
            reference="secretary-468",
            kind="release",
            body=body,
            request_id=request_id,
        )

    def test_each_marker_is_rendered_from_a_complete_typed_event(self) -> None:
        expected = {
            "report": ("card.reported", "report:done"),
            "verdict": ("card.verdict", "review:green"),
            "decision": ("card.decided", "decision:release"),
        }
        for family, (kind, marker) in expected.items():
            with self.subTest(family=family):
                request_id = f"typed-{family}"
                result = self._write(family, request_id)
                event = self.writer.audit.committed_event(request_id)
                assert event is not None
                self.assertEqual(event["record_type"], "board.protocol_event")
                self.assertEqual(event["kind"], kind)
                self.assertEqual(event["subject"], {"kind": "card", "ref": "secretary-468"})
                self.assertEqual(event["reason"], "complete typed reason")
                self.assertEqual(event["data"]["marker"], marker)
                self.assertEqual(event["data"]["body"], "complete typed reason")
                self.assertEqual(event["data"]["marker_occurrence"], 1)
                self.assertEqual(
                    self.client.comments(12)[-1]["comment"],
                    KanboardBoardHost.render_marker(Event.from_record(event)),
                )
                self.assertEqual(result["event_id"], event["event_id"])

    def test_backend_refusal_discards_each_typed_owner(self) -> None:
        served = self.client.call

        def refuse_comments(method: str, **params: object) -> object:
            if method == "createComment":
                raise TaskError("backend_error", "the board refused the comment write", 1)
            return served(method, **params)

        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family), mock.patch.object(self.client, "call", side_effect=refuse_comments):
                request_id = f"refused-{family}"
                with self.assertRaises(TaskError):
                    self._write(family, request_id)
                self.assertIsNone(self.writer.audit.event(request_id))

    def test_generic_pending_owner_cannot_be_replaced_by_any_typed_marker(self) -> None:
        for family in ("report", "verdict", "decision"):
            with self.subTest(family=family):
                request_id = f"generic-{family}"
                generic = {
                    "event_id": request_id,
                    "request_id": request_id,
                    "kind": "commented",
                    "payload": {},
                }
                self.writer.audit.stage(request_id, generic)
                with self.assertRaisesRegex(TaskError, "belongs to another operation"):
                    self._write(family, request_id)
                self.assertEqual(self.writer.audit.pending_event(request_id), generic)

    def test_every_write_has_to_declare_what_its_id_claims(self) -> None:
        """A new caller cannot inherit the blind replay by forgetting one keyword."""
        identity = inspect.signature(TaskWriter._write).parameters["identity"]
        self.assertIs(identity.default, inspect.Parameter.empty)

    def _routing_payload(self, attempt: int, head: str = "codex-terra") -> dict:
        return routing_payload(
            attempt=attempt,
            attempt_id="att-1",
            phase="worker",
            heads=[
                head_run_from_profile(
                    role="worker",
                    head=head,
                    head_source="role_default",
                    profile={"adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra"},
                    resources={},
                )
            ],
        )

    def _routing(self, attempt: int, **overrides: object) -> dict:
        call = {
            "role": "dispatcher",
            "actor": "pilot",
            "reference": "secretary-468",
            "payload": self._routing_payload(attempt),
            "request_id": "round-1",
        }
        call.update(overrides)
        return self.writer.routing(**call)  # type: ignore[arg-type]

    def test_a_reused_routing_id_with_another_record_is_refused(self) -> None:
        """A journal-only write is caller-supplied end to end, so it owns its id too."""
        first = self._routing(1)

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._routing(2)

        self.assertEqual(raised.exception.exit_code, 2)
        recorded = self._events("round-1")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["event_id"], first["event_id"])
        self.assertEqual(recorded[0]["payload"]["attempt"], 1)

    def test_a_staged_routing_record_is_owned_by_its_id_too(self) -> None:
        staged = {
            "event_id": "evt_staged_routing",
            "schema_version": 1,
            "occurred_at": "2026-08-03T00:00:00Z",
            "actor": {"role": "dispatcher", "id": "pilot"},
            "kind": "routing",
            "outcome": "success",
            "task_id": "task_kanboard_12",
            "ref": "secretary-468",
            "backend": {"kind": "kanboard", "task_id": 12, "revision": "pending"},
            "request_id": "round-1",
            "payload": self._routing_payload(1),
        }
        self.writer.audit.stage("round-1", staged)

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self._routing(2)

        self.assertEqual(self._events(), [])
        self.assertEqual(self.writer.audit.status(), {"ok": False, "pending": 1})

        # The record its own id claims still commits.
        self.assertEqual(self._routing(1)["event_id"], "evt_staged_routing")
        self.assertEqual(self._events("round-1")[0]["payload"]["attempt"], 1)

    def test_a_reused_edit_id_with_another_spec_is_refused(self) -> None:
        self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="first spec",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.edit(
                role="po",
                actor="operator",
                reference="secretary-468",
                description="second spec",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self.client.row(12)["description"], "first spec")

    def test_an_edit_retried_after_it_landed_stays_idempotent(self) -> None:
        """The `_was` digests are of text the edit replaced, so a retry must not compare them."""
        first = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="one spec",
            head="codex-terra",
            request_id="round-1",
        )
        second = self.writer.edit(
            role="po",
            actor="operator",
            reference="secretary-468",
            description="one spec",
            head="codex-terra",
            request_id="round-1",
        )

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(len([call for call in self.client.calls if call[0] == "updateTask"]), 1)

    def test_a_reused_claim_id_with_another_worker_is_refused(self) -> None:
        self.client.save_metadata(12, claim="")
        self.writer.claim(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            worker="worker-a",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.claim(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                worker="worker-b",
                request_id="round-1",
            )

        self.assertEqual(self._events("round-1")[0]["reason"], "claimed by worker-a")
        self.assertEqual(self.client.metadata(12)["claim"], "worker-a")

    def test_a_reused_move_id_with_another_destination_is_refused(self) -> None:
        self.client.move(12, "in_progress")
        self.writer.move(
            role="dispatcher",
            actor="d",
            reference="secretary-468",
            target="ready",
            reason="requeue",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="blocked",
                reason="requeue",
                request_id="round-1",
            )
        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.move(
                role="dispatcher",
                actor="d",
                reference="secretary-468",
                target="ready",
                reason="a different reason entirely",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["transition"]["target"], "ready")

    def test_a_move_retried_after_it_landed_stays_idempotent(self) -> None:
        """`from` is the column the move left, so a retry must not compare it."""
        self.client.move(12, "in_progress")
        call = {
            "role": "dispatcher",
            "actor": "d",
            "reference": "secretary-468",
            "target": "ready",
            "reason": "requeue",
            "request_id": "round-1",
        }
        first = self.writer.move(**call)  # type: ignore[arg-type]
        second = self.writer.move(**call)  # type: ignore[arg-type]

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(self._events("round-1")[0]["transition"]["source"], "in_progress")

    def test_a_reused_restore_id_with_another_placement_is_refused(self) -> None:
        self.writer.restore_card(
            reference="secretary-468",
            metadata={"project": "secretary"},
            target="ready",
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_card(
                reference="secretary-468",
                metadata={"project": "secretary"},
                target="blocked",
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._events("round-1")[0]["payload"]["target"], "ready")

    def test_a_reused_restore_comment_id_with_another_body_is_refused(self) -> None:
        self.writer.restore_comment(
            reference="secretary-468",
            body="the original comment",
            occurrence=0,
            request_id="round-1",
        )

        with self.assertRaisesRegex(TaskError, "belongs to another operation"):
            self.writer.restore_comment(
                reference="secretary-468",
                body="another comment entirely",
                occurrence=0,
                request_id="round-1",
            )

        self.assertEqual(len(self._events("round-1")), 1)
        self.assertEqual(self._comments(), ["the original comment"])

    def _create(self, **overrides: object) -> dict:
        call = {
            "role": "observer",
            "actor": "observer",
            "project": "secretary",
            "task_type": "code",
            "title": "First card",
            "target": "ready",
            "request_id": "create-1",
        }
        call.update(overrides)
        self.client.ensure_sprint("sprint:test")
        with open_sprint() as sprint:
            return self.writer.create(sprint=sprint, **call)  # type: ignore[arg-type]

    def test_a_reused_create_id_with_another_card_is_refused(self) -> None:
        created = self._create()
        cards = self.client.card_count()

        with self.assertRaisesRegex(TaskError, "belongs to another operation") as raised:
            self._create(title="A different card entirely")

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(self.client.card_count(), cards)
        self.assertEqual(len(self._events("create-1")), 1)
        self.assertEqual(self._events("create-1")[0]["event_id"], created["event_id"])

    def test_the_same_create_under_the_same_id_stays_idempotent(self) -> None:
        first = self._create()
        cards = self.client.card_count()

        second = self._create()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["task"]["ref"], second["task"]["ref"])
        self.assertIs(first["replayed"], False)
        self.assertIs(second["replayed"], True)
        self.assertEqual(self.client.card_count(), cards)
        self.assertEqual(len(self._events("create-1")), 1)


class AuditCommittedIndexTests(unittest.TestCase):
    """committed_event читает журнал инкрементально, а не целиком на каждый вызов.

    append()/stage() зовут committed_event на каждое событие, а тот разбирал весь
    events.ndjson с начала. На восстановлении 745 карточек (~30k событий) это давало
    квадратичный прогон: ~8 МБ JSON перечитывались на каждую запись, борд при этом
    отвечал за 5-10 мс. Индекс обязан оставаться согласованным с файлом, который
    дописывает другой процесс.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.audit = TaskAudit(self.tmpdir.name)
        Path(self.audit.board_dir).mkdir(parents=True, exist_ok=True)

    def _append_raw(self, payload: dict, terminated: bool = True) -> None:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write(line + ("\n" if terminated else ""))

    def test_missing_file_reads_as_no_event(self) -> None:
        self.assertIsNone(self.audit.committed_event("nope"))

    def test_picks_up_events_appended_after_the_first_read(self) -> None:
        self._append_raw({"request_id": "one", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("two"))

        self._append_raw({"request_id": "two", "event_id": "e2"})
        self.assertEqual(self.audit.committed_event("two")["event_id"], "e2")
        self.assertEqual(self.audit.committed_event("one")["event_id"], "e1")

    def test_earliest_duplicate_wins(self) -> None:
        self._append_raw({"request_id": "dup", "event_id": "first"})
        self._append_raw({"request_id": "dup", "event_id": "second"})
        self.assertEqual(self.audit.committed_event("dup")["event_id"], "first")

    def test_half_written_line_is_not_consumed_until_terminated(self) -> None:
        self._append_raw({"request_id": "done", "event_id": "e1"})
        self._append_raw({"request_id": "torn", "event_id": "e2"}, terminated=False)

        self.assertEqual(self.audit.committed_event("done")["event_id"], "e1")
        self.assertIsNone(self.audit.committed_event("torn"))

        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("\n")
        self.assertEqual(self.audit.committed_event("torn")["event_id"], "e2")

    def test_rebuilds_when_the_journal_is_replaced(self) -> None:
        self._append_raw({"request_id": "old", "event_id": "e1"})
        self.assertIsNotNone(self.audit.committed_event("old"))

        with open(self.audit.events_path, "w", encoding="utf-8") as events:
            events.write(json.dumps({"request_id": "new", "event_id": "e2"}) + "\n")

        self.assertIsNone(self.audit.committed_event("old"))
        self.assertEqual(self.audit.committed_event("new")["event_id"], "e2")

    def test_garbage_lines_are_skipped(self) -> None:
        with open(self.audit.events_path, "a", encoding="utf-8") as events:
            events.write("not json\n")
            events.write("[1,2,3]\n")
            events.write("\n")
        self._append_raw({"request_id": "good", "event_id": "e1"})
        self.assertEqual(self.audit.committed_event("good")["event_id"], "e1")

    def test_warm_index_answers_misses_without_reparsing_the_journal(self) -> None:
        """Суть фикса: на прогретом индексе промах не разбирает журнал заново.

        Именно этот путь исполнялся на каждой записи (append -> committed_event ->
        промах -> запись) и стоил полного json-разбора всего файла.
        """
        for index in range(20):
            self._append_raw({"request_id": f"r{index}", "event_id": f"e{index}"})
        self.assertIsNotNone(self.audit.committed_event("r0"))  # прогреваем индекс

        parsed: list[int] = []
        real_loads = json.loads

        def counting_loads(payload, *args, **kwargs):  # type: ignore[no-untyped-def]
            parsed.append(1)
            return real_loads(payload, *args, **kwargs)

        with mock.patch("secretary.tasks.json.loads", counting_loads):
            for index in range(20):
                self.assertIsNone(self.audit.committed_event(f"missing-{index}"))

        self.assertEqual(parsed, [])


class LegacyTaskCodecTests(unittest.TestCase):
    def test_reader_and_restore_share_legacy_task_codec(self) -> None:
        import importlib

        from secretary.board import legacy_codec

        restore = importlib.import_module("secretary.restore")

        self.assertIs(tasks._STATE_BY_COLUMN, legacy_codec.TASK_STATE_BY_COLUMN)
        self.assertIs(tasks._KNOWN_METADATA, legacy_codec.TASK_KNOWN_METADATA)
        self.assertIs(tasks._text, legacy_codec.text)
        self.assertIs(tasks._positive_int, legacy_codec.positive_int)
        self.assertIs(tasks._nonnegative_int, legacy_codec.nonnegative_int)
        self.assertIs(tasks._null_if_empty, legacy_codec.null_if_empty)
        self.assertIs(tasks._split_heads, legacy_codec.split_heads)
        self.assertIs(tasks._enum_or_default, legacy_codec.enum_or_default)
        self.assertIs(tasks._enum_or_none, legacy_codec.enum_or_none)
        self.assertIs(restore._STATE_BY_COLUMN, legacy_codec.TASK_STATE_BY_COLUMN)
        self.assertIs(restore._positive_int, legacy_codec.positive_int)
        self.assertIs(restore._enum_or_default, legacy_codec.enum_or_default)

    def test_legacy_task_codec_preserves_released_normalization(self) -> None:
        from secretary.board import legacy_codec

        self.assertEqual(legacy_codec.text(None), "")
        self.assertEqual(legacy_codec.text(17), "17")
        self.assertEqual(legacy_codec.positive_int("3"), 3)
        self.assertIsNone(legacy_codec.positive_int("0"))
        self.assertEqual(legacy_codec.nonnegative_int("-3"), 0)
        self.assertEqual(legacy_codec.split_heads("a,,b"), ["a", "b"])
        self.assertEqual(legacy_codec.enum_or_default("x", {"x"}, "d"), "x")
        self.assertEqual(legacy_codec.enum_or_default("y", {"x"}, "d"), "d")
        self.assertIsNone(legacy_codec.enum_or_none("y", {"x"}))
