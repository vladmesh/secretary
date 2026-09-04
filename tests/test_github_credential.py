import contextlib
import hashlib
import io
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary import installation, secret_store
from secretary.checkpoint import CheckpointPusher
from secretary.cli import main
from secretary.infra import github_credential
from secretary.secret_words import RECOVERY_WORDS


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout


def fast_key_params():
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {
            "id": "scrypt",
            "salt": secret_store._b64(b"0123456789abcdef"),
            "length": 32,
            "n": 2**8,
            "r": 8,
            "p": 1,
        },
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
        secret_store.set_secret(
            self.instance,
            secret_id=github_credential.CHECKPOINT_CREDENTIAL_ID,
            value=token,
            scope="installation",
            purpose=github_credential.CHECKPOINT_CREDENTIAL_PURPOSE,
            actor="test",
        )

    def run_cli(self, argv: list[str], stdin: bytes = b"") -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with (
            mock.patch("sys.stdin", stream),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            code = main(argv)
        return code, output.getvalue(), error.getvalue()

    @staticmethod
    def digest(value: str | bytes) -> str:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return hashlib.sha256(raw).hexdigest()

    def test_checkpoint_cli_normalizes_one_terminal_newline_and_is_idempotent(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        base = ["secret", "checkpoint-github", "set", "--instance", str(self.instance), "--stdin"]
        first, output, error = self.run_cli(base, (token + "\n").encode())
        self.assertEqual((first, error), (0, ""))
        self.assertNotIn(token, output)
        self.assertIn('"created": true', output)
        second, repeated, error = self.run_cli(base, (token + "\n").encode())
        self.assertEqual((second, error), (0, ""))
        self.assertNotIn(token, repeated)
        self.assertIn('"created": false', repeated)
        stored = secret_store.read_secret(self.instance, github_credential.CHECKPOINT_CREDENTIAL_ID)
        self.assertEqual(self.digest(stored), self.digest(token))
        tracked_contents = b"\n".join(
            (self.instance / path).read_bytes()
            for path in git(self.instance, "ls-files").splitlines()
            if (self.instance / path).is_file()
        )
        self.assertFalse(token.encode() in tracked_contents)

        replacement = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "checkpoint-token"
        source.write_text(replacement + "\r\n", encoding="utf-8")
        source.chmod(0o600)
        code, imported, error = self.run_cli(
            ["secret", "checkpoint-github", "import", "--instance", str(self.instance), "--file", str(source)]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn(replacement, imported)
        self.assertEqual(
            self.digest(secret_store.read_secret(self.instance, github_credential.CHECKPOINT_CREDENTIAL_ID)),
            self.digest(replacement),
        )

    def test_checkpoint_cli_refuses_insecure_or_padded_file_without_value_output(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "checkpoint-token"
        source.write_text(token + "\n", encoding="utf-8")
        source.chmod(0o644)
        argv = ["secret", "checkpoint-github", "set", "--instance", str(self.instance), "--file", str(source)]
        code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("mode-0600", output)
        self.assertNotIn(token, output)
        source.chmod(0o600)
        source.write_text(" " + token + "\n", encoding="utf-8")
        code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("content must", output)
        self.assertNotIn(token, output)
        source.write_text(token + "\n", encoding="utf-8")
        with mock.patch("secretary.secret_commands.os.geteuid", return_value=os.geteuid() + 1):
            code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("belongs to another user", output)
        self.assertNotIn(token, output)

    def test_real_git_helper_ignores_an_ambient_helper_and_uses_the_store(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        self.set_token(token.encode())
        environment = dict(os.environ)
        environment.update(github_credential.helper_environment(self.instance))
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        argv = [
            "git",
            "-c",
            "credential.helper=!false",
            *github_credential.helper_config_args(),
            "credential",
            "fill",
        ]
        self.assertNotIn(token, " ".join(argv))
        result = subprocess.run(
            argv,
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertEqual(reply.get("username"), "x-access-token")
        self.assertEqual(self.digest(reply.get("password", "")), self.digest(token))
        self.assertNotIn(token, result.stderr)

    def test_bootstrap_inputs_normalize_one_terminator_and_name_the_failure_kind(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "bootstrap"
        source.write_text(token + "\n", encoding="utf-8")
        source.chmod(0o600)
        args = SimpleNamespace(bootstrap_credential_file=str(source), bootstrap_credential_stdin=False)
        path, disposable = installation._bootstrap_credential(args, self.instance / "target")
        self.assertEqual((path, disposable), (source, None))
        self.assertEqual(self.digest(github_credential._bootstrap_token(source)), self.digest(token))
        source.write_text(" " + token + "\n", encoding="utf-8")
        with self.assertRaisesRegex(installation.InstallError, "content is rejected"):
            installation._bootstrap_credential(args, self.instance / "target")
        source.unlink()
        with self.assertRaisesRegex(installation.InstallError, "could not inspect bootstrap credential file"):
            installation._bootstrap_credential(args, self.instance / "target")

    def test_wrong_recovery_phrase_stays_a_sanitized_recovery_failure(self) -> None:
        self.set_token()
        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        with self.assertRaisesRegex(installation.InstallError, "secret store") as failure:
            installation._open_secret_store(
                self.instance, self.instance / "runtime.env", phrase="wrong recovery phrase", dry_run=True
            )
        self.assertNotIn("github-test-token", str(failure.exception))

    def test_run_install_rejects_two_stdin_secrets_before_reading_either(self) -> None:
        code, output, error = self.run_cli(
            [
                "recover",
                "--instance-remote",
                "https://github.com/example/private.git",
                "--instance-dir",
                str(self.instance / "target"),
                "--installation-user",
                "nobody",
                "--bootstrap-credential-stdin",
                "--recovery-phrase-stdin",
            ],
            b"not-consumed\n",
        )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("cannot share standard input", error)

    def test_private_remote_selector_enforces_clone_reuse_and_checkpoint_sources(self) -> None:
        with self.assertRaisesRegex(github_credential.CredentialError, "bootstrap credential is required"):
            github_credential.select_private_remote_auth("initial-clone")
        bootstrap = Path(self.temporary.name) / "bootstrap"
        bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        selected = github_credential.select_private_remote_auth("initial-clone", bootstrap_file=bootstrap)
        self.assertEqual(selected.source, "bootstrap")
        self.assertEqual(selected.environment["SECRETARY_GITHUB_BOOTSTRAP_FILE"], str(bootstrap))
        self.assertEqual(selected.command(["fetch", "origin"])[:2], ["-c", "credential.helper="])

        with self.assertRaisesRegex(github_credential.CredentialError, "needs a bootstrap credential"):
            github_credential.select_private_remote_auth("recovery-reuse", instance_dir=self.instance)
        self.set_token()
        managed = github_credential.select_private_remote_auth("recovery-reuse", instance_dir=self.instance)
        self.assertEqual(managed.source, "managed-store")
        self.assertEqual(managed.environment["SECRETARY_CHECKPOINT_INSTANCE"], str(self.instance))
        checkpoint = github_credential.select_private_remote_auth("checkpoint", instance_dir=self.instance)
        self.assertEqual(checkpoint.source, "managed-store")

    def test_recovery_reuse_selects_bootstrap_or_managed_auth_and_never_ambient(self) -> None:
        remote = "https://github.com/example/private.git"
        commands: list[tuple[list[str], dict[str, str]]] = []

        def inspect(_instance, args, **_kwargs):
            if args == ["remote", "get-url", "origin"]:
                return remote + "\n"
            if args == ["status", "--porcelain"]:
                return ""
            self.fail(f"unexpected local operation: {args}")

        def run(_instance, args, **kwargs):
            commands.append((args, kwargs["extra_env"]))
            return subprocess.CompletedProcess(args, 0, "", "")

        self.set_token()
        with (
            mock.patch("secretary.installation.state_repo.git", side_effect=inspect),
            mock.patch("secretary.installation.state_repo.run_git", side_effect=run),
        ):
            self.assertEqual(
                installation._clone_or_reuse(remote, self.instance, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
        self.assertEqual(len(commands), 2)
        for args, environment in commands:
            self.assertEqual(args[:2], ["-c", "credential.helper="])
            self.assertIn("SECRETARY_CHECKPOINT_INSTANCE", environment)

        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        commands.clear()
        with (
            mock.patch("secretary.installation.state_repo.git", side_effect=inspect),
            mock.patch("secretary.installation.state_repo.run_git", side_effect=run) as remote_run,
            self.assertRaisesRegex(installation.InstallError, "needs a bootstrap credential"),
        ):
            installation._clone_or_reuse(remote, self.instance, recovery=True, dry_run=False)
        remote_run.assert_not_called()

        bootstrap = Path(self.temporary.name) / "bootstrap"
        bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        with (
            mock.patch("secretary.installation.state_repo.git", side_effect=inspect),
            mock.patch("secretary.installation.state_repo.run_git", side_effect=run),
        ):
            installation._clone_or_reuse(
                remote, self.instance, recovery=True, dry_run=False, bootstrap_credential=bootstrap
            )
        self.assertEqual(commands[0][1]["SECRETARY_GITHUB_BOOTSTRAP_FILE"], str(bootstrap))

    def test_bootstrap_file_accepts_sudo_original_caller_only(self) -> None:
        source = Path(self.temporary.name) / "bootstrap"
        source.write_text("fixture-bootstrap\n", encoding="utf-8")
        source.chmod(0o600)
        info = source.lstat()
        with (
            mock.patch("secretary.infra.github_credential.os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(info.st_uid)}, clear=False),
        ):
            self.assertTrue(github_credential.bootstrap_file_owner_is_allowed(info))
            self.assertEqual(
                self.digest(github_credential._bootstrap_token(source)), self.digest("fixture-bootstrap")
            )
        with (
            mock.patch("secretary.infra.github_credential.os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(info.st_uid + 1)}, clear=False),
        ):
            self.assertFalse(github_credential.bootstrap_file_owner_is_allowed(info))

    def test_helper_emits_only_native_protocol_reply_for_github(self) -> None:
        self.set_token()
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "username=x-access-token\npassword=github-test-token\n\n")

    def test_helper_rejects_non_github_and_locked_store_without_leaking_token(self) -> None:
        token = b"github-test-token"
        self.set_token(token)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=example.invalid\n\n")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "")
        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(github_credential.run_helper("get"), 1)
        self.assertNotIn(token.decode(), stderr.getvalue())
        self.assertEqual(
            github_credential.checkpoint_credential_readiness(self.instance).state, "locked/unverifiable"
        )

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
