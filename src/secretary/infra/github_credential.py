"""The one product-owned GitHub credential consumer."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from secretary.secret_store import SecretStoreError, SecretStoreStateError, read_secret
from secretary.state_repo import StateRepoError

CHECKPOINT_CREDENTIAL_ID = "github.checkpoint-token"
CHECKPOINT_CREDENTIAL_PURPOSE = "GitHub credential for checkpoint remote"
GITHUB_HOST = "github.com"


class CredentialError(RuntimeError):
    """A managed credential cannot safely be used."""


@dataclass(frozen=True)
class RemoteAuthSelection:
    """Non-secret Git configuration selected before a private remote child starts."""

    source: str
    environment: dict[str, str]

    def command(self, args: list[str]) -> list[str]:
        return [*helper_config_args(), *args]


@dataclass(frozen=True)
class CredentialReadiness:
    state: str
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "managed-ready"


def validate_checkpoint_credential(value: bytes) -> str:
    try:
        token = value.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialError("managed GitHub credential is not UTF-8") from None
    # Shell pipelines and editors normally finish one input line with LF (or
    # CRLF).  It is transport syntax, not token padding.  Normalize only that
    # one terminator: a second terminator remains an embedded line and is
    # refused below, as are all other leading or trailing whitespace changes.
    if token.endswith("\r\n"):
        token = token[:-2]
    elif token.endswith("\n"):
        token = token[:-1]
    if not token or token.strip() != token or any(char in token for char in "\x00\r\n"):
        raise CredentialError("managed GitHub credential content must be one non-empty unpadded line")
    return token


def checkpoint_credential_readiness(instance_dir: Path) -> CredentialReadiness:
    """Check the encrypted value without exposing it to a caller."""
    try:
        validate_checkpoint_credential(read_secret(Path(instance_dir), CHECKPOINT_CREDENTIAL_ID))
    except SecretStoreStateError as exc:
        message = str(exc)
        state = "locked/unverifiable" if "installation key" in message else "missing/unavailable"
        return CredentialReadiness(state, _safe_reason(message))
    except (SecretStoreError, StateRepoError) as exc:
        return CredentialReadiness("missing/unavailable", _safe_reason(str(exc)))
    except CredentialError as exc:
        return CredentialReadiness("missing/unavailable", str(exc))
    return CredentialReadiness("managed-ready")


def helper_command() -> str:
    import shlex

    return "!" + shlex.quote(sys.executable) + " -m secretary.infra.github_credential helper"


def helper_environment(
    instance_dir: Path | None = None, *, bootstrap_file: Path | None = None
) -> dict[str, str]:
    environment: dict[str, str] = {}
    if instance_dir is not None:
        environment["SECRETARY_CHECKPOINT_INSTANCE"] = str(Path(instance_dir).expanduser().resolve())
    if bootstrap_file is not None:
        environment["SECRETARY_GITHUB_BOOTSTRAP_FILE"] = str(Path(bootstrap_file).expanduser())
    return environment


def helper_config_args() -> list[str]:
    """Clear every ambient helper then select this helper for one Git process."""
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper_command()}"]


def select_private_remote_auth(
    phase: str,
    *,
    instance_dir: Path | None = None,
    bootstrap_file: Path | None = None,
) -> RemoteAuthSelection:
    """Select the only allowed credential source before a private Git child starts.

    The three phases are deliberately explicit: the private checkout cannot
    read its own store before clone; recovery reuse may use supplied bootstrap
    material or an unlocked managed envelope; checkpoint operations never use
    bootstrap material.  Every result clears ambient helpers through
    :meth:`RemoteAuthSelection.command`.
    """
    if phase == "initial-clone":
        if bootstrap_file is None:
            raise CredentialError("bootstrap credential is required to clone the private instance remote")
        return RemoteAuthSelection("bootstrap", helper_environment(bootstrap_file=bootstrap_file))
    if phase == "recovery-reuse":
        if bootstrap_file is not None:
            return RemoteAuthSelection("bootstrap", helper_environment(bootstrap_file=bootstrap_file))
        if instance_dir is None:
            raise CredentialError("managed credential needs the existing instance checkout")
        readiness = checkpoint_credential_readiness(instance_dir)
        if not readiness.ready:
            detail = f": {readiness.reason}" if readiness.reason else ""
            raise CredentialError(
                "recovery remote access needs a bootstrap credential or an available managed "
                f"credential ({readiness.state}){detail}"
            )
        return RemoteAuthSelection("managed-store", helper_environment(instance_dir))
    if phase == "checkpoint":
        if instance_dir is None:
            raise CredentialError("managed checkpoint credential needs the instance checkout")
        return RemoteAuthSelection("managed-store", helper_environment(instance_dir))
    raise CredentialError(f"unknown private remote authentication phase: {phase}")


def bootstrap_file_owner_is_allowed(info: os.stat_result) -> bool:
    """Allow the current identity or sudo's original caller to own a 0600 file."""
    if info.st_uid == os.geteuid():
        return True
    original = os.environ.get("SUDO_UID", "")
    return os.geteuid() == 0 and original.isdecimal() and info.st_uid == int(original)


def _safe_reason(message: str) -> str:
    return " ".join(message.replace("\r", " ").replace("\n", " ").split())[:240]


def _request() -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            break
        name, separator, value = line.partition("=")
        if separator and name in {"protocol", "host", "path", "username"}:
            fields[name] = value
    return fields


def run_helper(action: str) -> int:
    fields = _request()
    if action != "get":
        return 0
    if fields.get("protocol") != "https" or fields.get("host", "").split(":", 1)[0].lower() != GITHUB_HOST:
        return 0
    try:
        bootstrap = os.environ.get("SECRETARY_GITHUB_BOOTSTRAP_FILE", "")
        if bootstrap:
            token = _bootstrap_token(Path(bootstrap))
        else:
            raw_instance = os.environ.get("SECRETARY_CHECKPOINT_INSTANCE", "")
            if not raw_instance:
                raise CredentialError("managed GitHub credential has no instance context")
            token = validate_checkpoint_credential(read_secret(Path(raw_instance), CHECKPOINT_CREDENTIAL_ID))
    except (CredentialError, SecretStoreError, StateRepoError) as exc:
        print(f"secretary managed GitHub credential unavailable: {_safe_reason(str(exc))}", file=sys.stderr)
        return 1
    sys.stdout.write("username=x-access-token\n")
    sys.stdout.write(f"password={token}\n\n")
    return 0


def _bootstrap_token(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CredentialError("bootstrap credential file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise CredentialError("bootstrap credential file must be a regular mode-0600 file")
    if not bootstrap_file_owner_is_allowed(info):
        raise CredentialError("bootstrap credential file belongs to another user")
    try:
        return validate_checkpoint_credential(path.read_bytes())
    except OSError as exc:
        raise CredentialError("bootstrap credential file is unreadable") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secretary-github-credential")
    parser.add_argument("mode", choices=("helper",))
    parser.add_argument("action", nargs="?", default="get")
    args = parser.parse_args(argv)
    return run_helper(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
