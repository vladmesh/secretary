"""Path/env resolving after the control-panel / triggered-agents decommission (secretary-624).

These cover the runtime path defaults that used to point at removed checkouts: the OpenRouter key
location, the optional setup provisioner and central manifest dir, and the pause-flag candidate
lists. They pin the new behaviour so a future edit can't silently repoint them back at a dead path.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.agents.pipeline import health, pause, worker
from secretary import dispatcher_pause


class OpenRouterKeyTests(unittest.TestCase):
    def test_default_env_file_is_hermes(self):
        self.assertEqual(health._OPENROUTER_ENV_FILE, Path.home() / ".hermes" / ".env")

    def test_reads_hermes_style_key_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nOPENROUTER_API_KEY=sk-or-hermes\n", encoding="utf-8")
            with mock.patch.object(health, "_OPENROUTER_ENV_FILE", env), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TA_OPENROUTER_KEY", None)
                self.assertEqual(health._read_openrouter_key(), "sk-or-hermes")

    def test_reads_legacy_key_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('open_router_key="sk-or-legacy"\n', encoding="utf-8")
            with mock.patch.object(health, "_OPENROUTER_ENV_FILE", env), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TA_OPENROUTER_KEY", None)
                self.assertEqual(health._read_openrouter_key(), "sk-or-legacy")

    def test_env_key_override_wins(self):
        override = os.environ.get("TA_OPENROUTER_KEY")
        with mock.patch.dict(os.environ, {"TA_OPENROUTER_KEY": "sk-or-override"}):
            self.assertEqual(health._read_openrouter_key(), "sk-or-override")
        self.assertEqual(os.environ.get("TA_OPENROUTER_KEY"), override)

    def test_missing_file_yields_no_key(self):
        with mock.patch.object(health, "_OPENROUTER_ENV_FILE", Path("/no/such/hermes/.env")), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TA_OPENROUTER_KEY", None)
            self.assertIsNone(health._read_openrouter_key())


class PauseCandidateTests(unittest.TestCase):
    def test_live_pause_candidates_drop_removed_checkout(self):
        dead = Path.home() / "triggered-agents" / "state" / "pipeline" / "pause.json"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TA_STATE", None)
            candidates = pause._candidate_pause_files()
        self.assertNotIn(dead, candidates)
        self.assertFalse(any("triggered-agents/state/pipeline/pause.json" in str(p)
                             for p in candidates))

    def test_legacy_pause_candidates_drop_removed_checkout(self):
        home_dead = Path.home() / "triggered-agents" / "state" / "pipeline" / "pause.json"
        abs_dead = Path("/home/dev/triggered-agents/state/pipeline/pause.json")
        clear = ("SECRETARY_LEGACY_PAUSE_FILE", "SECRETARY_LEGACY_PIPELINE_STATE_DIR",
                 "TA_PIPELINE_STATE_DIR", "TA_STATE")
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in clear:
                os.environ.pop(name, None)
            candidates = dispatcher_pause._legacy_pause_candidates()
        self.assertNotIn(home_dead, candidates)
        self.assertNotIn(abs_dead, candidates)
        self.assertFalse(any(str(p).endswith("triggered-agents/state/pipeline/pause.json")
                             for p in candidates))


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        preflight = mock.patch.object(worker.task_protocol, "preflight",
                                      return_value=(True, "preflight-ok"))
        preflight.start()
        self.addCleanup(preflight.stop)
        log = mock.patch.object(worker.STATE, "log_run")
        log.start()
        self.addCleanup(log.stop)

    def test_no_provisioner_configured_skips_and_succeeds(self):
        with mock.patch.object(worker, "PROVISION", None):
            ok, log = worker.provision("/tmp/ws")
        self.assertTrue(ok)
        self.assertIn("no provisioner configured", log)

    def test_configured_but_missing_provisioner_blocks(self):
        with mock.patch.object(worker, "PROVISION", Path("/no/such/provision.py")):
            ok, log = worker.provision("/tmp/ws")
        self.assertFalse(ok)
        self.assertIn("provisioner missing", log)


class LoadManifestTests(unittest.TestCase):
    def test_local_workspace_toml_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspace.toml").write_text(
                '[workspace]\nbase_branch = "dev"\ncontrib = true\n', encoding="utf-8")
            with mock.patch.object(worker, "project_root", return_value=root):
                self.assertEqual(worker.read_base_branch("proj"), "dev")
                self.assertTrue(worker.is_contrib("proj"))

    def test_central_manifest_only_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"      # no workspace.toml here
            root.mkdir()
            central = Path(tmp) / "manifests"
            central.mkdir()
            (central / "proj.toml").write_text('[workspace]\nbase_branch = "release"\n',
                                               encoding="utf-8")
            with mock.patch.object(worker, "project_root", return_value=root):
                with mock.patch.object(worker, "MANIFEST_DIR", None):
                    self.assertEqual(worker.read_base_branch("proj"), "main")
                with mock.patch.object(worker, "MANIFEST_DIR", central):
                    self.assertEqual(worker.read_base_branch("proj"), "release")


if __name__ == "__main__":
    unittest.main()
