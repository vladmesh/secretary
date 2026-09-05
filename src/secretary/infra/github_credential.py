"""The one product-owned GitHub credential consumer."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
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

    def __init__(self, message: str, *, code: str = "credential") -> None:
        super().__init__(message)
        self.code = code


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


def checkpoint_credential_readiness_for_child(
    instance_dir: Path, child: GitChildIdentity
) -> CredentialReadiness:
    """Read managed state only as the Git child that would consume it."""
    instance_dir = Path(instance_dir).expanduser().resolve()
    if child.uid == os.geteuid():
        return checkpoint_credential_readiness(instance_dir)
    try:
        result = state_repo.run_as_git_child(
            instance_dir,
            _helper_argv("readiness"),
            label="inspect managed GitHub credential",
            extra_env=_helper_runtime_environment(instance_dir),
            child=child,
        )
    except StateRepoError as exc:
        return CredentialReadiness("missing/unavailable", _safe_reason(str(exc)))
    try:
        payload = json.loads(result.stdout)
        state = str(payload["state"])
        reason = _safe_reason(str(payload.get("reason") or ""))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return CredentialReadiness(
            "missing/unavailable", "managed credential readiness probe returned invalid metadata"
        )
    if result.returncode or state not in {"managed-ready", "locked/unverifiable", "missing/unavailable"}:
        return CredentialReadiness(
            "missing/unavailable", reason or "managed credential readiness probe failed"
        )
    return CredentialReadiness(state, reason)


def helper_command() -> str:
    import shlex

    environment = _helper_runtime_environment()
    prefix = f"PYTHONPATH={shlex.quote(environment['PYTHONPATH'])} " if "PYTHONPATH" in environment else ""
    return "!" + prefix + " ".join(shlex.quote(argument) for argument in _helper_argv("helper"))


def _helper_argv(mode: str) -> list[str]:
    return [sys.executable, "-m", "secretary.infra.github_credential", mode]


def _helper_runtime_environment(instance_dir: Path | None = None) -> dict[str, str]:
    environment = helper_environment(instance_dir)
    source_root = Path(__file__).resolve().parents[2]
    # In a source checkout the import root is literally named `src`. Installed
    # modules live below site-packages and require no PYTHONPATH override.
    if source_root.name == "src" and (source_root / "secretary").is_dir():
        environment["PYTHONPATH"] = str(source_root)
    return environment


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
                return self.managed_credential_state
            return CredentialReadiness("managed-ready")
        return CredentialReadiness("ambient/manual-bypass", f"{self.transport} remote is unmanaged")

    @property
    def managed_credential_state(self) -> CredentialReadiness:
        """Inspect the store as its Git child, independently of remote transport."""
        if self.instance_dir is None:
            return CredentialReadiness("missing/unavailable", "managed credential has no instance context")
        try:
            child = self._child_identity(Path(self.instance_dir))
        except StateRepoError as exc:
            return CredentialReadiness("missing/unavailable", _safe_reason(str(exc)))
        return checkpoint_credential_readiness_for_child(self.instance_dir, child)

    def run_clone(
        self,
        target: Path,
        *,
        label: str,
        timeout: float,
        clone_args: list[str] | None = None,
        child_target: Path | None = None,
    ) -> str:
        """Clone through this boundary, optionally as an existing checkout's owner."""
        try:
            child = (
                self._child_identity(child_target)
                if child_target is not None
                else GitChildIdentity(os.geteuid(), os.getegid())
            )
        except StateRepoError as exc:
            raise CredentialError(f"{label}: could not select the Git child", code="identity") from exc
        with self._authorized(child) as (prefix, environment, source):
            self.source = source
            child_environment = state_repo.git_env()
            child_environment.update(environment)
            command = ["git", *prefix, "clone", *(clone_args or []), "--", self.remote, str(target)]
            try:
                if child_target is None:
                    completed = _proc.run_isolated(command, timeout=timeout, env=child_environment)
                else:
                    completed = state_repo.run_as_git_child(
                        child_target,
                        command,
                        label=label,
                        timeout=timeout,
                        extra_env=environment,
                        child=child,
                        isolated=True,
                    )
            except FileNotFoundError:
                raise CredentialError(f"{label}: command not found", code="command-not-found") from None
            except subprocess.TimeoutExpired:
                raise CredentialError(f"{label}: command timed out", code="timeout") from None
            except (OSError, StateRepoError) as exc:
                code = "timeout" if "timed out" in str(exc).lower() else "process"
                message = "command timed out" if code == "timeout" else "command could not run"
                raise CredentialError(f"{label}: {message}", code=code) from None
            if completed.returncode:
                code = _git_failure_code(completed.stderr or completed.stdout or "")
                raise CredentialError(f"{label}: {_git_failure_reason(code)}", code=code)
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
            child = self._child_identity(target)
        except StateRepoError as exc:
            raise CredentialError(f"{label}: {exc}") from None
        with self._authorized(child) as (prefix, environment, source):
            self.source = source
            try:
                return state_repo.run_git(
                    target,
                    [*prefix, *args],
                    label=label,
                    timeout=timeout,
                    extra_env=environment,
                    input=input,
                    child=child,
                )
            except StateRepoError as exc:
                raise CredentialError(str(exc)) from None

    @contextmanager
    def _authorized(self, child: GitChildIdentity) -> Iterator[tuple[list[str], dict[str, str], str]]:
        transport = self.transport
        try:
            parsed = urlsplit(self.remote)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme == "https" and (parsed.username or parsed.password):
            raise CredentialError("credential-bearing HTTPS remote is refused", code="unsafe-remote")
        if transport == "https-unsupported":
            raise CredentialError(
                "HTTPS remote is unsupported by GitHub credential management; only https://github.com is managed",
                code="unsupported-https",
            )
        if transport == "unsupported":
            raise CredentialError("remote transport is unsupported", code="unsupported-transport")
        if transport == "local":
            yield [], {}, "local"
            return
        if transport in {"ssh", "unmanaged"}:
            yield [], {}, "manual-bypass"
            return
        selection = select_private_remote_auth(
            self.phase, instance_dir=self.instance_dir, bootstrap_file=self.bootstrap_file
        )
        if selection.source == "managed-store":
            if self.instance_dir is None:
                raise CredentialError("managed credential has no instance context")
            readiness = checkpoint_credential_readiness_for_child(self.instance_dir, child)
            if not readiness.ready:
                detail = f": {readiness.reason}" if readiness.reason else ""
                if self.phase == "recovery-reuse":
                    raise CredentialError(
                        "recovery remote access needs a bootstrap credential or an available managed "
                        f"credential ({readiness.state}){detail}"
                    )
                raise CredentialError(f"managed GitHub credential {readiness.state}{detail}")
        with _operation_capability(selection, child) as environment:
            yield _helper_config_args(), environment, selection.source

    @staticmethod
    def _child_identity(target: Path) -> GitChildIdentity:
        return state_repo.git_child_identity(target)


@contextmanager
def _operation_capability(
    selection: RemoteAuthSelection, child: GitChildIdentity
) -> Iterator[dict[str, str]]:
    """Give one resolved Git child a bootstrap file only for this operation."""
    if selection.source != "bootstrap":
        yield selection.environment
        return
    raw_path = selection.environment.get("SECRETARY_GITHUB_BOOTSTRAP_FILE", "")
    if not raw_path:
        raise CredentialError("bootstrap credential is unavailable")
    token = _bootstrap_token(Path(raw_path))
    # `/tmp` is intentionally chosen over caller-controlled TMPDIR: the child
    # must be able to traverse the capability's parent after a root handoff.
    directory = Path(tempfile.mkdtemp(prefix="secretary-github-bootstrap-", dir="/tmp"))
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
    if phase in {"recovery-reuse", "project-provision"}:
        if bootstrap_file is not None:
            return RemoteAuthSelection("bootstrap", helper_environment(bootstrap_file=bootstrap_file))
        if instance_dir is None:
            raise CredentialError("managed credential needs the existing instance checkout")
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


def _git_failure_code(detail: str) -> str:
    """Classify Git output without returning any remote or credential-bearing text."""
    lowered = detail.lower()
    if any(
        marker in lowered
        for marker in ("authentication failed", "could not read username", "permission denied")
    ):
        return "authentication"
    if any(marker in lowered for marker in ("remote branch", "not found in upstream", "invalid refspec")):
        return "invalid-branch"
    if any(
        marker in lowered
        for marker in ("could not resolve host", "failed to connect", "network is unreachable")
    ):
        return "network"
    return "git"


def _git_failure_reason(code: str) -> str:
    return {
        "authentication": "authentication failed",
        "invalid-branch": "default branch is unavailable",
        "network": "network access failed",
    }.get(code, "Git operation failed")


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
    parser.add_argument("mode", choices=("helper", "readiness"))
    parser.add_argument("action", nargs="?", default="get")
    args = parser.parse_args(argv)
    if args.mode == "readiness":
        readiness = checkpoint_credential_readiness(
            Path(os.environ.get("SECRETARY_CHECKPOINT_INSTANCE", "."))
        )
        print(json.dumps({"state": readiness.state, "reason": readiness.reason}, sort_keys=True))
        return 0
    return run_helper(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
