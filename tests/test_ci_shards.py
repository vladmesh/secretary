from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.ci_test_shards import ManifestError, SHARDS, load_manifest, modules


class CiTestShardManifestTests(unittest.TestCase):
    def test_live_manifest_partitions_every_top_level_test_once(self) -> None:
        root = Path(__file__).resolve().parents[1]

        grouped = load_manifest(root)

        declared = [path for shard in SHARDS for path in grouped[shard]]
        discovered = sorted(path.relative_to(root).as_posix() for path in root.glob("tests/test_*.py"))
        self.assertEqual(sorted(declared), discovered)
        self.assertEqual(len(declared), len(set(declared)))
        self.assertTrue(all(grouped[shard] for shard in SHARDS))

    def test_new_or_duplicate_test_is_a_manifest_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py", "test_new.py"):
                (tests / name).write_text("", encoding="utf-8")
            manifest = tests / "ci-shards.txt"
            manifest.write_text(
                "core tests/test_a.py\n"
                "dispatcher tests/test_b.py\n"
                "runtime tests/test_c.py\n"
                "component tests/test_d.py\n"
                "component tests/test_d.py\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManifestError, "already belongs"):
                load_manifest(root, manifest)

            manifest.write_text(
                "core tests/test_a.py\n"
                "dispatcher tests/test_b.py\n"
                "runtime tests/test_c.py\n"
                "component tests/test_d.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "unclaimed tests: tests/test_new.py"):
                load_manifest(root, manifest)

    def test_paths_become_unittest_module_names(self) -> None:
        self.assertEqual(modules(["tests/test_ci_shards.py"]), ["tests.test_ci_shards"])

    def test_workflow_keeps_a_test_aggregator_after_all_shards(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        for shard in SHARDS:
            self.assertIn(shard, workflow)
        self.assertIn("python3 scripts/ci_test_shards.py --shard", workflow)
        self.assertIn("needs: test_shards", workflow)
        self.assertIn("name: test", workflow)


if __name__ == "__main__":
    unittest.main()
