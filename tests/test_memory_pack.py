from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from secretary import upgrade
from secretary.memory.pack import (
    MemoryPackError,
    load_product_pack,
    materialize_product_pack,
)
from tests.fakes.upgrade import FakeUnitInstaller


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def instance_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--initial-branch=main", "--quiet")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "instance.yaml").write_text("version: 1\nname: example\n", encoding="utf-8")
    git(path, "add", "instance.yaml")
    git(path, "commit", "-m", "initial", "--quiet")
    return path


def write_pack(root: Path, facts: dict[str, str]) -> Path:
    pack = root / "packaging" / "memory" / "product-secretary"
    pack.mkdir(parents=True, exist_ok=True)
    entries = []
    for fact_id, text in facts.items():
        filename = f"{fact_id}.md"
        raw = f"---\nsource: product:secretary\n---\n{text}\n".encode()
        (pack / filename).write_bytes(raw)
        entries.append({"id": fact_id, "path": filename, "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {
        "schema": 1,
        "product": "secretary",
        "namespace": "product:secretary",
        "status": "active",
        "ownership": "shipped",
        "fact_format": "markdown-frontmatter-v1",
        "reconciliation": {
            "identity": "id",
            "digest": "sha256",
            "manifest_is_complete": True,
            "absent_id": "delete",
            "unchanged_digest": "retain_embedding",
        },
        "overlay_policy": {"local_overlay_allowed": True, "shipped_id_collision": "reject"},
        "facts": entries,
    }
    (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


class ProductMemoryPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.product = write_pack(self.root / "product", {"one": "one", "two": "two"})
        self.instance = instance_repo(self.root / "instance")
        self.data = self.root / "data"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def materialize(self):
        return materialize_product_pack(
            load_product_pack(self.product), instance_dir=self.instance, data_dir=self.data
        )

    def commit_memory(self, message: str) -> None:
        git(self.instance, "add", "state/memory")
        git(self.instance, "commit", "-m", message, "--quiet")

    def test_portable_materialization_owns_product_scope_and_exports_it(self):
        result = self.materialize()
        export = self.data / "memory" / "export.ndjson"
        rows = [json.loads(line) for line in export.read_text(encoding="utf-8").splitlines()]
        ledger = json.loads(
            (self.instance / "state/memory/packs/product-secretary.json").read_text(encoding="utf-8")
        )

        self.assertTrue(result.changed)
        self.assertEqual({row["id"] for row in rows}, {"product-secretary/one", "product-secretary/two"})
        self.assertEqual(ledger["namespace"], "product:secretary")

    def test_overlay_collision_is_refused_before_any_instance_mutation(self):
        local = self.instance / "state/memory/facts/product-secretary/one.md"
        local.parent.mkdir(parents=True)
        local.write_text("local\n", encoding="utf-8")
        self.commit_memory("local fact")
        before = local.read_text(encoding="utf-8")

        with self.assertRaisesRegex(MemoryPackError, "collides"):
            self.materialize()

        self.assertEqual(local.read_text(encoding="utf-8"), before)
        self.assertFalse((self.instance / "state/memory/packs/product-secretary.json").exists())

    def test_complete_manifest_deletes_only_previously_owned_ids_and_is_noop_when_unchanged(self):
        self.materialize()
        overlay = self.instance / "state/memory/facts/product-secretary/local.md"
        overlay.write_text("local overlay\n", encoding="utf-8")
        project = self.instance / "state/memory/facts/project/local.md"
        project.parent.mkdir(parents=True)
        project.write_text("project fact\n", encoding="utf-8")
        self.commit_memory("local overlays")
        write_pack(self.product, {"one": "one changed", "three": "three"})

        changed = self.materialize()
        unchanged = self.materialize()

        facts = self.instance / "state/memory/facts/product-secretary"
        self.assertTrue((facts / "one.md").read_text(encoding="utf-8").endswith("one changed\n"))
        self.assertFalse((facts / "two.md").exists())
        self.assertTrue((facts / "three.md").exists())
        self.assertEqual(overlay.read_text(encoding="utf-8"), "local overlay\n")
        self.assertEqual(project.read_text(encoding="utf-8"), "project fact\n")
        self.assertEqual((changed.added, changed.updated, changed.deleted), (1, 1, 1))
        self.assertFalse(unchanged.changed)

    def test_invalid_shipped_digest_fails_before_writing_the_instance(self):
        fact = self.product / "packaging/memory/product-secretary/one.md"
        fact.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(MemoryPackError, "digest mismatch"):
            load_product_pack(self.product)

        self.assertFalse((self.instance / "state").exists())

    def test_no_pull_memory_pack_step_detects_ledger_drift_and_requests_restart(self):
        report = SimpleNamespace(data_dir=self.data, host={"unit_prefix": "secretary-"})
        context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(active={"secretary-memory.service"}),
            orca=None,
            automations=None,
            pull=False,
            report=report,
        )
        first = upgrade.step_memory_pack(context)
        restarted = upgrade.step_memory(context)
        second = upgrade.step_memory_pack(context)

        self.assertEqual(first.status, "changed")
        self.assertEqual(restarted.status, "changed")
        self.assertIn(("restart", "secretary-memory.service"), context.units.calls)
        self.assertEqual(second.status, "unchanged")
