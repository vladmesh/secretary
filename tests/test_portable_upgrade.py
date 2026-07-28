"""Upgrading an installation from a product checkout that is not the running one.

Every fixture here is built from nothing: a second product checkout with its own skill manifest,
head canon and unit templates, a home directory this test owns, and an instance that has never been
upgraded. Nothing under the developing machine's home or inside the checkout that runs these tests
may reach the result, because the whole point of the materializer is that another checkout can
install a host without the running one having a say.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import installation, role_skills, upgrade
from secretary.cli import main as cli_main
from secretary.config import validate_instance
from secretary.head_registry import (
    INSTANCE_ORIGIN,
    PRODUCT_ORIGIN,
    canonical_path,
    read_source,
    snapshot_path,
)

from tests.test_upgrade import FakeRegistrar, FakeUnitInstaller

UNIT_PREFIX = "secretary-"
# Read at import, before any fixture patches the home or the account database: these are the two
# places a portable run is not allowed to reach.
LIVE_HOME = str(Path.home())
RUNNING_CHECKOUT = str(Path(upgrade.__file__).resolve().parents[1])

# A canon small enough to read and complete enough to validate. The two fixtures below differ in
# which file carries it, which is the whole question the head-registry step answers.
PRODUCT_CANON = """
[resources.portable-sub]
account = "portable"
probe = "true"

[profiles.portable-head]
resource = "portable-sub"
adapter = "claude"
fallback = []

[profiles.portable-reviewer]
resource = "portable-sub"
adapter = "claude"
fallback = []

[role_defaults]
new_card = "portable-head"
reviewer = "portable-reviewer"
curator = "portable-head"
retro = "portable-head"
steward = "portable-head"
observer = "portable-reviewer"
"""

INSTANCE_CANON = PRODUCT_CANON.replace("portable", "owned")

SERVICE = """[Unit]
Description=Portable {component}

[Service]
Type=simple
User={{{{SECRETARY_RUNTIME_USER}}}}
WorkingDirectory={{{{SECRETARY_PRODUCT_ROOT}}}}
Environment=SECRETARY_INSTANCE={{{{SECRETARY_INSTANCE_PATH}}}}
Environment=SECRETARY_DATA_DIR={{{{SECRETARY_DATA_DIR}}}}
ExecStart={{{{SECRETARY_RUNTIME_HOME}}}}/.local/bin/secretary-{component}

[Install]
WantedBy=default.target
"""

TIMER = """[Unit]
Description=Portable {component} timer

[Timer]
OnCalendar=hourly
Unit=secretary-{component}.service

[Install]
WantedBy=timers.target
"""

MANIFEST = """
[roles.secretary]
skills = ["portable-skill"]

[targets.codex-portable]
shell = "codex"
root = "~/shells/codex/skills"
roles = ["secretary"]

[targets.claude-portable]
shell = "claude"
root = "~/shells/claude/skills"
roles = ["secretary"]
"""

OVERLAY = """
[roles.secretary]
skills = ["owned-skill"]
"""


class RecordingUnits(FakeUnitInstaller):
    """A systemd double that keeps the fixture host's inventory honest.

    `verify` re-plans against the host the run just wrote, so a fixture whose unit list never
    moves would report every install the run performed as still pending.
    """

    def __init__(self, fixture: Path) -> None:
        super().__init__()
        self.fixture = fixture
        self._publish()

    def _publish(self) -> None:
        (self.fixture / "units.txt").write_text(
            "".join(f"{name}\n" for name in sorted(self.files)), encoding="utf-8"
        )

    def install(self, unit) -> None:
        super().install(unit)
        self._publish()

    def remove(self, name: str) -> None:
        super().remove(name)
        self._publish()


class FakeAutomations:
    """The Orca automation client, with nothing registered and nothing reachable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list(self) -> list[dict[str, object]]:
        return []

    def run(self, argv: list[str], label: str) -> None:
        self.calls.append(label)


class PortableFixture(unittest.TestCase):
    """A clean installation materialized entirely from a checkout this test wrote."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Two homes, because they are two accounts. `home` belongs to the installation being
        # materialized; `invoker_home` is whoever typed the command — an operator repairing
        # somebody else's installation, or root after a recovery. Everything an upgrade writes
        # belongs in the first, and a step that reads the process environment lands in the second,
        # where these tests can see it.
        self.home = self.root / "home"
        self.invoker_home = self.root / "invoker"
        self.home.mkdir()
        self.invoker_home.mkdir()
        self.product = self.root / "product"
        self.instance = self.root / "instance"
        self.data = self.root / "data"
        self.host_fixture = self.root / "host"
        for path in (self.instance, self.data, self.host_fixture):
            path.mkdir()
        self.units = RecordingUnits(self.host_fixture)
        self.write_product()
        self.write_instance()
        # A fully replaced environment: an inherited SECRETARY_INSTANCE, TA_* or KANBOARD_* would
        # point some part of the run back at the live installation, which is exactly the failure
        # this fixture exists to rule out.
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.invoker_home), "PATH": os.environ.get("PATH", "")},
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        account = SimpleNamespace(pw_dir=str(self.home), pw_name="operator")
        for target, kwargs in (
            ("secretary.host_apply.pwd.getpwnam", {"return_value": account}),
            ("secretary.host_apply.pwd.getpwuid", {"return_value": account}),
            ("secretary.host_apply.find_orca_executable", {"return_value": Path("/usr/local/bin/orca")}),
            ("secretary.host_apply._is_executable", {"return_value": True}),
        ):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    # --- fixture construction -------------------------------------------------------------

    def write_product(self) -> None:
        manifest = self.product / "skills" / "manifest.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(MANIFEST, encoding="utf-8")
        skill = manifest.parent / "roles" / "secretary" / "portable-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# portable-skill\n", encoding="utf-8")
        (skill / "portable-skill.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        canon = self.product / "triggered_agents" / "agents" / "pipeline" / "heads.toml"
        canon.parent.mkdir(parents=True)
        canon.write_text(PRODUCT_CANON, encoding="utf-8")
        packaging = self.product / "packaging" / "systemd"
        packaging.mkdir(parents=True)
        for component in ("memory", "dispatcher-production"):
            (packaging / f"secretary-{component}.service").write_text(
                SERVICE.format(component=component), encoding="utf-8"
            )
        (packaging / "secretary-dispatcher-production.timer").write_text(
            TIMER.format(component="dispatcher-production"), encoding="utf-8"
        )
        # The non-secret codex runtime files an install seeds into the owner's managed CODEX_HOME.
        # A checkout without them is not one an install can materialize, so the fixture ships them.
        codex_home = self.product / "packaging" / "codex-home"
        codex_home.mkdir(parents=True)
        (codex_home / "AGENTS.md").write_text("# portable\n", encoding="utf-8")
        (codex_home / "config.toml").write_text("[portable]\n", encoding="utf-8")

    def write_instance(self) -> None:
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: portable\ndata_dir: "
            + str(self.data)
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\n"
            + f"host:\n  unit_prefix: {UNIT_PREFIX}\n",
            encoding="utf-8",
        )

    def own_a_canon(self) -> Path:
        canon = self.instance / "heads" / "heads.toml"
        canon.parent.mkdir(parents=True, exist_ok=True)
        canon.write_text(INSTANCE_CANON, encoding="utf-8")
        return canon

    def own_a_skill(self) -> Path:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(OVERLAY, encoding="utf-8")
        skill = overlay.parent / "roles" / "secretary" / "owned-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# owned-skill\n", encoding="utf-8")
        return overlay

    # --- driving the materializer ---------------------------------------------------------

    def context(self, **overrides) -> upgrade.UpgradeContext:
        report = validate_instance(self.instance)
        self.assertTrue(report.ok, report.errors)
        base = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=self.units,
            orca=FakeRegistrar(),
            automations=FakeAutomations(),
            host_fixture=self.host_fixture,
            pull=False,
            report=report,
            runtime_user="operator",
            runtime_home=self.home,
        )
        return base if not overrides else upgrade.replace(base, **overrides)

    def run_upgrade(self, **overrides) -> upgrade.UpgradeResult:
        return upgrade.run_steps(self.context(**overrides))

    def run_upgrade_command(self, **overrides) -> int:
        """The public entry point, with only the host-touching clients replaced by doubles.

        Everything an upgrade decides about which account it is materializing for is left to the
        command itself: it is the caller that has no `runtime_user` to pass, and the reason the
        argument exists is that the command has to work it out.
        """
        args = SimpleNamespace(
            instance=str(self.instance),
            product_root=str(self.product),
            base_branch="main",
            dry_run=False,
            no_pull=True,
            host_fixture=str(self.host_fixture),
            json=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        with mock.patch.object(upgrade, "SystemdUnitInstaller", return_value=self.units), \
                mock.patch.object(upgrade, "LiveOrcaRegistrar", FakeRegistrar), \
                mock.patch.object(upgrade, "OrcaAutomationClient", FakeAutomations):
            return upgrade.run_upgrade(args)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_main(argv)
        return code, output.getvalue()

    def run_json_cli(self, argv: list[str]) -> tuple[int, dict]:
        code, output = self.run_cli(argv)
        return code, json.loads(output)

    def statuses(self, result: upgrade.UpgradeResult) -> dict[str, str]:
        return {step.name: step.status for step in result.steps}

    def shell_skill(self, shell: str, skill: str, home: Path | None = None) -> Path:
        return (home or self.home) / "shells" / shell / "skills" / skill / "SKILL.md"

    def assert_invoker_home_untouched(self) -> None:
        self.assertEqual(sorted(path.name for path in self.invoker_home.iterdir()), [])

    def assert_hermetic(self, text: str) -> None:
        """Nothing in a result may name the developing machine's home or this checkout."""
        for foreign in (RUNNING_CHECKOUT, LIVE_HOME):
            self.assertNotIn(foreign, text)


class PortableInstallationTests(PortableFixture):
    """An installation with no overlays at all: everything comes from the named checkout."""

    def test_an_installation_with_no_overlays_materializes_from_the_named_checkout(self) -> None:
        result = self.run_upgrade()

        self.assertTrue(result.ok, result.render())
        self.assertEqual(self.statuses(result)["role-skills"], "changed")
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("claude", "portable-skill").is_file())
        self.assertEqual(
            (self.home / "bin" / "portable-skill").resolve(),
            self.product / "skills" / "roles" / "secretary" / "portable-skill" / "portable-skill.sh",
        )
        # The running checkout's own manifest targets `~/.claude/skills` and `~/.hermes/...`. It
        # was never named here, so a step that read it would leave those directories behind in
        # this fixture's home, and nothing else in the run would say so.
        self.assertEqual(
            sorted(path.name for path in self.home.iterdir()), ["bin", "shells"]
        )
        self.assert_invoker_home_untouched()
        self.assert_hermetic(result.render())

    def test_the_head_canon_falls_back_to_the_named_checkout_not_the_running_one(self) -> None:
        canonical, origin = canonical_path(self.product, self.instance)
        result = self.run_upgrade()

        pin = read_source(self.instance)
        self.assertEqual(canonical, self.product / "triggered_agents" / "agents" / "pipeline" / "heads.toml")
        self.assertEqual(origin, PRODUCT_ORIGIN)
        self.assertTrue(result.ok, result.render())
        self.assertIn("portable-head", snapshot_path(self.instance).read_text(encoding="utf-8"))
        self.assertEqual(pin["canonical"], str(canonical))
        self.assertEqual(pin["canonical_owner"], PRODUCT_ORIGIN)
        self.assertEqual(pin["product_root"], str(self.product))

    def test_the_units_are_rendered_from_the_named_checkout_and_the_installation_user(self) -> None:
        context = self.context()

        result = upgrade.run_steps(context)
        rendered = b"\n".join(
            unit.content
            for unit in upgrade.resolve_packaged(
                context.report.instance,
                self.product / "packaging" / "systemd",
                product_root=self.product,
                instance_path=self.instance,
                runtime_user="operator",
            )
        )

        self.assertTrue(result.ok, result.render())
        self.assertIn(f"WorkingDirectory={self.product}".encode(), rendered)
        self.assertIn(f"SECRETARY_INSTANCE={self.instance}".encode(), rendered)
        self.assertIn(f"ExecStart={self.home}/.local/bin".encode(), rendered)
        self.assertIn("User=operator", rendered.decode())
        self.assert_hermetic(rendered.decode())

    def test_a_second_run_against_the_installation_it_just_wrote_changes_nothing(self) -> None:
        first = self.run_upgrade()
        second = self.run_upgrade()

        self.assertTrue(first.ok, first.render())
        self.assertTrue(second.ok, second.render())
        self.assertTrue(first.changed)
        self.assertEqual(
            [step.name for step in second.steps if step.status == "changed"], [], second.render()
        )

    def test_offline_doctor_reads_the_fresh_installation_without_the_live_host(self) -> None:
        self.run_upgrade()

        code, report = self.run_json_cli(
            ["doctor", "--instance", str(self.instance), "--offline", "--json"]
        )

        registry = report["status"]["installation"]["head_registry"]
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"]["installation"]["instance"], str(self.instance / "instance.yaml"))
        self.assertEqual(registry["snapshot"], str(snapshot_path(self.instance)))
        self.assertEqual(registry["product_root"], str(self.product))
        self.assertEqual(registry["canonical_owner"], PRODUCT_ORIGIN)
        self.assertFalse(registry["error"], registry)

    def test_offline_doctor_plans_against_the_units_of_the_installations_own_checkout(self) -> None:
        """The pin says which checkout this host runs, and the units come from there.

        The running checkout ships a much larger unit catalogue under the same prefix — curator,
        retro, steward and the rest — so a doctor that read its own `packaging/systemd` would
        report those as missing on a host that was never installed from it.
        """
        self.run_upgrade()

        code, report = self.run_json_cli(
            ["doctor", "--instance", str(self.instance), "--offline", "--json"]
        )

        self.assertEqual(code, 0, report)
        self.assertEqual(
            sorted(unit["name"] for unit in report["status"]["host"]["units"]),
            [
                f"{UNIT_PREFIX}dispatcher-production.service",
                f"{UNIT_PREFIX}dispatcher-production.timer",
                f"{UNIT_PREFIX}memory.service",
            ],
        )
        self.assert_hermetic(json.dumps(report))

    def test_offline_doctor_reads_the_pinned_checkout_over_a_configured_one(self) -> None:
        """A decoy in the environment must not outrank what the installation was installed from."""
        self.run_upgrade()
        decoy = self.root / "decoy"
        (decoy / "packaging" / "systemd").mkdir(parents=True)

        with mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(decoy)}):
            code, report = self.run_json_cli(
                ["doctor", "--instance", str(self.instance), "--offline", "--json"]
            )

        self.assertEqual(code, 0, report)
        self.assertTrue(report["status"]["host"]["units"], report["status"]["host"])

    def test_the_upgrade_command_installs_the_checkout_it_was_pointed_at(self) -> None:
        """`--product-root` has to reach the steps, or the skills come from the running module."""
        seen: list[upgrade.UpgradeContext] = []

        with mock.patch.object(
            upgrade, "run_steps", side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult()
        ):
            code = upgrade.run_upgrade(SimpleNamespace(
                instance=str(self.instance),
                product_root=str(self.product),
                base_branch="main",
                dry_run=True,
                no_pull=True,
                host_fixture=str(self.host_fixture),
                json=False,
            ))

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].product_root, self.product)
        self.assertEqual(
            upgrade._role_skills_manifest(seen[0]),
            role_skills.product_manifest_path(self.product),
        )

    def test_the_role_skill_audit_reads_the_named_checkout_rather_than_the_running_one(self) -> None:
        """The running checkout ships neither this manifest nor this skill, so a stale read shows."""
        manifest = role_skills.product_manifest_path(self.product)

        audit = role_skills.audit(instance_path=self.instance, product_manifest=manifest)

        self.assertEqual([source["path"] for source in audit["manifests"]], [str(manifest)])
        self.assertEqual(
            sorted({item["skill"] for item in audit["missing"]}), ["portable-skill"]
        )
        self.assertNotEqual(manifest, role_skills.manifest_path())

    def test_the_command_line_delivers_the_named_checkouts_skills(self) -> None:
        """A hand-run sync has no installation owner to resolve and uses the caller's own home."""
        code, output = self.run_cli([
            "role-skills", "sync",
            "--instance", str(self.instance),
            "--product-root", str(self.product),
        ])

        self.assertTrue(self.shell_skill("codex", "portable-skill", self.invoker_home).is_file())
        self.assertEqual(code, 0, output)
        self.assert_hermetic(output)


class InstallationOwnerTests(PortableFixture):
    """Whose home an upgrade materializes into, when that is not the caller's.

    The reproduction is a repair: root, or an operator on the same box, runs `secretary upgrade`
    against an installation owned by somebody else. Every home-relative path the run writes has to
    be the owner's, because the units the same run renders name the owner's home and nothing else
    will go looking in `/root` for the skills they were supposed to find.
    """

    def test_the_upgrade_command_resolves_the_installation_owner_from_the_instance(self) -> None:
        seen: list[upgrade.UpgradeContext] = []

        with mock.patch.object(
            upgrade, "run_steps", side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult()
        ):
            code = self.run_upgrade_command(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].runtime_user, "operator")
        self.assertEqual(seen[0].runtime_home, self.home)
        self.assertNotEqual(seen[0].runtime_home, self.invoker_home)

    def test_an_explicit_runtime_user_wins_over_the_directory_owner(self) -> None:
        seen: list[upgrade.UpgradeContext] = []

        with mock.patch.object(
            upgrade, "run_steps", side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult()
        ), mock.patch("secretary.host_apply.pwd.getpwuid", side_effect=AssertionError("owner probed")):
            code = self.run_upgrade_command(dry_run=True, runtime_user="named")

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].runtime_user, "named")
        self.assertEqual(seen[0].runtime_home, self.home)

    def test_the_command_delivers_skills_and_entry_points_under_the_owners_home(self) -> None:
        code = self.run_upgrade_command()

        self.assertEqual(code, 0)
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("claude", "portable-skill").is_file())
        self.assertEqual(
            (self.home / "bin" / "portable-skill").resolve(),
            self.product / "skills" / "roles" / "secretary" / "portable-skill" / "portable-skill.sh",
        )
        self.assert_invoker_home_untouched()

    def test_role_worktrees_and_automation_workspaces_belong_to_the_owner(self) -> None:
        """Neither is written here; both are decided from a home, and it must be the owner's."""
        agent = self.product / "triggered_agents" / "agents" / "curator"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "automation.toml").write_text(
            'name = "curator"\nskill = "curate"\n', encoding="utf-8"
        )

        worktrees = upgrade.desired_role_worktrees(self.product, self.home)
        specs = upgrade.load_specs(self.product, home=self.home)

        self.assertEqual(worktrees, [self.home / "orca" / "workspaces" / "secretary" / "curator"])
        self.assertEqual(
            [spec.workspace for spec in specs],
            [str(self.home / "orca" / "workspaces" / "secretary" / "curator")],
        )

    def test_a_configured_workspaces_root_still_wins_over_the_owners_home(self) -> None:
        elsewhere = self.root / "elsewhere"

        with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(elsewhere)}):
            roots = upgrade.workspaces_root(self.home)

        self.assertEqual(roots, elsewhere)

    def test_a_configured_bin_dir_still_wins_over_the_owners_home(self) -> None:
        elsewhere = self.root / "elsewhere-bin"

        with mock.patch.dict(os.environ, {role_skills.BIN_DIR_ENV: str(elsewhere)}):
            self.assertEqual(role_skills.bin_dir(self.home), elsewhere)
        self.assertEqual(role_skills.bin_dir(self.home), self.home / "bin")

    def test_an_installation_owned_by_a_missing_account_is_refused_before_any_write(self) -> None:
        with mock.patch("secretary.host_apply.pwd.getpwnam", side_effect=KeyError("operator")):
            code, output = self.capture(lambda: self.run_upgrade_command())

        self.assertEqual(code, 2)
        self.assertIn("operator", output)
        self.assertFalse((self.home / "shells").exists())
        self.assert_invoker_home_untouched()

    def capture(self, call) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = call()
        return code, output.getvalue()


class ProductRootDefaultTests(PortableFixture):
    """Which checkout install and upgrade materialize when the operator names none.

    A candidate checkout is a normal place to run the command from — that is what upgrading to it
    looks like before it is installed. If the running module decided, the answer would be the
    working directory of whoever typed the command rather than anything the host configured.
    """

    def test_the_configured_checkout_wins_over_the_one_running_the_command(self) -> None:
        with mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(self.product)}):
            self.assertEqual(upgrade.default_product_root(), self.product)
            self.assertEqual(
                installation._product_root(SimpleNamespace(product_root=None)),
                self.product.resolve(),
            )

    def test_with_nothing_configured_the_default_hangs_off_the_running_users_home(self) -> None:
        self.assertEqual(upgrade.default_product_root(), self.invoker_home / "secretary")
        self.assertNotEqual(str(upgrade.default_product_root()), RUNNING_CHECKOUT)

    def test_the_default_role_skill_manifest_is_the_configured_checkouts(self) -> None:
        """`role-skills` without `--product-root` is the path the reviewer's finding walks.

        The manifest of the module running the command is never the answer: a candidate checkout
        auditing itself would report the host in sync with a registry it does not run.
        """
        with mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(self.product)}):
            resolved = role_skills.manifest_path()

        self.assertEqual(resolved, role_skills.product_manifest_path(self.product))
        self.assertNotEqual(resolved, role_skills.MANIFEST)

    def test_a_named_manifest_and_a_named_root_both_outrank_the_configured_checkout(self) -> None:
        named = self.root / "named" / "manifest.toml"
        explicit = self.root / "explicit" / "manifest.toml"
        env = {"TA_SECRETARY_REPO": str(self.product), role_skills.MANIFEST_ENV: str(named)}

        with mock.patch.dict(os.environ, env):
            self.assertEqual(role_skills.manifest_path(), named)
            self.assertEqual(role_skills.manifest_path(explicit), explicit)

    def test_an_audit_with_no_named_checkout_reads_the_configured_ones_skills(self) -> None:
        """End to end through the CLI, which is where the default is actually taken."""
        with mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(self.product)}):
            code, report = self.run_json_cli(
                ["role-skills", "audit", "--instance", str(self.instance), "--json"]
            )

        self.assertEqual(code, 0, report)
        self.assertEqual(
            [source["path"] for source in report["manifests"]],
            [str(role_skills.product_manifest_path(self.product))],
        )
        self.assertEqual(sorted({item["skill"] for item in report["missing"]}), ["portable-skill"])

    def test_an_explicitly_named_checkout_still_wins_over_the_configured_one(self) -> None:
        decoy = self.root / "decoy"
        with mock.patch.dict(os.environ, {"TA_SECRETARY_REPO": str(decoy)}):
            seen: list[upgrade.UpgradeContext] = []
            with mock.patch.object(
                upgrade,
                "run_steps",
                side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult(),
            ):
                code = self.run_upgrade_command(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].product_root, self.product)


class InstallationOwnedLayersTests(PortableFixture):
    """The same alternate checkout, over an installation that owns heads and skills of its own."""

    def setUp(self) -> None:
        super().setUp()
        self.canon = self.own_a_canon()
        self.overlay = self.own_a_skill()

    def test_an_upgrade_from_another_checkout_keeps_both_skill_layers(self) -> None:
        result = self.run_upgrade()

        audit = role_skills.audit(
            instance_path=self.instance,
            product_manifest=role_skills.product_manifest_path(self.product),
        )
        self.assertTrue(result.ok, result.render())
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("codex", "owned-skill").is_file())
        self.assertEqual(
            [(source["origin"], source["path"]) for source in audit["manifests"]],
            [
                (PRODUCT_ORIGIN, str(role_skills.product_manifest_path(self.product))),
                (INSTANCE_ORIGIN, str(self.overlay)),
            ],
        )

    def test_the_installations_own_canon_wins_over_the_named_checkouts_default(self) -> None:
        result = self.run_upgrade()

        pin = read_source(self.instance)
        self.assertTrue(result.ok, result.render())
        self.assertEqual(pin["canonical"], str(self.canon))
        self.assertEqual(pin["canonical_owner"], INSTANCE_ORIGIN)
        self.assertEqual(pin["product_root"], str(self.product))
        self.assertIn("owned-head", snapshot_path(self.instance).read_text(encoding="utf-8"))

    def test_the_head_canon_survives_a_second_upgrade_unchanged(self) -> None:
        self.run_upgrade()
        second = self.run_upgrade()

        self.assertTrue(second.ok, second.render())
        self.assertEqual(self.statuses(second)["head-registry"], "unchanged")
        self.assertEqual(self.canon.read_text(encoding="utf-8"), INSTANCE_CANON)


class RefusedBeforeAnyWriteTests(PortableFixture):
    """A registry the operator has to fix stops the run before the first materializing write."""

    def wrote_nothing(self) -> None:
        self.assertFalse(snapshot_path(self.instance).exists(), "a head snapshot was written")
        self.assertFalse((self.instance / "heads" / "source.yaml").exists(), "a pin was written")
        self.assertFalse((self.home / "shells").exists(), "a skill was delivered")
        self.assertFalse((self.home / "bin").exists(), "an entry point was linked")
        self.assertFalse((self.data / "host-managed.json").exists(), "the host manifest was written")

    def assert_refused(self, result: upgrade.UpgradeResult, named: Path) -> None:
        failed = [step for step in result.steps if step.failed]
        self.assertEqual([step.name for step in failed], ["registries"], result.render())
        self.assertIn(str(named), failed[0].detail)
        self.assertLessEqual(len(failed[0].detail.splitlines()), 2, failed[0].detail)
        self.wrote_nothing()

    def test_a_malformed_product_manifest_is_named_before_anything_is_materialized(self) -> None:
        manifest = role_skills.product_manifest_path(self.product)
        manifest.write_text("[roles.secretary\n", encoding="utf-8")

        self.assert_refused(self.run_upgrade(), manifest)

    def test_a_malformed_instance_overlay_is_named_before_anything_is_materialized(self) -> None:
        overlay = self.own_a_skill()
        overlay.write_text('[roles.secretary]\nskills = [1]\n', encoding="utf-8")

        self.assert_refused(self.run_upgrade(), overlay)

    def test_an_overlay_that_is_a_directory_is_not_a_portable_installation(self) -> None:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.mkdir(parents=True)

        self.assert_refused(self.run_upgrade(), overlay)

    def test_a_dangling_overlay_link_is_refused_rather_than_read_past(self) -> None:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True)
        overlay.symlink_to(self.instance / "never-checked-out.toml")

        self.assert_refused(self.run_upgrade(), overlay)

    def test_a_skill_the_named_checkout_does_not_ship_stops_the_delivery(self) -> None:
        """A manifest can be readable and still name a skill that is not beside it.

        Readable is not deliverable, and the difference has to be found in the same step as a
        syntax error: the head snapshot is written two steps before the skills are.
        """
        source = self.product / "skills" / "roles" / "secretary" / "portable-skill"
        (source / "SKILL.md").unlink()

        self.assert_refused(self.run_upgrade(), source / "SKILL.md")
        self.assertFalse((self.home / "shells").exists())

    def test_an_entry_point_the_registry_does_not_own_is_refused_before_the_snapshot(self) -> None:
        """A command that cannot be linked is a registry fault, not work the sync can do.

        `sync` refuses this one, but by then the snapshot is written. The command bin is also the
        one part of the plan that lives outside the shells, so nothing earlier would have touched
        it and noticed.
        """
        occupied = self.home / "bin" / "portable-skill"
        occupied.parent.mkdir(parents=True)
        occupied.write_text("#!/bin/sh\necho the operator's own\n", encoding="utf-8")

        result = self.run_upgrade()

        failed = [step for step in result.steps if step.failed]
        self.assertEqual([step.name for step in failed], ["registries"], result.render())
        self.assertIn(str(occupied), failed[0].detail)
        self.assertEqual(
            occupied.read_text(encoding="utf-8"), "#!/bin/sh\necho the operator's own\n"
        )
        self.assertFalse((self.home / "shells").exists())

    def test_a_bad_registry_stops_the_run_before_the_checkout_is_reinstalled(self) -> None:
        """`pip install -e` writes into the version being installed, so it is a materializing step.

        A pulled checkout with a virtualenv and a moved dependency manifest reinstalls itself. Doing
        that and only then refusing the manifest leaves the host part-way onto a version it never
        finished installing, which is the state the `registries` step exists to make impossible.
        """
        venv_python = self.product / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        marker = self.root / "pip-ran"
        venv_python.write_text(
            f"#!/bin/sh\nprintf '' > {marker}\nexit 0\n", encoding="utf-8"
        )
        venv_python.chmod(0o755)
        manifest = role_skills.product_manifest_path(self.product)
        manifest.write_text("[roles.secretary\n", encoding="utf-8")

        result = self.run_upgrade(changed_paths=("pyproject.toml",))

        self.assertEqual(
            [step.name for step in result.steps], ["pull", "registries"], result.render()
        )
        self.assertFalse(marker.exists(), "the checkout was reinstalled before the refusal")
        self.wrote_nothing()

    def test_the_registries_step_runs_before_every_step_that_writes(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]

        self.assertEqual(names[:2], ["step_pull", "step_registries"])

    def test_a_malformed_instance_canon_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.own_a_canon()
        canon.write_text("nope = [", encoding="utf-8")

        self.assert_refused(self.run_upgrade(), canon)

    def test_a_dangling_instance_canon_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.instance / "heads" / "heads.toml"
        canon.parent.mkdir(parents=True)
        canon.symlink_to(self.instance / "heads" / "gone.toml")

        self.assert_refused(self.run_upgrade(), canon)

    def test_a_canon_that_is_a_directory_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.instance / "heads" / "heads.toml"
        canon.mkdir(parents=True)

        self.assert_refused(self.run_upgrade(), canon)


if __name__ == "__main__":
    unittest.main()
