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

from triggered_agents.agents.pipeline import health, heads, pause, worker
from triggered_agents.runtime.owner import DEFAULT_OWNER, owner
from triggered_agents.runtime.paths import configured_instance_path, default_instance_path, instance_dir
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


class InstanceResolutionTests(unittest.TestCase):
    """No absolute path of one developer's host survives in the product's defaults."""

    def test_the_default_instance_is_home_relative(self):
        self.assertEqual(default_instance_path(), Path.home() / "secretary-instance")

    def test_the_configured_instance_wins_over_the_default(self):
        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": "/srv/other-instance"}):
            self.assertEqual(configured_instance_path(), Path("/srv/other-instance"))

    def test_an_instance_named_by_its_config_file_resolves_to_its_directory(self):
        self.assertEqual(instance_dir("/srv/inst/instance.yaml"), Path("/srv/inst"))
        self.assertEqual(instance_dir("/srv/inst"), Path("/srv/inst"))


class HeadRegistryPathTests(unittest.TestCase):
    """Which accounts and models exist is installation configuration, not product code."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(self.instance)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TA_HEADS_TOML", None)

    def own_registry(self) -> Path:
        (self.instance / "heads").mkdir(parents=True, exist_ok=True)
        owned = self.instance / "heads" / "heads.toml"
        owned.write_text(
            "[resources.claude-sub]\naccount = 'a'\nprobe = 'true'\n\n"
            "[profiles.house-head]\nresource = 'claude-sub'\nadapter = 'claude'\n"
            "model = 'opus'\nfallback = []\n",
            encoding="utf-8",
        )
        return owned

    def test_a_portable_installation_reads_the_product_default(self):
        self.assertEqual(heads.registry_path(), heads.HEADS_TOML)
        self.assertIn(heads.DEFAULT_PROFILE, heads.load_registry().profiles)

    def test_an_installation_that_owns_a_registry_is_read_from_it(self):
        owned = self.own_registry()

        self.assertEqual(heads.registry_path(), owned)
        self.assertEqual(sorted(heads.load_registry().profiles), ["house-head"])

    def test_an_explicit_override_outranks_both(self):
        self.own_registry()
        with mock.patch.dict(os.environ, {"TA_HEADS_TOML": str(heads.HEADS_TOML)}):
            self.assertEqual(heads.registry_path(), heads.HEADS_TOML)


class OwnerNameTests(unittest.TestCase):
    def test_an_unconfigured_installation_escalates_to_a_neutral_owner(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECRETARY_OWNER", None)
            self.assertEqual(owner(), DEFAULT_OWNER)
            self.assertNotIn("vladmesh", DEFAULT_OWNER)

    def test_a_configured_owner_is_the_name_a_blocked_card_carries(self):
        with mock.patch.dict(os.environ, {"SECRETARY_OWNER": "sam"}):
            self.assertEqual(owner(), "sam")


class PauseCandidateTests(unittest.TestCase):
    def test_live_pause_candidates_drop_removed_checkout(self):
        dead = Path.home() / "triggered-agents" / "state" / "pipeline" / "pause.json"
        # Pin the checkout root to a neutral path: the real worktree can itself sit under a
        # directory whose name contains "triggered-agents" (a task branch), which would otherwise
        # false-match the substring guard below without any dead path actually being scanned.
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(pause, "_checkout_root", return_value=Path(tmp)), \
             mock.patch.dict(os.environ, {}, clear=False):
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

    def test_live_pause_candidates_scan_secretary_agent_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "secretary" / "pipeline" / "state" / "pipeline" / "pause.json"
            live.parent.mkdir(parents=True)
            live.write_text("{}", encoding="utf-8")
            dead_root = root / "triggered-agents"
            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root)}, clear=False):
                os.environ.pop("TA_STATE", None)
                candidates = pause._candidate_pause_files()
        self.assertIn(live, candidates)
        self.assertFalse(any(dead_root in p.parents for p in candidates))

    def test_legacy_pause_candidates_point_at_secretary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dead_root = root / "triggered-agents"
            clear = ("SECRETARY_LEGACY_PAUSE_FILE", "SECRETARY_LEGACY_PIPELINE_STATE_DIR",
                     "TA_PIPELINE_STATE_DIR", "TA_STATE")
            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root)}, clear=False):
                for name in clear:
                    os.environ.pop(name, None)
                candidates = dispatcher_pause._legacy_pause_candidates()
        expected = root / "secretary" / "pipeline" / "state" / "pipeline" / "pause.json"
        self.assertIn(expected, candidates)
        self.assertFalse(any(dead_root in p.parents for p in candidates))


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
