"""Recoverable secret store: envelope format, installation key, open catalog.

Contract: the sprint document "Восстановимое хранилище секретов". The store lives
in the private instance repo, so it rides the same recovery chain as the board,
the runs and the knowledge plane:

    secretary-instance/
      .gitignore              secrets/installation.key
      secrets/
        catalog.yaml          open metadata, tracked, redact-scanned
        installation-key.json open KDF parameters plus a verifier, tracked
        installation.key      raw key, mode 0600, never committed
        values/<id>.enc.json  one versioned envelope per secret

Two keys, two jobs. The installation key opens the values after a reboot without
a human. The recovery phrase exists only to rebuild that key on a clean host: it
is generated here, shown once and never stored. `installation-key.json` holds the
KDF id and its parameters in the open, so a future version of the product can
still derive today's key, and a verifier so a mistyped phrase fails loudly
instead of producing a plausible-looking wrong key.

Every envelope carries its own format version, KDF id, KDF parameters and AEAD id
in the clear next to the ciphertext. Nothing about how a value was sealed lives
only in this module's constants. The primitives are `cryptography`'s (Scrypt,
HKDF, ChaCha20-Poly1305); there is no cryptographic math of our own here.

Writes go through `state_repo.state_repo_lock` and land as one commit, so the
catalog and the values it names can never diverge in the history.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from secretary import state_repo
from secretary._fsutil import publish_state_atomic
from secretary.config import validate
from secretary.secret_words import RECOVERY_WORDS
from secretary.state_repo import SECRETS_PATHSPEC

from triggered_agents.runtime.redact import redact


CATALOG_NAME = "catalog.yaml"
KEY_PARAMS_NAME = "installation-key.json"
KEY_NAME = "installation.key"
VALUES_DIRNAME = "values"
VALUE_SUFFIX = ".enc.json"

GITIGNORE_ENTRY = "secrets/installation.key"
# Init also commits .gitignore, because the key file is only safe once git is
# told to ignore it.
INIT_PATHSPEC = (*SECRETS_PATHSPEC, ".gitignore")

CATALOG_VERSION = 1

KEY_PARAMS_FORMAT = "secretary.installation-key"
KEY_PARAMS_VERSION = 1
PHRASE_KDF_ID = "scrypt"
PHRASE_KDF_N = 2**16
PHRASE_KDF_R = 8
PHRASE_KDF_P = 1
KEY_LENGTH = 32
VERIFIER_PLAINTEXT = b"secretary installation key v1"
VERIFIER_AAD = b"secretary/installation-key/v1"

ENVELOPE_FORMAT = "secretary.secret-envelope"
ENVELOPE_VERSION = 1
VALUE_KDF_ID = "hkdf-sha256"
VALUE_KDF_INFO = "secretary/secret/v1"
AEAD_ID = "chacha20poly1305"
NONCE_LENGTH = 12
SALT_LENGTH = 16

PHRASE_WORDS = 16
CONFIRM_WORDS = 3

INSTALLATION_SCOPE = "installation"
PROJECT_SCOPE_PREFIX = "project:"

_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")


class SecretStoreError(RuntimeError):
    """A store operation did not happen."""


class SecretStoreValidationError(SecretStoreError):
    """The request is not something the store accepts."""


class SecretStoreStateError(SecretStoreError):
    """The store on disk is not in the state this operation needs."""


class RecoveryPhraseError(SecretStoreError):
    """The recovery phrase does not open this installation key."""


@dataclass(frozen=True)
class InitResult:
    key_path: Path
    catalog_path: Path
    commit: str


@dataclass(frozen=True)
class SetResult:
    secret_id: str
    scope: str
    path: Path
    commit: str
    created: bool


# ---------------------------------------------------------------------------
# Paths


def secrets_dir(instance_dir: Path) -> Path:
    return state_repo.secrets_dir(instance_dir)


def catalog_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / CATALOG_NAME


def key_params_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / KEY_PARAMS_NAME


def key_path(instance_dir: Path) -> Path:
    return secrets_dir(instance_dir) / KEY_NAME


def value_path(instance_dir: Path, secret_id: str) -> Path:
    return secrets_dir(instance_dir) / VALUES_DIRNAME / f"{secret_id}{VALUE_SUFFIX}"


def is_initialized(instance_dir: Path) -> bool:
    return key_params_path(instance_dir).exists() and catalog_path(instance_dir).exists()


# ---------------------------------------------------------------------------
# Recovery phrase


def generate_recovery_phrase(words: int = PHRASE_WORDS) -> str:
    """A fresh phrase with `words` * 8 bits of entropy, chosen by the product.

    The user never invents this. `pysecrets.choice` is the system CSPRNG, and the
    wordlist is exactly 256 long, so the entropy is the word count times eight.
    """
    if words < 8:
        raise SecretStoreValidationError("a recovery phrase needs at least 8 words")
    return " ".join(pysecrets.choice(RECOVERY_WORDS) for _ in range(words))


def normalize_phrase(phrase: str) -> str:
    """Collapse the shape a human types back to the shape that was generated."""
    normalized = " ".join(str(phrase).lower().split())
    if not normalized:
        raise SecretStoreValidationError("recovery phrase is empty")
    return normalized


# ---------------------------------------------------------------------------
# Installation key


def _new_key_params() -> dict[str, Any]:
    return {
        "format": KEY_PARAMS_FORMAT,
        "version": KEY_PARAMS_VERSION,
        "kdf": {
            "id": PHRASE_KDF_ID,
            "salt": _b64(pysecrets.token_bytes(SALT_LENGTH)),
            "length": KEY_LENGTH,
            "n": PHRASE_KDF_N,
            "r": PHRASE_KDF_R,
            "p": PHRASE_KDF_P,
        },
    }


def _derive_key(phrase: str, params: dict[str, Any]) -> bytes:
    kdf = params.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("id") != PHRASE_KDF_ID:
        raise SecretStoreStateError(
            f"unsupported installation key kdf: {kdf.get('id') if isinstance(kdf, dict) else kdf!r}"
        )
    try:
        derivation = Scrypt(
            salt=_unb64(kdf["salt"], "installation key salt"),
            length=int(kdf["length"]),
            n=int(kdf["n"]),
            r=int(kdf["r"]),
            p=int(kdf["p"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SecretStoreStateError(f"installation key parameters are unusable: {exc}") from None
    return derivation.derive(normalize_phrase(phrase).encode("utf-8"))


def _seal_verifier(key: bytes) -> dict[str, str]:
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, VERIFIER_PLAINTEXT, VERIFIER_AAD)
    return {"id": AEAD_ID, "nonce": _b64(nonce), "ciphertext": _b64(sealed)}


def _check_verifier(key: bytes, params: dict[str, Any]) -> None:
    verifier = params.get("verifier")
    if not isinstance(verifier, dict) or verifier.get("id") != AEAD_ID:
        raise SecretStoreStateError("installation key file carries no usable verifier")
    try:
        opened = ChaCha20Poly1305(key).decrypt(
            _unb64(verifier["nonce"], "verifier nonce"),
            _unb64(verifier["ciphertext"], "verifier ciphertext"),
            VERIFIER_AAD,
        )
    except (InvalidTag, KeyError, TypeError):
        raise RecoveryPhraseError(
            "recovery phrase does not match this installation; nothing was written"
        ) from None
    if opened != VERIFIER_PLAINTEXT:
        raise RecoveryPhraseError("recovery phrase does not match this installation")


def load_installation_key(instance_dir: Path) -> bytes:
    """Read the key from disk, refusing a file the wrong user or mode owns."""
    path = key_path(instance_dir)
    try:
        info = path.lstat()
    except OSError:
        raise SecretStoreStateError(
            f"installation key is missing: {path}; restore it from the recovery phrase"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise SecretStoreStateError("installation key must be a regular file, not a symlink")
    if info.st_mode & 0o077:
        raise SecretStoreStateError("installation key permissions are too broad; run chmod 0600")
    if info.st_uid != os.geteuid():
        raise SecretStoreStateError("installation key belongs to another user")
    try:
        material = _unb64(path.read_text(encoding="utf-8").strip(), "installation key")
    except OSError as exc:
        raise SecretStoreStateError(f"could not read the installation key: {exc}") from None
    if len(material) != KEY_LENGTH:
        raise SecretStoreStateError("installation key has the wrong length")
    _check_verifier(material, _read_key_params(instance_dir))
    return material


def restore_installation_key(instance_dir: Path, phrase: str) -> Path:
    """Rebuild the key file from the phrase. Wrong phrase writes nothing."""
    instance_dir = state_repo.require_repo(instance_dir)
    params = _read_key_params(instance_dir)
    key = _derive_key(phrase, params)
    _check_verifier(key, params)
    with state_repo.state_repo_lock(instance_dir):
        _write_key_file(key_path(instance_dir), key)
    return key_path(instance_dir)


def _write_key_file(path: Path, key: bytes) -> None:
    """Write the raw key 0600 without ever leaving it world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_b64(key) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SecretStoreError(f"could not write the installation key: {exc}") from None


def _read_key_params(instance_dir: Path) -> dict[str, Any]:
    path = key_params_path(instance_dir)
    try:
        params = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            "secret store is not initialized; run `secretary secret init` first"
        ) from None
    except (OSError, ValueError) as exc:
        raise SecretStoreStateError(f"could not read {KEY_PARAMS_NAME}: {exc}") from None
    if not isinstance(params, dict) or params.get("format") != KEY_PARAMS_FORMAT:
        raise SecretStoreStateError(f"{KEY_PARAMS_NAME} is not an installation key file")
    if params.get("version") != KEY_PARAMS_VERSION:
        raise SecretStoreStateError(
            f"{KEY_PARAMS_NAME} has format version {params.get('version')!r}, "
            f"this product reads {KEY_PARAMS_VERSION}"
        )
    return params


# ---------------------------------------------------------------------------
# Envelope


def seal_value(key: bytes, secret_id: str, value: bytes) -> dict[str, Any]:
    """Wrap one value. Everything needed to open it later is in the result."""
    salt = pysecrets.token_bytes(SALT_LENGTH)
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    header = {
        "format": ENVELOPE_FORMAT,
        "version": ENVELOPE_VERSION,
        "id": secret_id,
        "kdf": {
            "id": VALUE_KDF_ID,
            "salt": _b64(salt),
            "length": KEY_LENGTH,
            "info": VALUE_KDF_INFO,
        },
        "aead": {"id": AEAD_ID, "nonce": _b64(nonce)},
    }
    subkey = _derive_value_key(key, header)
    ciphertext = ChaCha20Poly1305(subkey).encrypt(nonce, value, _header_bytes(header))
    return {**header, "ciphertext": _b64(ciphertext)}


def open_value(key: bytes, envelope: dict[str, Any]) -> bytes:
    """Unwrap one envelope, reading its own declared parameters, not ours."""
    if not isinstance(envelope, dict) or envelope.get("format") != ENVELOPE_FORMAT:
        raise SecretStoreStateError("value file is not a secret envelope")
    version = envelope.get("version")
    if version != ENVELOPE_VERSION:
        raise SecretStoreStateError(
            f"envelope format version {version!r} is newer than this product reads "
            f"({ENVELOPE_VERSION}); upgrade secretary"
        )
    aead = envelope.get("aead")
    if not isinstance(aead, dict) or aead.get("id") != AEAD_ID:
        raise SecretStoreStateError(f"unsupported envelope aead: {envelope.get('aead')!r}")
    header = {name: field for name, field in envelope.items() if name != "ciphertext"}
    subkey = _derive_value_key(key, header)
    try:
        return ChaCha20Poly1305(subkey).decrypt(
            _unb64(aead["nonce"], "envelope nonce"),
            _unb64(envelope["ciphertext"], "envelope ciphertext"),
            _header_bytes(header),
        )
    except (InvalidTag, KeyError, TypeError):
        raise SecretStoreStateError(
            f"could not open the value for {envelope.get('id')!r}: "
            "wrong installation key or a damaged envelope"
        ) from None


def _derive_value_key(key: bytes, header: dict[str, Any]) -> bytes:
    kdf = header.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("id") != VALUE_KDF_ID:
        raise SecretStoreStateError(f"unsupported envelope kdf: {header.get('kdf')!r}")
    try:
        derivation = HKDF(
            algorithm=SHA256(),
            length=int(kdf["length"]),
            salt=_unb64(kdf["salt"], "envelope salt"),
            info=f"{kdf['info']}:{header.get('id')}".encode("utf-8"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SecretStoreStateError(f"envelope parameters are unusable: {exc}") from None
    return derivation.derive(key)


def _header_bytes(header: dict[str, Any]) -> bytes:
    """The open part of the envelope, bound into the AEAD tag.

    Serialized with sorted keys and no spaces so the bytes are the same whether
    the header was just built or parsed back from the file.
    """
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Catalog


def load_catalog(instance_dir: Path) -> dict[str, Any]:
    path = catalog_path(instance_dir)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            "secret store is not initialized; run `secretary secret init` first"
        ) from None
    except (OSError, yaml.YAMLError) as exc:
        raise SecretStoreStateError(f"could not read {CATALOG_NAME}: {exc}") from None
    errors = validate(data, "secret-catalog", f"secrets/{CATALOG_NAME}")
    if errors:
        raise SecretStoreStateError(f"{CATALOG_NAME} is invalid: {errors[0]}")
    return data


def list_secrets(instance_dir: Path) -> tuple[dict[str, Any], ...]:
    """Catalog metadata only. There is no path from here to a value."""
    return tuple(load_catalog(instance_dir)["secrets"])


def store_divergence(instance_dir: Path) -> tuple[str, ...]:
    """Names the catalog and the values directory disagree about.

    A consistent store has exactly one envelope per catalog entry and no envelope
    without one. Used by the tests that interrupt a write, and by whatever
    reports store health later.
    """
    catalogued = {entry["id"] for entry in list_secrets(instance_dir)}
    root = secrets_dir(instance_dir) / VALUES_DIRNAME
    stored = set()
    if root.is_dir():
        stored = {
            path.name[: -len(VALUE_SUFFIX)]
            for path in root.iterdir()
            if path.is_file() and path.name.endswith(VALUE_SUFFIX)
        }
    return tuple(
        sorted(
            [f"{name}: catalogued with no value" for name in catalogued - stored]
            + [f"{name}: value with no catalog entry" for name in stored - catalogued]
        )
    )


def _catalog_text(catalog: dict[str, Any]) -> str:
    return yaml.safe_dump(catalog, sort_keys=True, allow_unicode=True, default_flow_style=False)


# ---------------------------------------------------------------------------
# Operations


def initialize_store(instance_dir: Path, *, phrase: str, actor: str) -> InitResult:
    """Create the key, the key parameters and an empty catalog. Never overwrites.

    The caller owns showing the phrase and confirming it; by the time this runs
    the user has already proved they wrote it down.
    """
    actor = _clean_actor(actor)
    instance_dir = state_repo.require_repo(instance_dir)
    params = _new_key_params()
    key = _derive_key(phrase, params)
    params["verifier"] = _seal_verifier(key)
    catalog = {"version": CATALOG_VERSION, "secrets": []}

    with state_repo.state_repo_lock(instance_dir):
        if key_params_path(instance_dir).exists() or catalog_path(instance_dir).exists():
            raise SecretStoreStateError(
                "secret store is already initialized; init will not overwrite it. "
                "Rotating the recovery phrase is a separate operation."
            )
        secrets_dir(instance_dir).mkdir(parents=True, exist_ok=True)
        # Ignore the key before it exists, so no window has an unignored key file.
        _ensure_gitignore(instance_dir)
        _write_key_file(key_path(instance_dir), key)
        _assert_key_ignored(instance_dir)
        catalog_text = _catalog_text(catalog)
        params_text = json.dumps(params, indent=2, sort_keys=True) + "\n"
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        _scan_open_file(f"secrets/{KEY_PARAMS_NAME}", params_text)
        _publish(
            [
                (key_params_path(instance_dir), params_text),
                (catalog_path(instance_dir), catalog_text),
            ]
        )
        commit = state_repo.commit(
            instance_dir, INIT_PATHSPEC, _commit_message("init", "store initialized", actor)
        )
        if commit is None:
            raise SecretStoreError("secret store init produced nothing to commit")
    return InitResult(
        key_path=key_path(instance_dir),
        catalog_path=catalog_path(instance_dir),
        commit=commit,
    )


def set_secret(
    instance_dir: Path,
    *,
    secret_id: str,
    value: bytes,
    scope: str,
    purpose: str,
    actor: str,
    environment: str | None = None,
) -> SetResult:
    """Seal one value and record its metadata, as a single commit."""
    actor = _clean_actor(actor)
    secret_id = _clean_secret_id(secret_id)
    scope = _clean_scope(scope)
    purpose = _clean_purpose(purpose)
    environment = _clean_environment(environment)
    if not isinstance(value, (bytes, bytearray)):
        raise SecretStoreValidationError("secret value must be bytes")
    if not value:
        raise SecretStoreValidationError("secret value is empty")

    instance_dir = state_repo.require_repo(instance_dir)
    with state_repo.state_repo_lock(instance_dir):
        key = load_installation_key(instance_dir)
        # Re-checked on every write, not only at init: this commit is the moment
        # a key that stopped being ignored would enter the history.
        _assert_key_ignored(instance_dir)
        catalog = load_catalog(instance_dir)
        entries = {entry["id"]: dict(entry) for entry in catalog["secrets"]}
        existing = entries.get(secret_id)
        entry = {
            "id": secret_id,
            "scope": scope,
            "purpose": purpose,
            "created_at": existing["created_at"] if existing else _now(),
        }
        if environment:
            entry["environment"] = environment
        entries[secret_id] = entry
        catalog = {
            "version": CATALOG_VERSION,
            "secrets": [entries[name] for name in sorted(entries)],
        }
        errors = validate(catalog, "secret-catalog", f"secrets/{CATALOG_NAME}")
        if errors:
            raise SecretStoreValidationError(f"catalog entry is invalid: {errors[0]}")

        catalog_text = _catalog_text(catalog)
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        envelope_text = json.dumps(
            seal_value(key, secret_id, bytes(value)), indent=2, sort_keys=True
        ) + "\n"
        # No redact scan on the envelope. Its body is ciphertext plus the open
        # parameters needed to decrypt it: a pattern match there would be a
        # coincidence of base64, and a value that redact happened to recognize is
        # exactly the kind of value the store exists to hold.
        _publish(
            [
                (value_path(instance_dir, secret_id), envelope_text),
                (catalog_path(instance_dir), catalog_text),
            ]
        )
        commit = state_repo.commit(
            instance_dir,
            SECRETS_PATHSPEC,
            _commit_message("set", secret_id, actor),
        )
        if commit is None:
            commit = state_repo.head(instance_dir) or ""
    return SetResult(
        secret_id=secret_id,
        scope=scope,
        path=value_path(instance_dir, secret_id),
        commit=commit,
        created=existing is None,
    )


def read_secret(instance_dir: Path, secret_id: str) -> bytes:
    """Internal API. No command in this card puts the result on stdout."""
    secret_id = _clean_secret_id(secret_id)
    instance_dir = state_repo.require_repo(instance_dir)
    if not any(entry["id"] == secret_id for entry in list_secrets(instance_dir)):
        raise SecretStoreStateError(f"no secret named {secret_id!r} in the catalog")
    path = value_path(instance_dir, secret_id)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            f"secret {secret_id!r} is catalogued but its value file is missing"
        ) from None
    except (OSError, ValueError) as exc:
        raise SecretStoreStateError(f"could not read the value for {secret_id!r}: {exc}") from None
    return open_value(load_installation_key(instance_dir), envelope)


# ---------------------------------------------------------------------------
# Helpers


def _publish(writes: list[tuple[Path, str]]) -> None:
    for path, _ in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        publish_state_atomic(writes)
    except (OSError, RuntimeError) as exc:
        raise SecretStoreError(f"could not write the secret store: {exc}") from None


def _scan_open_file(name: str, text: str) -> None:
    """The open half of the store leaves the host, so it passes the same gate.

    `checkpoint.py` blocks a commit when `redact()` changes `state/`; the catalog
    is tracked plaintext with the same reach, so a value pasted into a purpose
    field stops here rather than in the history.
    """
    if redact(text) != text:
        raise SecretStoreValidationError(f"secret detected in {name}")


def _ensure_gitignore(instance_dir: Path) -> None:
    path = instance_dir / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as exc:
        raise SecretStoreError(f"could not read .gitignore: {exc}") from None
    if GITIGNORE_ENTRY in current.split():
        return
    updated = current if not current or current.endswith("\n") else current + "\n"
    _publish([(path, updated + GITIGNORE_ENTRY + "\n")])


def _assert_key_ignored(instance_dir: Path) -> None:
    """Prove to git, not to ourselves, that the key cannot be committed."""
    try:
        state_repo.git(
            instance_dir,
            ["check-ignore", "--quiet", "--", GITIGNORE_ENTRY],
            label="verify the installation key is gitignored",
        )
    except state_repo.StateRepoError:
        raise SecretStoreError(
            f"{GITIGNORE_ENTRY} is not ignored by this repo; refusing to keep a committable key"
        ) from None


def _clean_actor(actor: str) -> str:
    value = str(actor).strip()
    if not value:
        raise SecretStoreValidationError("actor is required")
    return value


def _clean_secret_id(secret_id: str) -> str:
    value = str(secret_id).strip()
    if not value or len(value) > 128:
        raise SecretStoreValidationError("secret id must be 1..128 characters")
    if value[0] not in _ID_ALLOWED - set("._-") or any(char not in _ID_ALLOWED for char in value):
        raise SecretStoreValidationError(
            "secret id must be lowercase letters, digits, dot, dash or underscore, "
            "starting with a letter or digit"
        )
    if ".." in value:
        raise SecretStoreValidationError("secret id must not contain '..'")
    return value


def _clean_scope(scope: str) -> str:
    value = str(scope).strip()
    if value == INSTALLATION_SCOPE:
        return value
    if value.startswith(PROJECT_SCOPE_PREFIX):
        project = value[len(PROJECT_SCOPE_PREFIX):]
        if project and project[0] in _ID_ALLOWED - set("._-"):
            if all(char in _ID_ALLOWED for char in project):
                return value
    raise SecretStoreValidationError(
        f"scope must be '{INSTALLATION_SCOPE}' or '{PROJECT_SCOPE_PREFIX}<id>'"
    )


def _clean_purpose(purpose: str) -> str:
    value = " ".join(str(purpose).split())
    if not value:
        raise SecretStoreValidationError("purpose is required")
    if len(value) > 500:
        raise SecretStoreValidationError("purpose must be at most 500 characters")
    return value


def _clean_environment(environment: str | None) -> str | None:
    if environment is None:
        return None
    value = str(environment).strip()
    if not value:
        return None
    if len(value) > 64 or not value.replace("_", "").isalnum():
        raise SecretStoreValidationError("environment must be an environment variable name")
    return value


def _commit_message(operation: str, subject: str, actor: str) -> str:
    return "\n".join(
        [
            f"secrets: {operation} {subject}",
            "",
            f"Principal: {actor}",
            f"Operation: {operation}",
        ]
    ) + "\n"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: Any, what: str) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise SecretStoreStateError(f"{what} is not valid base64") from None
