"""The role-skill registry: the product canon, the optional instance overlay layered over it, the
command entry points either of them can declare, and the delivery check that reads both."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.role_skills import (
    INSTANCE_ORIGIN,
    MANIFEST_ENV,
    PRODUCT_ORIGIN,
    RegistryError,
    audit,
    find_overlapping_target_roots,
    instance_manifest_path,
    iter_expected,
    load_manifest,
    load_registry,
    main,
    manifest_sources,
    roles_root,
    skill_delivery,
    sync,
)

OBSERVER_SKILL = "observe-sprint"
OPEN_SPRINT_SKILL = "open-sprint"


class CanonicalRegistryTests(unittest.TestCase):
    """Read the product manifest itself, not a fixture: it is what `role-skills sync` delivers.

    The instance is an empty directory on purpose. The default instance path is the host's real
    one, and on the machine that develops this product that instance owns an overlay; a test about
    the product canon has to say so or it silently asserts against the developer's installation.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name)
        self.manifest = load_manifest()
        self.registry = load_registry(self.instance)

    def test_the_observer_role_owns_its_own_skill(self) -> None:
        self.assertEqual(self.manifest["roles"]["observer"]["skills"], [OBSERVER_SKILL])

    def test_the_canonical_observer_skill_is_in_this_repository(self) -> None:
        source = roles_root() / "observer" / OBSERVER_SKILL / "SKILL.md"

        self.assertTrue(source.is_file(), f"{source} is missing")

    def test_the_observer_skill_reaches_the_shell_its_head_runs_in(self) -> None:
        """`role_defaults.observer` is a codex profile, so the codex shell must carry the skill."""
        shells = {
            item.shell
            for item in iter_expected(self.registry)
            if item.role == "observer" and item.skill == OBSERVER_SKILL
        }

        self.assertIn("codex", shells)

    def test_no_target_root_is_nested_in_another_of_the_same_shell(self) -> None:
        self.assertEqual(find_overlapping_target_roots(self.registry), [])

    def test_the_interactive_secretary_keeps_its_own_sprint_skill(self) -> None:
        """The double loop is deliberate: the observer skill does not replace `run-sprint`."""
        self.assertIn("run-sprint", self.manifest["roles"]["secretary"]["skills"])
        self.assertTrue((roles_root() / "secretary" / "run-sprint" / "SKILL.md").is_file())

    def test_the_document_loop_stays_next_to_the_entity_loop(self) -> None:
        """`open-sprint` is added beside `start-sprint`, not instead of it."""
        skills = self.manifest["roles"]["secretary"]["skills"]

        self.assertIn("start-sprint", skills)
        self.assertTrue((roles_root() / "secretary" / "start-sprint" / "SKILL.md").is_file())

    def test_the_secretary_role_owns_the_sprint_entity_skill(self) -> None:
        self.assertIn(OPEN_SPRINT_SKILL, self.manifest["roles"]["secretary"]["skills"])

    def test_the_canonical_open_sprint_skill_is_in_this_repository(self) -> None:
        source = roles_root() / "secretary" / OPEN_SPRINT_SKILL / "SKILL.md"

        self.assertTrue(source.is_file(), f"{source} is missing")

    def test_open_sprint_reaches_both_secretary_shells(self) -> None:
        """Sprint birth must not depend on which secretary the human opened."""
        shells = {
            item.shell
            for item in iter_expected(self.registry)
            if item.role == "secretary" and item.skill == OPEN_SPRINT_SKILL
        }

        self.assertLessEqual({"claude", "codex"}, shells)

    def test_every_skill_the_product_declares_has_a_source_in_this_repository(self) -> None:
        missing = [
            str(item.source)
            for item in iter_expected(self.registry)
            if not (item.source / "SKILL.md").is_file()
        ]

        self.assertEqual(missing, [])

    def test_the_product_canon_reads_the_same_without_an_instance_directory(self) -> None:
        """A checkout on a machine with no installation at all is still a readable registry."""
        registry = load_registry(self.instance / "not-installed")

        self.assertEqual([source.origin for source in registry.sources], [PRODUCT_ORIGIN])
        self.assertEqual(registry.roles.keys(), self.registry.roles.keys())


class OverlayFixture(unittest.TestCase):
    """A product manifest and an instance directory this test owns end to end."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.shell_root = self.root / "codex-shell"
        self.product = self.root / "product" / "manifest.toml"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.write_product()
        env = mock.patch.dict(os.environ, {MANIFEST_ENV: str(self.product)})
        env.start()
        self.addCleanup(env.stop)

    def write_product(self, body: str | None = None) -> None:
        self.product.parent.mkdir(parents=True, exist_ok=True)
        self.product.write_text(
            body
            if body is not None
            else (
                '[roles.secretary]\nskills = ["shipped"]\n\n'
                f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\n'
                'roles = ["secretary"]\n'
            ),
            encoding="utf-8",
        )
        self.write_skill(self.product.parent / "roles" / "secretary" / "shipped", "# shipped\n")

    def write_overlay(self, body: str) -> Path:
        manifest = self.instance / "skills" / "manifest.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(body, encoding="utf-8")
        return manifest

    def write_skill(self, directory: Path, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        skill = directory / "SKILL.md"
        skill.write_text(text, encoding="utf-8")
        return skill

    def personal_skill_dir(self) -> Path:
        return self.instance / "skills" / "roles" / "secretary" / "personal"


class LayeredRegistryTests(OverlayFixture):
    """Two manifests, each owning the `roles/` tree beside it."""

    def test_a_missing_overlay_is_a_supported_installation(self) -> None:
        sources = manifest_sources(self.instance)
        result = sync(instance_path=self.instance)

        self.assertEqual([source.origin for source in sources], [PRODUCT_ORIGIN])
        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())

    def test_the_overlay_adds_to_a_product_role_instead_of_replacing_it(self) -> None:
        self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        registry = load_registry(self.instance)

        self.assertEqual(
            [skill for skill, _ in registry.roles["secretary"]], ["shipped", "personal"]
        )

    def test_each_skill_resolves_beside_the_manifest_that_declared_it(self) -> None:
        self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        by_skill = {item.skill: item for item in iter_expected(load_registry(self.instance))}

        self.assertEqual(
            by_skill["shipped"].source, self.product.parent / "roles" / "secretary" / "shipped"
        )
        self.assertEqual(by_skill["shipped"].origin, PRODUCT_ORIGIN)
        self.assertEqual(by_skill["personal"].source, self.personal_skill_dir())
        self.assertEqual(by_skill["personal"].origin, INSTANCE_ORIGIN)

    def test_sync_delivers_both_layers_into_one_shell_root(self) -> None:
        self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        result = sync(instance_path=self.instance)

        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())
        self.assertTrue((self.shell_root / "personal" / "SKILL.md").is_file())
        self.assertEqual(
            {item["skill"]: item["origin"] for item in result["copied"]},
            {"shipped": PRODUCT_ORIGIN, "personal": INSTANCE_ORIGIN},
        )

    def test_an_overlay_skill_with_no_source_is_reported_against_its_own_manifest(self) -> None:
        overlay = self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [
                (item["skill"], item["origin"], item["manifest"])
                for item in result["source_missing"]
            ],
            [("personal", INSTANCE_ORIGIN, str(overlay))],
        )

    def test_the_audit_names_every_manifest_it_read(self) -> None:
        overlay = self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")
        sync(instance_path=self.instance)

        result = audit(instance_path=self.instance)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["manifests"],
            [
                {"origin": PRODUCT_ORIGIN, "path": str(self.product)},
                {"origin": INSTANCE_ORIGIN, "path": str(overlay)},
            ],
        )

    def test_an_overlay_target_replaces_the_product_target_of_the_same_name(self) -> None:
        """A target is one shell root: merging two of them means nothing, so the last one wins."""
        other = self.root / "other-shell"
        self.write_overlay(
            f'[targets.t]\nshell = "codex"\nroot = "{other}"\nroles = ["secretary"]\n'
        )

        registry = load_registry(self.instance)

        self.assertEqual(registry.targets["t"][0]["root"], str(other))
        self.assertEqual(registry.targets["t"][1].origin, INSTANCE_ORIGIN)

    def test_the_overlay_is_found_from_instance_yaml_just_as_from_the_directory(self) -> None:
        """`--instance` takes either the private repo or the config file inside it."""
        self.assertEqual(
            instance_manifest_path(self.instance / "instance.yaml"),
            instance_manifest_path(self.instance),
        )

    def test_the_configured_instance_is_used_when_no_path_is_passed(self) -> None:
        overlay = self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')

        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)}):
            sources = manifest_sources()

        self.assertEqual([str(source.path) for source in sources], [str(self.product), str(overlay)])

    def test_the_command_default_instance_comes_from_the_environment(self) -> None:
        self.write_overlay('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.personal_skill_dir(), "# personal\n")

        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)}):
            code = main(["sync"])

        self.assertEqual(code, 0)
        self.assertTrue((self.shell_root / "personal" / "SKILL.md").is_file())


class MalformedManifestTests(OverlayFixture):
    """An operator edits these files by hand, so a bad edit has to read like a bad edit."""

    def assert_bounded(self, code: int, output: str, manifest: Path) -> None:
        self.assertEqual(code, 2, output)
        self.assertIn(str(manifest), output)
        self.assertNotIn("Traceback", output)
        self.assertLessEqual(len(output.splitlines()), 3, output)

    def run_command(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([*argv, "--instance", str(self.instance)])
        return code, out.getvalue()

    def test_an_overlay_that_is_not_toml_names_itself_in_the_audit(self) -> None:
        overlay = self.write_overlay("this is not toml = [")

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)

    def test_an_overlay_with_the_wrong_shape_names_itself_and_the_key(self) -> None:
        overlay = self.write_overlay('[roles.secretary]\nskills = "personal"\n')

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)
        self.assertIn("roles.secretary.skills", output)

    def test_a_target_naming_an_unknown_role_is_rejected(self) -> None:
        overlay = self.write_overlay(
            f'[targets.u]\nshell = "codex"\nroot = "{self.root / "u"}"\nroles = ["ghost"]\n'
        )

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, overlay)
        self.assertIn("ghost", output)

    def test_a_malformed_overlay_makes_sync_write_nothing(self) -> None:
        overlay = self.write_overlay('[roles.secretary]\nskills = [1]\n')

        code, output = self.run_command("sync")

        self.assert_bounded(code, output, overlay)
        self.assertFalse(self.shell_root.exists(), "sync delivered the product half of a bad registry")

    def test_a_malformed_product_manifest_names_the_product_manifest(self) -> None:
        self.write_product("[roles.secretary\n")

        code, output = self.run_command("audit")

        self.assert_bounded(code, output, self.product)


class CommandEntryPointTests(OverlayFixture):
    """A skill that ships a helper is only usable once the helper is on PATH."""

    def setUp(self) -> None:
        super().setUp()
        self.bin_dir = self.root / "bin"
        self.link = self.bin_dir / "personal-tool"
        self.helper = self.personal_skill_dir() / "personal-tool.sh"

    def install_personal_skill(self, *, with_command: bool = True) -> Path:
        self.write_skill(self.personal_skill_dir(), "# personal\n")
        self.helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        body = '[roles.secretary]\nskills = ["personal"]\n'
        if with_command:
            body += (
                "\n[commands.personal-tool]\n"
                'role = "secretary"\nskill = "personal"\n'
                f'source = "personal-tool.sh"\ndest = "{self.link}"\n'
            )
        return self.write_overlay(body)

    def test_sync_materializes_an_executable_entry_point(self) -> None:
        self.install_personal_skill()

        result = sync(instance_path=self.instance)

        self.assertEqual(self.link.resolve(), self.helper)
        self.assertTrue(os.access(self.link, os.X_OK))
        self.assertTrue(bool(self.helper.stat().st_mode & stat.S_IXUSR))
        self.assertEqual(
            [(item["command"], item["was"], item["origin"]) for item in result["linked"]],
            [("personal-tool", "missing", INSTANCE_ORIGIN)],
        )

    def test_sync_repairs_a_link_left_behind_by_the_move_out_of_the_product(self) -> None:
        """The helper moved from the product tree into the installation; the old link dangles."""
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        moved_from = self.root / "old-product" / "skills" / "roles" / "secretary" / "personal"
        self.link.symlink_to(moved_from / "personal-tool.sh")
        self.assertFalse(self.link.exists(), "the fixture link is supposed to dangle")

        result = sync(instance_path=self.instance)

        self.assertEqual(self.link.resolve(), self.helper)
        self.assertEqual([item["was"] for item in result["linked"]], ["stale"])

    def test_a_second_sync_changes_nothing(self) -> None:
        self.install_personal_skill()
        sync(instance_path=self.instance)

        result = sync(instance_path=self.instance)

        self.assertEqual([item["was"] for item in result["linked"]], ["ok"])
        self.assertEqual(self.link.resolve(), self.helper)
        self.assertTrue(result["after"]["ok"], result["after"])

    def test_sync_refuses_to_overwrite_a_real_file(self) -> None:
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        self.assertIn(str(self.link), str(caught.exception))
        self.assertEqual(self.link.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

    def test_sync_refuses_a_link_this_registry_does_not_own(self) -> None:
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        other = self.root / "somebody-elses-tool"
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        self.link.symlink_to(other)

        with self.assertRaises(RegistryError) as caught:
            sync(instance_path=self.instance)

        self.assertIn(str(other), str(caught.exception))
        self.assertEqual(self.link.resolve(), other)

    def test_a_conflicting_entry_point_stops_sync_before_any_skill_is_copied(self) -> None:
        self.install_personal_skill()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.link.write_text("mine\n", encoding="utf-8")

        with self.assertRaises(RegistryError):
            sync(instance_path=self.instance)

        self.assertFalse(self.shell_root.exists())

    def test_the_audit_reports_an_entry_point_that_was_never_materialized(self) -> None:
        overlay = self.install_personal_skill()
        sync(instance_path=self.instance)
        self.link.unlink()

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [(item["command"], item["status"], item["manifest"]) for item in result["entry_points"]],
            [("personal-tool", "missing", str(overlay))],
        )

    def test_a_command_naming_a_skill_no_manifest_declares_is_rejected(self) -> None:
        self.write_overlay(
            "[commands.personal-tool]\n"
            'role = "secretary"\nskill = "absent"\n'
            f'source = "personal-tool.sh"\ndest = "{self.link}"\n'
        )

        with self.assertRaises(RegistryError) as caught:
            audit(instance_path=self.instance)

        self.assertIn("secretary/absent", str(caught.exception))

    def test_a_command_source_may_not_escape_its_skill_directory(self) -> None:
        overlay = self.write_overlay(
            '[roles.secretary]\nskills = ["personal"]\n\n'
            "[commands.personal-tool]\n"
            'role = "secretary"\nskill = "personal"\n'
            f'source = "../../../etc/profile"\ndest = "{self.link}"\n'
        )

        with self.assertRaises(RegistryError) as caught:
            audit(instance_path=self.instance)

        self.assertIn(str(overlay), str(caught.exception))

    def test_a_registry_with_no_command_has_no_entry_points_to_answer_for(self) -> None:
        self.install_personal_skill(with_command=False)

        result = sync(instance_path=self.instance)

        self.assertEqual(result["linked"], [])
        self.assertTrue(result["after"]["ok"], result["after"])


class SkillDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shell_root = self.root / "codex-shell"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.manifest = self.root / "manifest.toml"
        self.write_manifest(shell="codex")
        env = mock.patch.dict(
            os.environ,
            {MANIFEST_ENV: str(self.manifest), "SECRETARY_INSTANCE": str(self.instance)},
        )
        env.start()
        self.addCleanup(env.stop)

    def write_manifest(self, *, shell: str) -> None:
        self.manifest.write_text(
            f'[roles.observer]\nskills = ["{OBSERVER_SKILL}"]\n\n'
            f'[targets.t]\nshell = "{shell}"\n'
            f'root = "{self.shell_root}"\nroles = ["observer"]\n',
            encoding="utf-8",
        )

    def write_overlay(self, body: str) -> Path:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(body, encoding="utf-8")
        return overlay

    def deliver(self) -> Path:
        skill = self.shell_root / OBSERVER_SKILL / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# skill\n", encoding="utf-8")
        return skill

    def test_a_delivered_skill_reads_as_delivered_with_its_path(self) -> None:
        skill = self.deliver()

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertTrue(result["delivered"])
        self.assertEqual(result["paths"], [str(skill)])
        self.assertEqual(result["reason"], "")

    def test_a_missing_skill_names_the_file_and_the_repair(self) -> None:
        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn(str(self.shell_root / OBSERVER_SKILL / "SKILL.md"), result["reason"])
        self.assertIn("role-skills sync", result["reason"])

    def test_a_shell_without_a_target_is_not_delivery(self) -> None:
        self.deliver()

        result = skill_delivery("observer", OBSERVER_SKILL, "claude")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["paths"], [])
        self.assertIn("no claude target", result["reason"])

    def test_an_unreadable_registry_is_not_delivery(self) -> None:
        self.deliver()
        self.manifest.write_text("this is not toml = [", encoding="utf-8")

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn("could not be read", result["reason"])

    def test_an_unreadable_overlay_is_not_delivery_either(self) -> None:
        """An unreadable registry is not evidence that the skill is there, whoever owns it."""
        self.deliver()
        self.write_overlay("this is not toml = [")

        result = skill_delivery("observer", OBSERVER_SKILL, "codex")

        self.assertFalse(result["delivered"])
        self.assertIn("could not be read", result["reason"])

    def test_an_overlay_skill_is_checked_in_the_shell_the_head_opens(self) -> None:
        self.write_overlay('[roles.observer]\nskills = ["personal"]\n')

        result = skill_delivery("observer", "personal", "codex")

        self.assertFalse(result["delivered"])
        self.assertEqual(result["paths"], [str(self.shell_root / "personal" / "SKILL.md")])


if __name__ == "__main__":
    unittest.main()
