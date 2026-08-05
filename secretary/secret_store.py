"""Recoverable secret store: envelope format, installation key, open catalog.

Contract: docs/RECOVERY.md, "Secrets" — a recoverable secret store. The store lives
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

A secret that some process reads as an environment variable also carries a
`materialize` record: the variable name, which file it belongs to and which line
of that file it is. That record is what lets a recovered installation put its env
files back without a human listing paths. `materialize_secrets` writes each file
whole and by rename, because the one file this exists for, `runtime.env`, is read
by systemd on every unit start and may never be seen missing or half-written.

The env-file format the store round-trips is `KEY=VALUE` lines, LF, one variable
per line, nothing else: no comments, no blank lines, no padding, and a final
newline. `import` refuses anything outside it rather than take in a file it would
hand back as different bytes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets as pysecrets
import stat
import tempfile
from collections.abc import Container
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

from secretary import role_env, state_repo
from secretary.board_transport import DEFAULT_TOKEN, BoardTransportError, resolve as resolve_board_transport
from secretary._fsutil import publish_state_atomic
from secretary.config import _safe_yaml_error, validate
from secretary.secret_words import RECOVERY_WORDS
from secretary.state_repo import SECRETS_PATHSPEC

from triggered_agents.runtime.redact import looks_like_credential, redact


LEGACY_BOARD_SECRET_IDS = frozenset({"kanboard_url", "kanboard_api_user", "kanboard_api_token"})


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

# Where a value goes when it is materialized. `runtime-env` is the installation's
# own env file, whose path only `role_env.runtime_env_path()` may answer; `file`
# names any other env file, and carries the path in the catalog.
MATERIALIZE_RUNTIME_ENV = "runtime-env"
MATERIALIZE_FILE = "file"
MATERIALIZE_TARGETS = (MATERIALIZE_RUNTIME_ENV, MATERIALIZE_FILE)

_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


@dataclass(frozen=True)
class RemoveResult:
    secret_id: str
    path: Path
    commit: str


@dataclass(frozen=True)
class ImportResult:
    """What one `import` did, per secret id. Values never appear here."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    commit: str


@dataclass(frozen=True)
class MaterializeResult:
    target: str
    path: Path
    variables: tuple[str, ...]
    changed: bool


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


def _store_exists(instance_dir: Path) -> bool:
    """Whether `secrets/` holds any trace of a store, complete or not.

    `is_initialized` requires the catalog and the key params together, so a
    store missing just one of those files reads as fully absent to it. Health
    and findings need to tell that apart from a directory that was never
    touched, so this checks each file that `init` can produce independently,
    plus a non-empty values directory.
    """
    if (
        catalog_path(instance_dir).exists()
        or key_params_path(instance_dir).exists()
        or key_path(instance_dir).exists()
    ):
        return True
    values_dir = secrets_dir(instance_dir) / VALUES_DIRNAME
    return values_dir.is_dir() and any(values_dir.iterdir())


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


def verify_recovery_phrase(instance_dir: Path, phrase: str) -> None:
    """Answer whether the phrase opens this store, touching no file.

    A preview run has to say what a real run would do without doing it, and the
    only honest way to promise the store would open is to derive the key and put
    it against the verifier. The derived key is dropped here; nothing is written.
    """
    params = _read_key_params(instance_dir)
    _check_verifier(_derive_key(phrase, params), params)


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
            f"{KEY_PARAMS_NAME} has an unsupported format version; "
            f"this product reads version {KEY_PARAMS_VERSION}"
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
    except yaml.YAMLError as exc:
        raise SecretStoreStateError(
            f"{CATALOG_NAME} is invalid: {_safe_yaml_error(exc)}"
        ) from None
    except OSError as exc:
        raise SecretStoreStateError(
            f"could not read {CATALOG_NAME}: {exc.strerror or 'unreadable'}"
        ) from None
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
# Observability
#
# `store_health` and `store_findings` are the only two functions in this module
# that `status` and `doctor` call. Both read the catalog and the key's own
# metadata (mode, presence, whether it opens the store); neither ever touches
# `read_secret` or `open_value`, so no value, sealed or otherwise, and no key
# material can reach either report.


def store_health(instance_dir: Path) -> dict[str, Any]:
    """Non-secret snapshot of the store for `secretary status --json`.

    A `secrets/` directory holding none of catalog, key params or key file
    reports as absent rather than raising: an installation with no secrets in
    it yet is a valid state, not a defect. Any other partial shape reports as
    present, with `initialized` reflecting whether catalog and key params are
    both there.
    """
    instance_dir = Path(instance_dir)
    if not _store_exists(instance_dir):
        return {
            "initialized": False,
            "secret_count": 0,
            "last_modified_at": None,
            "installation_key": {"present": False, "usable": None},
            "materialize": [],
        }
    try:
        secrets = list_secrets(instance_dir)
    except SecretStoreError:
        secrets = ()
    present, usable = _key_presence(instance_dir)
    return {
        "initialized": is_initialized(instance_dir),
        "secret_count": len(secrets),
        "last_modified_at": _mtime(catalog_path(instance_dir)),
        "installation_key": {"present": present, "usable": usable},
        "materialize": _materialize_summary(secrets),
    }


def store_findings(instance_dir: Path) -> tuple[str, ...]:
    """Everything wrong with the store on disk, for `secretary doctor`.

    An empty `secrets/` directory gives no findings: absence is a valid state.
    Any other shape, including a partial one where only some of catalog, key
    params or key file exist, is checked against the four ways it can be
    unhealthy: a divergence between the catalog and the values directory, an
    installation key with permissions wider than 0600, and an installation key
    that is missing or does not open the store while the catalog is not empty.
    """
    instance_dir = Path(instance_dir)
    if not _store_exists(instance_dir):
        return ()
    findings: list[str] = []
    try:
        secrets = list_secrets(instance_dir)
    except SecretStoreError as exc:
        findings.append(f"secret store: {exc}")
        secrets = ()
    else:
        findings.extend(f"secret store: {item}" for item in store_divergence(instance_dir))

    path = key_path(instance_dir)
    wide_permissions = False
    try:
        info = path.lstat()
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and (info.st_mode & 0o077):
        wide_permissions = True
        findings.append(
            f"secret store: installation key permissions are too broad; run chmod 0600 {path}"
        )

    if secrets and not wide_permissions:
        try:
            load_installation_key(instance_dir)
        except SecretStoreError as exc:
            findings.append(f"secret store: installation key is missing or unusable: {exc}")
    return tuple(findings)


def _key_presence(instance_dir: Path) -> tuple[bool, bool | None]:
    """Whether a key file is there, and whether it opens this store.

    `usable` is `None` when there is no key file to judge, so a status reader
    cannot mistake "absent" for "present but broken".
    """
    try:
        path = key_path(instance_dir)
        if not stat.S_ISREG(path.lstat().st_mode):
            return True, False
    except OSError:
        return False, None
    try:
        load_installation_key(instance_dir)
    except SecretStoreError:
        return True, False
    return True, True


def _materialize_summary(secrets: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """One row per materialization target, counted, never with a secret's name."""
    counts: dict[tuple[str, str], int] = {}
    for entry in secrets:
        instruction = entry.get("materialize")
        if not instruction:
            continue
        slot = _materialize_slot(instruction)
        counts[slot] = counts.get(slot, 0) + 1
    return [
        {"target": target, "path": path or None, "count": count}
        for (target, path), count in sorted(counts.items())
    ]


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
    materialize: dict[str, Any] | None = None,
) -> SetResult:
    """Seal one value and record its metadata, as a single commit."""
    actor = _clean_actor(actor)
    secret_id = _new_secret_id(secret_id)
    scope = _clean_scope(scope)
    purpose = _clean_purpose(purpose)
    environment = _clean_environment(environment)
    materialize = _clean_materialize(materialize)
    _check_value(value)

    instance_dir = state_repo.require_repo(instance_dir)
    with state_repo.state_repo_lock(instance_dir):
        key = load_installation_key(instance_dir)
        # Re-checked on every write, not only at init: this commit is the moment
        # a key that stopped being ignored would enter the history.
        _assert_key_ignored(instance_dir)
        catalog = load_catalog(instance_dir)
        entries = {entry["id"]: dict(entry) for entry in catalog["secrets"]}
        existing = entries.get(secret_id)
        entries[secret_id] = _entry(
            secret_id,
            scope=scope,
            purpose=purpose,
            environment=environment,
            materialize=_assign_order(
                entries, secret_id=secret_id, materialize=materialize, existing=existing
            ),
            existing=existing,
        )
        catalog = _catalog(entries)

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
    if secret_id in LEGACY_BOARD_SECRET_IDS:
        raise SecretStoreValidationError(
            f"{secret_id} is board transport configuration, not a recoverable secret"
        )
    instance_dir = state_repo.require_repo(instance_dir)
    if not any(entry["id"] == secret_id for entry in list_secrets(instance_dir)):
        raise SecretStoreStateError(f"no secret named {secret_id!r} in the catalog")
    return _read_value(instance_dir, secret_id, load_installation_key(instance_dir))


def redaction_values(instance_dir: Path) -> tuple[str, ...]:
    """Return plaintext values that an instance's writers must redact.

    An old runtime import recorded every line as a secret, including ordinary
    paths and configuration.  Include an entry only when the canonical runtime
    name marks it sensitive or its plaintext independently looks like a known
    credential (including URL userinfo).  That protects custom credential
    names without turning SECRETARY_DATA_DIR or TA_SECRETARY_REPO into an
    over-broad exact-value redaction rule.

    A locked or partial store contributes no plaintext values.  It remains a
    separate doctor finding; treating it as a checkpoint gate would turn a
    recovery that deliberately left stale encrypted credentials locked into a
    permanent durability outage.  Runtime-file and pattern scanning still run.
    """
    instance_dir = Path(instance_dir).expanduser()
    values: list[str] = []
    if _store_exists(instance_dir) and is_initialized(instance_dir) and key_path(instance_dir).is_file():
        try:
            for entry in list_secrets(instance_dir):
                if entry.get("id") in LEGACY_BOARD_SECRET_IDS:
                    # Legacy encrypted transport values deliberately stay inert until
                    # an operator removes them. They must not block publication.
                    continue
                environment = str(entry.get("environment") or "")
                try:
                    value = read_secret(instance_dir, str(entry["id"])).decode("utf-8", errors="strict")
                except (SecretStoreError, UnicodeDecodeError):
                    # A valid binary secret cannot appear in text verbatim.  A
                    # missing/bad envelope remains visible through store_findings;
                    # one entry must not make us forget other readable credentials.
                    continue
                if role_env.is_sensitive_env_name(environment) or looks_like_credential(value):
                    values.append(value)
        except SecretStoreError:
            pass
    try:
        transport = resolve_board_transport(instance_dir)
    except BoardTransportError:
        pass
    else:
        if transport.token != DEFAULT_TOKEN:
            values.append(transport.token)
    return tuple(values)


def remove_secret(instance_dir: Path, *, secret_id: str, actor: str) -> RemoveResult:
    """Drop the catalog entry and its envelope in one commit.

    A missing id is an error, not a quiet success: the caller asked for a state
    that never existed, and hiding that turns a typo into a secret nobody knows
    is still stored under its real name.
    """
    actor = _clean_actor(actor)
    secret_id = _clean_secret_id(secret_id)
    instance_dir = state_repo.require_repo(instance_dir)
    with state_repo.state_repo_lock(instance_dir):
        catalog = load_catalog(instance_dir)
        entries = {entry["id"]: dict(entry) for entry in catalog["secrets"]}
        if secret_id not in entries:
            raise SecretStoreStateError(f"no secret named {secret_id!r} in the catalog")
        del entries[secret_id]
        catalog_text = _catalog_text(_catalog(entries))
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        path = value_path(instance_dir, secret_id)
        try:
            publish_state_atomic([(catalog_path(instance_dir), catalog_text)], removes=[path])
        except (OSError, RuntimeError) as exc:
            raise SecretStoreError(f"could not remove {secret_id!r}: {exc}") from None
        commit = state_repo.commit(
            instance_dir, SECRETS_PATHSPEC, _commit_message("remove", secret_id, actor)
        )
        if commit is None:
            commit = state_repo.head(instance_dir) or ""
    return RemoveResult(secret_id=secret_id, path=path, commit=commit)


def import_env_file(
    instance_dir: Path,
    *,
    source: Path,
    scope: str,
    purpose: str,
    actor: str,
    materialize: dict[str, Any] | None = None,
) -> ImportResult:
    """Take an existing env file into the store, one secret per variable.

    The file's own line order is what the catalog records, so `materialize` puts
    the same bytes back; a variable that already materializes into the same file
    but is not in this import keeps its value and moves after the imported block.

    Idempotent by content: a variable whose sealed value and metadata already
    match is left alone, envelope bytes included, so re-importing the same file
    adds no duplicates and no commit. The report says which ids moved.
    """
    actor = _clean_actor(actor)
    scope = _clean_scope(scope)
    purpose = _clean_purpose(purpose)
    materialize = _clean_materialize(materialize)
    source = Path(source).expanduser()
    try:
        # Bytes, then decode: read_text would translate CRLF into LF and hide a
        # file whose bytes this store cannot give back.
        text = source.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise SecretStoreValidationError(f"env file not found: {source}") from None
    except (OSError, UnicodeError) as exc:
        raise SecretStoreValidationError(f"could not read {source}: {exc}") from None
    variables = parse_env_file(text, source=str(source))
    if not variables:
        raise SecretStoreValidationError(f"{source} defines no variables")
    _assert_distinct_secret_ids(variables, source=str(source))

    instance_dir = state_repo.require_repo(instance_dir)
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    with state_repo.state_repo_lock(instance_dir):
        key = load_installation_key(instance_dir)
        _assert_key_ignored(instance_dir)
        before = {entry["id"]: dict(entry) for entry in load_catalog(instance_dir)["secrets"]}
        entries = {name: dict(entry) for name, entry in before.items()}
        imported = {secret_id_for_variable(name) for name in variables}
        if materialize:
            _shift_foreign_lines(entries, materialize, len(variables), keep=imported)
        writes: list[tuple[Path, str]] = []
        for line, (name, raw) in enumerate(variables.items()):
            secret_id = secret_id_for_variable(name)
            value = raw.encode("utf-8")
            if not value:
                raise SecretStoreValidationError(
                    f"{source}: {name} has an empty value; the store holds no empty secrets"
                )
            existing = entries.get(secret_id)
            if existing is not None and existing.get("environment", name) != name:
                raise SecretStoreValidationError(
                    f"{source}: {name} would take over secret {secret_id!r}, which already holds "
                    f"{existing['environment']}; remove one of them before importing"
                )
            entry = _entry(
                secret_id,
                scope=scope,
                purpose=purpose,
                environment=name,
                materialize={**materialize, "order": line} if materialize else None,
                existing=existing,
            )
            entries[secret_id] = entry
            stored = None if existing is None else _stored_value(instance_dir, secret_id, key)
            if existing is None:
                created.append(secret_id)
            elif existing == entry and stored == value:
                unchanged.append(secret_id)
                continue
            else:
                updated.append(secret_id)
            if stored == value:
                # Metadata moved, the value did not. Resealing would rewrite the
                # envelope with a fresh nonce for nothing.
                continue
            writes.append(
                (
                    value_path(instance_dir, secret_id),
                    json.dumps(seal_value(key, secret_id, value), indent=2, sort_keys=True) + "\n",
                )
            )

        if not writes and entries == before:
            return ImportResult(
                created=(),
                updated=(),
                unchanged=tuple(unchanged),
                commit=state_repo.head(instance_dir) or "",
            )
        catalog_text = _catalog_text(_catalog(entries))
        _scan_open_file(f"secrets/{CATALOG_NAME}", catalog_text)
        _publish([*writes, (catalog_path(instance_dir), catalog_text)])
        commit = state_repo.commit(
            instance_dir,
            SECRETS_PATHSPEC,
            _commit_message("import", f"{len(variables)} secrets from {source.name}", actor),
        )
        if commit is None:
            commit = state_repo.head(instance_dir) or ""
    return ImportResult(
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        commit=commit,
    )


def materialize_secrets(
    instance_dir: Path, *, target: str | None = None, paths: Container[Path] | None = None
) -> tuple[MaterializeResult, ...]:
    """Write every materializing secret into its env file.

    One file per target, written whole: the values that belong there are the
    values that end up there, and a variable dropped from the catalog is gone
    from the file. Callers that only want one file pass `target`, and a caller
    that already knows which files it may write whole passes `paths`; recovery
    uses that to leave a file alone when one of its secrets is unreadable, rather
    than publish an env file with a line missing.

    Line order is the catalog's `materialize.order`, so a file that `import`
    took in comes back byte for byte, and two runs over an unchanged store write
    the same bytes.
    """
    if target is not None and target not in MATERIALIZE_TARGETS:
        raise SecretStoreValidationError(
            f"unknown materialization target {target!r}; expected one of "
            + ", ".join(MATERIALIZE_TARGETS)
        )
    instance_dir = state_repo.require_repo(instance_dir)
    with state_repo.state_repo_lock(instance_dir):
        key = load_installation_key(instance_dir)
        groups: dict[Path, list[dict[str, Any]]] = {}
        for entry in list_secrets(instance_dir):
            if entry.get("id") in LEGACY_BOARD_SECRET_IDS:
                continue
            instruction = entry.get("materialize")
            if not instruction:
                continue
            if target is not None and instruction.get("target") != target:
                continue
            path = materialize_path(instance_dir, entry)
            if paths is not None and path not in paths:
                continue
            groups.setdefault(path, []).append(entry)

        results = []
        for path in sorted(groups):
            entries = sorted(
                groups[path],
                key=lambda item: (item["materialize"].get("order", 0), item["environment"]),
            )
            _assert_one_secret_per_variable(path, entries)
            _assert_one_secret_per_line(path, entries)
            _assert_writable_target(instance_dir, path)
            lines = []
            for entry in entries:
                value = _read_value(instance_dir, entry["id"], key)
                lines.append(f"{entry['environment']}={_env_value(entry, value)}\n")
            changed = _publish_env_file(path, "".join(lines))
            results.append(
                MaterializeResult(
                    target=entries[0]["materialize"]["target"],
                    path=path,
                    variables=tuple(entry["environment"] for entry in entries),
                    changed=changed,
                )
            )
    return tuple(results)


def materialize_path(instance_dir: Path, entry: dict[str, Any]) -> Path:
    """Resolve one catalog entry's materialization target to a path.

    `runtime-env` never carries a path of its own: the installation's env file is
    whatever `role_env` says it is, override included, so the store and a
    launched head always mean the same file.
    """
    instruction = entry.get("materialize") or {}
    target = instruction.get("target")
    if target == MATERIALIZE_RUNTIME_ENV:
        return role_env.runtime_env_path()
    if target == MATERIALIZE_FILE:
        path = Path(str(instruction.get("path", ""))).expanduser()
        if not str(path):
            raise SecretStoreStateError(
                f"secret {entry.get('id')!r} materializes to a file with no path"
            )
        return path if path.is_absolute() else instance_dir / path
    raise SecretStoreStateError(
        f"secret {entry.get('id')!r} has an unknown materialization target {target!r}"
    )


def parse_env_file(text: str, *, source: str = "env file") -> dict[str, str]:
    """Read the env-file format the store can hand back byte for byte.

    That is the whole point of being strict here. The store keeps variable names,
    values and line order, and nothing else; a comment, a blank line, a stray
    space or a CR would be dropped on the way in and could not be put back on the
    way out, so a file carrying one is refused instead of round-tripped into a
    different file. Values are taken literally, with no unquoting: whatever stood
    to the right of the first `=` is the value.

    Returns the variables in file order.
    """
    if not text:
        return {}
    if "\r" in text:
        raise SecretStoreValidationError(
            f"{source} has CR line endings; the store keeps env files in LF only"
        )
    if not text.endswith("\n"):
        raise SecretStoreValidationError(f"{source} does not end with a newline")
    values: dict[str, str] = {}
    for number, line in enumerate(text[:-1].split("\n"), 1):
        if not line.strip():
            raise SecretStoreValidationError(
                f"{source} line {number} is blank; the store keeps no blank lines"
            )
        if line.startswith("#"):
            raise SecretStoreValidationError(
                f"{source} line {number} is a comment; the store keeps no comments"
            )
        if line != line.strip():
            raise SecretStoreValidationError(
                f"{source} line {number} is padded with whitespace; write it as KEY=VALUE"
            )
        if line.startswith("export ") or "=" not in line:
            raise SecretStoreValidationError(f"{source} line {number} must use KEY=VALUE syntax")
        name, value = line.split("=", 1)
        if not _ENV_NAME_RE.match(name):
            raise SecretStoreValidationError(f"{source} line {number} has an invalid variable name")
        if name in values:
            raise SecretStoreValidationError(f"{source} defines {name} twice")
        values[name] = value
    return values


def secret_id_for_variable(name: str) -> str:
    """Map an environment-variable name to its validated store identifier."""
    return _new_secret_id(str(name).strip().lower())


# ---------------------------------------------------------------------------
# Helpers


def _entry(
    secret_id: str,
    *,
    scope: str,
    purpose: str,
    environment: str | None,
    materialize: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """One catalog entry. `created_at` belongs to the first write, not this one."""
    if materialize and not environment:
        raise SecretStoreValidationError(
            "a secret that materializes needs the environment variable it materializes into"
        )
    entry: dict[str, Any] = {
        "id": secret_id,
        "scope": scope,
        "purpose": purpose,
        "created_at": existing["created_at"] if existing else _now(),
    }
    if environment:
        entry["environment"] = environment
    if materialize:
        entry["materialize"] = materialize
    return entry


def _catalog(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    catalog = {
        "version": CATALOG_VERSION,
        "secrets": [entries[name] for name in sorted(entries)],
    }
    errors = validate(catalog, "secret-catalog", f"secrets/{CATALOG_NAME}")
    if errors:
        raise SecretStoreValidationError(f"catalog entry is invalid: {errors[0]}")
    return catalog


def _read_value(instance_dir: Path, secret_id: str, key: bytes) -> bytes:
    path = value_path(instance_dir, secret_id)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SecretStoreStateError(
            f"secret {secret_id!r} is catalogued but its value file is missing"
        ) from None
    except (OSError, ValueError) as exc:
        raise SecretStoreStateError(f"could not read the value for {secret_id!r}: {exc}") from None
    return open_value(key, envelope)


def _stored_value(instance_dir: Path, secret_id: str, key: bytes) -> bytes | None:
    """The value already in the store, or None if it is not readable as one."""
    try:
        return _read_value(instance_dir, secret_id, key)
    except SecretStoreStateError:
        return None


def _env_value(entry: dict[str, Any], value: bytes) -> str:
    """The right-hand side of one env line, or a refusal.

    An env file has no escaping this format can rely on: `installation` reads the
    rest of the line literally, so a value with a newline in it would silently
    become a different variable, or a truncated one.
    """
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        raise SecretStoreValidationError(
            f"secret {entry['id']!r} is not text and cannot go into an env file"
        ) from None
    if any(char in text for char in "\n\r\x00"):
        raise SecretStoreValidationError(
            f"secret {entry['id']!r} contains a newline and cannot go into an env file"
        )
    return text


def _assert_distinct_secret_ids(variables: dict[str, str], *, source: str) -> None:
    """Refuse a file whose variables would share one secret id.

    Ids are lower case, so `FOO` and `foo` are two variables but one id: importing
    both would leave the second one's value under the first one's name and hand
    back a file with one line where the source had two. The store cannot keep
    such a file, so it does not take it in, and it says so before writing
    anything.
    """
    seen: dict[str, str] = {}
    for name in variables:
        secret_id = secret_id_for_variable(name)
        first = seen.get(secret_id)
        if first is not None:
            raise SecretStoreValidationError(
                f"{source} defines {first} and {name}, which differ only in case and would share "
                f"the secret id {secret_id!r}; rename one of them before importing"
            )
        seen[secret_id] = name


def _assert_one_secret_per_variable(path: Path, entries: list[dict[str, Any]]) -> None:
    """Two secrets claiming one variable is a store fault, not a write order."""
    seen: dict[str, str] = {}
    for entry in entries:
        name = entry["environment"]
        if name in seen:
            raise SecretStoreStateError(
                f"{seen[name]} and {entry['id']} both materialize {name} into {path}"
            )
        seen[name] = entry["id"]


def _assert_one_secret_per_line(path: Path, entries: list[dict[str, Any]]) -> None:
    """Two secrets claiming one line means the recorded file layout is not a file."""
    seen: dict[int, str] = {}
    for entry in entries:
        order = entry["materialize"].get("order")
        if order is None:
            raise SecretStoreStateError(
                f"secret {entry['id']} materializes into {path} without a line number"
            )
        if order in seen:
            raise SecretStoreStateError(
                f"{seen[order]} and {entry['id']} both claim line {order} of {path}"
            )
        seen[order] = entry["id"]


def _assert_writable_target(instance_dir: Path, path: Path) -> None:
    """Refuse a target that git would track, or that is not a plain file."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise SecretStoreStateError(
            f"materialization target {path} is not a regular file; refusing to replace it"
        )
    try:
        relative = path.resolve().relative_to(instance_dir.resolve())
    except ValueError:
        return
    try:
        state_repo.git(
            instance_dir,
            ["check-ignore", "--quiet", "--", str(relative)],
            label="verify the materialization target is gitignored",
        )
    except state_repo.StateRepoError:
        raise SecretStoreError(
            f"materialization target {relative} is inside the instance repo but is not "
            "gitignored; refusing to write plaintext where git can pick it up"
        ) from None


def _publish_env_file(path: Path, text: str) -> bool:
    """Replace the env file in one step, or leave it exactly as it was.

    systemd reads this file on every unit start, so there is no moment it may be
    missing, empty or half-written: the content is written to a neighbour, given
    its mode there, and only then renamed over the target. A rename is the whole
    swap; a crash before it leaves the old file untouched.
    """
    desired = text.encode("utf-8")
    try:
        if path.exists() and path.read_bytes() == desired:
            return False
    except OSError as exc:
        raise SecretStoreError(f"could not read the materialization target {path}: {exc}") from None

    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise SecretStoreError(f"could not write {path}: {exc}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    return True


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


def _new_secret_id(secret_id: str) -> str:
    value = _clean_secret_id(secret_id)
    if value in LEGACY_BOARD_SECRET_IDS:
        raise SecretStoreValidationError(
            f"{value} is board transport configuration, not a recoverable secret"
        )
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
    if len(value) > 64 or not _ENV_NAME_RE.match(value):
        raise SecretStoreValidationError("environment must be an environment variable name")
    return value


def _clean_materialize(materialize: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize one materialization instruction. `order` may be filled in later."""
    if materialize is None:
        return None
    if not isinstance(materialize, dict):
        raise SecretStoreValidationError("materialize must be a mapping")
    target = str(materialize.get("target", "")).strip()
    if target not in MATERIALIZE_TARGETS:
        raise SecretStoreValidationError(
            f"materialization target must be one of {', '.join(MATERIALIZE_TARGETS)}"
        )
    cleaned: dict[str, Any] = {"target": target}
    if target == MATERIALIZE_RUNTIME_ENV:
        if materialize.get("path"):
            raise SecretStoreValidationError(
                f"the {MATERIALIZE_RUNTIME_ENV} target carries no path; "
                "it is resolved at write time"
            )
    else:
        path = str(materialize.get("path", "")).strip()
        if not path:
            raise SecretStoreValidationError(f"the {MATERIALIZE_FILE} target needs a path")
        cleaned["path"] = path
    order = materialize.get("order")
    if order is not None:
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise SecretStoreValidationError("materialization order must be a line number from 0")
        cleaned["order"] = order
    return cleaned


def _materialize_slot(materialize: dict[str, Any]) -> tuple[str, str]:
    """The file an instruction writes into, as far as the catalog can tell.

    Two instructions with the same slot land in the same file, so they compete
    for line numbers; `runtime-env` resolves to one path per host, `file` to its
    own written path.
    """
    return (str(materialize.get("target", "")), str(materialize.get("path", "")))


def _shift_foreign_lines(
    entries: dict[str, dict[str, Any]],
    materialize: dict[str, Any],
    imported_lines: int,
    *,
    keep: set[str],
) -> None:
    """Move whatever else writes into this file below the imported block.

    An import owns the top of the file it came from, line for line. Anything a
    `set` had already put there stays in the file and keeps its relative order,
    just after, so no two secrets end up claiming the same line.
    """
    slot = _materialize_slot(materialize)
    foreign = [
        name
        for name, entry in entries.items()
        if name not in keep
        and entry.get("materialize")
        and _materialize_slot(entry["materialize"]) == slot
    ]
    foreign.sort(key=lambda name: (entries[name]["materialize"].get("order", 0), name))
    for offset, name in enumerate(foreign):
        entry = dict(entries[name])
        entry["materialize"] = {**entry["materialize"], "order": imported_lines + offset}
        entries[name] = entry


def _assign_order(
    entries: dict[str, dict[str, Any]],
    *,
    secret_id: str,
    materialize: dict[str, Any] | None,
    existing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Give a materializing secret its line number in the target file.

    A caller that names no order keeps the one it already had, or takes the next
    free line at the end of the file. Nothing an existing entry holds moves, so a
    file that came in through `import` keeps the order it came in with.
    """
    if materialize is None:
        return None
    if "order" in materialize:
        return materialize
    slot = _materialize_slot(materialize)
    previous = (existing or {}).get("materialize") or {}
    if "order" in previous and _materialize_slot(previous) == slot:
        return {**materialize, "order": previous["order"]}
    used = [
        entry["materialize"]["order"]
        for name, entry in entries.items()
        if name != secret_id
        and entry.get("materialize")
        and _materialize_slot(entry["materialize"]) == slot
        and "order" in entry["materialize"]
    ]
    return {**materialize, "order": max(used) + 1 if used else 0}


def _check_value(value: bytes) -> None:
    if not isinstance(value, (bytes, bytearray)):
        raise SecretStoreValidationError("secret value must be bytes")
    if not value:
        raise SecretStoreValidationError("secret value is empty")


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


def _mtime(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: Any, what: str) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise SecretStoreStateError(f"{what} is not valid base64") from None
