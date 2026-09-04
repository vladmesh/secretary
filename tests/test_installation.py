from __future__ import annotations

import contextlib
import getpass
import io
import json
import os
import pwd
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import installation
from secretary.board_transport import DEFAULT_TRANSPORT
from secretary.cli import main
from secretary.data import export_runs
from secretary.installation import (
    InstallError,
    _clone_or_reuse,
    _ensure_installation_user,
    _product_root,
    check_prerequisites,
    install,
    materialize_checkpoint,
    materialize_pipeline_state,
    pipeline_state_path,
    provision_codex_home,
    provision_project_checkouts,
)
from secretary.routing_journal import attempts
from secretary.runtime_env import RuntimeEnvError
from secretary.upgrade import UpgradeResult, step_host
from tests.fakes.installation import CARD, PRODUCT_ROOT, SPRINT, _checkpoint, _git


# The checkout these tests run out of, which is the one they have. Nothing resolves it for them:
# an install materializes the configured checkout or `~/secretary`, and neither exists on a machine
# that only checked this branch out somewhere.
class InstallationTests(unittest.TestCase):
    def test_prerequisite_probe_accepts_the_planned_transport_before_it_exists_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = DEFAULT_TRANSPORT
            with (
                mock.patch("secretary.installation.shutil.which", return_value="/usr/bin/orca"),
                mock.patch("secretary.installation._run"),
                mock.patch("secretary.installation.TaskReader") as reader,
            ):
                check_prerequisites(transport=transport, instance_dir=Path(tmp))

        self.assertEqual(reader.call_args.args[0]._transport, transport)

    def test_only_an_absent_runtime_env_is_ignored_for_an_unlocked_store(self):
        target = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, target)
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote="remote",
            installation_user=getpass.getuser(),
            recover=True,
            adopt=False,
            dry_run=True,
            runtime_env=None,
            product_root=str(PRODUCT_ROOT),
        )
        unlocked = installation.SecretRecovery(store_present=True, unlocked=True)
        with (
            mock.patch("secretary.installation._ensure_installation_user"),
            mock.patch("secretary.installation._clone_or_reuse", return_value="reused checkpoint checkout"),
            mock.patch("secretary.installation._open_secret_store", return_value=unlocked),
            mock.patch("secretary.installation.read_runtime_env", side_effect=RuntimeEnvError("unsafe mode")),
            mock.patch("secretary.installation.ensure_from_runtime_values") as migrate,
        ):
            result = install(args)

        self.assertFalse(result.ok)
        migrate.assert_not_called()

    def test_recovery_materializes_pipeline_state_before_host_steps_can_start_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events: list[str] = []

            def run(context, *, steps):
                events.append("post-host" if step_host in steps else "pre-host")
                return UpgradeResult()

            with (
                mock.patch("secretary.installation.validate_instance", return_value=SimpleNamespace(ok=True)),
                mock.patch(
                    "secretary.installation.resolve_runtime_owner", return_value=("operator", root / "home")
                ),
                mock.patch("secretary.installation.run_steps", side_effect=run),
            ):
                installation.materialize_host(
                    root / "instance", root / "product", before_host=lambda _context: events.append("restore")
                )

            self.assertEqual(events, ["pre-host", "restore", "post-host"])

    def test_pipeline_state_materialization_rebuilds_the_checkpointed_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            record = {"event": "claim", "reference": "secretary-1"}
            (source / "runs.ndjson").write_text(
                json.dumps({"source": "runs.jsonl", "line": 1, "record": record}) + "\n",
                encoding="utf-8",
            )
            state_dir = (
                root / "home" / "orca" / "workspaces" / "secretary" / "pipeline" / "state" / "pipeline"
            )

            first = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((first.records, first.changed), (1, True))

            self.assertEqual(
                [
                    json.loads(line)
                    for line in (state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                ],
                [record],
            )
            stamp = (state_dir / "runs.jsonl").stat().st_mtime_ns
            second = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((second.records, second.changed), (1, False))
            self.assertEqual((state_dir / "runs.jsonl").stat().st_mtime_ns, stamp)

    def test_pipeline_state_path_honors_the_dispatcher_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            override = Path(temporary) / "overridden-pipeline-state"
            with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(override)}):
                self.assertEqual(pipeline_state_path(Path(temporary) / "home"), override)

    def test_pipeline_state_materialization_refuses_to_overwrite_different_live_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            (source / "runs.ndjson").write_text(
                json.dumps({"source": "runs.jsonl", "line": 1, "record": {"event": "checkpoint"}}) + "\n",
                encoding="utf-8",
            )
            state_dir = root / "pipeline-state"
            state_dir.mkdir()
            journal = state_dir / "runs.jsonl"
            journal.write_text(json.dumps({"event": "live"}) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "does not extend the checkpoint"):
                materialize_pipeline_state(instance, state_dir)

            self.assertEqual(journal.read_text(encoding="utf-8"), json.dumps({"event": "live"}) + "\n")

    def test_pipeline_state_materialization_keeps_a_valid_live_append_and_ignores_blank_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            canonical = [
                {"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}},
                {"source": "runs.jsonl", "line": 3, "record": {"event": "review"}},
            ]
            (source / "runs.ndjson").write_text(
                "\n".join(json.dumps(record) for record in canonical) + "\n", encoding="utf-8"
            )
            state_dir = root / "pipeline-state"
            state_dir.mkdir()
            journal = state_dir / "runs.jsonl"
            journal.write_text(
                '{"event":"claim"}\n\n{"event":"review"}\n{"event":"release"}\n',
                encoding="utf-8",
            )

            result = materialize_pipeline_state(instance, state_dir)
            self.assertEqual((result.records, result.changed), (2, False))

            self.assertIn('{"event":"release"}', journal.read_text(encoding="utf-8"))

    def test_gapped_checkpoint_journal_round_trips_through_the_next_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            source = instance / "state" / "runs"
            source.mkdir(parents=True)
            rows = [
                {"source": "runs.jsonl", "line": 1, "record": {"event": "claim"}},
                {"source": "runs.jsonl", "line": 3, "record": {"event": "review"}},
            ]
            canonical = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
            (source / "runs.ndjson").write_text(canonical, encoding="utf-8")
            state_dir = root / "pipeline-state"

            materialize_pipeline_state(instance, state_dir)
            export_runs(root / "data", state_dir=state_dir)

            self.assertEqual((state_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()[1], "")
            self.assertEqual((root / "data" / "runs" / "runs.ndjson").read_text(encoding="utf-8"), canonical)

    def test_missing_project_checkout_is_cloned_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "projects" / "demo"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "README.md").write_text("demo\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "initial")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            binding = {
                "id": "demo",
                "repo": str(target),
                "remote": str(remote),
                "default_branch": "main",
            }

            self.assertEqual(provision_project_checkouts([binding], None), 1)
            self.assertEqual(provision_project_checkouts([binding], None), 0)
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "demo\n")

    def test_codex_home_seeds_only_missing_non_secret_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root / "product"
            source = product / "packaging" / "codex-home"
            source.mkdir(parents=True)
            (source / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (source / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            account = SimpleNamespace(pw_dir=str(root / "home"), pw_uid=os.getuid(), pw_gid=os.getgid())
            with (
                mock.patch("secretary.installation.pwd.getpwnam", return_value=account),
                mock.patch("secretary.installation._set_installation_owner"),
            ):
                self.assertEqual(provision_codex_home(product, "dev"), 2)
                target = root / "home" / ".config" / "orca" / "codex-runtime-home" / "home"
                (target / "config.toml").write_text("operator state\n", encoding="utf-8")
                self.assertEqual(provision_codex_home(product, "dev"), 0)
            self.assertEqual((target / "config.toml").read_text(encoding="utf-8"), "operator state\n")

    def test_codex_home_upgrade_repairs_only_the_managed_memory_bearer_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root / "product"
            source = product / "packaging" / "codex-home"
            source.mkdir(parents=True)
            (source / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (source / "config.toml").write_text(
                'model = "test"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n'
                'bearer_token_env_var = "SECRETARY_MEMORY_ACCESS_TOKEN"\n',
                encoding="utf-8",
            )
            target = root / "home" / ".config" / "orca" / "codex-runtime-home" / "home"
            target.mkdir(parents=True)
            (target / "config.toml").write_text(
                'model = "operator-choice"\n\n[mcp_servers.memory]\nurl = "http://127.0.0.1:8077/mcp"\n',
                encoding="utf-8",
            )
            account = SimpleNamespace(pw_dir=str(root / "home"), pw_uid=os.getuid(), pw_gid=os.getgid())
            with (
                mock.patch("secretary.installation.pwd.getpwnam", return_value=account),
                mock.patch("secretary.installation._set_installation_owner"),
            ):
                self.assertEqual(provision_codex_home(product, "dev"), 2)

            rendered = (target / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "operator-choice"', rendered)
            self.assertIn('bearer_token_env_var = "SECRETARY_MEMORY_ACCESS_TOKEN"', rendered)

    def test_root_checks_orca_as_installation_user(self):
        with (
            mock.patch("secretary.installation.os.geteuid", return_value=0),
            mock.patch("secretary.installation.shutil.which", return_value="/usr/local/bin/orca"),
            mock.patch("secretary.installation._run") as run,
            mock.patch("secretary.installation.KanboardClient"),
            mock.patch("secretary.installation.TaskReader") as reader,
        ):
            check_prerequisites(DEFAULT_TRANSPORT, Path("/tmp/instance"), "dev")

        self.assertIn(
            ["runuser", "--user", "dev", "--", "orca", "--version"],
            [call.args[0] for call in run.call_args_list],
        )
        reader.return_value.list.assert_called_once()

    def test_prerequisite_probe_requires_the_selected_transport(self):
        with self.assertRaises(TypeError):
            check_prerequisites()  # type: ignore[call-arg]

    def test_existing_runtime_env_is_not_a_bootstrap_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            (target / "runtime.env").write_text("KANBOARD_API_TOKEN=existing\n", encoding="utf-8")

            with (
                mock.patch("secretary.installation.state_repo.git", return_value="remote\n"),
                self.assertRaisesRegex(InstallError, "choose --recover"),
            ):
                _clone_or_reuse("remote", target, recovery=False, dry_run=True)

            (target / ".secretary-bootstrap").write_text("bootstrap\n", encoding="utf-8")
            with mock.patch("secretary.installation.state_repo.git", side_effect=("remote\n", "")):
                self.assertEqual(
                    _clone_or_reuse("remote", target, recovery=False, dry_run=True),
                    "reused checkpoint checkout",
                )

    def test_reused_checkout_uses_the_owner_scoped_state_repository_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git").mkdir(parents=True)
            with mock.patch("secretary.installation.state_repo.git", side_effect=("remote\n", "")) as git:
                self.assertEqual(
                    _clone_or_reuse("remote", target, recovery=True, dry_run=True),
                    "reused checkpoint checkout",
                )

            self.assertEqual(
                [call.args[1] for call in git.call_args_list],
                [["remote", "get-url", "origin"], ["status", "--porcelain"]],
            )

    @unittest.skipUnless(os.geteuid() == 0, "requires a root clean-host fixture")
    def test_root_can_reuse_a_checkout_owned_by_installation_user(self):
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("fixture has no nobody user")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "checkpoint").write_text("ok\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True)
            subprocess.run(["git", "clone", str(remote), str(target)], check=True)
            for path in (target, *target.rglob("*")):
                os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)

            self.assertEqual(
                _clone_or_reuse(str(remote), target, recovery=True, dry_run=True),
                "reused checkpoint checkout",
            )

    def test_existing_installation_user_requires_recover_or_adopt_choice(self):
        with self.assertRaisesRegex(InstallError, "choose --recover.*adopt"):
            _ensure_installation_user(getpass.getuser(), recovery=False, dry_run=False)

    def test_bootstrap_stamp_allows_the_existing_user_for_first_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            target.mkdir()
            (target / ".secretary-bootstrap").write_text("bootstrap\n", encoding="utf-8")
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="remote",
                installation_user=getpass.getuser(),
                recover=False,
                adopt=False,
                dry_run=False,
                runtime_env=None,
            )
            with (
                mock.patch("secretary.installation._ensure_installation_user") as ensure_user,
                mock.patch(
                    "secretary.installation._clone_or_reuse", return_value="reused checkpoint checkout"
                ),
                mock.patch(
                    "secretary.installation.read_runtime_env",
                    side_effect=RuntimeEnvError("stop after user check"),
                ),
            ):
                result = install(args)

            ensure_user.assert_called_once_with(getpass.getuser(), recovery=True, dry_run=False)
            self.assertFalse(result.ok)
            self.assertIn("stop after user check", result.steps[-1].detail)

    def test_checkpoint_materialization_builds_local_json_and_never_copies_derived_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)
            stale = instance / "state" / "memory" / "index.sqlite"
            stale.write_bytes(b"must not move")

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            cards = json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))
            self.assertEqual(cards["cards"], [CARD])
            self.assertFalse((data / "memory" / "index.sqlite").exists())
            self.assertFalse((data / "worktrees").exists())

    def test_checkpoint_materialization_rebuilds_the_sprint_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data, sprints=[SPRINT])

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            sprints = json.loads((data / "board" / "sprints.json").read_text(encoding="utf-8"))
            self.assertEqual(sprints["sprints"], [SPRINT])
            self.assertTrue((data / "board" / "sprints.ndjson").is_file())

    def test_checkpoint_sprint_count_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data, sprints=[SPRINT])
            (instance / "state" / "board" / "sprints.ndjson").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "sprint count does not match"):
                materialize_checkpoint(instance, data)

    def test_checkpoint_predating_sprint_export_materializes_an_empty_set(self):
        """An instance repo whose last tick ran before sprints joined the export."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)

            self.assertEqual(materialize_checkpoint(instance, data), (1, 0))

            sprints = json.loads((data / "board" / "sprints.json").read_text(encoding="utf-8"))
            self.assertEqual(sprints["sprints"], [])

    def test_checkpoint_materialization_restores_the_routing_journal(self):
        """secretary-716: per-attempt head telemetry lives only in the journal, so a recovery that
        rebuilt the data plane without `events.ndjson` would lose every finished card's head pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            _checkpoint(instance, data)
            event = {
                "event_id": "evt_routing",
                "schema_version": 1,
                "kind": "routing",
                "occurred_at": "2026-07-24T00:00:00Z",
                "outcome": "success",
                "actor": {"role": "dispatcher", "id": "secretary-dispatcher"},
                "task_id": "task_kanboard_1",
                "ref": "secretary-1",
                "backend": {"kind": "kanboard", "task_id": 1, "revision": "updated_at:x"},
                "request_id": "routing-verdict",
                "payload": {
                    "attempt": 1,
                    "attempt_id": "attempt-1",
                    "phase": "verdict",
                    "outcome": "red",
                    "heads": [
                        {
                            "role": "worker",
                            "head": "codex",
                            "model": "gpt-5.6-terra",
                            "model_source": "profile",
                        },
                        {
                            "role": "reviewer",
                            "head": "claude-opus",
                            "model": "opus",
                            "model_source": "profile",
                        },
                    ],
                },
            }
            (instance / "state" / "board" / "events.ndjson").write_text(
                json.dumps(event) + "\n", encoding="utf-8"
            )

            materialize_checkpoint(instance, data)

            restored = [
                json.loads(line)
                for line in (data / "board" / "events.ndjson").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            history = attempts(restored, "secretary-1")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].reviewer.head, "claude-opus")
            self.assertEqual(history[0].outcome, "red")

    def test_non_secretary_data_target_is_refused_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            data.mkdir()
            marker = data / "owned-by-operator"
            marker.write_text("keep", encoding="utf-8")
            _checkpoint(instance, data)

            with self.assertRaisesRegex(InstallError, "choose adopt"):
                materialize_checkpoint(instance, data)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_clean_target_clones_then_resumes_recovery_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            remote = root / "instance.git"
            target = root / "target"
            data = root / "data"
            bootstrap = root / "bootstrap-token"
            bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
            bootstrap.chmod(0o600)
            source.mkdir()
            _checkpoint(source, data)
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            # The config identity is the clone URL the recovery command verifies.
            text = (source / "instance.yaml").read_text(encoding="utf-8")
            (source / "instance.yaml").write_text(text.replace("placeholder", str(remote)), encoding="utf-8")
            _git(source, "add", "instance.yaml")
            _git(source, "commit", "-m", "remote identity")
            _git(source, "push", str(remote), "HEAD:master")

            # An install materializes the checkout it is told to, and this one is not `~/secretary`.
            base = [
                "--instance-remote",
                str(remote),
                "--instance-dir",
                str(target),
                "--installation-user",
                getpass.getuser(),
                "--product-root",
                str(PRODUCT_ROOT),
                "--bootstrap-credential-file",
                str(bootstrap),
            ]
            host = SimpleNamespace(steps=[SimpleNamespace(status="changed")])
            patches = (
                mock.patch(
                    "secretary.installation.check_prerequisites",
                    side_effect=(InstallError("simulated interrupted recovery"), None, None),
                ),
                mock.patch("secretary.installation.import_normalized_board", return_value=1),
                mock.patch("secretary.installation.rebuild_memory_index", return_value=1),
                mock.patch("secretary.installation.materialize_host", return_value=host),
                mock.patch("secretary.installation.materialize_pipeline_state", return_value=0),
                mock.patch("secretary.installation.restore_findings", return_value=[]),
                mock.patch("secretary.bootstrap.ensure_pipeline_board"),
            )
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                mock.patch("secretary.installation._set_installation_owner") as set_owner,
            ):
                with mock.patch("secretary.installation._ensure_installation_user"):
                    first_code, first_output = self._cli(["install", *base])
                second_code, second_output = self._cli(["recover", *base])
                third_code, third_output = self._cli(["recover", *base])

            self.assertEqual(first_code, 1, first_output)
            self.assertIn("simulated interrupted recovery", first_output)
            self.assertIn(
                mock.call(target / ".gitignore", getpass.getuser()),
                set_owner.call_args_list,
            )
            self.assertTrue((target / ".git").exists())
            self.assertFalse((target / "runtime.env").exists())
            self.assertIn("skipped   runtime-env", second_output)
            self.assertEqual(second_code, 0, second_output)
            self.assertEqual(third_code, 0, third_output)
            self.assertIn("status: ok", second_output)
            self.assertEqual(
                json.loads((data / "board" / "cards.json").read_text(encoding="utf-8"))["cards"],
                [CARD],
            )
            self.assertFalse((target / "state" / "memory" / "index.sqlite").exists())

    def test_recover_dry_run_validates_checkpoint_without_materializing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            data = root / "data"
            source.mkdir()
            _checkpoint(source, data)
            materialize_checkpoint(source, data)
            before = {path.relative_to(data): path.read_bytes() for path in data.rglob("*") if path.is_file()}
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            runtime = target / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://127.0.0.1/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=test\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)

            with (
                mock.patch("secretary.installation.check_prerequisites") as prerequisites,
                mock.patch("secretary.installation.import_normalized_board") as board,
                mock.patch("secretary.installation.rebuild_memory_index") as memory,
                mock.patch("secretary.installation.materialize_host") as host,
                mock.patch("secretary.installation.mark_reconcile_applied") as reconcile,
            ):
                code, output = self._cli(
                    [
                        "recover",
                        "--instance-remote",
                        str(source),
                        "--instance-dir",
                        str(target),
                        "--installation-user",
                        getpass.getuser(),
                        "--dry-run",
                    ]
                )

            self.assertEqual(code, 0, output)
            self.assertIn("would-change checkpoint", output)
            self.assertIn("preview made no recovery changes", output)
            self.assertTrue(prerequisites.call_args.args[0].token)
            after = {path.relative_to(data): path.read_bytes() for path in data.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            board.assert_not_called()
            memory.assert_not_called()
            host.assert_not_called()
            reconcile.assert_not_called()

    def test_existing_checkout_requires_explicit_recovery_choice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            _checkpoint(source, root / "data")
            _git(source, "init")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", str(source), str(target)], check=True, capture_output=True, text=True
            )

            with mock.patch("secretary.installation._ensure_installation_user"):
                code, output = self._cli(
                    [
                        "install",
                        "--instance-remote",
                        str(source),
                        "--instance-dir",
                        str(target),
                        "--installation-user",
                        getpass.getuser(),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("choose --recover", output)
            self.assertIn("adopt", output)

    def test_a_product_root_holding_no_product_is_refused_by_name(self):
        """The selected checkout is checked where it is selected.

        An empty or moved path would otherwise arrive as an ENOENT on a file inside it, several
        steps later, naming a directory the operator never meant to install from.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "not-a-checkout"
            empty.mkdir()
            args = SimpleNamespace(product_root=str(empty))

            with self.assertRaises(InstallError) as refusal:
                _product_root(args)

            self.assertIn(str(empty), str(refusal.exception))
            self.assertIn("--product-root", str(refusal.exception))

    def test_the_default_product_root_follows_the_configured_checkout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            home.mkdir()
            args = SimpleNamespace(product_root=None)

            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                os.environ.pop("TA_SECRETARY_REPO", None)
                with self.assertRaisesRegex(InstallError, str(home / "secretary")):
                    _product_root(args)
                os.environ["TA_SECRETARY_REPO"] = str(PRODUCT_ROOT)
                self.assertEqual(_product_root(args), PRODUCT_ROOT)

    @staticmethod
    def _cli(argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()


if __name__ == "__main__":
    unittest.main()
