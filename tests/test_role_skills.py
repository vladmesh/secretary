"""The role-skill registry: the observer role in the canon, and the delivery check over it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.role_skills import (
    MANIFEST_ENV,
    find_overlapping_target_roots,
    iter_expected,
    load_manifest,
    roles_root,
    skill_delivery,
)

OBSERVER_SKILL = "observe-sprint"
OPEN_SPRINT_SKILL = "open-sprint"


class CanonicalRegistryTests(unittest.TestCase):
    """Read the product manifest itself, not a fixture: it is what `role-skills sync` delivers."""

    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_the_observer_role_owns_its_own_skill(self) -> None:
        self.assertEqual(self.manifest["roles"]["observer"]["skills"], [OBSERVER_SKILL])

    def test_the_canonical_observer_skill_is_in_this_repository(self) -> None:
        source = roles_root() / "observer" / OBSERVER_SKILL / "SKILL.md"

        self.assertTrue(source.is_file(), f"{source} is missing")

    def test_the_observer_skill_reaches_the_shell_its_head_runs_in(self) -> None:
        """`role_defaults.observer` is a codex profile, so the codex shell must carry the skill."""
        shells = {
            item.shell
            for item in iter_expected(self.manifest)
            if item.role == "observer" and item.skill == OBSERVER_SKILL
        }

        self.assertIn("codex", shells)

    def test_no_target_root_is_nested_in_another_of_the_same_shell(self) -> None:
        self.assertEqual(find_overlapping_target_roots(self.manifest), [])

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
            for item in iter_expected(self.manifest)
            if item.role == "secretary" and item.skill == OPEN_SPRINT_SKILL
        }

        self.assertLessEqual({"claude", "codex"}, shells)


class SkillDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shell_root = self.root / "codex-shell"
        self.manifest = self.root / "manifest.toml"
        self.write_manifest(shell="codex")
        env = mock.patch.dict(os.environ, {MANIFEST_ENV: str(self.manifest)})
        env.start()
        self.addCleanup(env.stop)

    def write_manifest(self, *, shell: str) -> None:
        self.manifest.write_text(
            f'[roles.observer]\nskills = ["{OBSERVER_SKILL}"]\n\n'
            f'[targets.t]\nshell = "{shell}"\n'
            f'root = "{self.shell_root}"\nroles = ["observer"]\n',
            encoding="utf-8",
        )

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


if __name__ == "__main__":
    unittest.main()
