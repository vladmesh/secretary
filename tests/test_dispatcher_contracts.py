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
import os
import pwd
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher as dispatcher_module
from secretary import (
    dispatcher_observer,
    dispatcher_production,
    dispatcher_review,
    tasks as tasks_module,
)
from secretary.dispatcher import CommandHostRuntime, DispatcherRuntime, InstanceCatalog
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_launcher import HeadLaunchError, wrap_role_shell_command
from secretary.role_env import observer_binding
from secretary import role_env as head_role_env
from secretary.dispatcher_state import DispatcherRecord
from secretary import upgrade
from secretary.head_registry import canonical_heads, materialize_snapshot
from secretary.host import SHIPPED_PACKAGING_ROOT, SystemdLayout, render_systemd_unit
from secretary.host_apply import resolve_packaged
from triggered_agents.agents.pipeline import heads
from triggered_agents.runtime import dispatch, role_env
from secretary.tasks import KanboardClient

from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard

# Modules that reach through a runtime into the host/catalog collaborators.
_RUNTIME_MODULES = (
    dispatcher_module,
    dispatcher_production,
    dispatcher_review,
    dispatcher_observer,
)

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


def _declares(cls, name: str) -> bool:
    """Plain data attributes (`catalog.instance_dir`) are part of the surface too.

    They are assigned in `__init__` rather than declared on the class, so `hasattr`
    alone would read them as missing on the real class and as a drift on the double.
    """
    if hasattr(cls, name):
        return True
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        for node in ast.walk(tree)
    )


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
                    _declares(InstanceCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on InstanceCatalog",
                )
                self.assertTrue(
                    _declares(FakeCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on FakeCatalog",
                )
                if not callable(getattr(InstanceCatalog, name, None)):
                    continue
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

    def test_prepare_observer_returns_the_same_keys(self) -> None:
        sprint = {"ref": "sprint:1", "goal": "g", "definition_of_done": "d", "repositories": []}
        real = self.real.prepare_observer(sprint, "codex-observer", prompt="# Sprint sprint:1\n")
        fake = self.fake.prepare_observer(sprint, "codex-observer", prompt="# Sprint sprint:1\n")
        self.assertEqual(sorted(real), sorted(fake))
        for key in real:
            self.assertIsInstance(fake[key], type(real[key]), f"prepare_observer[{key!r}] type drift")
        # Both write the sprint document the head opens at the workspace root.
        for result in (real, fake):
            self.assertTrue((Path(result["workspace"]) / "SPRINT.md").is_file())

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
        product_root = Path(__file__).resolve().parents[1]
        unit = product_root / "packaging" / "systemd" / "secretary-dispatcher-production.service"
        rendered = render_systemd_unit(
            unit.read_bytes(),
            SystemdLayout(
                product_root,
                Path("/srv/secretary-instance"),
                Path("/srv/secretary-data"),
                "operator",
                Path("/home/operator"),
            ),
        )
        lines = [
            line.split("=", 1)[1]
            for line in rendered.decode("utf-8").splitlines()
            if line.startswith("Environment=PATH=")
        ]
        self.assertEqual(len(lines), 1, "the production unit must declare exactly one PATH")
        self.assertIn("/home/operator/.local/bin", lines[0].split("PATH=", 1)[1])


class HeadRegistrySourceContractTests(unittest.TestCase):
    """The live catalog reads the installation's registry, never the checkout it was imported from.

    The production unit starts the dispatcher out of the working checkout, so comparing the live
    registry against that checkout made an unmerged `heads.toml` commit stop every tick.
    """

    def instance(self, root: Path, snapshot: str) -> Path:
        (root / "instance.yaml").write_text(
            "version: 1\nname: contract\ndata_dir: " + str(root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            + "host:\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        (root / "heads").mkdir()
        (root / "heads" / "heads.yaml").write_text(snapshot, encoding="utf-8")
        return root

    def snapshot(self, *, role_default: str = "installed-head") -> str:
        return (
            "resources:\n  installed-resource:\n    account: installed-account\n"
            "profiles:\n  installed-head:\n    resource: installed-resource\n    adapter: claude\n"
            f"role_defaults:\n  new_card: {role_default}\n"
        )

    def test_catalog_runs_off_the_installation_snapshot_not_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.snapshot())

            catalog = InstanceCatalog(instance)

            # Deliberately a registry the product canon does not contain: had the catalog compared
            # the snapshot against this checkout, construction would have raised `invalid_heads`.
            self.assertEqual(catalog.head_profile("installed-head")["adapter"], "claude")
            self.assertEqual(catalog.worker_head({}), "installed-head")

    def test_a_broken_installation_snapshot_still_stops_the_tick_by_name(self) -> None:
        broken = {
            "missing table": "profiles:\n  installed-head:\n    adapter: claude\n",
            "unknown resource": (
                "resources: {}\nprofiles:\n  installed-head:\n    resource: gone\n    adapter: claude\n"
                "role_defaults:\n  new_card: installed-head\n"
            ),
            "unrouted role": self.snapshot(role_default="not-a-head"),
        }
        for name, snapshot in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), snapshot)

                with self.assertRaises(dispatcher_module.DispatcherError) as caught:
                    InstanceCatalog(instance)

                self.assertEqual(caught.exception.code, "invalid_heads")
                self.assertIn("heads.yaml", str(caught.exception))


class RoleRoutingGenerationTests(unittest.TestCase):
    """One registry generation routes all six roles, whichever process is asking.

    The dispatcher routes worker, reviewer and observer off the installation snapshot; the
    background agents' driver resolves curator, retro and steward through the pipeline registry.
    Those used to be two different files — the checkout's `heads.toml` and the installation's
    `heads.yaml` — so a role could be routed by one generation and launched by another.
    """

    CANON = (
        "[resources.owned-sub]\naccount = \"owned\"\nprobe = \"true\"\n"
        "[profiles.owned-worker]\nresource = \"owned-sub\"\nadapter = \"claude\"\nfallback = []\n"
        "[profiles.owned-reviewer]\nresource = \"owned-sub\"\nadapter = \"claude\"\nfallback = []\n"
        "[profiles.owned-observer]\nresource = \"owned-sub\"\nadapter = \"claude\"\nfallback = []\n"
        "[profiles.owned-watcher]\nresource = \"owned-sub\"\nadapter = \"claude\"\nfallback = []\n"
        "[role_defaults]\nnew_card = \"owned-worker\"\nreviewer = \"owned-reviewer\"\n"
        "observer = \"owned-observer\"\ncurator = \"owned-watcher\"\nretro = \"owned-watcher\"\n"
        "steward = \"owned-watcher\"\n"
    )

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name)
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: routing\ndata_dir: " + str(self.instance / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            + "host:\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        (self.instance / "heads").mkdir()
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def materialize(self, canon: str | None) -> None:
        if canon is not None:
            (self.instance / "heads" / "heads.toml").write_text(canon, encoding="utf-8")
        materialize_snapshot(self.instance, upgrade.running_product_root())

    def test_the_installations_own_canon_routes_every_role(self) -> None:
        self.materialize(self.CANON)
        catalog = InstanceCatalog(self.instance)

        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)}):
            self.assertEqual(heads.registry_path(), self.instance / "heads" / "heads.yaml")
            self.assertEqual(catalog.worker_head({}), "owned-worker")
            self.assertEqual(catalog.review_head({}), "owned-reviewer")
            self.assertEqual(catalog.observer_head(), "owned-observer")
            for agent in ("curator", "retro", "steward"):
                with self.subTest(agent=agent):
                    spec = {"head": "product-static-head"}
                    self.assertEqual(dispatch._preferred_head(agent, spec), "owned-watcher")

    def test_an_installation_with_no_canon_runs_the_product_defaults(self) -> None:
        self.materialize(None)
        product = canonical_heads(upgrade.running_product_root())["role_defaults"]
        catalog = InstanceCatalog(self.instance)

        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)}):
            self.assertEqual(catalog.worker_head({}), product["new_card"])
            self.assertEqual(catalog.review_head({}), product["reviewer"])
            self.assertEqual(catalog.observer_head(), product["observer"])
            for agent in ("curator", "retro", "steward"):
                with self.subTest(agent=agent):
                    self.assertEqual(dispatch._preferred_head(agent, {}), product[agent])

    def test_a_checkout_with_no_installation_reads_the_shipped_registry(self) -> None:
        """No `SECRETARY_INSTANCE` means no installation to read — never the host's own."""
        env = {key: value for key, value in os.environ.items() if key != "SECRETARY_INSTANCE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(heads.registry_path(), heads.HEADS_TOML)

    def test_claude_effort_is_validated_and_rendered_by_shared_launcher(self) -> None:
        resources = {"claude-sub": {"account": "subscription"}}
        profiles = {
            "opus-medium": {
                "resource": "claude-sub", "adapter": "claude", "model": "opus",
                "effort": "medium", "fallback": [],
            }
        }
        heads.validate_registry(resources, profiles)
        registry = heads.Registry(resources, profiles)

        command = heads.render_command(
            "opus-medium", role="reviewer", prompt="review", registry=registry
        )

        self.assertIn("--model opus --effort medium", command)
        with self.assertRaisesRegex(heads.HeadRegistryError, "unknown claude effort"):
            heads.validate_registry(
                resources,
                {"bad": {**profiles["opus-medium"], "effort": "unbounded"}},
            )

    def test_a_selected_installation_without_a_usable_snapshot_fails_by_its_path(self) -> None:
        """The product default is for no installation at all, not for a broken one.

        Falling back here would route a packaged role off whichever checkout the process happens
        to run from — the mutable file the snapshot exists to keep out of a live tick — and would
        abandon the instance the operator selected without saying so.
        """
        for name, build in (
            ("missing", lambda snapshot: None),
            ("directory", lambda snapshot: snapshot.mkdir()),
            ("dangling", lambda snapshot: snapshot.symlink_to(snapshot.parent / "gone.yaml")),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                (instance / "heads").mkdir()
                (instance / "heads" / "heads.toml").write_text(self.CANON, encoding="utf-8")
                snapshot = instance / "heads" / "heads.yaml"
                build(snapshot)

                with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(instance)}):
                    heads._load_registry.cache_clear()
                    self.assertEqual(heads.registry_path(), snapshot)
                    with self.assertRaises(heads.HeadRegistryError) as caught:
                        heads.load_registry()

                self.assertIn(str(snapshot), str(caught.exception))


class PackagedRoleUnitInstanceTests(unittest.TestCase):
    """Every packaged role process resolves the instance its unit was rendered for.

    A role that is handed no `SECRETARY_INSTANCE` falls back to the default installation path, so
    on a host with a real installation a unit rendered for another instance would quietly route
    off `~/secretary-instance`'s heads instead of its own.
    """

    UNITS = (
        "secretary-dispatcher-production.service",
        "secretary-curator.service",
        "secretary-retro.service",
        "secretary-steward.service",
        "secretary-steward-deep-sweep.service",
    )

    # The layout resolves a home directory through `pwd`, so the account has to exist wherever
    # the suite runs. The invoking account is the one guaranteed to.
    RUNTIME_USER = pwd.getpwuid(os.getuid()).pw_name

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Deliberately not the default instance path: the assertion below is only worth anything
        # if the wrong answer is a different directory.
        self.instance = self.root / "other-instance"
        (self.instance / "heads").mkdir(parents=True)
        (self.instance / "heads" / "heads.toml").write_text(
            RoleRoutingGenerationTests.CANON, encoding="utf-8"
        )
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        (self.instance / "runtime.env").write_text(
            "KANBOARD_URL=https://board.invalid/jsonrpc.php\n"
            "KANBOARD_API_USER=svc\nKANBOARD_API_TOKEN=secret\n",
            encoding="utf-8",
        )
        materialize_snapshot(self.instance, upgrade.running_product_root())
        self.layout = SystemdLayout(
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.root / "data",
            runtime_user=self.RUNTIME_USER,
            runtime_home=self.root / "home",
        )
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def unit_env(self, name: str) -> dict[str, str]:
        rendered = render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / name).read_bytes(), self.layout
        ).decode()
        env = {}
        for line in rendered.splitlines():
            if line.startswith("Environment="):
                key, value = line[len("Environment="):].split("=", 1)
                env[key] = value
        return env

    def test_every_role_unit_carries_the_rendered_instance_path(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                self.assertEqual(self.unit_env(name)["SECRETARY_INSTANCE"], str(self.instance))

    def test_the_units_apply_would_install_carry_it_too(self) -> None:
        """Through `resolve_packaged`, the compile step `reconcile apply` actually installs from."""
        packaged = resolve_packaged(
            {"host": {"unit_prefix": "secretary-"}, "data_dir": str(self.root / "data")},
            SHIPPED_PACKAGING_ROOT,
            product_root=self.root / "product",
            instance_path=self.instance,
            runtime_user=self.RUNTIME_USER,
            orca_executable=Path("/usr/local/bin/orca"),
        )
        compiled = {unit.name: unit.content for unit in packaged}

        self.assertLessEqual(set(self.UNITS), set(compiled))
        for name in self.UNITS:
            with self.subTest(unit=name):
                self.assertIn(
                    f"Environment=SECRETARY_INSTANCE={self.instance}".encode(), compiled[name]
                )

    def test_the_role_process_resolves_that_instances_registry(self) -> None:
        """Through role_env, the way the unit actually starts the process."""
        for name, role in (("secretary-curator.service", "curator"),
                           ("secretary-retro.service", "retro"),
                           ("secretary-steward.service", "steward"),
                           ("secretary-steward-deep-sweep.service", "steward"),
                           ("secretary-dispatcher-production.service", "pipeline")):
            with self.subTest(unit=name):
                env = role_env.runtime_env(
                    role, base_env=self.unit_env(name),
                    env_file=self.instance / "runtime.env", require=True,
                )

                with mock.patch.dict(os.environ, env, clear=True):
                    heads._load_registry.cache_clear()
                    self.assertEqual(
                        heads.registry_path(), self.instance / "heads" / "heads.yaml"
                    )
                    self.assertEqual(heads.default_head(), "owned-worker")
                    self.assertEqual(heads.reviewer_head(), "owned-reviewer")

    def test_every_role_unit_names_the_runtime_env_file_of_its_own_instance(self) -> None:
        """A role that reloads runtime.env has to reload the selected installation's copy.

        `EnvironmentFile=` alone only seeds the unit's own process. Both role-env modules resolve
        the file again for the process they launch, from a name that defaults to the default
        installation, so the unit has to export it.
        """
        expected = str(self.instance / "runtime.env")
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = self.unit_env(name)
                if name == "secretary-dispatcher-production.service":
                    self.assertEqual(env["SECRETARY_RUNTIME_ENV_FILE"], expected)
                self.assertEqual(env["TA_RUNTIME_ENV_FILE"], expected)

    def test_the_runtime_env_file_cannot_hand_a_role_another_instance(self) -> None:
        """The launcher's binding wins over the file's own line, in both role-env modules."""
        self.decoy_runtime_env()
        for module, role in ((head_role_env, "worker"), (role_env, "steward")):
            with self.subTest(module.__name__):
                base = self.unit_env("secretary-dispatcher-production.service")
                env = module.runtime_env(
                    role, base_env=base, env_file=self.instance / "runtime.env", require=True
                )

                self.assertEqual(env["SECRETARY_INSTANCE"], str(self.instance))

    def test_every_background_unit_carries_the_product_root_it_was_rendered_for(self) -> None:
        """An upgrade from an alternate checkout renders these units against that checkout.

        Nothing else tells a launched process which product to import: the gate resolves
        ``$HOME/secretary`` when neither name is set, which on an upgraded host is the checkout
        the installation was moved off. Every one of these services launches something further —
        heads through the dispatcher, task commands and curator memory writes through the deferred
        product lookup — so the binding has to be on the unit, not only on the dispatcher.
        """
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = self.unit_env(name)

                self.assertEqual(env["TA_SECRETARY_REPO"], str(self.root / "product"))
                # The gate-launched services also import from it directly; the dispatcher runs the
                # checkout's own venv entry point and needs no PYTHONPATH of its own.
                self.assertEqual(
                    env.get("TA_RUNTIME_PYTHONPATH", str(self.root / "product")),
                    str(self.root / "product"),
                )

    def test_the_launched_head_command_names_that_checkout_rather_than_a_home(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                **self.unit_env("secretary-dispatcher-production.service"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root / "home"),
            },
            clear=True,
        ):
            command = wrap_role_shell_command("worker", "true")

        self.assertIn(f"TA_SECRETARY_REPO={self.root / 'product'}", command)
        self.assertIn(f"PYTHONPATH={self.root / 'product'}", command)
        self.assertNotIn("$HOME/secretary", command)

    def test_a_dispatcher_launched_head_imports_the_checkout_the_unit_named(self) -> None:
        """End to end, in a terminal that has no ``TA_SECRETARY_REPO`` of its own.

        The rendered checkout has to be the real one here, because the wrapper runs
        ``secretary.role_env`` out of it; the layout points at it the way an alternate upgrade
        would, and the launching shell is given a home where no checkout exists at all.
        """
        product = Path(__file__).resolve().parents[1]
        layout = SystemdLayout(
            product_root=product,
            instance_path=self.instance,
            data_dir=self.root / "data",
            runtime_user=self.RUNTIME_USER,
            runtime_home=self.root / "home",
        )
        rendered = render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / "secretary-dispatcher-production.service").read_bytes(),
            layout,
        ).decode()
        unit_env = dict(
            line[len("Environment="):].split("=", 1)
            for line in rendered.splitlines()
            if line.startswith("Environment=")
        )
        with mock.patch.dict(
            os.environ, {**unit_env, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, clear=True
        ):
            command = wrap_role_shell_command("worker", "printenv TA_SECRETARY_REPO")

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True, text=True, timeout=120,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root / "home"),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(product), result.stderr)

    def test_a_dispatcher_launched_head_resolves_the_selected_instance(self) -> None:
        """End to end through the rendered command, the way a head actually starts.

        Orca creates the head's terminal, so it inherits nothing the dispatcher unit exported:
        the binding has to travel inside the command string. The subprocess here is given an
        environment with no `SECRETARY_*` in it at all, which is what the reviewer's reproduction
        found routing heads back to `/home/dev/secretary-instance`.
        """
        self.decoy_runtime_env()
        bound = self.unit_env("secretary-dispatcher-production.service")
        bound["TA_SECRETARY_REPO"] = str(Path(__file__).resolve().parents[1])
        with mock.patch.dict(os.environ, bound, clear=True):
            command = wrap_role_shell_command("worker", "printenv SECRETARY_INSTANCE")

        # The role wrapper starts in a worktree.  A package there must not shadow the selected
        # control plane merely because Python's default path would put the cwd first.
        shadow = self.root / "secretary"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise RuntimeError('shadow package imported')\n")

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True, text=True, timeout=120,
            cwd=self.root,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root),
                "TA_SECRETARY_REPO": str(Path(__file__).resolve().parents[1]),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.instance), result.stderr)

    def decoy_runtime_env(self) -> None:
        """The selected instance's runtime.env, carrying the default installation's own name.

        A copied or inherited `SECRETARY_INSTANCE` line is exactly how a non-default installation
        loses its heads, so the fixture keeps one in the file the roles read.
        """
        (self.instance / "runtime.env").write_text(
            "KANBOARD_URL=https://board.invalid/jsonrpc.php\n"
            "KANBOARD_API_USER=svc\nKANBOARD_API_TOKEN=secret\n"
            "SECRETARY_INSTANCE=/home/dev/secretary-instance\n"
            "SECRETARY_OBSERVER_SPRINT=sprint:somebody-else\n"
            "SECRETARY_OBSERVER_GENERATION=forged\n",
            encoding="utf-8",
        )

    def test_an_observer_head_carries_the_sprint_the_launcher_bound_it_to(self) -> None:
        """End to end, against a `runtime.env` claiming another sprint.

        The identity travels in the command the launcher renders, and `runtime_env` treats it the
        way it treats the installation: a file inside the installation may not rename the caller,
        or a copied line would let a head sign for a sprint it was never launched for.
        """
        self.decoy_runtime_env()
        identity = observer_binding("sprint:1126", "abc123def456")
        bound = self.unit_env("secretary-dispatcher-production.service")
        bound["TA_SECRETARY_REPO"] = str(Path(__file__).resolve().parents[1])
        with mock.patch.dict(os.environ, bound, clear=True):
            command = wrap_role_shell_command(
                "observer",
                "printenv SECRETARY_OBSERVER_SPRINT; printenv SECRETARY_OBSERVER_GENERATION",
                identity=identity,
            )

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True, text=True, timeout=120,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root),
                "TA_SECRETARY_REPO": str(Path(__file__).resolve().parents[1]),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["sprint:1126", "abc123def456"], result.stderr)

    def test_a_worker_head_is_never_given_an_observer_binding(self) -> None:
        """The binding is one role's, and a launcher asking for it elsewhere is a defect."""
        with self.assertRaisesRegex(HeadLaunchError, "SECRETARY_OBSERVER_SPRINT"):
            wrap_role_shell_command(
                "worker", "true", identity=observer_binding("sprint:1126", "abc123def456"),
            )


if __name__ == "__main__":
    unittest.main()
