from __future__ import annotations

import contextlib
import getpass
import hashlib
import io
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import _proc, installation, restore_commands, secret_store, state_repo
from secretary.board_transport import DEFAULT_TRANSPORT
from secretary.checkpoint import CheckpointPusher
from secretary.cli import main
from secretary.config import InstanceReport
from secretary.data import export_runs
from secretary.host import CollectResult, HostInventory
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
from secretary.projects.availability import ProjectAvailability
from secretary.routing_journal import attempts
from secretary.runtime_env import RuntimeEnvError
from secretary.secret_words import RECOVERY_WORDS
from secretary.upgrade import UpgradeResult, step_host
from tests.fakes.installation import CARD, PRODUCT_ROOT, SPRINT, _checkpoint, _git


# The checkout these tests run out of, which is the one they have. Nothing resolves it for them:
# an install materializes the configured checkout or `~/secretary`, and neither exists on a machine
# that only checked this branch out somewhere.
class InstallationTests(unittest.TestCase):
    def test_recovery_ownership_barrier_precedes_reused_checkpoint_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            data = Path(temporary) / "data"
            key = secret_store.key_path(target)
            key.parent.mkdir(parents=True)
            key.write_text("not-secret-fixture-material\n", encoding="utf-8")
            key.chmod(0o600)
            report = SimpleNamespace(data_dir=data)
            events: list[str] = []
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="remote",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
            )

            def barrier(*_args):
                events.append("ownership")

            def reuse(*_args, **_kwargs):
                events.append("git")
                raise InstallError("stop after ordering proof")

            with (
                mock.patch("secretary.installation._ensure_installation_user"),
                mock.patch("secretary.installation._validated_instance", return_value=report),
                mock.patch(
                    "secretary.installation._establish_recovery_ownership_barrier",
                    side_effect=barrier,
                ),
                mock.patch("secretary.installation._clone_or_reuse", side_effect=reuse),
            ):
                result = installation.install(args)

            self.assertEqual(events, ["ownership", "git"])
            self.assertEqual(result.status, "failed")

    def test_recovery_ownership_barrier_refuses_unsafe_key_shape_or_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            data = root / "data"
            key = secret_store.key_path(instance)
            key.parent.mkdir(parents=True)
            key.write_text("fixture\n", encoding="utf-8")
            key.chmod(0o640)
            with self.assertRaisesRegex(InstallError, "mode 0600"):
                installation._establish_recovery_ownership_barrier(instance, data, None)
            key.unlink()
            target = root / "elsewhere"
            target.write_text("fixture\n", encoding="utf-8")
            key.symlink_to(target)
            with self.assertRaisesRegex(InstallError, "regular non-symlink"):
                installation._establish_recovery_ownership_barrier(instance, data, None)

    @unittest.skipUnless(os.geteuid() == 0, "requires a real root-to-runtime-user recovery fixture")
    def test_recovery_barrier_enables_real_child_git_and_key_loading(self):
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("fixture has no nobody user")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            instance = root / "instance"
            data = root / "data"
            instance.mkdir()
            data.mkdir()
            _git(instance, "init", "-b", "main")
            _git(instance, "config", "user.name", "Test")
            _git(instance, "config", "user.email", "test@example.invalid")
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            _git(instance, "add", "instance.yaml")
            _git(instance, "commit", "-m", "fixture")
            phrase = " ".join(RECOVERY_WORDS[:16])
            secret_store.initialize_store(instance, phrase=phrase, actor="fixture")
            key = secret_store.key_path(instance)
            os.chown(key, 0, 0)

            installation._establish_recovery_ownership_barrier(instance, data, account.pw_name)

            identity = state_repo.git_child_identity(instance)
            self.assertEqual((identity.uid, identity.gid), (account.pw_uid, account.pw_gid))
            self.assertEqual(state_repo.git(instance, ["rev-parse", "--is-inside-work-tree"]), "true\n")
            completed = subprocess.run(
                [
                    "runuser",
                    "--user",
                    account.pw_name,
                    "--",
                    sys.executable,
                    "-c",
                    (
                        "import os,sys; from secretary.secret_store import load_installation_key; "
                        "load_installation_key(sys.argv[1]); print(os.geteuid())"
                    ),
                    str(instance),
                ],
                cwd="/",
                env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.stdout.strip(), str(account.pw_uid))
            info = key.lstat()
            self.assertEqual(
                (info.st_uid, info.st_gid, info.st_mode & 0o777),
                (account.pw_uid, account.pw_gid, 0o600),
            )

    def test_isolated_git_timeout_reaps_its_descendant_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); child.wait()"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                _proc.run_isolated([sys.executable, "-c", script], timeout=0.2)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(100):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.01)
            self.assertFalse(Path(f"/proc/{child_pid}").exists(), "timed-out clone descendant survived")
            print(f"timeout descendant cleanup: pid {child_pid} absent")

    def test_isolated_git_interrupt_reaps_its_descendant_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            script = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); child.wait()"
            )
            interrupt = threading.Timer(0.2, os.kill, args=(os.getpid(), signal.SIGINT))
            interrupt.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    _proc.run_isolated([sys.executable, "-c", script], timeout=10)
            finally:
                interrupt.cancel()
                interrupt.join()
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(100):
                if not Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.01)
            self.assertFalse(Path(f"/proc/{child_pid}").exists(), "interrupted clone descendant survived")
            print(f"interrupt descendant cleanup: pid {child_pid} absent")

    def test_fresh_instance_clone_is_bounded_and_reuse_stays_shallow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "instance.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            payload = source / "historical.bin"
            for revision in range(8):
                payload.write_bytes(
                    b"".join(hashlib.sha256(f"{revision}:{block}".encode()).digest() for block in range(8192))
                )
                _git(source, "add", "historical.bin")
                _git(source, "commit", "-m", f"large history {revision}")
            payload.unlink()
            (source / "checkpoint").write_text("current\n", encoding="utf-8")
            _git(source, "add", "-A")
            _git(source, "commit", "-m", "current checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(remote, "gc")
            remote_bytes = sum(path.stat().st_size for path in (remote / "objects" / "pack").glob("*.pack"))
            remote_url = remote.as_uri()

            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "cloned private instance remote",
            )

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(target), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            first = git("rev-parse", "HEAD")
            self.assertEqual(git("rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(git("rev-list", "--count", "HEAD"), "1")
            self.assertEqual(git("rev-parse", "--abbrev-ref", "@{u}"), "origin/main")
            clone_bytes = sum(
                path.stat().st_size for path in (target / ".git" / "objects" / "pack").glob("*.pack")
            )
            self.assertLess(clone_bytes * 4, remote_bytes)

            (source / "checkpoint").write_text("advanced\n", encoding="utf-8")
            _git(source, "add", "checkpoint")
            _git(source, "commit", "-m", "advance checkpoint")
            _git(source, "push", str(remote), "main")
            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
            second = git("rev-parse", "HEAD")
            self.assertNotEqual(first, second)
            self.assertEqual(git("rev-parse", "--is-shallow-repository"), "true")
            self.assertEqual(git("rev-list", "--count", "HEAD"), "2")
            object_bytes = sum(
                path.stat().st_size for path in (target / ".git" / "objects").rglob("*") if path.is_file()
            )

            self.assertEqual(
                _clone_or_reuse(remote_url, target, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
            self.assertEqual(git("rev-parse", "HEAD"), second)
            self.assertEqual(
                sum(
                    path.stat().st_size for path in (target / ".git" / "objects").rglob("*") if path.is_file()
                ),
                object_bytes,
            )
            print(
                "shallow recovery evidence: "
                f"remote_pack={remote_bytes} fresh_pack={clone_bytes} "
                f"fresh_commits=1 reused_commits=2 reused_objects={object_bytes} "
                "unchanged_objects=" + str(object_bytes)
            )

            _git(target, "config", "user.name", "Test")
            _git(target, "config", "user.email", "test@example.invalid")
            (target / "checkpoint").write_text("locally checkpointed\n", encoding="utf-8")
            _git(target, "add", "checkpoint")
            _git(target, "commit", "-m", "local checkpoint")
            pushed = CheckpointPusher(target, interval_seconds=0).push()
            self.assertEqual(pushed["status"], "pushed")
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                git("rev-parse", "HEAD"),
            )

    def test_initial_clone_stages_validates_and_adopts_an_empty_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            target.mkdir()

            def clone(_execution, staging, **kwargs):
                self.assertEqual(
                    kwargs["clone_args"],
                    ["--depth=1", "--single-branch", "--no-tags", "--no-local"],
                )
                self.assertEqual(staging.parent.stat().st_mode & 0o777, 0o700)
                staging.mkdir()

            with (
                mock.patch(
                    "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                ),
                mock.patch("secretary.installation._validate_initial_clone") as validate,
            ):
                installation._clone_instance("remote", target, bootstrap_credential=None)

            validate.assert_called_once()
            self.assertTrue(target.is_dir())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_failures_preserve_target_and_remove_staging(self):
        for failure in (InstallError("invalid clone"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "instance"
                target.mkdir()

                def clone(_execution, staging, **_kwargs):
                    staging.mkdir()

                with (
                    mock.patch(
                        "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                    ),
                    mock.patch("secretary.installation._validate_initial_clone", side_effect=failure),
                    self.assertRaises(type(failure)),
                ):
                    installation._clone_instance("remote", target, bootstrap_credential=None)
                self.assertTrue(target.is_dir())
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_atomic_adoption_failure_preserves_empty_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            target.mkdir()

            def clone(_execution, staging, **_kwargs):
                staging.mkdir()

            with (
                mock.patch(
                    "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                ),
                mock.patch("secretary.installation._validate_initial_clone"),
                mock.patch("secretary.installation.os.replace", side_effect=OSError("fixture")),
                self.assertRaisesRegex(InstallError, "atomic replacement failed"),
            ):
                installation._clone_instance("remote", target, bootstrap_credential=None)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_ownership_failure_prevents_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"

            def clone(_execution, staging, **_kwargs):
                staging.mkdir()

            with (
                mock.patch(
                    "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                ),
                mock.patch("secretary.installation._validate_initial_clone"),
                mock.patch(
                    "secretary.installation._set_installation_owner",
                    side_effect=InstallError("ownership handoff failed"),
                ),
                self.assertRaisesRegex(InstallError, "ownership handoff failed"),
            ):
                installation._clone_instance(
                    "remote", target, bootstrap_credential=None, installation_user="runtime"
                )
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_initial_clone_refuses_an_invalid_remote_default_branch_without_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            remote = root / "remote.git"
            target = root / "instance"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "checkpoint").write_text("current\n", encoding="utf-8")
            _git(source, "add", "checkpoint")
            _git(source, "commit", "-m", "checkpoint")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            _git(remote, "symbolic-ref", "HEAD", "refs/heads/missing")

            with self.assertRaisesRegex(InstallError, "cloned branch"):
                installation._clone_instance(remote.as_uri(), target, bootstrap_credential=None)
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".instance.clone-*")), [])

    def test_existing_invalid_remote_and_dirty_checkout_are_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            target.mkdir()
            (target / ".git").mkdir()
            with (
                mock.patch("secretary.installation.state_repo.git", return_value="other\n"),
                self.assertRaisesRegex(InstallError, "different instance remote"),
            ):
                _clone_or_reuse("expected", target, recovery=True, dry_run=False)
            marker = target / "marker"
            marker.write_text("untouched", encoding="utf-8")
            with (
                mock.patch(
                    "secretary.installation.state_repo.git", side_effect=("expected\n", " M marker\n")
                ),
                self.assertRaisesRegex(InstallError, "local changes"),
            ):
                _clone_or_reuse("expected", target, recovery=True, dry_run=False)
            self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")

    def test_prefixed_partial_git_directory_gets_cleanup_or_fresh_target_guidance(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "instance"
            (target / ".git" / "objects").mkdir(parents=True)
            marker = target / "partial-pack"
            marker.write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(InstallError, "remove a failed partial target.*fresh"):
                _clone_or_reuse("remote", target, recovery=True, dry_run=False)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

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

            first = provision_project_checkouts([binding], None, instance_dir=source)
            second = provision_project_checkouts([binding], None, instance_dir=source)
            self.assertEqual([row.outcome for row in first], ["cloned"])
            self.assertEqual([row.outcome for row in second], ["unchanged"])
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "demo\n")

    def test_project_failures_are_isolated_and_rows_are_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            bindings = [
                {
                    "id": "first",
                    "repo": str(root / "first"),
                    "remote": "https://example.invalid/a",
                    "default_branch": "main",
                },
                {
                    "id": "middle",
                    "repo": str(root / "middle"),
                    "remote": "https://github.example/b",
                    "default_branch": "main",
                },
                {
                    "id": "healthy",
                    "repo": str(root / "healthy"),
                    "remote": str(root / "healthy.git"),
                    "default_branch": "main",
                },
            ]
            source = root / "source"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Test")
            _git(source, "config", "user.email", "test@example.invalid")
            (source / "README").write_text("ok", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "initial")
            subprocess.run(
                ["git", "clone", "--bare", str(source), str(root / "healthy.git")],
                check=True,
                capture_output=True,
            )

            rows = provision_project_checkouts(bindings, None, instance_dir=instance)

            self.assertEqual([row.outcome for row in rows], ["failed", "failed", "cloned"])
            self.assertEqual([row.code for row in rows], ["unsupported-https", "unsupported-https", "cloned"])
            rendered = json.dumps([row.__dict__ for row in rows])
            self.assertNotIn("example.invalid/a", rendered)
            self.assertTrue((root / "healthy" / ".git").exists())

    def test_invalid_binding_target_and_collision_are_project_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            collision = root / "collision"
            collision.write_text("leave untouched", encoding="utf-8")
            loop = root / "loop"
            loop.symlink_to(loop)
            rows = provision_project_checkouts(
                [
                    {"id": "binding"},
                    {
                        "id": "target",
                        "repo": str(loop / "checkout"),
                        "remote": str(root / "remote"),
                        "default_branch": "main",
                    },
                    {
                        "id": "collision",
                        "repo": str(collision),
                        "remote": str(root / "remote"),
                        "default_branch": "main",
                    },
                ],
                None,
                instance_dir=instance,
            )
            self.assertEqual(
                [row.code for row in rows], ["invalid-binding", "invalid-target", "target-collision"]
            )
            self.assertEqual(collision.read_text(encoding="utf-8"), "leave untouched")

    def test_project_timeout_removes_staging_and_propagates_operator_interrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            binding = {
                "id": "slow",
                "repo": str(root / "slow"),
                "remote": str(root / "remote"),
                "default_branch": "main",
            }
            timeout = installation.CredentialError("clone: command timed out", code="timeout")
            with mock.patch("secretary.installation.RemoteExecution.run_clone", side_effect=timeout):
                rows = provision_project_checkouts([binding], None, instance_dir=instance)
            self.assertEqual((rows[0].code, rows[0].retryable), ("timeout", True))
            self.assertFalse((root / "slow").exists())
            self.assertEqual(list(root.glob(".slow.clone-*")), [])
            with (
                mock.patch("secretary.installation.RemoteExecution.run_clone", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                provision_project_checkouts([binding], None, instance_dir=instance)

    def test_project_remote_failures_keep_typed_sanitized_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            for code, retryable in (
                ("authentication", True),
                ("network", True),
                ("process", True),
                ("invalid-branch", False),
            ):
                binding = {
                    "id": code,
                    "repo": str(root / code),
                    "remote": "https://github.com/example/private.git",
                    "default_branch": "main",
                }
                failure = installation.CredentialError("contains-secret-value", code=code)
                with mock.patch("secretary.installation.RemoteExecution.run_clone", side_effect=failure):
                    row = provision_project_checkouts([binding], None, instance_dir=instance)[0]
                self.assertEqual((row.code, row.retryable), (code, retryable))
                self.assertNotIn("contains-secret-value", row.reason)
                self.assertFalse((root / code).exists())

    def test_project_retry_only_clones_the_previous_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            bindings = [
                {
                    "id": name,
                    "repo": str(root / name),
                    "remote": str(root / f"{name}.git"),
                    "default_branch": "main",
                }
                for name in ("one", "two", "three")
            ]
            calls: list[str] = []

            def clone(execution, target, **_kwargs):
                calls.append(target.parent.name)
                if target.parent.name.endswith("two.clone-fixture"):
                    raise installation.CredentialError("failed", code="network")
                target.mkdir()
                (target / ".git").mkdir()

            # Use deterministic staging names so the injected middle failure is clear.
            counter = iter(("one", "two", "three"))

            def staging(*_args, dir=None, **_kwargs):
                path = Path(dir) / f".{next(counter)}.clone-fixture"
                path.mkdir()
                return str(path)

            with (
                mock.patch("secretary.installation.tempfile.mkdtemp", side_effect=staging),
                mock.patch(
                    "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=clone
                ),
            ):
                first = provision_project_checkouts(bindings, None, instance_dir=instance)
            self.assertEqual([row.outcome for row in first], ["cloned", "failed", "cloned"])

            def repaired(execution, target, **_kwargs):
                calls.append(target.parent.name)
                target.mkdir()
                (target / ".git").mkdir()

            with mock.patch(
                "secretary.installation.RemoteExecution.run_clone", autospec=True, side_effect=repaired
            ):
                second = provision_project_checkouts(bindings, None, instance_dir=instance)
            self.assertEqual([row.outcome for row in second], ["unchanged", "cloned", "unchanged"])

    def test_degraded_project_results_are_truthful_in_text_and_json(self):
        result = installation.InstallResult(
            projects=[
                installation.ProjectProvisionResult(
                    "missing",
                    "missing",
                    "github-https",
                    "failed",
                    "authentication",
                    "remote authentication failed",
                    True,
                )
            ]
        )
        result.add("status", "degraded", "core ready; rerun secretary recover with the same inputs")
        self.assertEqual(result.status, "degraded")
        self.assertIn("status: degraded", result.render())
        args = SimpleNamespace(
            bootstrap_credential_stdin=False,
            recovery_phrase_stdin=False,
            json=True,
        )
        output = io.StringIO()
        with (
            mock.patch("secretary.installation.install", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            code = installation.run_install(args)
        payload = json.loads(output.getvalue())
        self.assertEqual((code, payload["status"]), (1, "degraded"))
        self.assertEqual(payload["projects"][0]["project_id"], "missing")

    def test_project_progress_persists_only_non_secret_outcomes_and_is_identity_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            progress = root / "recovery-progress.json"
            secret = "credential-material"
            binding = {
                "id": "demo",
                "repo": str(root / "demo"),
                "remote": f"https://user:{secret}@github.com/example/private.git",
                "default_branch": "main",
            }
            rows = provision_project_checkouts(
                [binding],
                None,
                instance_dir=instance,
                progress_path=progress,
                recovery_identity="identity-one",
            )
            self.assertEqual(rows[0].code, "unsafe-remote")
            stored = progress.read_text(encoding="utf-8")
            self.assertNotIn(secret, stored)
            self.assertNotIn(str(root / "demo"), stored)
            self.assertEqual(
                installation._read_recovery_progress(progress, "identity-two"),
                {"version": 1, "identity": "identity-two"},
            )

    def test_recovery_identity_tracks_changed_added_and_removed_memory_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            _checkpoint(instance, root / "data")
            facts = instance / "state" / "memory" / "facts"
            original = facts / "fact.md"
            progress = root / "recovery-progress.json"

            baseline = installation._recovery_identity(instance, [])
            self.assertEqual(installation._recovery_identity(instance, []), baseline)
            installation._write_recovery_progress(progress, baseline, memory="complete")
            self.assertEqual(
                installation._read_recovery_progress(progress, baseline)["memory"],
                "complete",
            )

            original.write_text("# changed fact\n", encoding="utf-8")
            changed = installation._recovery_identity(instance, [])
            self.assertNotEqual(changed, baseline)
            self.assertNotIn("memory", installation._read_recovery_progress(progress, changed))
            original.write_text("# recovered fact\n", encoding="utf-8")
            self.assertEqual(installation._recovery_identity(instance, []), baseline)

            added = facts / "nested" / "added.md"
            added.parent.mkdir()
            added.write_text("# added fact\n", encoding="utf-8")
            self.assertNotEqual(installation._recovery_identity(instance, []), baseline)
            added.unlink()
            added.parent.rmdir()
            self.assertEqual(installation._recovery_identity(instance, []), baseline)

            original.unlink()
            self.assertNotEqual(installation._recovery_identity(instance, []), baseline)

    def test_recovery_identity_length_delimits_fact_paths_types_and_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = root / "instance"
            instance.mkdir()
            _checkpoint(instance, root / "data")
            facts = instance / "state" / "memory" / "facts"
            (facts / "fact.md").unlink()
            first = facts / "a"
            first.write_bytes(b"b\0file\0Z")
            one_file = installation._recovery_identity(instance, [])

            first.write_bytes(b"")
            (facts / "b").write_bytes(b"Z")
            two_files = installation._recovery_identity(instance, [])

            self.assertNotEqual(one_file, two_files)

    def test_restore_reconcile_leaves_unavailable_checkout_explicitly_degraded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = SimpleNamespace(
                ok=True,
                data_dir=root / "data",
                instance_path=root / "instance" / "instance.yaml",
                instance={},
                host={},
                bindings=[
                    {
                        "id": "missing",
                        "repo": str(root / "missing-checkout"),
                        "enabled": True,
                        "orca_binding": "missing",
                    }
                ],
            )
            source = mock.Mock()
            source.collect.return_value = CollectResult(inventory=HostInventory())

            with (
                mock.patch.object(restore_commands, "validate_instance", return_value=report),
                mock.patch.object(restore_commands, "resolve_installed_packaged", return_value=[]),
                mock.patch.object(
                    restore_commands,
                    "_target",
                    return_value=(report.instance_path, report.data_dir, {}),
                ),
                mock.patch.object(restore_commands, "LiveHostSource", return_value=source),
                mock.patch.object(restore_commands, "_print_json") as emit,
                mock.patch.object(restore_commands, "mark_reconcile_applied") as mark_applied,
            ):
                code = restore_commands.run_restore_reconcile(
                    SimpleNamespace(instance=str(report.instance_path.parent))
                )

            self.assertEqual(code, 1)
            payload = emit.call_args.args[0]
            self.assertEqual(payload["status"], "degraded")
            self.assertEqual(payload["unavailable_projects"], ["missing"])
            self.assertIn("remains incomplete", payload["error"])
            mark_applied.assert_not_called()

    def test_materializer_preserves_desired_bindings_and_carries_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = InstanceReport(
                instance_path=root / "instance" / "instance.yaml",
                name="test",
                projects=2,
                adapters=0,
                adapter_drafts=0,
                has_manifest=True,
                manifest_path=root / "manifest",
                errors=[],
                warnings=[],
                bindings=[{"id": "ready"}, {"id": "missing"}],
                host={},
                instance={},
                data_dir=root / "data",
            )

            def run(context, *, steps=installation.STEPS):
                self.assertEqual(context.report.bindings, [{"id": "ready"}, {"id": "missing"}])
                self.assertEqual(context.project_availability.unavailable, frozenset({"missing"}))
                return UpgradeResult()

            with (
                mock.patch("secretary.installation.validate_instance", return_value=report),
                mock.patch(
                    "secretary.installation.resolve_runtime_owner", return_value=(None, root / "home")
                ),
                mock.patch("secretary.installation.run_steps", side_effect=run),
            ):
                installation.materialize_host(
                    root / "instance",
                    root / "product",
                    project_availability=ProjectAvailability(frozenset({"missing"})),
                )

    def test_degraded_recovery_finishes_host_pipeline_state_and_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            data = root / "data"
            target.mkdir()
            (target / ".git").mkdir()
            _checkpoint(target, data)
            report = InstanceReport(
                instance_path=target / "instance.yaml",
                name="test",
                projects=1,
                adapters=0,
                adapter_drafts=0,
                has_manifest=True,
                manifest_path=data / "data-manifest.json",
                errors=[],
                warnings=[],
                bindings=[
                    {
                        "id": "missing",
                        "repo": str(root / "missing"),
                        "remote": "https://example.invalid/private.git",
                        "default_branch": "main",
                    }
                ],
                host={},
                instance={},
                data_dir=data,
            )
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="file:///instance.git",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
                host_fixture=None,
            )
            transport = SimpleNamespace(
                transport=DEFAULT_TRANSPORT,
                changed=False,
                render=lambda **_kwargs: "unchanged",
            )
            host_result = SimpleNamespace(
                steps=[
                    SimpleNamespace(
                        name="head-registry-checkpoint",
                        status="degraded",
                        detail="head registry checkpoint failed; local checkpoint deadbeef",
                    ),
                    SimpleNamespace(name="host", status="changed", detail="completed"),
                ]
            )

            def host(*_args, before_host=None, **_kwargs):
                before_host(SimpleNamespace(runtime_home=root / "home"))
                return host_result

            with (
                mock.patch("secretary.installation._ensure_installation_user"),
                mock.patch(
                    "secretary.installation._clone_or_reuse", return_value="reused checkpoint checkout"
                ),
                mock.patch(
                    "secretary.installation._open_secret_store",
                    return_value=installation.SecretRecovery(store_present=True, unlocked=True),
                ),
                mock.patch("secretary.installation.read_runtime_env", return_value={}),
                mock.patch("secretary.installation.ensure_from_runtime_values", return_value=transport),
                mock.patch("secretary.installation.check_prerequisites"),
                mock.patch("secretary.installation._validated_instance", return_value=report),
                mock.patch("secretary.bootstrap.ensure_pipeline_board"),
                mock.patch("secretary.installation.import_normalized_board", return_value=1),
                mock.patch("secretary.installation.rebuild_memory_index", return_value=1),
                mock.patch("secretary.installation.provision_codex_home", return_value=0),
                mock.patch(
                    "secretary.installation.materialize_pipeline_state",
                    return_value=installation.PipelineStateMaterialization(0, True),
                ),
                mock.patch("secretary.installation.materialize_host", side_effect=host) as materialize,
                mock.patch("secretary.installation.mark_reconcile_applied"),
                mock.patch("secretary.installation.restore_findings", return_value=[]),
                mock.patch("secretary.installation._set_installation_owner") as owner,
            ):
                result = installation.install(args)

            self.assertEqual(result.status, "degraded")
            self.assertEqual(result.projects[0].code, "unsupported-https")
            self.assertEqual(
                [step.name for step in result.steps if step.status == "degraded"],
                ["runtime", "checkpoint-publication", "status"],
            )
            self.assertTrue(any(step.name == "pipeline-state" for step in result.steps))
            self.assertEqual(
                materialize.call_args.kwargs["project_availability"].unavailable,
                frozenset({"missing"}),
            )
            self.assertIn(mock.call(target, getpass.getuser()), owner.call_args_list)

    def test_fatal_board_failure_does_not_enter_project_or_host_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "instance"
            data = root / "data"
            target.mkdir()
            (target / ".git").mkdir()
            _checkpoint(target, data)
            report = InstanceReport(
                target / "instance.yaml",
                "test",
                0,
                0,
                0,
                True,
                data / "data-manifest.json",
                [],
                [],
                [],
                {},
                {},
                data,
            )
            args = SimpleNamespace(
                instance_dir=str(target),
                instance_remote="file:///instance.git",
                installation_user=getpass.getuser(),
                recover=True,
                adopt=False,
                dry_run=False,
                runtime_env=None,
                product_root=str(PRODUCT_ROOT),
                bootstrap_credential_file=None,
                bootstrap_credential_stdin=False,
                recovery_phrase_file=None,
                recovery_phrase_stdin=False,
                host_fixture=None,
            )
            transport = SimpleNamespace(
                transport=DEFAULT_TRANSPORT, changed=False, render=lambda **_kwargs: "unchanged"
            )
            with (
                mock.patch("secretary.installation._ensure_installation_user"),
                mock.patch(
                    "secretary.installation._clone_or_reuse", return_value="reused checkpoint checkout"
                ),
                mock.patch(
                    "secretary.installation._open_secret_store",
                    return_value=installation.SecretRecovery(True, True),
                ),
                mock.patch("secretary.installation.read_runtime_env", return_value={}),
                mock.patch("secretary.installation.ensure_from_runtime_values", return_value=transport),
                mock.patch("secretary.installation.check_prerequisites"),
                mock.patch("secretary.installation._validated_instance", return_value=report),
                mock.patch("secretary.bootstrap.ensure_pipeline_board"),
                mock.patch(
                    "secretary.installation.import_normalized_board",
                    side_effect=installation.RestoreError("parity failed"),
                ),
                mock.patch("secretary.installation.provision_project_checkouts") as projects,
                mock.patch("secretary.installation.materialize_host") as host,
                mock.patch("secretary.installation._set_installation_owner"),
            ):
                result = installation.install(args)
            self.assertEqual(result.status, "failed")
            projects.assert_not_called()
            host.assert_not_called()

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
                patches[1] as board_restore,
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
            board_restore.assert_called_once_with(data, instance=target)
            self.assertIn("unchanged board", third_output)
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
