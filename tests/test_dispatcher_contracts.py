"""Contract tests binding the dispatcher test doubles to the real runtime classes.

The dispatcher suite drives `DispatcherRuntime` against `FakeHost`/`FakeCatalog`/`FakeKanboard`
(tests/test_dispatcher.py). Those doubles stand in for `CommandHostRuntime`, `InstanceCatalog` and
`KanboardClient`. When a real class grew a method (`teardown`, `gate_check`) or changed a return
shape, the double had to be patched by hand, and every time it lagged the suite stayed green while
production broke. These tests make that drift a build failure.

Two layers:

surface  — the set of host/catalog attributes `DispatcherRuntime` and its helpers actually touch is
           discovered from the source (AST, not a hand-kept list), then required to exist on both
           the real class and the double with a call-compatible signature.
behaviour — the real host is run in `mode="noop"`, which needs no orca/git/gh, and its results are
           compared to the double's for the same call. That pins return shapes (the keys of
           `prepare_worker`, the `GateResult` of `gate_check`) rather than just method names.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import textwrap
import unittest
from pathlib import Path

from secretary import dispatcher as dispatcher_module
from secretary import dispatcher_production, dispatcher_review, tasks as tasks_module
from secretary.dispatcher import CommandHostRuntime, DispatcherRuntime, InstanceCatalog
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_state import DispatcherRecord
from secretary.tasks import KanboardClient

from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard

# Modules that reach through a runtime into the host/catalog collaborators.
_RUNTIME_MODULES = (dispatcher_module, dispatcher_production, dispatcher_review)

# Attribute owners as they are spelled at the call sites: `self.host` inside DispatcherRuntime,
# `runtime.host` / `host.` inside the extracted helper modules.
_RUNTIME_HOLDERS = ("self", "runtime")


def _runtime_trees() -> list[ast.AST]:
    """Sources that drive a runtime's collaborators. dispatcher.py is narrowed to the
    `DispatcherRuntime` class: `CommandHostRuntime` also holds a `self.catalog`, but that is the
    host's own use of the real catalog, not the surface the doubles have to cover."""
    trees: list[ast.AST] = []
    for module in _RUNTIME_MODULES:
        tree = ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))
        if module is dispatcher_module:
            trees.extend(
                node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "DispatcherRuntime"
            )
            continue
        trees.append(tree)
    return trees


def _used_attributes(collaborator: str) -> set[str]:
    """Every attribute the dispatcher sources read off `<runtime>.<collaborator>`."""
    used: set[str] = set()
    for tree in _runtime_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
                continue
            inner = node.value
            if inner.attr != collaborator:
                continue
            if isinstance(inner.value, ast.Name) and inner.value.id in _RUNTIME_HOLDERS:
                used.add(node.attr)
    return used


def _signature(func) -> list[tuple[str, str, bool]]:
    """(name, kind, has-default) per parameter, ignoring `self` and annotations."""
    parameters = list(inspect.signature(func).parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    return [(p.name, str(p.kind), p.default is not inspect.Parameter.empty) for p in parameters]


class HostSurfaceContractTests(unittest.TestCase):
    """`FakeHost` must accept every call the runtime makes on the real host."""

    def test_runtime_host_calls_are_discovered(self) -> None:
        # Guard the AST scan itself: a rename of the holder attribute would otherwise silently
        # reduce the contract to the empty set and pass vacuously.
        used = _used_attributes("host")
        self.assertIn("gate_check", used)
        self.assertIn("teardown", used)
        self.assertIn("prepare_worker", used)

    def test_fake_host_covers_the_real_host_surface(self) -> None:
        for name in sorted(_used_attributes("host")):
            with self.subTest(method=name):
                self.assertTrue(
                    hasattr(CommandHostRuntime, name),
                    f"DispatcherRuntime calls host.{name}, missing on CommandHostRuntime",
                )
                self.assertTrue(
                    hasattr(FakeHost, name),
                    f"DispatcherRuntime calls host.{name}, missing on FakeHost",
                )
                self.assertEqual(
                    _signature(getattr(CommandHostRuntime, name)),
                    _signature(getattr(FakeHost, name)),
                    f"FakeHost.{name} signature drifted from CommandHostRuntime.{name}",
                )

    def test_fake_catalog_covers_the_real_catalog_surface(self) -> None:
        # The gate reads the catalog through the host, so its calls count as runtime usage too.
        used = _used_attributes("catalog") | {"adapter", "binding", "default_branch"}
        for name in sorted(used):
            with self.subTest(method=name):
                self.assertTrue(
                    hasattr(InstanceCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on InstanceCatalog",
                )
                self.assertTrue(
                    hasattr(FakeCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on FakeCatalog",
                )
                self.assertEqual(
                    _signature(getattr(InstanceCatalog, name)),
                    _signature(getattr(FakeCatalog, name)),
                    f"FakeCatalog.{name} signature drifted from InstanceCatalog.{name}",
                )

    def test_host_internal_catalog_calls_exist_on_the_real_catalog(self) -> None:
        """`CommandHostRuntime` drives the catalog itself when launching a head. No double covers
        that path (the launcher tests use the real classes), but a rename must still fail here."""
        tree = ast.parse(Path(inspect.getsourcefile(dispatcher_module)).read_text(encoding="utf-8"))
        host_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CommandHostRuntime"
        )
        used = {
            node.attr
            for node in ast.walk(host_class)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "catalog"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        }
        self.assertIn("head_launch", used)
        for name in sorted(used):
            with self.subTest(method=name):
                self.assertTrue(hasattr(InstanceCatalog, name), f"catalog.{name} is gone")

    def test_runtime_collaborators_are_named_host_and_catalog(self) -> None:
        # The scan keys off those attribute names; a rename must break here, loudly.
        parameters = inspect.signature(DispatcherRuntime.__init__).parameters
        self.assertIn("host", parameters)
        self.assertIn("catalog", parameters)


class HostBehaviourContractTests(unittest.TestCase):
    """Same call, real host in noop mode vs FakeHost: the results must have the same shape."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.real = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="noop")  # type: ignore[arg-type]
        self.fake = FakeHost(self.root / "fake")
        (self.root / "fake").mkdir(parents=True, exist_ok=True)

    def _task(self) -> dict:
        return {
            "ref": "secretary-635",
            "project": "secretary",
            "description": "contract",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, workspace: str) -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-635-worker",
            workspace=workspace,
            handle="term:1",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def test_prepare_worker_returns_the_same_keys(self) -> None:
        task = self._task()
        real = self.real.prepare_worker(task, "w1", "codex", attempt_id="attempt-1")
        fake = self.fake.prepare_worker(task, "w1", "codex", attempt_id="attempt-1")
        self.assertEqual(sorted(real), sorted(fake))
        for key in real:
            self.assertIsInstance(fake[key], type(real[key]), f"prepare_worker[{key!r}] type drift")

    def test_gate_check_returns_a_gate_result(self) -> None:
        task = self._task()
        record = self._record(str(self.root / "fake" / "w1"))
        real = self.real.gate_check(task, record)
        fake = self.fake.gate_check(task, record)
        self.assertIsInstance(real, GateResult)
        self.assertIsInstance(fake, GateResult)
        self.assertEqual(real.status, "green")
        self.assertEqual(fake.status, "green")

    def test_restore_workspace_returns_a_path_string(self) -> None:
        task = self._task()
        self.assertIsInstance(self.real.restore_workspace(task, "w1"), str)
        self.assertIsInstance(self.fake.restore_workspace(task, "w1"), str)

    def test_void_methods_stay_void(self) -> None:
        task = self._task()
        record = self._record(str(self.root / "fake" / "w1"))
        self.assertIsNone(self.real.verify_worker_result(task, record))
        self.assertIsNone(self.fake.verify_worker_result(task, record))
        self.assertIsNone(self.real.complete_green(task, record))
        self.assertIsNone(self.fake.complete_green(task, record))
        self.assertIsNone(self.real.stop(record))
        self.assertIsNone(self.fake.stop(record))
        self.assertIsNone(self.real.teardown(record))
        self.assertIsNone(self.fake.teardown(record))

    def test_teardown_stops_the_terminals_first(self) -> None:
        """Real teardown is stop() plus `orca worktree rm`; the fake must record the stop too, or a
        runtime that forgot to stop terminals before removing the worktree would look correct."""
        stops: list[str] = []
        real = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]
        real._run_json = lambda args: stops.append(args[1]) or {}  # type: ignore[assignment]
        record = self._record(str(self.root / "fake" / "w1"))
        real.teardown(record)
        self.assertEqual(stops, ["terminal", "worktree"])

        self.fake.teardown(record)
        self.assertEqual(self.fake.stopped, [record.worker])
        self.assertEqual(self.fake.torn_down, [record.worker])

    def test_stop_and_teardown_swallow_host_errors(self) -> None:
        """Both are best-effort cleanups on paths that must still reach the board move. A raising
        stop() would abort `_finish_green` before the card ever moves to Done."""
        real = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="real")  # type: ignore[arg-type]

        def boom(args):
            raise dispatcher_module.HostError("orca is down")

        real._run_json = boom  # type: ignore[assignment]
        record = self._record(str(self.root / "fake" / "w1"))
        self.assertIsNone(real.stop(record))
        self.assertIsNone(real.teardown(record))


class KanboardContractTests(unittest.TestCase):
    def test_fake_call_signature_matches_the_real_client(self) -> None:
        self.assertEqual(_signature(KanboardClient.call), _signature(FakeKanboard.call))

    def test_fake_only_answers_methods_the_real_code_calls(self) -> None:
        """A branch for an RPC method nothing calls is a fake that has drifted away from the
        protocol; the reverse direction already fails loudly through the fake's AssertionError."""
        source = Path(inspect.getsourcefile(tasks_module)).read_text(encoding="utf-8")
        called = {
            node.args[0].value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        self.assertIn("getAllTasks", called)
        handled = _handled_rpc_methods(FakeKanboard.call)
        self.assertTrue(handled)
        self.assertEqual(
            sorted(handled - called),
            [],
            "FakeKanboard answers RPC methods secretary/tasks.py never calls",
        )


def _handled_rpc_methods(func) -> set[str]:
    """String literals the fake dispatches on in `if method == "...":`."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {
        comparator.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "method"
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    }


class RuntimeWiringContractTests(unittest.TestCase):
    def test_production_unit_puts_local_bin_on_path(self) -> None:
        """The dispatcher shells out to `orca` by bare name. systemd starts with a minimal PATH, so
        the unit must carry one that contains the install dir, or every host call fails at runtime
        while the whole unit test suite stays green."""
        unit = Path(__file__).resolve().parents[1] / "packaging" / "systemd" / "secretary-dispatcher-production.service"
        lines = [
            line.split("=", 1)[1]
            for line in unit.read_text(encoding="utf-8").splitlines()
            if line.startswith("Environment=PATH=")
        ]
        self.assertEqual(len(lines), 1, "the production unit must declare exactly one PATH")
        self.assertIn("/home/dev/.local/bin", lines[0].split("PATH=", 1)[1])


if __name__ == "__main__":
    unittest.main()
