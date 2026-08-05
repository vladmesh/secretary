import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary import installation, secret_commands, secret_store, state_repo
from secretary.cli import main
from secretary.config import validate
from secretary.secret_store import (
    CATALOG_NAME,
    GITIGNORE_ENTRY,
    KEY_NAME,
    KEY_PARAMS_NAME,
    RecoveryPhraseError,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    generate_recovery_phrase,
    import_env_file,
    initialize_store,
    list_secrets,
    load_installation_key,
    materialize_secrets,
    read_secret,
    remove_secret,
    restore_installation_key,
    set_secret,
    store_divergence,
)
from secretary.board_transport import ensure as ensure_board_transport
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


class LegacyBoardSecretTests(SecretStoreCase):
    """Pre-transport catalog entries remain removable but never regain authority."""

    def setUp(self) -> None:
        super().setUp()
        self.initialize()

    def _historical_entry(
        self, *, materialize: dict | None = None, value: bytes = b"migrated-live-token"
    ) -> None:
        # Historical stores legitimately contain this id.  Bypass only the new-write guard to
        # construct that on-disk predecessor, then exercise the public behavior normally.
        with mock.patch.object(secret_store, "_new_secret_id", secret_store._clean_secret_id):
            set_secret(
                self.instance_dir,
                secret_id="kanboard_api_token",
                value=value,
                scope="installation",
                purpose="historic board token",
                environment="KANBOARD_API_TOKEN",
                materialize=materialize,
                actor="tester",
            )

    def test_legacy_entries_cannot_be_created_or_read_but_can_be_removed(self) -> None:
        with self.assertRaisesRegex(SecretStoreValidationError, "board transport"):
            set_secret(
                self.instance_dir, secret_id="kanboard_api_token", value=b"new",
                scope="installation", purpose="no", actor="tester",
            )
        self._historical_entry()
        with self.assertRaisesRegex(SecretStoreValidationError, "board transport"):
            read_secret(self.instance_dir, "kanboard_api_token")

        remove_secret(self.instance_dir, secret_id="kanboard_api_token", actor="tester")

        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_legacy_entry_is_not_materialized_but_migrated_transport_is_redacted(self) -> None:
        self._historical_entry(materialize={"target": "runtime-env"}, value=b"still-live-old-token")
        transport = ensure_board_transport(
            self.instance_dir,
            legacy_values={
                "KANBOARD_URL": "http://legacy/jsonrpc.php",
                "KANBOARD_API_USER": "jsonrpc",
                "KANBOARD_API_TOKEN": "migrated-live-token",
            },
        ).transport

        self.assertEqual(materialize_secrets(self.instance_dir), ())
        self.assertIn(transport.token, secret_store.redaction_values(self.instance_dir))
        self.assertIn("still-live-old-token", secret_store.redaction_values(self.instance_dir))

    def test_legacy_board_url_and_user_are_not_global_redaction_needles(self) -> None:
        with mock.patch.object(secret_store, "_new_secret_id", secret_store._clean_secret_id):
            for secret_id, environment, value in (
                ("kanboard_url", "KANBOARD_URL", b"http://127.0.0.1:8080/jsonrpc.php"),
                ("kanboard_api_user", "KANBOARD_API_USER", b"jsonrpc"),
            ):
                set_secret(
                    self.instance_dir, secret_id=secret_id, value=value,
                    scope="installation", purpose="historic board configuration",
                    environment=environment, actor="tester",
                )
        values = secret_store.redaction_values(self.instance_dir)
        self.assertNotIn("http://127.0.0.1:8080/jsonrpc.php", values)
        self.assertNotIn("jsonrpc", values)

    def test_insecure_migrated_transport_blocks_redaction_instead_of_dropping_its_token(self) -> None:
        transport = ensure_board_transport(
            self.instance_dir,
            legacy_values={
                "KANBOARD_URL": "http://legacy/jsonrpc.php",
                "KANBOARD_API_USER": "jsonrpc",
                "KANBOARD_API_TOKEN": "migrated-live-token",
            },
        ).transport
        (self.instance_dir / "board-transport.env").chmod(0o644)

        with self.assertRaisesRegex(SecretStoreStateError, "redaction is unavailable"):
            secret_store.redaction_values(self.instance_dir)
        self.assertEqual(transport.token, "migrated-live-token")


# The three keys the live installation's runtime.env holds, in the order the live
# file holds them, which is not alphabetical: URL, user, token. The token ends in
# '=' padding so a value that looks like another KEY=VALUE split has to survive
# the round trip too.
LIVE_RUNTIME_ENV = (
    "EXAMPLE_URL=https://board.example.invalid/jsonrpc.php\n"
    "EXAMPLE_API_USER=secretary\n"
    "EXAMPLE_API_TOKEN=1f2e3d4c5b6a==\n"
)


class EnvStoreCase(SecretStoreCase):
    """An initialized store plus a runtime.env shaped like the live one."""

    def setUp(self) -> None:
        super().setUp()
        self.initialize()
        self.source = Path(self.tmpdir.name) / "runtime.env"
        self.source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        os.chmod(self.source, 0o600)
        self.target = Path(self.tmpdir.name) / "materialized" / "runtime.env"
        override = mock.patch.dict(
            os.environ, {"SECRETARY_RUNTIME_ENV_FILE": str(self.target)}
        )
        override.start()
        self.addCleanup(override.stop)

    def do_import(self, source: Path | None = None, **overrides):
        request = {
            "source": source or self.source,
            "scope": "installation",
            "purpose": "board api",
            "actor": "tester",
            "materialize": {"target": "runtime-env"},
            **overrides,
        }
        return import_env_file(self.instance_dir, **request)


class ImportCase(EnvStoreCase):
    def test_import_makes_one_secret_per_variable(self) -> None:
        result = self.do_import()
        self.assertEqual(
            result.created, ("example_url", "example_api_user", "example_api_token")
        )
        entries = list_secrets(self.instance_dir)
        self.assertEqual(
            [(entry["id"], entry["environment"]) for entry in entries],
            [
                ("example_api_token", "EXAMPLE_API_TOKEN"),
                ("example_api_user", "EXAMPLE_API_USER"),
                ("example_url", "EXAMPLE_URL"),
            ],
        )
        # The catalog is sorted by id, the file is not: each entry carries the
        # line it came from, so the file's own order survives the store.
        self.assertEqual(
            [(entry["id"], entry["materialize"]) for entry in entries],
            [
                ("example_api_token", {"target": "runtime-env", "order": 2}),
                ("example_api_user", {"target": "runtime-env", "order": 1}),
                ("example_url", {"target": "runtime-env", "order": 0}),
            ],
        )
        self.assertEqual(read_secret(self.instance_dir, "example_api_user"), b"secretary")
        self.assertEqual(store_divergence(self.instance_dir), ())

    def test_import_lands_as_one_commit(self) -> None:
        before = state_repo.head(self.instance_dir)
        result = self.do_import()
        self.assertNotEqual(result.commit, before)
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(
            sorted(touched),
            [
                "secrets/catalog.yaml",
                "secrets/values/example_api_token.enc.json",
                "secrets/values/example_api_user.enc.json",
                "secrets/values/example_url.enc.json",
            ],
        )

    def test_reimporting_the_same_file_duplicates_nothing_and_writes_nothing(self) -> None:
        self.do_import()
        head = state_repo.head(self.instance_dir)
        envelope = self.instance_dir / "secrets" / "values" / "example_url.enc.json"
        sealed = envelope.read_bytes()

        result = self.do_import()
        self.assertEqual(result.created, ())
        self.assertEqual(result.updated, ())
        self.assertEqual(
            result.unchanged, ("example_url", "example_api_user", "example_api_token")
        )
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assertEqual(envelope.read_bytes(), sealed)
        self.assertEqual(len(list_secrets(self.instance_dir)), 3)

    def test_reimport_names_the_variable_that_moved(self) -> None:
        self.do_import()
        self.source.write_text(
            LIVE_RUNTIME_ENV.replace("=secretary\n", "=secretary-two\n"), encoding="utf-8"
        )
        result = self.do_import()
        self.assertEqual(result.updated, ("example_api_user",))
        self.assertEqual(result.created, ())
        self.assertEqual(result.unchanged, ("example_url", "example_api_token"))
        self.assertEqual(read_secret(self.instance_dir, "example_api_user"), b"secretary-two")
        # Only the rotated envelope moves: the catalog says the same thing it did
        # before, so the commit does not restate it.
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(touched, ["secrets/values/example_api_user.enc.json"])

    def test_import_keeps_created_at_across_a_rotation(self) -> None:
        self.do_import()
        created_at = list_secrets(self.instance_dir)[0]["created_at"]
        self.source.write_text(
            LIVE_RUNTIME_ENV.replace("=1f2e3d4c5b6a\n", "=rotated\n"), encoding="utf-8"
        )
        self.do_import()
        self.assertEqual(list_secrets(self.instance_dir)[0]["created_at"], created_at)

    def test_a_file_import_cannot_read_is_refused_before_anything_is_written(self) -> None:
        head = state_repo.head(self.instance_dir)
        cases = [
            "export EXAMPLE_URL=https://board\n",
            "KANBOARD URL\n",
            "1BAD=value\n",
            "EXAMPLE_URL=a\nEXAMPLE_URL=b\n",
            "EXAMPLE_URL=\n",
            "# only a comment\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.source.write_text(text, encoding="utf-8")
                with self.assertRaises(SecretStoreValidationError):
                    self.do_import()
        self.assertEqual(list_secrets(self.instance_dir), ())
        self.assertEqual(state_repo.head(self.instance_dir), head)

    def test_a_file_the_store_could_not_reproduce_is_refused(self) -> None:
        """Anything the catalog cannot record is refused rather than dropped.

        The store keeps names, values and line order. A comment, a blank line, a
        padded line, a CR or a missing final newline would come back out as
        different bytes, so the import says so instead.
        """
        head = state_repo.head(self.instance_dir)
        cases = {
            "no trailing newline": "EXAMPLE_URL=https://board\nEXAMPLE_API_USER=x",
            "blank line between": "EXAMPLE_URL=https://board\n\nEXAMPLE_API_USER=x\n",
            "blank line at the end": "EXAMPLE_URL=https://board\n\n",
            "comment above": "# board\nEXAMPLE_URL=https://board\n",
            "padded name": "  EXAMPLE_URL=https://board\n",
            "space around the equals": "EXAMPLE_URL = https://board\n",
            "trailing space in the value": "EXAMPLE_URL=https://board \n",
            "crlf": "EXAMPLE_URL=https://board\r\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.source.write_text(text, encoding="utf-8", newline="")
                with self.assertRaises(SecretStoreValidationError):
                    self.do_import()
        self.assertEqual(list_secrets(self.instance_dir), ())
        self.assertEqual(state_repo.head(self.instance_dir), head)

    def test_import_moves_an_earlier_variable_below_the_imported_block(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="extra.flag",
            value=b"on",
            scope="installation",
            purpose="added by hand",
            environment="EXTRA_FLAG",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        self.do_import()
        orders = {
            entry["id"]: entry["materialize"]["order"] for entry in list_secrets(self.instance_dir)
        }
        self.assertEqual(
            orders,
            {
                "example_url": 0,
                "example_api_user": 1,
                "example_api_token": 2,
                "extra.flag": 3,
            },
        )
        materialize_secrets(self.instance_dir)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV + "EXTRA_FLAG=on\n"
        )

    def test_import_refuses_names_that_differ_only_in_case(self) -> None:
        # One id per variable, so two names sharing an id are refused whole:
        # taking the file in would drop a line on the way back out.
        cased = Path(self.tmpdir.name) / "cased.env"
        cased.write_text("FOO=upper\nfoo=lower\n", encoding="utf-8")
        head = state_repo.head(self.instance_dir)
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=cased)
        self.assertIn("differ only in case", str(caught.exception))
        self.assertNotIn("upper", str(caught.exception))
        self.assertNotIn("lower", str(caught.exception))
        # Refused before the first write: no entry, no envelope, no commit.
        self.assertEqual(list(list_secrets(self.instance_dir)), [])
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assertEqual(list((self.instance_dir / "secrets" / "values").glob("*")), [])
        self.assertEqual(materialize_secrets(self.instance_dir), ())
        self.assertFalse(self.target.exists())

    def test_import_does_not_take_over_a_variable_already_stored_under_that_id(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="foo",
            value=b"upper",
            scope="installation",
            purpose="board api",
            environment="FOO",
            materialize={"target": "runtime-env", "order": 0},
            actor="tester",
        )
        lower = Path(self.tmpdir.name) / "lower.env"
        lower.write_text("foo=lower\n", encoding="utf-8")
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=lower)
        self.assertIn("would take over", str(caught.exception))
        entry = next(item for item in list_secrets(self.instance_dir) if item["id"] == "foo")
        self.assertEqual(entry["environment"], "FOO")
        self.assertEqual(read_secret(self.instance_dir, "foo"), b"upper")

    def test_import_does_not_read_a_file_that_is_not_there(self) -> None:
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.do_import(source=Path(self.tmpdir.name) / "absent.env")
        self.assertIn("not found", str(caught.exception))


class RemoveCase(EnvStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_remove_drops_the_entry_and_the_envelope_in_one_commit(self) -> None:
        envelope = self.instance_dir / "secrets" / "values" / "example_url.enc.json"
        result = remove_secret(self.instance_dir, secret_id="example_url", actor="tester")
        self.assertEqual(result.commit, state_repo.head(self.instance_dir))
        self.assertFalse(envelope.exists())
        self.assertEqual(
            [entry["id"] for entry in list_secrets(self.instance_dir)],
            ["example_api_token", "example_api_user"],
        )
        self.assertEqual(store_divergence(self.instance_dir), ())
        self.assertEqual(state_repo.status(self.instance_dir, ("secrets",)), "")
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(
            sorted(touched),
            ["secrets/catalog.yaml", "secrets/values/example_url.enc.json"],
        )
        self.assertNotIn(
            "secrets/values/example_url.enc.json",
            git(self.instance_dir, "ls-tree", "-r", "--name-only", "HEAD").split(),
        )

    def test_removing_a_secret_that_is_not_there_is_an_error(self) -> None:
        head = state_repo.head(self.instance_dir)
        with self.assertRaises(SecretStoreStateError) as caught:
            remove_secret(self.instance_dir, secret_id="never.stored", actor="tester")
        self.assertIn("no secret named", str(caught.exception))
        self.assertEqual(state_repo.head(self.instance_dir), head)
        self.assertEqual(len(list_secrets(self.instance_dir)), 3)


class MaterializeCase(EnvStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_materialize_writes_the_env_file_0600(self) -> None:
        results = materialize_secrets(self.instance_dir)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target, "runtime-env")
        self.assertEqual(results[0].path, self.target)
        self.assertTrue(results[0].changed)
        self.assertEqual(self.target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_the_target_path_comes_from_role_env_not_from_a_constant(self) -> None:
        moved = Path(self.tmpdir.name) / "elsewhere" / "runtime.env"
        with mock.patch.dict(os.environ, {"SECRETARY_RUNTIME_ENV_FILE": str(moved)}):
            results = materialize_secrets(self.instance_dir)
        self.assertEqual(results[0].path, moved)
        self.assertEqual(moved.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)
        self.assertFalse(self.target.exists())

    def test_secretary_runtime_env_pin_beats_an_ambient_ta_override(self) -> None:
        pinned = Path(self.tmpdir.name) / "recovery" / "runtime.env"
        ambient = Path(self.tmpdir.name) / "live" / "runtime.env"
        entry = {"materialize": {"target": "runtime-env"}}
        with mock.patch.dict(os.environ, {"TA_RUNTIME_ENV_FILE": str(ambient)}, clear=True):
            with installation._runtime_environment({"SECRETARY_RUNTIME_ENV_FILE": str(pinned)}):
                self.assertEqual(secret_store.materialize_path(self.instance_dir, entry), pinned)

    def test_a_second_run_leaves_the_file_byte_for_byte_the_same(self) -> None:
        materialize_secrets(self.instance_dir)
        first = self.target.read_bytes()
        before = self.target.stat().st_ino
        results = materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), first)
        self.assertFalse(results[0].changed)
        # Unchanged means untouched: systemd never sees a rename it did not need.
        self.assertEqual(self.target.stat().st_ino, before)

    def test_an_interrupted_swap_leaves_the_previous_file_in_place(self) -> None:
        materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        set_secret(
            self.instance_dir,
            secret_id="example_api_user",
            value=b"rotated",
            scope="installation",
            purpose="board api",
            environment="EXAMPLE_API_USER",
            materialize={"target": "runtime-env"},
            actor="tester",
        )

        def fail_replace(source, destination):
            raise OSError("interrupted between the temporary file and the target")

        with mock.patch.object(secret_store.os, "replace", side_effect=fail_replace):
            with self.assertRaises(SecretStoreError):
                materialize_secrets(self.instance_dir)

        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        leftovers = [path.name for path in self.target.parent.iterdir()]
        self.assertEqual(leftovers, [self.target.name])

    def test_the_generated_file_passes_the_installation_validator(self) -> None:
        materialize_secrets(self.instance_dir)
        values = installation.read_runtime_env(self.instance_dir, str(self.target))
        self.assertEqual(
            values,
            {
                "EXAMPLE_API_TOKEN": "1f2e3d4c5b6a==",
                "EXAMPLE_API_USER": "secretary",
                "EXAMPLE_URL": "https://board.example.invalid/jsonrpc.php",
            },
        )

    def test_import_then_materialize_reproduces_the_original_bytes(self) -> None:
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        # Not by accident of sorting: the file's order is not alphabetical, and
        # the last line carries '=' padding that a re-split would mangle.
        written = self.target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(written[0].split("=", 1)[0], "EXAMPLE_URL")
        self.assertNotEqual(written, sorted(written))
        self.assertTrue(written[-1].endswith("1f2e3d4c5b6a=="))

    def test_a_reordered_source_moves_the_lines_and_nothing_else(self) -> None:
        materialize_secrets(self.instance_dir)
        reordered = "".join(reversed(LIVE_RUNTIME_ENV.splitlines(keepends=True)))
        self.source.write_text(reordered, encoding="utf-8")
        result = self.do_import()
        # Same values, new layout: only the catalog moves, no envelope is resealed.
        self.assertEqual(result.created, ())
        # The middle line did not move, so only the two that swapped are updated.
        self.assertEqual(result.updated, ("example_api_token", "example_url"))
        self.assertEqual(result.unchanged, ("example_api_user",))
        touched = git(self.instance_dir, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(touched, ["secrets/catalog.yaml"])
        materialize_secrets(self.instance_dir)
        self.assertEqual(self.target.read_text(encoding="utf-8"), reordered)

    def test_a_file_target_is_written_where_the_catalog_says(self) -> None:
        elsewhere = Path(self.tmpdir.name) / "other" / "app.env"
        set_secret(
            self.instance_dir,
            secret_id="app.token",
            value=b"app-value",
            scope="project:secretary",
            purpose="app credentials",
            environment="APP_TOKEN",
            materialize={"target": "file", "path": str(elsewhere)},
            actor="tester",
        )
        results = materialize_secrets(self.instance_dir)
        self.assertEqual({result.path for result in results}, {self.target, elsewhere})
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "APP_TOKEN=app-value\n")

        only_runtime = materialize_secrets(self.instance_dir, target="runtime-env")
        self.assertEqual([result.path for result in only_runtime], [self.target])

    def test_materialize_refuses_a_target_git_would_pick_up(self) -> None:
        inside = self.instance_dir / "tracked.env"
        set_secret(
            self.instance_dir,
            secret_id="app.token",
            value=b"app-value",
            scope="installation",
            purpose="app credentials",
            environment="APP_TOKEN",
            materialize={"target": "file", "path": "tracked.env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreError) as caught:
            materialize_secrets(self.instance_dir, target="file")
        self.assertIn("not gitignored", str(caught.exception))
        self.assertFalse(inside.exists())

        (self.instance_dir / ".gitignore").write_text(
            f"{GITIGNORE_ENTRY}\ntracked.env\n", encoding="utf-8"
        )
        materialize_secrets(self.instance_dir, target="file")
        self.assertEqual(inside.read_text(encoding="utf-8"), "APP_TOKEN=app-value\n")

    def test_a_value_with_a_newline_never_becomes_an_env_line(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="app.key",
            value=b"-----BEGIN KEY-----\nbody\n",
            scope="installation",
            purpose="pem body",
            environment="APP_KEY",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreValidationError) as caught:
            materialize_secrets(self.instance_dir)
        self.assertIn("newline", str(caught.exception))
        self.assertFalse(self.target.exists())

    def test_two_secrets_claiming_one_variable_stop_the_write(self) -> None:
        materialize_secrets(self.instance_dir)
        before = self.target.read_bytes()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.url.copy",
            value=b"https://other.example.invalid/jsonrpc.php",
            scope="installation",
            purpose="a second claim on the same variable",
            environment="EXAMPLE_URL",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        with self.assertRaises(SecretStoreStateError) as caught:
            materialize_secrets(self.instance_dir)
        self.assertIn("EXAMPLE_URL", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), before)

    def test_a_secret_with_no_materialize_record_stays_in_the_store(self) -> None:
        set_secret(
            self.instance_dir,
            secret_id="offline.note",
            value=b"not an env var",
            scope="installation",
            purpose="kept for recovery only",
            actor="tester",
        )
        materialize_secrets(self.instance_dir)
        self.assertNotIn("offline", self.target.read_text(encoding="utf-8"))


class ObservabilityCase(SecretStoreCase):
    """`store_health` and `store_findings` are the surface `status`/`doctor` use."""

    def test_uninitialized_store_is_absent_and_finding_free(self) -> None:
        health = secret_store.store_health(self.instance_dir)
        self.assertEqual(
            health,
            {
                "initialized": False,
                "secret_count": 0,
                "last_modified_at": None,
                "installation_key": {"present": False, "usable": None},
                "materialize": [],
            },
        )
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())

    def test_healthy_store_reports_counts_and_no_findings(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            environment="EXAMPLE_API_TOKEN",
            materialize={"target": "runtime-env"},
            actor="tester",
        )
        health = secret_store.store_health(self.instance_dir)
        self.assertTrue(health["initialized"])
        self.assertEqual(health["secret_count"], 1)
        self.assertIsNotNone(health["last_modified_at"])
        self.assertEqual(health["installation_key"], {"present": True, "usable": True})
        self.assertEqual(
            health["materialize"], [{"target": "runtime-env", "path": None, "count": 1}]
        )
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())

    def test_health_never_carries_a_value_or_the_recovery_phrase(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"super-secret-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        dump = json.dumps(secret_store.store_health(self.instance_dir))
        self.assertNotIn("super-secret-value", dump)
        self.assertNotIn(self.phrase, dump)

    def test_initialized_empty_store_gives_no_finding_for_a_missing_key(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / KEY_NAME).unlink()
        self.assertEqual(secret_store.store_findings(self.instance_dir), ())
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": False, "usable": None},
        )

    def test_missing_key_with_a_non_empty_catalog_is_a_finding(self) -> None:
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
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("installation key is missing or unusable", findings[0])
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": False, "usable": None},
        )

    def test_wide_key_permissions_are_a_finding_even_with_an_empty_catalog(self) -> None:
        self.initialize()
        key_path = self.instance_dir / "secrets" / KEY_NAME
        os.chmod(key_path, 0o644)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("permissions are too broad", findings[0])
        self.assertEqual(
            secret_store.store_health(self.instance_dir)["installation_key"],
            {"present": True, "usable": False},
        )

    def test_wide_key_permissions_do_not_duplicate_the_unusable_finding(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        os.chmod(self.instance_dir / "secrets" / KEY_NAME, 0o644)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("permissions are too broad", findings[0])

    def test_catalog_value_divergence_is_a_finding(self) -> None:
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / "values" / "kanboard.api-token.enc.json").unlink()
        findings = secret_store.store_findings(self.instance_dir)
        self.assertIn("secret store: kanboard.api-token: catalogued with no value", findings)

    def test_missing_key_params_with_a_non_empty_catalog_is_a_finding(self) -> None:
        """Reproduces a store where `init` ran and a secret was set, then only
        installation-key.json was lost. Catalog and envelope survive, but the
        raw key can no longer be checked against a verifier, so it must read
        as unusable, not as a store that was never initialized."""
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        (self.instance_dir / "secrets" / KEY_PARAMS_NAME).unlink()
        health = secret_store.store_health(self.instance_dir)
        self.assertFalse(health["initialized"])
        self.assertEqual(health["secret_count"], 1)
        self.assertEqual(health["installation_key"], {"present": True, "usable": False})
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("installation key is missing or unusable", findings[0])

    def test_corrupted_key_params_version_does_not_leak_the_installation_key(self) -> None:
        """A key-params file with the raw installation key stuffed into its
        `version` field (as `restore_installation_key` or manual tampering
        could produce) must not have that value echoed back by a finding."""
        self.initialize()
        set_secret(
            self.instance_dir,
            secret_id="kanboard.api-token",
            value=b"token-value",
            scope="installation",
            purpose="board api",
            actor="tester",
        )
        key_path = self.instance_dir / "secrets" / KEY_NAME
        raw_key = key_path.read_text(encoding="utf-8").strip()
        params_path = self.instance_dir / "secrets" / KEY_PARAMS_NAME
        params = json.loads(params_path.read_text(encoding="utf-8"))
        params["version"] = raw_key
        params_path.write_text(json.dumps(params), encoding="utf-8")
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertNotIn(raw_key, findings[0])
        self.assertIn("installation key is missing or unusable", findings[0])

    def test_missing_catalog_with_key_params_present_is_a_finding_not_absence(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / CATALOG_NAME).unlink()
        health = secret_store.store_health(self.instance_dir)
        self.assertFalse(health["initialized"])
        self.assertEqual(health["secret_count"], 0)
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])

    def test_malformed_catalog_is_a_finding_not_a_crash(self) -> None:
        self.initialize()
        (self.instance_dir / "secrets" / CATALOG_NAME).write_text(
            "bad: catalog\n", encoding="utf-8"
        )
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])
        health = secret_store.store_health(self.instance_dir)
        self.assertTrue(health["initialized"])

    def test_a_malformed_scalar_does_not_leak_its_content_into_a_finding(self) -> None:
        self.initialize()
        sentinel = "sentinel-secret-do-not-leak"
        (self.instance_dir / "secrets" / CATALOG_NAME).write_text(
            f'version: 1\nsecrets: "{sentinel}\n', encoding="utf-8"
        )
        findings = secret_store.store_findings(self.instance_dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("secret store:", findings[0])
        for finding in findings:
            self.assertNotIn(sentinel, finding)


class CatalogSchemaCase(unittest.TestCase):
    """The materialization record is only as good as the schema that guards it."""

    def catalog(self, entry: dict) -> dict:
        return {
            "version": secret_store.CATALOG_VERSION,
            "secrets": [
                {
                    "id": "example_url",
                    "scope": "installation",
                    "purpose": "board api",
                    "created_at": "2026-07-26T10:00:00Z",
                    **entry,
                }
            ],
        }

    def test_a_usable_record_validates(self) -> None:
        for instruction in (
            {"target": "runtime-env", "order": 0},
            {"target": "file", "path": "/etc/secretary/app.env", "order": 7},
        ):
            with self.subTest(instruction=instruction):
                catalog = self.catalog(
                    {"environment": "EXAMPLE_URL", "materialize": instruction}
                )
                self.assertEqual(validate(catalog, "secret-catalog", "catalog.yaml"), [])

    def test_a_record_nothing_could_act_on_is_rejected(self) -> None:
        cases = [
            {"environment": "EXAMPLE_URL", "materialize": {"target": "elsewhere", "order": 0}},
            {"environment": "EXAMPLE_URL", "materialize": {"target": "file", "order": 0}},
            {
                "environment": "EXAMPLE_URL",
                "materialize": {
                    "target": "runtime-env", "path": "/etc/runtime.env", "order": 0
                },
            },
            # Without a variable name there is nothing to write on the left of '='.
            {"materialize": {"target": "runtime-env", "order": 0}},
            {
                "environment": "not-an-env-name",
                "materialize": {"target": "runtime-env", "order": 0},
            },
            # Without a line number the file layout is not recorded at all.
            {"environment": "EXAMPLE_URL", "materialize": {"target": "runtime-env"}},
            {"environment": "EXAMPLE_URL", "materialize": {"target": "runtime-env", "order": -1}},
            {
                "environment": "EXAMPLE_URL",
                "materialize": {"target": "runtime-env", "order": "first"},
            },
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertNotEqual(
                    validate(self.catalog(entry), "secret-catalog", "catalog.yaml"), []
                )


class SecretCliCase(SecretStoreCase):
    def run_cli(
        self,
        argv: list[str],
        stdin: bytes = b"",
        *,
        interactive: bool = True,
        clear_ok: bool = True,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("sys.stdout", out))
            stack.enter_context(mock.patch("sys.stderr", err))
            stack.enter_context(mock.patch("sys.stdin", stream))
            if interactive:
                stack.enter_context(
                    mock.patch.object(
                        secret_commands, "_stdin_and_stderr_are_interactive", return_value=True
                    )
                )
            if clear_ok:
                stack.enter_context(
                    mock.patch.object(
                        secret_commands, "_clear_screen_and_scrollback", return_value=True
                    )
                )
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_init_shows_the_phrase_once_and_needs_it_confirmed(self) -> None:
        answers: list[str] = []
        phrase = " ".join(RECOVERY_WORDS[64:80])

        def fake_read_line(prompt: str) -> str:
            answers.append(prompt)
            if prompt.startswith("Type 'yes'"):
                return "yes"
            position = int(prompt.split()[1].rstrip(":")) - 1
            return phrase.split()[position]

        with mock.patch.object(secret_commands, "generate_recovery_phrase", return_value=phrase):
            with mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line):
                code, out, err = self.run_cli(
                    ["secret", "init", "--instance", str(self.instance_dir)]
                )
        self.assertEqual(code, 0)
        # One "written it down" acknowledgement plus one question per confirmed word.
        self.assertEqual(len(answers), secret_store.CONFIRM_WORDS + 1)
        # The phrase is shown on stderr, so a redirected stdout cannot capture it.
        self.assertIn(phrase.split()[0], err)
        self.assertNotIn(phrase.split()[0], out)
        self.assertTrue(json.loads(out)["ok"])
        self.assertTrue(secret_store.is_initialized(self.instance_dir))

    def test_init_without_a_correct_confirmation_initializes_nothing(self) -> None:
        def fake_read_line(prompt: str) -> str:
            return "yes" if prompt.startswith("Type 'yes'") else "wrong"

        with mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line):
            code, out, err = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))
        self.assertFalse((self.instance_dir / "secrets" / KEY_NAME).exists())
        # A wrong answer must not get the phrase printed again, nor the right word hinted.
        self.assertEqual(err.count("Recovery phrase."), 1)
        self.assertNotIn("wrong", out)

    def test_second_init_refuses_through_the_cli(self) -> None:
        self.initialize()
        code, out, _ = self.run_cli(["secret", "init", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("already initialized", json.loads(out)["message"])

    def test_init_refuses_when_not_interactive_before_generating_a_phrase(self) -> None:
        with mock.patch.object(secret_commands, "generate_recovery_phrase") as generate:
            code, out, err = self.run_cli(
                ["secret", "init", "--instance", str(self.instance_dir)], interactive=False
            )
        generate.assert_not_called()
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("interactive", payload["message"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))
        combined_words = set(re.findall(r"[a-z]+", (out + err).lower()))
        self.assertFalse(combined_words & set(RECOVERY_WORDS))

    def test_init_refuses_if_the_screen_cannot_be_cleared(self) -> None:
        def fake_read_line(prompt: str) -> str:
            return "yes"

        with mock.patch.object(secret_commands, "_read_line", side_effect=fake_read_line):
            with mock.patch.object(
                secret_commands, "_clear_screen_and_scrollback", return_value=False
            ):
                code, out, _ = self.run_cli(
                    ["secret", "init", "--instance", str(self.instance_dir)], clear_ok=False
                )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("clear", payload["message"])
        self.assertFalse(secret_store.is_initialized(self.instance_dir))

    def test_init_sequence_is_show_then_acknowledge_then_clear_then_questions(self) -> None:
        order: list[str] = []

        def show(_phrase: str) -> None:
            order.append("show")

        def acknowledge() -> bool:
            order.append("acknowledge")
            return True

        def clear() -> bool:
            order.append("clear")
            return True

        def confirm(_phrase: str) -> bool:
            order.append("confirm")
            return True

        with mock.patch.object(secret_commands, "_show_phrase", side_effect=show), mock.patch.object(
            secret_commands, "_acknowledge_written_down", side_effect=acknowledge
        ), mock.patch.object(
            secret_commands, "_clear_screen_and_scrollback", side_effect=clear
        ), mock.patch.object(
            secret_commands, "_confirm_phrase", side_effect=confirm
        ):
            code, _out, _err = self.run_cli(
                ["secret", "init", "--instance", str(self.instance_dir)], clear_ok=False
            )
        self.assertEqual(code, 0)
        self.assertEqual(order, ["show", "acknowledge", "clear", "confirm"])

    def test_clear_screen_and_scrollback_refuses_on_a_dumb_terminal(self) -> None:
        err = io.StringIO()
        err.isatty = lambda: True
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "dumb"}):
            self.assertFalse(secret_commands._clear_screen_and_scrollback())

    def test_clear_screen_and_scrollback_refuses_when_stderr_is_not_a_tty(self) -> None:
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "xterm"}):
            self.assertFalse(secret_commands._clear_screen_and_scrollback())

    def test_clear_screen_and_scrollback_writes_the_full_clear_sequence(self) -> None:
        err = io.StringIO()
        err.isatty = lambda: True
        with mock.patch("sys.stderr", err), mock.patch.dict(os.environ, {"TERM": "xterm-256color"}):
            self.assertTrue(secret_commands._clear_screen_and_scrollback())
        self.assertIn("\033[3J", err.getvalue())

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
        """There is no `--value`; the public CLI returns a structured usage error."""
        self.initialize()
        code, output, errors = self.run_cli(
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
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "usage")
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_import_materialize_and_remove_through_the_cli(self) -> None:
        self.initialize()
        source = Path(self.tmpdir.name) / "runtime.env"
        source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        target = Path(self.tmpdir.name) / "out" / "runtime.env"

        code, out, _ = self.run_cli(
            [
                "secret",
                "import",
                "--instance",
                str(self.instance_dir),
                "--file",
                str(source),
                "--scope",
                "installation",
                "--purpose",
                "board api",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(report["created"]), 3)
        # The report names ids and nothing else; no value reaches stdout.
        self.assertNotIn("1f2e3d4c5b6a", out)
        self.assertNotIn("secretary-instance/secrets/values", out)

        with mock.patch.dict(os.environ, {"SECRETARY_RUNTIME_ENV_FILE": str(target)}):
            code, out, _ = self.run_cli(
                ["secret", "materialize", "--instance", str(self.instance_dir)]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["targets"][0]["path"], str(target))
        self.assertNotIn("1f2e3d4c5b6a", out)
        self.assertEqual(target.read_text(encoding="utf-8"), LIVE_RUNTIME_ENV)

        code, out, _ = self.run_cli(
            ["secret", "remove", "--instance", str(self.instance_dir), "--id", "example_url"]
        )
        self.assertEqual(code, 0)
        self.assertEqual([entry["id"] for entry in list_secrets(self.instance_dir)],
                         ["example_api_token", "example_api_user"])

        code, out, _ = self.run_cli(
            ["secret", "remove", "--instance", str(self.instance_dir), "--id", "example_url"]
        )
        self.assertEqual(code, 3)
        self.assertIn("no secret named", json.loads(out)["message"])

    def test_a_file_target_needs_its_path_on_the_command_line(self) -> None:
        self.initialize()
        source = Path(self.tmpdir.name) / "runtime.env"
        source.write_text(LIVE_RUNTIME_ENV, encoding="utf-8")
        code, out, _ = self.run_cli(
            [
                "secret",
                "import",
                "--instance",
                str(self.instance_dir),
                "--file",
                str(source),
                "--scope",
                "installation",
                "--purpose",
                "board api",
                "--materialize",
                "file",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("--materialize-path", json.loads(out)["message"])
        self.assertEqual(list_secrets(self.instance_dir), ())

    def test_list_before_init_says_so_instead_of_failing_obscurely(self) -> None:
        code, out, _ = self.run_cli(["secret", "list", "--instance", str(self.instance_dir)])
        self.assertEqual(code, 3)
        self.assertIn("not initialized", json.loads(out)["message"])


if __name__ == "__main__":
    unittest.main()
