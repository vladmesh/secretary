"""The role-skill registry: the product canon, the instance overlay layered over it, and the
delivery check that reads both."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.role_skills import (
    INSTANCE_ORIGIN,
    MANIFEST_ENV,
    PRODUCT_ORIGIN,
    audit,
    find_overlapping_target_roots,
    instance_manifest_path,
    iter_expected,
    load_manifest,
    load_registry,
    manifest_sources,
    roles_root,
    skill_delivery,
    sync,
)

OBSERVER_SKILL = "observe-sprint"
OPEN_SPRINT_SKILL = "open-sprint"
# Skills that belong to one operator's own machine live in that installation's repository, never
# here. The product tree must stay installable by someone who has none of those accounts.
PERSONAL_SKILLS = ("remote-browser", "google-session-transfer", "colab-run")


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

    def test_every_declared_product_skill_has_a_source_in_this_repository(self) -> None:
        missing = [
            str(item.source)
            for item in iter_expected(self.registry)
            if not (item.source / "SKILL.md").is_file()
        ]

        self.assertEqual(missing, [])

    def test_the_product_manifest_declares_no_personal_skill(self) -> None:
        skills = set(self.manifest["roles"]["secretary"]["skills"])

        for personal in PERSONAL_SKILLS:
            self.assertNotIn(personal, skills)

    def test_no_personal_skill_source_is_left_in_the_product_tree(self) -> None:
        for personal in PERSONAL_SKILLS:
            self.assertFalse((roles_root() / "secretary" / personal).exists(), personal)

    def test_target_roots_are_home_relative(self) -> None:
        """A product that hardcodes one user's home is installable by that user only."""
        for name, (target, _) in self.registry.targets.items():
            self.assertTrue(target["root"].startswith("~/"), f"{name}: {target['root']}")


class LayeredManifestTests(unittest.TestCase):
    """Two manifests, each owning the `roles/` tree beside it."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shell_root = self.root / "codex-shell"
        self.product = self.root / "product" / "manifest.toml"
        self.instance = self.root / "instance"
        self.write_product()
        env = mock.patch.dict(os.environ, {MANIFEST_ENV: str(self.product)})
        env.start()
        self.addCleanup(env.stop)

    def write_product(self) -> None:
        self.product.parent.mkdir(parents=True, exist_ok=True)
        self.product.write_text(
            '[roles.secretary]\nskills = ["shipped"]\n\n'
            f'[targets.t]\nshell = "codex"\nroot = "{self.shell_root}"\nroles = ["secretary"]\n',
            encoding="utf-8",
        )
        self.write_skill(self.product.parent / "roles" / "secretary" / "shipped", "# shipped\n")

    def write_instance(self, body: str) -> Path:
        manifest = self.instance / "skills" / "manifest.toml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(body, encoding="utf-8")
        return manifest

    def write_skill(self, directory: Path, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        skill = directory / "SKILL.md"
        skill.write_text(text, encoding="utf-8")
        return skill

    def test_a_missing_overlay_is_a_supported_installation(self) -> None:
        sources = manifest_sources(self.instance)
        result = sync(instance_path=self.instance)

        self.assertEqual([source.origin for source in sources], [PRODUCT_ORIGIN])
        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())

    def test_the_overlay_adds_to_a_product_role_instead_of_replacing_it(self) -> None:
        self.write_instance('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.instance / "skills" / "roles" / "secretary" / "personal", "# personal\n")

        registry = load_registry(self.instance)

        self.assertEqual(
            [skill for skill, _ in registry.roles["secretary"]], ["shipped", "personal"]
        )

    def test_each_skill_resolves_beside_the_manifest_that_declared_it(self) -> None:
        self.write_instance('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.instance / "skills" / "roles" / "secretary" / "personal", "# personal\n")

        by_skill = {item.skill: item for item in iter_expected(load_registry(self.instance))}

        self.assertEqual(
            by_skill["shipped"].source, self.product.parent / "roles" / "secretary" / "shipped"
        )
        self.assertEqual(by_skill["shipped"].origin, PRODUCT_ORIGIN)
        self.assertEqual(
            by_skill["personal"].source,
            self.instance / "skills" / "roles" / "secretary" / "personal",
        )
        self.assertEqual(by_skill["personal"].origin, INSTANCE_ORIGIN)

    def test_sync_delivers_both_layers_into_one_shell_root(self) -> None:
        self.write_instance('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.instance / "skills" / "roles" / "secretary" / "personal", "# personal\n")

        result = sync(instance_path=self.instance)

        self.assertTrue(result["after"]["ok"], result["after"])
        self.assertTrue((self.shell_root / "shipped" / "SKILL.md").is_file())
        self.assertTrue((self.shell_root / "personal" / "SKILL.md").is_file())
        self.assertEqual(
            {item["skill"]: item["origin"] for item in result["copied"]},
            {"shipped": PRODUCT_ORIGIN, "personal": INSTANCE_ORIGIN},
        )

    def test_an_overlay_skill_with_no_source_is_reported_against_its_own_manifest(self) -> None:
        overlay = self.write_instance('[roles.secretary]\nskills = ["personal"]\n')

        result = audit(instance_path=self.instance)

        self.assertFalse(result["ok"])
        self.assertEqual(
            [(item["skill"], item["origin"], item["manifest"]) for item in result["source_missing"]],
            [("personal", INSTANCE_ORIGIN, str(overlay))],
        )

    def test_the_audit_names_every_manifest_it_read(self) -> None:
        overlay = self.write_instance('[roles.secretary]\nskills = ["personal"]\n')
        self.write_skill(self.instance / "skills" / "roles" / "secretary" / "personal", "# personal\n")
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
        self.write_instance(
            f'[targets.t]\nshell = "codex"\nroot = "{other}"\nroles = ["secretary"]\n'
        )

        registry = load_registry(self.instance)

        self.assertEqual(registry.targets["t"][0]["root"], str(other))
        self.assertEqual(registry.targets["t"][1].origin, INSTANCE_ORIGIN)

    def test_the_instance_manifest_is_found_from_instance_yaml_too(self) -> None:
        """`--instance` takes either the directory or the config file inside it."""
        self.assertEqual(
            instance_manifest_path(self.instance / "instance.yaml"),
            instance_manifest_path(self.instance),
        )

    def test_the_configured_instance_is_used_when_no_path_is_passed(self) -> None:
        overlay = self.write_instance('[roles.secretary]\nskills = ["personal"]\n')

        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)}):
            sources = manifest_sources()

        self.assertEqual([str(source.path) for source in sources], [str(self.product), str(overlay)])


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
            os.environ, {MANIFEST_ENV: str(self.manifest), "SECRETARY_INSTANCE": str(self.instance)}
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
