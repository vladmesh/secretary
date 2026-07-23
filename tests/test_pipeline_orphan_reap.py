"""Unit coverage for the pipeline's untracked-workspace sweep."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from triggered_agents.agents.pipeline import dispatcher, worker


class OrphanWorkspaceReapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / "workspaces"
        self.root.mkdir()
        self.sweep_file = Path(self.tmpdir.name) / "state" / "orphan_sweep.json"
        self.logs = []
        self.patches = [
            mock.patch.object(worker, "WORKSPACES_ROOT", self.root),
            mock.patch.object(dispatcher, "ORPHAN_SWEEP_FILE", self.sweep_file),
            mock.patch.object(dispatcher, "ORPHAN_GRACE_SECONDS", 1800),
            mock.patch.object(dispatcher, "ORPHAN_SWEEP_INTERVAL_S", 0),
            mock.patch.object(dispatcher.ops, "list_cards", return_value=[]),
            mock.patch.object(dispatcher.pause_flag, "load", return_value={}),
            mock.patch.object(dispatcher.STATE, "log_run", side_effect=self._log),
            mock.patch.object(dispatcher.STATE, "ensure_dir", side_effect=self.sweep_file.parent.mkdir),
            mock.patch.object(worker, "prune_worktrees"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.teardown = mock.patch.object(worker, "teardown", side_effect=self._remove_workspace)
        self.mock_teardown = self.teardown.start()
        self.addCleanup(self.teardown.stop)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _log(self, event: str, **fields) -> None:
        self.logs.append((event, fields))

    def _workspace(self, project: str, name: str, *, age_s: int = 3600) -> Path:
        path = self.root / project / name
        path.mkdir(parents=True)
        then = time.time() - age_s
        os.utime(path, (then, then))
        return path

    @staticmethod
    def _remove_workspace(workspace: str) -> None:
        Path(workspace).rmdir()

    def test_reaps_old_workspace_with_no_record_or_card(self) -> None:
        orphan = self._workspace("secretary", "secretary-637-old-attempt")

        self.assertFalse(dispatcher._reap_orphan_workspaces({}))

        self.mock_teardown.assert_called_once_with(str(orphan))
        worker.prune_worktrees.assert_called_once_with("secretary")
        self.assertFalse(orphan.exists())
        self.assertIn(("reap-orphan-summary", {
            "candidates": 1, "reaped": 1, "skipped_grace": 0,
        }), self.logs)

    def test_does_not_rescan_before_its_interval(self) -> None:
        orphan = self._workspace("project-a", "old-attempt")
        dispatcher.ORPHAN_SWEEP_INTERVAL_S = 600
        self.addCleanup(setattr, dispatcher, "ORPHAN_SWEEP_INTERVAL_S", 0)

        dispatcher._reap_orphan_workspaces({})
        self._workspace("project-a", "second-old-attempt")
        dispatcher._reap_orphan_workspaces({})

        self.mock_teardown.assert_called_once_with(str(orphan))

    def test_preserves_records_blocked_claim_durable_roles_review_and_grace(self) -> None:
        live = self._workspace("project-a", "live-worker")
        review = self._workspace("project-a", "live-review")
        blocked = self._workspace("project-a", "blocked-claim")
        durable = self._workspace("secretary", "pipeline")
        young = self._workspace("project-a", "new-orphan", age_s=10)
        records = {"project-a-1": {"workspace": str(live), "review_ws": str(review)}}
        dispatcher.ops.list_cards.return_value = [{
            "reference": "project-a-2", "project": "project-a", "claim": "blocked-claim",
        }]

        dispatcher._reap_orphan_workspaces(records)

        self.mock_teardown.assert_not_called()
        for path in (live, review, blocked, durable, young):
            self.assertTrue(path.exists())
        self.assertIn(("reap-orphan-summary", {
            "candidates": 1, "reaped": 0, "skipped_grace": 1,
        }), self.logs)

    def test_preserves_blocked_card_with_empty_claim_by_legacy_reference_prefix(self) -> None:
        blocked = self._workspace("secretary", "secretary-637-before-claim")
        dispatcher.ops.list_cards.return_value = [{
            "reference": "secretary-637", "project": "secretary", "claim": "",
        }]

        dispatcher._reap_orphan_workspaces({})

        self.mock_teardown.assert_not_called()
        self.assertTrue(blocked.exists())

    def test_preserves_blocked_card_reviewer_workspace(self) -> None:
        worker_workspace = self._workspace("secretary", "637-checkpoint-writer")
        reviewer_workspace = self._workspace("secretary", "review-637-checkpoint-writer-2")
        dispatcher.ops.list_cards.return_value = [{
            "reference": "secretary-637", "project": "secretary", "claim": "637-checkpoint-writer",
        }]

        dispatcher._reap_orphan_workspaces({})

        self.mock_teardown.assert_not_called()
        self.assertTrue(worker_workspace.exists())
        self.assertTrue(reviewer_workspace.exists())

    def test_teardown_failure_does_not_stop_later_orphans(self) -> None:
        first = self._workspace("project-a", "first")
        second = self._workspace("project-a", "second")

        def teardown(workspace: str) -> None:
            if workspace == str(first):
                raise RuntimeError("orca unavailable")
            Path(workspace).rmdir()

        self.mock_teardown.side_effect = teardown

        dispatcher._reap_orphan_workspaces({})

        self.assertEqual(self.mock_teardown.call_args_list,
                         [mock.call(str(first)), mock.call(str(second))])
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(worker.prune_worktrees.call_count, 2)
        reap_events = [fields for event, fields in self.logs if event == "reap-orphan"]
        self.assertTrue(any(fields["result"] == "teardown-failed" for fields in reap_events))
        self.assertIn(("reap-orphan-summary", {
            "candidates": 2, "reaped": 1, "skipped_grace": 0,
        }), self.logs)

    def test_tick_sweeps_after_reconcile_and_before_advance(self) -> None:
        calls = []
        with mock.patch.object(dispatcher, "_auto_resume_stale_hard_pause"), \
             mock.patch.object(dispatcher, "_tick_lock", return_value=nullcontext()), \
             mock.patch.object(dispatcher, "_load_cards", return_value={}), \
             mock.patch.object(dispatcher.health, "refresh", return_value={}), \
             mock.patch.object(
                 dispatcher, "_reconcile", side_effect=lambda records: calls.append("reconcile") or False), \
             mock.patch.object(
                 dispatcher, "_reap_orphan_workspaces", side_effect=lambda records: calls.append("reap") or False), \
             mock.patch.object(
                 dispatcher, "_advance", side_effect=lambda records, statuses: calls.append("advance") or False), \
             mock.patch.object(
                 dispatcher.validate, "run", side_effect=lambda *args: calls.append("validate") or False), \
             mock.patch.object(dispatcher, "_claim_next", side_effect=lambda *args: calls.append("claim")):
            self.assertEqual(dispatcher.tick(), 0)

        self.assertEqual(calls, ["reconcile", "reap", "advance", "validate", "claim"])


if __name__ == "__main__":
    unittest.main()
