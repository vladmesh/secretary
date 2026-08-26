import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np

    from secretary import memory_reindex, memory_service
except ImportError:  # The base install deliberately excludes the heavy memory extra.
    np = None
    memory_reindex = None
    memory_service = None


@unittest.skipIf(memory_service is None, "secretary[memory] is not installed")
class IncrementalMemoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.canon = self.root / "facts"
        self.canon.mkdir()
        self.export = self.root / "export.ndjson"
        self.db = self.root / "index.sqlite"
        self.calls = []

    def tearDown(self):
        if memory_service is not None:
            memory_service.mark_search_not_ready()
        self.temp.cleanup()

    def embed(self, text):
        self.calls.append(text)
        if text == "explode":
            raise RuntimeError("forced embedding failure")
        raw = sum(text.encode("utf-8")) or 1
        return np.asarray([raw % 11, raw % 13, raw % 17, raw % 19], dtype=np.float32)

    def write_export(self, facts):
        with self.export.open("w", encoding="utf-8") as handle:
            for fact_id, text in facts:
                raw = "---\nsource: test\ncreated: 2026-07-17\n---\n" + text + "\n"
                handle.write(
                    json.dumps(
                        {
                            "id": fact_id,
                            "path": f"{fact_id}.md",
                            "text": raw,
                        }
                    )
                    + "\n"
                )

    def rows(self):
        conn = memory_service.db(self.db)
        try:
            return {
                fact_id: (rowid, text)
                for rowid, fact_id, text in conn.execute(
                    "SELECT id, fact_id, text FROM memories ORDER BY fact_id"
                )
            }
        finally:
            conn.close()

    def update(self):
        return memory_service.incremental_update(
            self.canon,
            self.export,
            self.db,
            "test-model",
            4,
            document_embed=self.embed,
            allow_empty=True,
        )

    def test_add_update_delete_reuses_unchanged_embedding(self):
        self.write_export([("global/a", "alpha"), ("global/b", "bravo")])
        first = self.update()
        self.assertEqual(first["mode"], "rebuild")
        before = self.rows()
        self.calls.clear()

        self.write_export([("global/a", "alpha"), ("global/b", "bravo changed"), ("global/c", "charlie")])
        second = self.update()
        middle = self.rows()
        self.assertEqual(
            {key: second[key] for key in ("added", "updated", "deleted", "reused")},
            {"added": 1, "updated": 1, "deleted": 0, "reused": 1},
        )
        self.assertCountEqual(self.calls, ["bravo changed", "charlie"])
        self.assertEqual(before["global/a"][0], middle["global/a"][0])

        self.calls.clear()
        self.write_export([("global/a", "alpha"), ("global/c", "charlie")])
        third = self.update()
        self.assertEqual(third["deleted"], 1)
        self.assertEqual(third["reused"], 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn("global/b", self.rows())

    def test_embedding_failure_preserves_index(self):
        self.write_export([("global/a", "alpha")])
        self.update()
        before = self.rows()
        self.write_export([("global/a", "explode")])
        with self.assertRaisesRegex(RuntimeError, "forced embedding failure"):
            self.update()
        self.assertEqual(self.rows(), before)

    def test_git_snapshot_marks_every_command_safe_for_root_recovery(self):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if "rev-parse" in command:
                return mock.Mock(returncode=0, stdout=str(self.root) + "\n")
            if "ls-tree" in command:
                return mock.Mock(stdout=b"facts/example.md\0")
            if "show" in command:
                return mock.Mock(stdout="# example\n")
            raise AssertionError(command)

        with mock.patch.object(memory_service.subprocess, "run", side_effect=run):
            facts = memory_service.load_git_head_snapshot(self.canon)

        self.assertEqual(len(facts), 1)
        for command in commands:
            self.assertEqual(command[:3], ["git", "-c", "safe.directory=*"])

    def test_bootstrap_reconciles_compatible_index_without_full_rebuild(self):
        self.write_export([("global/a", "alpha")])
        self.update()
        self.calls.clear()

        with (
            mock.patch.object(memory_service, "CANON", self.canon),
            mock.patch.object(memory_service, "CANON_EXPORT", self.export),
            mock.patch.object(memory_service, "DB_PATH", str(self.db)),
            mock.patch.object(memory_service, "MODEL", "test-model"),
            mock.patch.object(memory_service, "DIM", 4),
            mock.patch.object(memory_service, "embedder") as warm_embedder,
            mock.patch.object(memory_service, "embed_doc", side_effect=self.embed),
            mock.patch.object(
                memory_service, "update_index", wraps=memory_service.update_index
            ) as update_index,
            mock.patch.object(
                memory_service,
                "offline_rebuild",
                side_effect=AssertionError("compatible bootstrap must not rebuild"),
            ),
        ):
            indexed = memory_service.bootstrap_index()

        self.assertEqual(indexed, 1)
        warm_embedder.assert_called_once_with()
        update_index.assert_called_once_with()
        self.assertEqual(self.calls, [])

    def test_embedder_uses_configured_persistent_cache_and_thread_limit(self):
        fake_embedding = mock.Mock()
        with (
            mock.patch.object(memory_service, "_embedder", None),
            mock.patch.object(memory_service, "TextEmbedding", return_value=fake_embedding) as embedding,
            mock.patch.object(memory_service, "MODEL", "test-model"),
            mock.patch.object(memory_service, "MODEL_CACHE_DIR", self.root / "fastembed-cache"),
            mock.patch.object(memory_service, "THREADS", 1),
        ):
            self.assertIs(memory_service.embedder(), fake_embedding)

        embedding.assert_called_once_with(
            model_name="test-model", cache_dir=str(self.root / "fastembed-cache"), threads=1
        )

    def test_offline_rebuild_fallback_uses_configured_cache_and_thread_limit(self):
        self.write_export([("global/a", "alpha")])
        cache_dir = self.root / "fastembed-cache"
        with mock.patch.object(
            memory_service, "build_document_embedder", return_value=self.embed
        ) as build_embedder:
            result = memory_service.offline_rebuild(
                self.canon,
                self.export,
                self.db,
                "test-model",
                4,
                cache_dir=cache_dir,
                threads=2,
            )

        self.assertTrue(result["ok"])
        build_embedder.assert_called_once_with("test-model", cache_dir, 2)

    def test_reindex_entrypoint_forwards_cache_and_thread_limit(self):
        cache_dir = self.root / "fastembed-cache"
        with mock.patch.object(memory_service, "offline_rebuild", return_value={"ok": True}) as rebuild:
            self.assertEqual(
                memory_reindex.rebuild(
                    self.canon,
                    self.export,
                    self.db,
                    "test-model",
                    4,
                    cache_dir=cache_dir,
                    threads=2,
                ),
                {"ok": True},
            )

        rebuild.assert_called_once_with(
            self.canon, self.export, self.db, "test-model", 4, None, False, cache_dir, 2
        )


if __name__ == "__main__":
    unittest.main()
