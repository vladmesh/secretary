from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.head_health import HEALTH_ENV, health_path, resource_statuses
from secretary.head_registry import canonical_heads
from secretary.dispatcher import InstanceCatalog


REPO_ROOT = Path(__file__).resolve().parents[1]


class ResourceStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.instance = Path(self.tmpdir.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(HEALTH_ENV, None)

    def write(self, payload: dict) -> None:
        path = health_path(self.instance)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_absent_file_reports_nothing_rather_than_red(self) -> None:
        """No probe has run. Routing must stay exactly as it is, not re-route the whole queue."""
        self.assertEqual(resource_statuses(self.instance), {})

    def test_unreadable_file_reports_nothing(self) -> None:
        path = health_path(self.instance)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        self.assertEqual(resource_statuses(self.instance), {})

    def test_fresh_entries_are_read_as_written(self) -> None:
        now = time.time()
        self.write({
            "openai-sub": {"status": "red", "checked_at": now},
            "claude-sub": {"status": "green", "checked_at": now},
        })

        self.assertEqual(
            resource_statuses(self.instance), {"openai-sub": "red", "claude-sub": "green"}
        )

    def test_stale_entry_stops_counting(self) -> None:
        """A red nobody has re-probed for hours is not evidence about now, and keeping it would
        pin every card onto the fallback family long after the resource came back."""
        self.write({"openai-sub": {"status": "red", "checked_at": time.time() - 4000}})

        self.assertEqual(resource_statuses(self.instance), {})

    def test_env_override_points_at_an_external_probe_output(self) -> None:
        external = Path(self.tmpdir.name) / "elsewhere.json"
        external.write_text(
            json.dumps({"openai-sub": {"status": "red", "checked_at": time.time()}}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {HEALTH_ENV: str(external)}):
            self.assertEqual(resource_statuses(self.instance), {"openai-sub": "red"})


class ResolveHeadTests(unittest.TestCase):
    """The resolver on the real registry, which is where `codex-reviewer -> claude-opus` lives."""

    def catalog(self, instance_dir: Path) -> InstanceCatalog:
        catalog = object.__new__(InstanceCatalog)
        catalog.instance_dir = instance_dir  # type: ignore[attr-defined]
        catalog._heads = canonical_heads(REPO_ROOT)  # type: ignore[attr-defined]
        return catalog

    def test_red_resource_walks_the_declared_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            health = health_path(instance)
            health.parent.mkdir(parents=True, exist_ok=True)
            health.write_text(
                json.dumps({"openai-sub": {"status": "red", "checked_at": time.time()}}),
                encoding="utf-8",
            )
            catalog = self.catalog(instance)

            reviewer = catalog.resolve_head("codex-reviewer")
            worker = catalog.resolve_head("codex")

        self.assertEqual(reviewer.resolved, "claude-opus")
        self.assertTrue(reviewer.fallback)
        self.assertEqual(reviewer.skipped, ("codex-reviewer",))
        # `codex` declares no chain: there is nowhere to fall back to, so the card keeps its head
        # and the bring-up fails on its own terms rather than on a routing decision invented here.
        self.assertEqual(worker.resolved, "codex")
        self.assertFalse(worker.fallback)

    def test_green_resource_resolves_to_the_requested_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.catalog(Path(tmp))

            resolution = catalog.resolve_head("codex-reviewer")

        self.assertEqual(resolution.resolved, "codex-reviewer")
        self.assertFalse(resolution.fallback)
        self.assertEqual(resolution.skipped, ())


if __name__ == "__main__":
    unittest.main()
