"""Where the head-registry pair lives: `<data>/heads/`, with a legacy fallback for the deploy skew.

`ummanu upgrade` and `recover` write `heads.yaml` and `source.yaml` into the data directory and
commit neither (ummanu-26). New code reads `<data>/heads/` from the moment it is merged, but only
the next `ummanu upgrade` writes it, so until then every reader falls back to the pair an older
upgrade committed into the live root, and says so. The cutover card removes that fallback.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.head_registry import legacy_pair, write_installed_pair
from ummanu import cli, installation, status, task_commands, upgrade
from ummanu.config import validate_instance
from ummanu.dispatch.host import InstanceCatalog
from ummanu.head_registry import (
    HeadRegistryConfigError,
    canonical_heads,
    generated_pair,
    installed_heads,
    installed_pair,
)
from ummanu.runtime import heads

ROLES = ("new_card", "reviewer", "observer", "curator", "retro", "steward")


def canon(head: str) -> str:
    """An installation-owned canon that routes every role to ``head``."""
    roles = "".join(f'{role} = "{head}"\n' for role in ROLES)
    return (
        '[resources.acct]\naccount = "acct"\n\n'
        f'[profiles.{head}]\nresource = "acct"\nadapter = "codex"\nfallback = []\n\n'
        f"[role_defaults]\n{roles}"
    )


def instance_yaml(data_dir: Path) -> str:
    return (
        f"version: 1\nname: skew\ndata_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
        "host:\n  unit_prefix: ummanu-\n"
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


class DeploySkewTests(unittest.TestCase):
    """A live root holding only the legacy pair, and an empty `<data>/heads/`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.data_dir = self.root / "data"
        (self.instance / "heads").mkdir(parents=True)
        (self.data_dir / "heads").mkdir(parents=True)
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data_dir), encoding="utf-8")
        self.canon = self.instance / "heads" / "heads.toml"
        self.canon.write_text(canon("legacy-head"), encoding="utf-8")
        snapshot = yaml.safe_dump(
            canonical_heads(upgrade.running_product_root(), self.instance), sort_keys=False
        )
        self.legacy = write_installed_pair(self.instance, snapshot, legacy=True)
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def upgrade_step(self) -> upgrade.StepResult:
        report = validate_instance(self.instance)
        self.assertTrue(report.ok, report.errors)
        context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=mock.Mock(),
            report=report,
        )
        return upgrade.step_head_registry(context)

    def readers(self) -> dict[str, object]:
        """What every reader of the pair sees right now."""
        report = validate_instance(self.instance)
        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
            heads._load_registry.cache_clear()
            registry_path = heads.registry_path()
            runtime_default = heads.load_registry().role_default("new_card")
        record = status._head_registry(self.instance, report.data_dir)
        return {
            "catalog": InstanceCatalog(self.instance).worker_head({}),
            "task": task_commands._load_heads(self.instance)["role_defaults"]["new_card"],
            "runtime": (registry_path, runtime_default),
            "status": (record["snapshot"], record["legacy_source"], record["error"]),
            "doctor": cli._legacy_head_registry_source(report),
        }

    def test_readers_follow_the_legacy_pair_until_one_upgrade_step_writes_the_data_pair(self):
        data_pair = generated_pair(self.instance)
        self.assertEqual(data_pair.snapshot, self.data_dir / "heads" / "heads.yaml")
        self.assertEqual(self.legacy, self.instance / "heads" / "heads.yaml")

        before = self.readers()

        self.assertEqual(before["catalog"], "legacy-head")
        self.assertEqual(before["task"], "legacy-head")
        self.assertEqual(before["runtime"], (self.legacy, "legacy-head"))
        # status and doctor name the legacy source the readers fell back to.
        self.assertEqual(before["status"], (str(self.legacy), str(self.legacy), None))
        self.assertEqual(before["doctor"], str(self.legacy))

        self.canon.write_text(canon("upgraded-head"), encoding="utf-8")
        result = self.upgrade_step()
        after = self.readers()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(data_pair.snapshot.is_file())
        self.assertTrue(data_pair.source.is_file())
        self.assertEqual(after["catalog"], "upgraded-head")
        self.assertEqual(after["task"], "upgraded-head")
        self.assertEqual(after["runtime"], (data_pair.snapshot, "upgraded-head"))
        self.assertEqual(after["status"], (str(data_pair.snapshot), None, None))
        self.assertIsNone(after["doctor"])
        # The upgrade neither rewrote nor removed the live root's copy: it is simply not read.
        self.assertIn("legacy-head", self.legacy.read_text(encoding="utf-8"))

    def test_a_data_pair_that_is_present_wins_even_when_it_is_broken(self):
        """The fallback is for an absent `<data>/heads/heads.yaml` only, never for a broken one."""
        broken = generated_pair(self.instance).snapshot
        broken.write_text("profiles: {}\n", encoding="utf-8")

        pair = installed_pair(self.instance)

        self.assertEqual(pair.snapshot, broken)
        self.assertFalse(pair.legacy)
        with self.assertRaisesRegex(HeadRegistryConfigError, str(broken)):
            installed_heads(self.instance)

    def test_with_no_pair_anywhere_the_error_names_the_data_directory(self):
        legacy = legacy_pair(self.instance)
        legacy.snapshot.unlink()
        legacy.source.unlink()

        pair = installed_pair(self.instance)

        self.assertEqual(pair, generated_pair(self.instance))
        with self.assertRaisesRegex(HeadRegistryConfigError, str(pair.snapshot)):
            installed_heads(self.instance)


class RecoverRegenerationTests(unittest.TestCase):
    """Recover step 7 regenerates the pair into `<data>/heads/`, whatever the checkpoint holds."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.data_dir = self.root / "data"
        (self.instance / "heads").mkdir(parents=True)
        self.data_dir.mkdir()
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data_dir), encoding="utf-8")
        (self.instance / "heads" / "heads.toml").write_text(canon("recovered-head"), encoding="utf-8")

    def materialize(self) -> None:
        """The recover materializer, with only its head-registry step running."""

        def head_registry_only(context, steps=installation.STEPS):
            self.assertIn(upgrade.step_head_registry, steps)
            return upgrade.run_steps(context, steps=(upgrade.step_head_registry,))

        with mock.patch.object(installation, "run_steps", side_effect=head_registry_only):
            result = installation.materialize_host(self.instance, upgrade.running_product_root())
        self.assertTrue(result.ok, result.render())

    def assert_regenerated(self) -> None:
        pair = installed_pair(self.instance)
        self.assertEqual(pair, generated_pair(self.instance))
        self.assertEqual(pair.snapshot.parent, self.data_dir / "heads")
        self.assertEqual(installed_heads(self.instance)["role_defaults"]["new_card"], "recovered-head")

    def test_from_a_legacy_checkpoint_that_tracks_the_pair(self):
        stale = "# a pair an older upgrade committed\nresources: {}\n"
        (self.instance / "heads" / "heads.yaml").write_text(stale, encoding="utf-8")
        (self.instance / "heads" / "source.yaml").write_text("revision: old\n", encoding="utf-8")
        git(self.instance, "init", "--quiet", "--initial-branch", "main")
        git(self.instance, "add", ".")
        git(
            self.instance, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "--quiet", "-m", "legacy checkpoint",
        )
        head = git(self.instance, "rev-parse", "HEAD")

        self.materialize()

        self.assert_regenerated()
        # The tracked legacy copy is left exactly as checked out, and nothing was committed.
        self.assertEqual((self.instance / "heads" / "heads.yaml").read_text(encoding="utf-8"), stale)
        self.assertEqual(git(self.instance, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(git(self.instance, "rev-parse", "HEAD"), head)

    def test_from_an_exporter_shaped_tree_that_lacks_it(self):
        (self.instance / "snapshot-manifest.json").write_text("{}\n", encoding="utf-8")
        self.assertFalse((self.instance / "heads" / "heads.yaml").exists())

        self.materialize()

        self.assert_regenerated()
        self.assertFalse((self.instance / "heads" / "heads.yaml").exists())
        self.assertFalse((self.instance / ".git").exists())


if __name__ == "__main__":
    unittest.main()
