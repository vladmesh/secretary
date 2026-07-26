import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary import secret_commands, secret_store, state_repo
from secretary.cli import main
from secretary.secret_store import (
    CATALOG_NAME,
    GITIGNORE_ENTRY,
    KEY_NAME,
    RecoveryPhraseError,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    generate_recovery_phrase,
    initialize_store,
    list_secrets,
    load_installation_key,
    read_secret,
    restore_installation_key,
    set_secret,
    store_divergence,
)
from secretary.secret_words import RECOVERY_WORDS


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    )
    return result.stdout


# Scrypt at the production work factor costs about a tenth of a second per call;
# a test that initializes a store in every setUp would spend most of its time
# there. The parameters are read back out of the file, so a cheaper factor
# exercises the same code path.
FAST_KDF = {
    "format": secret_store.KEY_PARAMS_FORMAT,
    "version": secret_store.KEY_PARAMS_VERSION,
    "kdf": {"id": "scrypt", "salt": "", "length": 32, "n": 2**8, "r": 8, "p": 1},
}


def fast_key_params():
    params = json.loads(json.dumps(FAST_KDF))
    params["kdf"]["salt"] = secret_store._b64(b"0123456789abcdef")
    return params


class SecretStoreCase(unittest.TestCase):
    """An instance repo with a store that has been initialized."""

    phrase = " ".join(RECOVERY_WORDS[:16])

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.instance_dir = Path(self.tmpdir.name) / "secretary-instance"
        self.instance_dir.mkdir(parents=True)
        git(self.instance_dir, "init", "--quiet", "--initial-branch", "main")
        git(self.instance_dir, "config", "user.name", "operator")
        git(self.instance_dir, "config", "user.email", "operator@example.invalid")
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance_dir, "add", "instance.yaml")
        git(self.instance_dir, "commit", "--quiet", "-m", "config")
        self.kdf_patch = mock.patch.object(
            secret_store, "_new_key_params", side_effect=fast_key_params
        )
        self.kdf_patch.start()
        self.addCleanup(self.kdf_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)

    def initialize(self) -> None:
        initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")

    def tracked(self) -> list[str]:
        return git(self.instance_dir, "ls-files").split()

    def catalog(self) -> dict:
        return yaml.safe_load(
            (self.instance_dir / "secrets" / CATALOG_NAME).read_text(encoding="utf-8")
        )


class InitCase(SecretStoreCase):
    def test_init_creates_key_catalog_and_one_commit(self) -> None:
        before = state_repo.head(self.instance_dir)
        result = initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")
        head = state_repo.head(self.instance_dir)
        self.assertNotEqual(before, head)
        self.assertEqual(result.commit, head)
        self.assertEqual(result.catalog_path, self.instance_dir / "secrets" / CATALOG_NAME)
        self.assertEqual(
            self.catalog(), {"version": secret_store.CATALOG_VERSION, "secrets": []}
        )
        tracked = self.tracked()
        self.assertIn("secrets/catalog.yaml", tracked)
        self.assertIn("secrets/installation-key.json", tracked)
        self.assertIn(".gitignore", tracked)

    def test_installation_key_is_0600_and_never_committed(self) -> None:
        self.initialize()
        key = self.instance_dir / "secrets" / KEY_NAME
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(f"secrets/{KEY_NAME}", self.tracked())
        self.assertIn(
            GITIGNORE_ENTRY,
            (self.instance_dir / ".gitignore").read_text(encoding="utf-8").split(),
        )
        every_commit = git(self.instance_dir, "log", "--all", "--name-only", "--format=")
        self.assertNotIn(KEY_NAME, every_commit)

    def test_key_params_are_open_and_hold_no_key_material(self) -> None:
        self.initialize()
        params = json.loads(
            (self.instance_dir / "secrets" / "installation-key.json").read_text("utf-8")
        )
        self.assertEqual(params["format"], secret_store.KEY_PARAMS_FORMAT)
        self.assertEqual(params["version"], secret_store.KEY_PARAMS_VERSION)
        self.assertEqual(params["kdf"]["id"], "scrypt")
        self.assertEqual(params["verifier"]["id"], "chacha20poly1305")
        key = load_installation_key(self.instance_dir)
        self.assertNotIn(secret_store._b64(key), json.dumps(params))

    def test_second_init_refuses_and_changes_nothing(self) -> None:
        self.initialize()
        head = state_repo.head(self.instance_dir)
        key_before = (self.instance_dir / "secrets" / KEY_NAME).read_bytes()
        with self.assertRaises(SecretStoreStateError) as caught:
            initialize_store(self.instance_dir, phrase=self.phrase, actor="tester")
        self.assertIn("already initialized", str(caught.exception))
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assertEqual((self.instance_dir / "secrets" / KEY_NAME).read_bytes(), key_before)


class RecoveryPhraseCase(SecretStoreCase):
    def test_generated_phrase_is_from_the_wordlist_and_long_enough(self) -> None:
        phrase = generate_recovery_phrase()
        words = phrase.split()
        self.assertEqual(len(words), 16)
        self.assertTrue(set(words) <= set(RECOVERY_WORDS))
        self.assertNotEqual(phrase, generate_recovery_phrase())

    def test_wordlist_is_exactly_256_distinct_words(self) -> None:
        self.assertEqual(len(RECOVERY_WORDS), 256)
        self.assertEqual(len(set(RECOVERY_WORDS)), 256)

    def test_phrase_rebuilds_the_same_key_after_the_key_file_is_lost(self) -> None:
        self.initialize()
        key_file = self.instance_dir / "secrets" / KEY_NAME
        original = load_installation_key(self.instance_dir)
        key_file.unlink()
        with self.assertRaises(SecretStoreStateError):
            load_installation_key(self.instance_dir)
        restore_installation_key(self.instance_dir, self.phrase.upper() + "  ")
        self.assertEqual(load_installation_key(self.instance_dir), original)
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_restored_key_opens_a_value_written_before_the_loss(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / KEY_NAME).unlink()
        restore_installation_key(self.instance_dir, self.phrase)
        self.assertEqual(read_secret(self.instance_dir, "kanboard.api-token"), b"token-value")

    def test_wrong_phrase_is_an_explicit_error_and_writes_nothing(self) -> None:
        self.initialize()
        key_file = self.instance_dir / "secrets" / KEY_NAME
        key_file.unlink()
        wrong = " ".join(RECOVERY_WORDS[16:32])
        with self.assertRaises(RecoveryPhraseError) as caught:
            restore_installation_key(self.instance_dir, wrong)
        self.assertIn("does not match", str(caught.exception))
        self.assertFalse(key_file.exists())

    def test_wrong_phrase_never_yields_a_usable_key(self) -> None:
        self.initialize()
        good = load_installation_key(self.instance_dir)
        params = json.loads(
            (self.instance_dir / "secrets" / "installation-key.json").read_text("utf-8")
        )
        wrong = secret_store._derive_key(" ".join(RECOVERY_WORDS[32:48]), params)
        self.assertNotEqual(wrong, good)


class RoundTripCase(SecretStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def test_set_list_read_round_trip(self) -> None:
        result = set_secret(
            self.instance_dir,
            secret_id="openrouter.api-token",
            value=b"sk-live-value",
            scope="installation",
            purpose="model routing",
            environment="OPENROUTER_API_KEY",
            actor="tester",
        )
        self.assertTrue(result.created)
        entries = list_secrets(self.instance_dir)
        self.assertEqual(
            [dict(entry) for entry in entries],
            [
                {
                    "id": "openrouter.api-token",
                    "scope": "installation",
                    "purpose": "model routing",
                    "environment": "OPENROUTER_API_KEY",
                    "created_at": entries[0]["created_at"],
                }
            ],
        )
        self.assertEqual(read_secret(self.instance_dir, "openrouter.api-token"), b"sk-live-value")

    def test_multiline_and_binary_values_survive_unchanged(self) -> None:
        multiline = (
            "-----BEGIN CERTIFICATE-----\nline one\r\nline two\n\n  trailing spaces   \n"
        ).encode("utf-8")
        binary = bytes(range(256)) * 4
        set_secret(
            self.instance_dir,
            secret_id="github.app-key",
            value=multiline,
            scope="project:secretary",
            purpose="github app private key",
            actor="tester",
        )
        set_secret(
            self.instance_dir,
            secret_id="binary.blob",
            value=binary,
            scope="installation",
            purpose="raw bytes",
            actor="tester",
        )
        self.assertEqual(read_secret(self.instance_dir, "github.app-key"), multiline)
        self.assertEqual(read_secret(self.instance_dir, "binary.blob"), binary)

    def test_catalog_holds_metadata_only_and_the_value_file_hides_the_value(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"plaintext-needle",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        catalog_text = (self.instance_dir / "secrets" / CATALOG_NAME).read_text("utf-8")
        self.assertNotIn("plaintext-needle", catalog_text)
        envelope_text = (
            self.instance_dir / "secrets" / "values" / "kanboard.api-token.enc.json"
        ).read_text("utf-8")
        self.assertNotIn("plaintext-needle", envelope_text)
        self.assertNotIn(
            "plaintext-needle",
            git(self.instance_dir, "log", "--all", "-p", "--format="),
        )

    def test_envelope_declares_its_format_kdf_and_aead_in_the_open(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        envelope = json.loads(
            (self.instance_dir / "secrets" / "values" / "kanboard.api-token.enc.json").read_text(
                "utf-8"
            )
        )
        self.assertEqual(envelope["format"], secret_store.ENVELOPE_FORMAT)
        self.assertEqual(envelope["version"], secret_store.ENVELOPE_VERSION)
        self.assertEqual(envelope["kdf"]["id"], "hkdf-sha256")
        self.assertEqual(envelope["aead"]["id"], "chacha20poly1305")
        self.assertIn("salt", envelope["kdf"])
        self.assertIn("nonce", envelope["aead"])

    def test_a_newer_envelope_version_is_refused_not_guessed_at(self) -> None:
        key = load_installation_key(self.instance_dir)
        envelope = secret_store.seal_value(key, "x", b"value")
        envelope["version"] = secret_store.ENVELOPE_VERSION + 1
        with self.assertRaises(SecretStoreStateError) as caught:
            secret_store.open_value(key, envelope)
        self.assertIn("upgrade secretary", str(caught.exception))

    def test_tampering_with_the_open_header_breaks_the_seal(self) -> None:
        key = load_installation_key(self.instance_dir)
        envelope = secret_store.seal_value(key, "x", b"value")
        envelope["id"] = "y"
        with self.assertRaises(SecretStoreStateError):
            secret_store.open_value(key, envelope)

    def test_updating_a_secret_keeps_created_at_and_one_catalog_entry(self) -> None:
        first = set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"one",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        created_at = list_secrets(self.instance_dir)[0]["created_at"]
        second = set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"two",
            scope="installation",
            purpose="board api, rotated",
            actor="tester",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        entries = list_secrets(self.instance_dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["created_at"], created_at)
        self.assertEqual(entries[0]["purpose"], "board api, rotated")
        self.assertEqual(read_secret(self.instance_dir, "kanboard.api-token"), b"two")

    def test_catalog_and_value_land_in_the_same_commit(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(
            sorted(touched),
            ["secrets/catalog.yaml", "secrets/values/kanboard.api-token.enc.json"],
        )

    def test_set_refuses_once_the_key_stops_being_ignored(self) -> None:
        (self.instance_dir / ".gitignore").write_text("# nothing ignored\n", encoding="utf-8")
        with self.assertRaises(SecretStoreError) as caught:
            set_secret(
                self.instance_dir,
                secret_id="kanboard.api-token",
                value=b"token",
                scope="installation",
                purpose="board api",
                actor="tester",
            )
        self.assertIn("committable key", str(caught.exception))
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_a_pasted_secret_in_an_open_field_stops_the_write(self) -> None:
        head = state_repo.head(self.instance_dir)
        with self.assertRaises(SecretStoreValidationError) as caught:
            set_secret(
                self.instance_dir,
                secret_id="kanboard.api-token",
                value=b"token",
                scope="installation",
                purpose="use AKIAIOSFODNN7EXAMPLE for the bucket",
                actor="tester",
            )
        self.assertIn("secret detected", str(caught.exception))
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_bad_input_is_rejected_before_anything_is_written(self) -> None:
        cases = [
            {"secret_id": "Not-Lower"},
            {"secret_id": "../escape"},
            {"scope": "team:everyone"},
            {"purpose": "   "},
            {"value": b""},
        ]
        for override in cases:
            request = {
                "secret_id": "kanboard.api-token",
                "value": b"token",
                "scope": "installation",
                "purpose": "board api",
                "actor": "tester",
                **override,
            }
            with self.subTest(override=override):
                with self.assertRaises(SecretStoreValidationError):
                    set_secret(self.instance_dir, **request)
        self.assertEqual(list_secrets(self.instance_dir), ())


class InterruptedWriteCase(SecretStoreCase):
    """A write cut in the middle leaves the catalog and the values agreeing."""

    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="first.secret",
            value=b"first",
            scope="installation",
            purpose="already stored",
            actor="tester",
        )

    def assert_consistent(self) -> None:
        self.assertEqual(store_divergence(self.instance_dir), ())
        for entry in list_secrets(self.instance_dir):
            self.assertTrue(read_secret(self.instance_dir, entry["id"]))
        dirty = state_repo.status(self.instance_dir, ("secrets",))
        self.assertEqual(dirty, "")

    def test_interrupt_between_the_value_and_the_catalog_rolls_both_back(self) -> None:
        head = state_repo.head(self.instance_dir)
        real_replace = secret_store.os.replace
        calls = {"count": 0}

        def failing_replace(source, destination):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("interrupted between the value and the catalog")
            return real_replace(source, destination)

        with mock.patch.object(secret_store.os, "replace", side_effect=failing_replace):
            with self.assertRaises(SecretStoreError):
                set_secret(
                    self.instance_dir,
                    secret_id="second.secret",
                    value=b"second",
                    scope="installation",
                    purpose="interrupted",
                    actor="tester",
                )
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assert_consistent()
        self.assertEqual(
            [entry["id"] for entry in list_secrets(self.instance_dir)], ["first.secret"]
        )

    def test_interrupt_before_the_commit_leaves_a_consistent_pair_to_commit(self) -> None:
        with mock.patch.object(
            state_repo, "commit", side_effect=state_repo.StateRepoError("commit state failed")
        ):
            with self.assertRaises(state_repo.StateRepoError):
                set_secret(
                    self.instance_dir,
                    secret_id="second.secret",
                    value=b"second",
                    scope="installation",
                    purpose="interrupted",
                    actor="tester",
                )
        # The commit never happened, so the history still holds only the first
        # secret; the worktree holds a matching catalog and value, so the retry
        # commits a consistent pair rather than half of one.
        committed = git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split()
        self.assertNotIn("secrets/values/second.secret.enc.json", committed)
        self.assertEqual(store_divergence(self.instance_dir), ())
        self.assertEqual(read_secret(self.instance_dir, "second.secret"), b"second")

        set_secret(
            self.instance_dir,
            secret_id="second.secret",
            value=b"second",
            scope="installation",
            purpose="interrupted",
            actor="tester",
        )
        self.assert_consistent()

    def test_divergence_is_reported_when_a_value_file_disappears(self) -> None:
        (self.instance_dir / "secrets" / "values" / "first.secret.enc.json").unlink()
        self.assertEqual(
            store_divergence(self.instance_dir), ("first.secret: catalogued with no value",)
        )


class SecretCliCase(SecretStoreCase):
    def run_cli(self, argv: list[str], stdin: bytes = b"") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), mock.patch(
            "sys.stdin", stream
        ):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_init_shows_the_phrase_once_and_needs_it_confirmed(self) -> None:
        answers: list[str] = []
        phrase = " ".join(RECOVERY_WORDS[64:80])

        def fake_input(prompt: str) -> str:
            position = int(prompt.split()[1].rstrip(":")) - 1
            answers.append(prompt)
            return phrase.split()[position]

        with mock.patch.object(secret_commands, "generate_recovery_phrase", return_value=phrase):
            with mock.patch("builtins.input", side_effect=fake_input):
                code, out, err = self.run_cli(
                    ["secret", "init", "--instance", str(self.instance_dir)]
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(answers), secret_store.CONFIRM_WORDS)
        # The phrase is shown on stderr, so a redirected stdout cannot capture it.
        self.assertIn(phrase.split()[0], err)
        self.assertNotIn(phrase.split()[0], out)
        self.assertTrue(json.loads(out)["ok"])
        self.assertTrue(secret_store.is_initialized(self.instance_dir))

    def test_init_without_a_correct_confirmation_initializes_nothing(self) -> None:
        with mock.patch("builtins.input", return_value="wrong"):
            code, out, _ = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))
        self.assertFalse((self.instance_dir / "secrets" / KEY_NAME).exists())

    def test_second_init_refuses_through_the_cli(self) -> None:
        self.initialize()
        code, out, _ = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("already initialized", json.loads(out)["message"])

    def test_set_reads_stdin_and_list_prints_metadata_only(self) -> None:
        self.initialize()
        code, out, _ = self.run_cli(
            [
                "secret",
                "set",
                "--instance",
                str(self.instance_dir),
                "--id",
                "kanboard.api-token",
                "--scope",
                "installation",
                "--purpose",
                "board api",
                "--stdin",
            ],
            stdin=b"multi\nline\nvalue\n",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("multi", out)
        self.assertEqual(json.loads(out)["bytes"], len(b"multi\nline\nvalue\n"))

        code, out, _ = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 0)
        listed = json.loads(out)["secrets"]
        self.assertEqual([entry["id"] for entry in listed], ["kanboard.api-token"])
        self.assertNotIn("multi", out)
        self.assertNotIn("value", out.replace("kanboard.api-token", ""))
        self.assertEqual(
            read_secret(self.instance_dir, "kanboard.api-token"), b"multi\nline\nvalue\n"
        )

    def test_set_reads_a_binary_file_without_touching_argv(self) -> None:
        self.initialize()
        blob = bytes(range(256))
        source = Path(self.tmpdir.name) / "value.bin"
        source.write_bytes(blob)
        code, out, _ = self.run_cli(
            [
                "secret",
                "set",
                "--instance",
                str(self.instance_dir),
                "--id",
                "binary.blob",
                "--scope",
                "project:secretary",
                "--purpose",
                "raw bytes",
                "--file",
                str(source),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(read_secret(self.instance_dir, "binary.blob"), blob)

    def test_no_command_takes_a_value_on_the_command_line(self) -> None:
        """There is no `--value`; argparse rejects it before anything runs."""
        self.initialize()
        with self.assertRaises(SystemExit):
            self.run_cli(
                [
                    "secret",
                    "set",
                    "--instance",
                    str(self.instance_dir),
                    "--id",
                    "kanboard.api-token",
                    "--scope",
                    "installation",
                    "--purpose",
                    "board api",
                    "--value",
                    "secret",
                ]
            )
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_list_before_init_says_so_instead_of_failing_obscurely(self) -> None:
        code, out, _ = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("not initialized", json.loads(out)["message"])


if __name__ == "__main__":
    unittest.main()
