import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import installation, secret_store
from secretary.infra import github_credential
from secretary.checkpoint import CheckpointPusher
from secretary.secret_words import RECOVERY_WORDS


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout


def fast_key_params():
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {"id": "scrypt", "salt": secret_store._b64(b"0123456789abcdef"), "length": 32, "n": 2**8, "r": 8, "p": 1},
    }


class ManagedGithubCredentialTests(unittest.TestCase):
    phrase = " ".join(RECOVERY_WORDS[:16])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.instance = Path(self.temporary.name) / "instance"
        self.instance.mkdir()
        git(self.instance, "init", "--quiet", "--initial-branch", "main")
        git(self.instance, "config", "user.name", "test")
        git(self.instance, "config", "user.email", "test@example.invalid")
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance, "add", "instance.yaml")
        git(self.instance, "commit", "--quiet", "-m", "initial")
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            secret_store.initialize_store(self.instance, phrase=self.phrase, actor="test")

    def set_token(self, token: bytes = b"github-test-token") -> None:
        secret_store.set_secret(self.instance, secret_id=github_credential.CHECKPOINT_CREDENTIAL_ID, value=token, scope="installation", purpose=github_credential.CHECKPOINT_CREDENTIAL_PURPOSE, actor="test")

    def test_helper_emits_only_native_protocol_reply_for_github(self) -> None:
        self.set_token()
        output = io.StringIO()
        with (mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False), mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")), contextlib.redirect_stdout(output)):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "username=x-access-token\npassword=github-test-token\n\n")

    def test_helper_rejects_non_github_and_locked_store_without_leaking_token(self) -> None:
        token = b"github-test-token"
        self.set_token(token)
        output = io.StringIO()
        with (mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False), mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=example.invalid\n\n")), contextlib.redirect_stdout(output)):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "")
        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        stderr = io.StringIO()
        with (mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False), mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")), contextlib.redirect_stderr(stderr)):
            self.assertEqual(github_credential.run_helper("get"), 1)
        self.assertNotIn(token.decode(), stderr.getvalue())
        self.assertEqual(github_credential.checkpoint_credential_readiness(self.instance).state, "locked/unverifiable")

    def test_pusher_clears_ambient_helpers_and_fails_before_github_network_when_unready(self) -> None:
        commands: list[list[str]] = []

        def run_git(_instance, args, **_kwargs):
            commands.append(args)
            operation = args[-3:]
            if operation == ["remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(args, 0, "https://github.com/example/private.git\n", "")
            if args[-4:] == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "main\n", "")
            if args[-1:] == ["remote"]:
                return subprocess.CompletedProcess(args, 0, "origin\n", "")
            self.fail(f"unexpected command {args}")

        with mock.patch("secretary.checkpoint.state_repo.run_git", side_effect=run_git):
            state = CheckpointPusher(self.instance).push()
        self.assertEqual(state["status"], "failed")
        self.assertIn("missing/unavailable", state["reason"])
        self.assertTrue(all("credential.helper=" in command for command in commands))
        self.assertEqual(state["credential"]["state"], "missing/unavailable")

    def test_bootstrap_file_is_mode_checked_and_not_retained(self) -> None:
        source = Path(self.temporary.name) / "bootstrap"
        source.write_bytes(b"bootstrap-token")
        source.chmod(0o600)
        args = SimpleNamespace(bootstrap_credential_file=str(source), bootstrap_credential_stdin=False)
        path, disposable = installation._bootstrap_credential(args, self.instance / "target")
        self.assertEqual(path, source)
        self.assertIsNone(disposable)
        source.chmod(0o644)
        with self.assertRaisesRegex(installation.InstallError, "mode-0600"):
            installation._bootstrap_credential(args, self.instance / "target")
