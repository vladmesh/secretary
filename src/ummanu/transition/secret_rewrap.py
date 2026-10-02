"""Re-wrap the installation key and re-seal every stored secret under the new names (§2, §T3.7).

The store binds its names cryptographically: the key file's verifier is sealed with an AAD that
spells the product, and each value's subkey is derived with an HKDF info that does too, while the
formats of both files are checked by name. The renamed code reads only the new names, so every file
is rewritten here, with both columns of constants passed in explicitly from the transition's table.

The installation key itself and the phrase KDF parameters do not change: the recovery phrase keeps
opening the store. Each value is opened under the old constants, sealed under the new ones, and
opened again under the new ones before any file is replaced; the old files are copied aside first.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .context import TransitionError
from .names import Names

AEAD_ID = "chacha20poly1305"
VALUE_KDF_ID = "hkdf-sha256"
NONCE_LENGTH = 12
SALT_LENGTH = 16
KEY_LENGTH = 32
KEY_PARAMS_NAME = "installation-key.json"
KEY_NAME = "installation.key"
VALUE_SUFFIX = ".enc.json"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: Any) -> bytes:
    try:
        return base64.b64decode(str(text), validate=True)
    except (ValueError, TypeError):
        raise TransitionError("a secret store field is not valid base64") from None


def _header_bytes(header: dict[str, Any]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_key(secrets_dir: Path) -> bytes:
    try:
        key = _unb64((secrets_dir / KEY_NAME).read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise TransitionError(f"cannot read the installation key: {exc}") from None
    if len(key) != KEY_LENGTH:
        raise TransitionError("the installation key has the wrong length")
    return key


def check_verifier(key: bytes, params: dict[str, Any], names: Names) -> None:
    if params.get("format") != names.key_params_format:
        raise TransitionError(f"{KEY_PARAMS_NAME} is not a {names.key_params_format} file")
    verifier = params.get("verifier")
    if not isinstance(verifier, dict) or verifier.get("id") != AEAD_ID:
        raise TransitionError(f"{KEY_PARAMS_NAME} carries no usable verifier")
    try:
        opened = ChaCha20Poly1305(key).decrypt(
            _unb64(verifier["nonce"]), _unb64(verifier["ciphertext"]), names.verifier_aad
        )
    except (InvalidTag, KeyError):
        raise TransitionError(f"the installation key does not open the {names.key_params_format} verifier") from None
    if opened != names.verifier_plaintext:
        raise TransitionError("the verifier opened to the wrong plaintext")


def rewrap_params(key: bytes, params: dict[str, Any], old: Names, new: Names) -> dict[str, Any]:
    """The same KDF parameters under the new format, with a verifier sealed under the new AAD."""
    check_verifier(key, params, old)
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, new.verifier_plaintext, new.verifier_aad)
    rewrapped = {
        **params,
        "format": new.key_params_format,
        "verifier": {"id": AEAD_ID, "nonce": _b64(nonce), "ciphertext": _b64(sealed)},
    }
    check_verifier(key, rewrapped, new)
    return rewrapped


def _value_key(key: bytes, header: dict[str, Any]) -> bytes:
    kdf = header.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("id") != VALUE_KDF_ID:
        raise TransitionError(f"unsupported envelope kdf: {kdf!r}")
    return HKDF(
        algorithm=SHA256(),
        length=int(kdf["length"]),
        salt=_unb64(kdf["salt"]),
        info=f"{kdf['info']}:{header.get('id')}".encode(),
    ).derive(key)


def open_value(key: bytes, envelope: dict[str, Any], names: Names) -> bytes:
    """Open one envelope that must carry exactly `names`' format and KDF info."""
    if envelope.get("format") != names.envelope_format:
        raise TransitionError(f"{envelope.get('id')!r} is not a {names.envelope_format}")
    kdf = envelope.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("info") != names.value_kdf_info:
        raise TransitionError(f"{envelope.get('id')!r} is not sealed with {names.value_kdf_info}")
    aead = envelope.get("aead")
    if not isinstance(aead, dict) or aead.get("id") != AEAD_ID:
        raise TransitionError(f"{envelope.get('id')!r} has an unsupported aead")
    header = {name: value for name, value in envelope.items() if name != "ciphertext"}
    try:
        return ChaCha20Poly1305(_value_key(key, header)).decrypt(
            _unb64(aead["nonce"]), _unb64(envelope["ciphertext"]), _header_bytes(header)
        )
    except (InvalidTag, KeyError, TypeError, ValueError):
        raise TransitionError(f"could not open {envelope.get('id')!r} under {names.value_kdf_info}") from None


def seal_value(key: bytes, secret_id: str, value: bytes, names: Names, *, version: int = 1) -> dict[str, Any]:
    nonce = pysecrets.token_bytes(NONCE_LENGTH)
    header = {
        "format": names.envelope_format,
        "version": version,
        "id": secret_id,
        "kdf": {
            "id": VALUE_KDF_ID,
            "salt": _b64(pysecrets.token_bytes(SALT_LENGTH)),
            "length": KEY_LENGTH,
            "info": names.value_kdf_info,
        },
        "aead": {"id": AEAD_ID, "nonce": _b64(nonce)},
    }
    sealed = ChaCha20Poly1305(_value_key(key, header)).encrypt(nonce, value, _header_bytes(header))
    return {**header, "ciphertext": _b64(sealed)}


@dataclass(frozen=True)
class Rewrapped:
    key_file: str
    values: tuple[str, ...]
    already: bool


def _write(path: Path, payload: dict[str, Any], mode: int) -> None:
    temporary = path.with_name(f".{path.name}.transition")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TransitionError(f"cannot read {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise TransitionError(f"{path} is not an object")
    return payload


def rewrap_store(secrets_dir: Path, backup_dir: Path, old: Names, new: Names) -> Rewrapped:
    """Re-seal the store in place. Idempotent per file: a file already under the new names is
    verified and kept. Nothing is replaced until every new file opened under the new names."""
    params_path = secrets_dir / KEY_PARAMS_NAME
    if not params_path.is_file():
        return Rewrapped("", (), True)
    key = load_key(secrets_dir)
    params = _read(params_path)
    values = sorted((secrets_dir / "values").glob(f"*{VALUE_SUFFIX}"))
    staged: list[tuple[Path, dict[str, Any], int]] = []
    if params.get("format") == new.key_params_format:
        check_verifier(key, params, new)
    else:
        staged.append((params_path, rewrap_params(key, params, old, new), params_path.stat().st_mode & 0o777))
    for path in values:
        envelope = _read(path)
        if envelope.get("format") == new.envelope_format:
            open_value(key, envelope, new)
            continue
        plaintext = open_value(key, envelope, old)
        resealed = seal_value(key, str(envelope.get("id")), plaintext, new, version=int(envelope.get("version", 1)))
        if open_value(key, resealed, new) != plaintext:
            raise TransitionError(f"{path.name} did not round-trip under the new names")
        staged.append((path, resealed, path.stat().st_mode & 0o777))
    if not staged:
        return Rewrapped(str(params_path), tuple(path.name for path in values), True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    for path, _payload, _mode in staged:
        target = backup_dir / path.relative_to(secrets_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
    for path, payload, mode in staged:
        _write(path, payload, mode)
    return Rewrapped(str(params_path), tuple(path.name for path in values), False)


def verify_store(secrets_dir: Path, names: Names) -> list[str]:
    """Every value opens under `names`; the ids that did."""
    key = load_key(secrets_dir)
    check_verifier(key, _read(secrets_dir / KEY_PARAMS_NAME), names)
    opened = []
    for path in sorted((secrets_dir / "values").glob(f"*{VALUE_SUFFIX}")):
        envelope = _read(path)
        open_value(key, envelope, names)
        opened.append(str(envelope.get("id")))
    return opened


__all__ = [
    "Rewrapped",
    "check_verifier",
    "open_value",
    "rewrap_params",
    "rewrap_store",
    "seal_value",
    "verify_store",
]
