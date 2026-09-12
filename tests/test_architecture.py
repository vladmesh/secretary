"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import inspect
import os
import unittest
from pathlib import Path
from unittest import mock

from secretary import _env
from secretary.infra import env

ROOT = Path(__file__).resolve().parents[1]

# Existing flat modules may leave this set one feature at a time. New modules belong in one of the
# feature packages documented in ARCHITECTURE.md instead of making the root wider again.
LEGACY_FLAT_MODULES = frozenset(
    """
    __init__.py __main__.py _env.py _fsutil.py _proc.py automations.py backup.py
    backup_policy.py backup_retention.py backup_verify.py board_transport.py bootstrap.py
    broad_check.py candidate_history.py check_commands.py checkpoint.py cli.py cli_output.py
    codex_provider_events.py config.py data.py dispatcher.py dispatcher_commands.py
    dispatcher_gate.py dispatcher_gate_receipt.py dispatcher_heartbeat.py dispatcher_helpers.py
    dispatcher_launch.py dispatcher_launcher.py dispatcher_observer.py
    dispatcher_observer_fence.py dispatcher_pause.py dispatcher_pause_ops.py
    dispatcher_production.py dispatcher_review.py dispatcher_state.py dispatcher_tui.py
    dispatcher_types.py dispatcher_watchdog.py dispatcher_worker_lifecycle.py gate.py
    head_health.py head_registry.py host.py host_apply.py host_commands.py installation.py
    knowledge_write.py memory_errors.py memory_journal.py memory_reindex.py memory_service.py
    memory_write.py observer_root.py onboarding.py product_issue_commands.py product_issues.py
    product_lanes.py provision.py restore.py restore_commands.py role_env.py role_skills.py
    routing_journal.py runtime_env.py secret_commands.py secret_recover.py secret_store.py
    secret_words.py session.py sprint_close.py sprint_commands.py sprint_observer.py sprints.py
    state_repo.py status.py task_commands.py task_restore.py tasks.py upgrade.py
    """.split()
)

# These are the only approved product edges.  Production telemetry reads the installation config;
# curator discovery reads the canonical project registry and SprintReader rather than copying either
# protocol into the triggered-agent package. Holding the exact set prevents another back edge.
LEGACY_TRIGGERED_AGENTS_IMPORTS = frozenset(
    {
        ("runtime/production_telemetry.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.sprints"),
    }
)


class SourceLayoutTests(unittest.TestCase):
    def test_test_support_never_imports_a_test_module(self) -> None:
        """Shared fakes are a one-way dependency, not bridges between test modules."""
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and (module == "tests.test" or module.startswith("tests.test_")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

    def test_new_secretary_modules_do_not_widen_the_flat_root(self) -> None:
        current = {path.name for path in (ROOT / "src" / "secretary").glob("*.py")}
        self.assertEqual(current - LEGACY_FLAT_MODULES, set())

    def test_triggered_agents_adds_no_new_dependency_on_secretary(self) -> None:
        package = ROOT / "src" / "triggered_agents"
        imports: set[tuple[str, str]] = set()
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(
                        (path.relative_to(package).as_posix(), alias.name)
                        for alias in node.names
                        if alias.name == "secretary" or alias.name.startswith("secretary.")
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and (node.module == "secretary" or node.module.startswith("secretary."))
                ):
                    imports.add((path.relative_to(package).as_posix(), node.module))
        self.assertEqual(imports, LEGACY_TRIGGERED_AGENTS_IMPORTS)

    def test_old_environment_import_is_the_same_implementation(self) -> None:
        self.assertIs(_env.positive_int, env.positive_int)
        with mock.patch.dict(os.environ, {"COUNT": "7"}):
            self.assertEqual(env.positive_int("COUNT", 3), 7)


# Every place in `secretary` that builds a board client, other than the switch itself, and the
# reason each one is allowed to name Kanboard directly (`docs/BOARD_STORE.md` §2, §6).  A new
# entry here is a new consumer that decided its backend by default instead of by the switch, so
# it is added deliberately with its reason or it is a defect.
KANBOARD_ONLY_CONSTRUCTIONS = {
    "bootstrap.py": (
        "it creates the Kanboard service's own board, columns and swimlanes and waits for the "
        "container to answer; the store has no equivalent to create"
    ),
    "board/import_board.py": (
        "the importer's subject is the Kanboard board it copies into the store, so the switch "
        "would ask the destination to be the source"
    ),
    "board/backend.py": "the switch itself, which is where the choice is made",
}


# Every place in `secretary` that builds the *file* audit (`TaskAudit(<data dir>)`) rather than
# asking `secretary.tasks.task_audit_for` for the audit owner of a card client, and the reason each
# one may. The file journal is the Kanboard backend's canon and `requests`/`board_events` is the
# PostgreSQL one (`docs/BOARD_STORE.md` §7.3), so a live reader built from a data directory alone
# answers from a store the installation may not write: on 2026-09-10 that made a committed
# `report:done` invisible to the dispatcher (sprint:1437, secretary-1614), and it is the same shape
# as an empty command history or a false `not_found`. A new entry here is a new reader that decided
# its audit by default instead of by its client, so it is added deliberately with its reason or it
# is a defect.
FILE_AUDIT_CONSTRUCTIONS = {
    "tasks.py": (
        "`task_audit_for` itself, which is where the choice is made: it returns this for a Kanboard "
        "client and `SqlTaskAudit` for a PostgreSQL one"
    ),
    "board/events.py": (
        "the typed canon's own storage internal, for a caller that has no client at all -- offline "
        "or Kanboard-only; every caller that has one passes the audit its client named, and with "
        "neither an audit nor a data directory the construction refuses"
    ),
    "product_issues.py": (
        "the pre-v2 pending-layout gate and the unmigrated-file-claim check, which are statements "
        "about the file layout itself and are run *because* the client is PostgreSQL"
    ),
    "dispatch/host.py": (
        "the default of a command host built with no audit, which only tests do; the dispatcher "
        "hands its own backend-selected audit in (`dispatcher.py`)"
    ),
}


class FileAuditOwnershipTests(unittest.TestCase):
    """A live audit reader follows its card client, and the exceptions are named with their reasons."""

    def _constructions(self) -> dict[str, list[int]]:
        """Every `TaskAudit(...)` call in `src/secretary`, by module and line."""
        found: dict[str, list[int]] = {}
        for path in sorted((ROOT / "src" / "secretary").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Name) and target.id == "TaskAudit":
                    key = str(path.relative_to(ROOT / "src" / "secretary"))
                    found.setdefault(key, []).append(node.lineno)
        return found

    def test_only_the_named_modules_build_the_file_audit_directly(self) -> None:
        """Anything else reads a journal its installation's backend may never write."""
        offenders = sorted(set(self._constructions()) - set(FILE_AUDIT_CONSTRUCTIONS))
        self.assertEqual(
            offenders,
            [],
            "these modules build the file journal's TaskAudit from a data directory instead of "
            "asking secretary.tasks.task_audit_for for the audit owner of their card client",
        )

    def test_every_named_module_still_builds_one(self) -> None:
        """The allowance is a statement about live code, not a list that outlives its reasons."""
        self.assertEqual(sorted(self._constructions()), sorted(FILE_AUDIT_CONSTRUCTIONS))

    def test_every_live_reader_construction_site_goes_through_the_selector(self) -> None:
        """The readers this card enumerated, each holding the selector call in its own source.

        Named rather than derived: these are the sites that had a data-dir-only audit and are the
        ones a regression would arrive at again. A module that stops calling `task_audit_for` is
        either a deliberate removal of a reader or the bypass coming back, and both belong in a diff
        that has to change this list.
        """
        for module in (
            "checkpoint.py",
            "task_commands.py",
            "webproto/command_reads.py",
            "webproto/ops.py",
            "webproto/reads.py",
            "webproto/sprint_reads.py",
            "board/kanboard.py",
            "sprints.py",
            "data.py",
            "dispatcher.py",
            "product_issues.py",
        ):
            source = (ROOT / "src" / "secretary" / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertIn("task_audit_for(", source, module)

    def test_the_sprint_traversal_cannot_be_built_from_a_data_directory(self) -> None:
        """`_AuditOnce` takes records or an audit owner, and has no directory to fall back to."""
        from secretary.sprints import _AuditOnce

        parameters = inspect.signature(_AuditOnce.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["events", "audit"])
        self.assertEqual(_AuditOnce().events(), [])


# The *indirect* file readers: a class that opens `<data>/board/events.ndjson` itself, so a
# construction of it is a file audit read without a `TaskAudit(...)` call for the test above to see.
# `secretary-1622`'s first submission is why this list exists: the product-run events had moved to
# `requests` while `ReadLayer.task_snapshot` and `task_events` still built `EventJournal(data_dir)`,
# so a migrated installation answered a card's history from a projection its writers never touch --
# unavailable where it had been swept, a successful empty or stale page where an old one remained.
# Each entry names the one function the construction may live in, which is the function that has
# already asked the client which backend it is.
FILE_EVENT_READER_CONSTRUCTIONS = {
    "webproto/reads.py": (
        "_events",
        (
            "the Kanboard branch of the read layer's backend-selected event reader, chosen after "
            "the card client has been asked what it is; the PostgreSQL branch pages the audit "
            "owner's own traversal through `CommittedAudit`"
        ),
    ),
}


class IndirectFileAuditReaderTests(unittest.TestCase):
    """A reader that opens the journal itself is selected by the backend, like every other."""

    def _constructions(self, name: str) -> dict[str, list[tuple[str, int]]]:
        """Every `<name>(...)` call in `src/secretary`, by module, with its enclosing function."""
        found: dict[str, list[tuple[str, int]]] = {}
        for path in sorted((ROOT / "src" / "secretary").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for holder in ast.walk(tree):
                if not isinstance(holder, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(holder):
                    target = node.func if isinstance(node, ast.Call) else None
                    if isinstance(target, ast.Name) and target.id == name:
                        key = str(path.relative_to(ROOT / "src" / "secretary"))
                        found.setdefault(key, []).append((holder.name, node.lineno))
        return found

    def test_only_the_named_modules_open_the_file_event_reader(self) -> None:
        """Anything else pages a file the installation's backend may never write."""
        built = self._constructions("EventJournal")
        self.assertEqual(
            sorted(set(built) - set(FILE_EVENT_READER_CONSTRUCTIONS)),
            [],
            "these modules build webproto.journal.EventJournal, which opens "
            "<data>/board/events.ndjson itself, instead of reading the audit owner of their card "
            "client (secretary.tasks.task_audit_for)",
        )
        self.assertEqual(sorted(built), sorted(FILE_EVENT_READER_CONSTRUCTIONS))

    def test_each_one_sits_in_the_function_that_selected_the_backend(self) -> None:
        """The allowance is the selection seam itself, not the module as a whole."""
        for module, (function, _reason) in FILE_EVENT_READER_CONSTRUCTIONS.items():
            with self.subTest(module=module):
                self.assertEqual(
                    sorted({holder for holder, _line in self._constructions("EventJournal")[module]}),
                    [function],
                )

    def test_the_read_layer_selects_its_event_reader_from_its_client(self) -> None:
        """Both card-event operations read the owner the client names, and neither a file by default."""
        source = (ROOT / "src" / "secretary" / "webproto" / "reads.py").read_text(encoding="utf-8")
        self.assertIn("task_audit_for(", source)
        self.assertIn("CommittedAudit(", source)
        for operation in ("def task_snapshot", "def task_events"):
            with self.subTest(operation=operation):
                body = source[source.index(operation) :]
                body = body[: body.index("\n    def ", 1)]
                self.assertNotIn("EventJournal(", body)

    def test_the_sql_reader_never_touches_a_path(self) -> None:
        """`CommittedAudit` has no data directory to read: it pages what its audit owner traverses."""
        from secretary.webproto.journal import CommittedAudit

        parameters = inspect.signature(CommittedAudit.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["audit", "backend"])
        source = inspect.getsource(CommittedAudit)
        for forbidden in ("open(", "Path(", "events.ndjson"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class CardBackendSwitchTests(unittest.TestCase):
    """The switch is acted on in one place, and every consumer goes through it."""

    def _constructions(self) -> dict[str, list[int]]:
        """Every `KanboardClient(...)` call in `src/secretary`, by module and line."""
        found: dict[str, list[int]] = {}
        for path in sorted((ROOT / "src" / "secretary").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Attribute) and target.attr == "for_instance":
                    target = target.value
                if isinstance(target, ast.Name) and target.id == "KanboardClient":
                    key = str(path.relative_to(ROOT / "src" / "secretary"))
                    found.setdefault(key, []).append(node.lineno)
        return found

    def test_only_the_named_kanboard_only_modules_build_a_kanboard_client(self) -> None:
        """Anything else names one backend where the installation names two."""
        offenders = sorted(set(self._constructions()) - set(KANBOARD_ONLY_CONSTRUCTIONS))
        self.assertEqual(
            offenders,
            [],
            "these modules build a Kanboard client directly instead of asking "
            "secretary.board.backend.board_client for the installation's own backend",
        )

    def test_every_named_kanboard_only_module_still_builds_one(self) -> None:
        """The allowance is a statement about live code, not a list that outlives its reasons."""
        built = self._constructions()
        self.assertEqual(sorted(built), sorted(KANBOARD_ONLY_CONSTRUCTIONS))

    def test_the_two_kanboard_only_consumers_say_so_in_their_own_source(self) -> None:
        """`bootstrap` and the importer are Kanboard-only by statement, not by default."""
        for module in ("bootstrap.py", "board/import_board.py"):
            source = (ROOT / "src" / "secretary" / module).read_text(encoding="utf-8")
            self.assertIn("Kanboard-only", source, module)


if __name__ == "__main__":
    unittest.main()
