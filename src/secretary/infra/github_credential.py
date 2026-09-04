"""The one product-owned GitHub credential consumer."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from secretary import _proc, state_repo
from secretary.secret_store import SecretStoreError, SecretStoreStateError, read_secret
from secretary.state_repo import GitChildIdentity, StateRepoError

CHECKPOINT_CREDENTIAL_ID = "github.checkpoint-token"
CHECKPOINT_CREDENTIAL_PURPOSE = "GitHub credential for checkpoint remote"
GITHUB_HOST = "github.com"


class CredentialError(RuntimeError):
    """A managed credential cannot safely be used."""


def _remote_transport(remote: str) -> str:
    """Classify every transport before a remote Git child is started."""
    try:
        parsed = urlsplit(remote)
    except ValueError:
        return "unsupported"
    if parsed.scheme == "https":
        return "github-https" if (parsed.hostname or "").lower() == GITHUB_HOST else "https-unsupported"
    if parsed.scheme == "ssh" or (not parsed.scheme and "@" in remote and ":" in remote):
        return "ssh"
    if parsed.scheme in {"", "file"}:
        return "local"
    return "unmanaged"


@dataclass(frozen=True)
class RemoteAuthSelection:
    """Non-secret Git configuration selected before a private remote child starts."""

    source: str
    environment: dict[str, str]


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

    # An installed product imports normally.  Source-checkout tests cross to a
    # different Unix user, whose runuser environment deliberately drops the
    # caller's PYTHONPATH, so make that checkout import root explicit too.  It
    # is product code location, never credential material.
    source_root = Path(__file__).resolve().parents[2]
    prefix = f"PYTHONPATH={shlex.quote(str(source_root))} " if (source_root / "secretary").is_dir() else ""
    return "!" + prefix + shlex.quote(sys.executable) + " -m secretary.infra.github_credential helper"


def helper_environment(
    instance_dir: Path | None = None, *, bootstrap_file: Path | None = None
) -> dict[str, str]:
    environment: dict[str, str] = {}
    if instance_dir is not None:
        environment["SECRETARY_CHECKPOINT_INSTANCE"] = str(Path(instance_dir).expanduser().resolve())
    if bootstrap_file is not None:
        environment["SECRETARY_GITHUB_BOOTSTRAP_FILE"] = str(Path(bootstrap_file).expanduser())
    return environment


def _helper_config_args() -> list[str]:
    """Clear every ambient helper then select this helper for one Git process."""
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper_command()}"]


@dataclass
class RemoteExecution:
    """The sole remote-Git trust boundary for the private instance repository.

    It classifies the transport, resolves the Git child's real identity, selects
    a permitted source for the phase, creates an operation-scoped capability,
    starts Git with ambient HTTPS helpers disabled, then removes that capability
    before returning.  Callers supply a remote operation, never helper options
    or a credential path for a child they have not resolved.
    """

    remote: str
    phase: str
    instance_dir: Path | None = None
    bootstrap_file: Path | None = None
    source: str = ""

    @property
    def transport(self) -> str:
        return _remote_transport(self.remote)

    @property
    def credential_state(self) -> CredentialReadiness:
        if self.transport == "github-https":
            if self.phase == "checkpoint":
                return checkpoint_credential_readiness(Path(self.instance_dir or "."))
            return CredentialReadiness("managed-ready")
        return CredentialReadiness("ambient/manual-bypass", f"{self.transport} remote is unmanaged")

    def run_clone(self, target: Path, *, label: str, timeout: float) -> str:
        """Clone through the same boundary before a checkout owner exists."""
        child = GitChildIdentity(os.geteuid(), os.getegid())
        with self._authorized(child) as (prefix, environment, source):
            self.source = source
            child_environment = state_repo.git_env()
            child_environment.update(environment)
            try:
                completed = _proc.run(
                    ["git", *prefix, "clone", "--", self.remote, str(target)],
                    timeout=timeout,
                    env=child_environment,
                )
            except FileNotFoundError:
                raise CredentialError(f"{label}: command not found") from None
            except OSError:
                raise CredentialError(f"{label}: command could not run") from None
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "").strip().splitlines()
                raise CredentialError(f"{label}: {detail[-1] if detail else f'exited {completed.returncode}'}")
            return (completed.stdout or "").strip()

    def run_instance(
        self,
        target: Path,
        args: list[str],
        *,
        label: str,
        timeout: float = 120,
        input: str | None = None,
    ):
        """Run one existing-checkout remote operation through the boundary."""
        try:
            child = state_repo.git_child_identity(target)
        except StateRepoError as exc:
            raise CredentialError(f"{label}: {exc}") from None
        with self._authorized(child) as (prefix, environment, source):
            self.source = source
            try:
                return state_repo.run_git(
                    target, [*prefix, *args], label=label, timeout=timeout, extra_env=environment, input=input
                )
            except StateRepoError as exc:
                raise CredentialError(str(exc)) from None

    @contextmanager
    def _authorized(self, child: GitChildIdentity) -> Iterator[tuple[list[str], dict[str, str], str]]:
        transport = self.transport
        if transport == "https-unsupported":
            raise CredentialError("private instance HTTPS remote is unsupported; only https://github.com is managed")
        if transport == "local":
            yield [], {}, "local"
            return
        if transport in {"ssh", "unmanaged"}:
            yield [], {}, "manual-bypass"
            return
        selection = select_private_remote_auth(
            self.phase, instance_dir=self.instance_dir, bootstrap_file=self.bootstrap_file
        )
        with _operation_capability(selection, child) as environment:
            yield _helper_config_args(), environment, selection.source


@contextmanager
def _operation_capability(selection: RemoteAuthSelection, child: GitChildIdentity) -> Iterator[dict[str, str]]:
    """Give one resolved Git child a bootstrap file only for this operation."""
    if selection.source != "bootstrap":
        yield selection.environment
        return
    raw_path = selection.environment.get("SECRETARY_GITHUB_BOOTSTRAP_FILE", "")
    if not raw_path:
        raise CredentialError("bootstrap credential is unavailable")
    token = _bootstrap_token(Path(raw_path))
    directory = Path(tempfile.mkdtemp(prefix="secretary-github-bootstrap-"))
    capability = directory / "credential"
    try:
        try:
            os.chmod(directory, 0o700)
            if directory.stat().st_uid != child.uid:
                os.chown(directory, child.uid, child.gid)
            descriptor = os.open(capability, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(token.encode("utf-8"))
            os.chmod(capability, 0o600)
            if capability.stat().st_uid != child.uid:
                os.chown(capability, child.uid, child.gid)
        except OSError as exc:
            raise CredentialError("could not prepare bootstrap credential capability") from exc
        environment = dict(selection.environment)
        environment["SECRETARY_GITHUB_BOOTSTRAP_FILE"] = str(capability)
        yield environment
    finally:
        try:
            capability.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass


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
    bootstrap material.  RemoteExecution owns the helper arguments and launch.
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
