"""Non-secret, local Kanboard JSON-RPC transport configuration.

Kanboard uses HTTP Basic authentication, so an application token still has to
match the container and its clients.  It is deliberately not a recovery secret:
this file is ordinary installation configuration and its fresh-install default
is deterministic.  A pre-transport installation can import its complete legacy
``runtime.env`` tuple once; disagreement with an existing file is an operator
action, never a silent repair.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from secretary._fsutil import write_text_atomic
from secretary import state_repo
from triggered_agents.runtime.board_transport import (
    BoardTransport, BoardTransportError, DEFAULT_TOKEN, DEFAULT_TRANSPORT,
    TRANSPORT_ENV, TRANSPORT_FILE, parse as _parse, resolve, resolve_for_environ, transport_path,
)


@dataclass(frozen=True)
class TransportOutcome:
    """Independent lifecycle actions taken for one transport reconciliation."""
    transport: BoardTransport
    source: str = "existing"
    mode_repaired: bool = False
    ignore_added: bool = False
    legacy_retired: bool = False

    @property
    def changed(self) -> bool:
        return self.source != "existing" or self.mode_repaired or self.ignore_added or self.legacy_retired

    def render(self, *, dry_run: bool = False) -> str:
        actions: list[str] = []
        if self.source == "created-default":
            actions.append("would create default transport" if dry_run else "created default transport")
        elif self.source == "imported-legacy":
            actions.append("would import legacy transport" if dry_run else "imported legacy transport")
        if self.mode_repaired:
            actions.append("would secure transport mode" if dry_run else "secured transport mode")
        if self.ignore_added:
            actions.append("would add transport ignore" if dry_run else "added transport ignore")
        if self.legacy_retired:
            actions.append("would retire legacy runtime values" if dry_run else "retired legacy runtime values")
        return "; ".join(actions) if actions else "unchanged"

def legacy_transport(values: Mapping[str, str] | None) -> BoardTransport | None:
    """Turn a complete old runtime tuple into a transport, rejecting partial state."""
    if not values:
        return None
    present = [name for name in TRANSPORT_ENV if values.get(name)]
    if not present:
        return None
    if len(present) != len(TRANSPORT_ENV):
        raise BoardTransportError("legacy Kanboard runtime configuration is incomplete: " + ", ".join(
            name for name in TRANSPORT_ENV if name not in present
        ))
    return BoardTransport(*(str(values[name]) for name in TRANSPORT_ENV))


def ensure(instance_dir: Path | str, *, legacy_values: Mapping[str, str] | None = None,
           dry_run: bool = False, allow_default: bool = False) -> TransportOutcome:
    """Create a deterministic file, or perform the one explicit legacy import."""
    path = transport_path(instance_dir)
    legacy = legacy_transport(legacy_values)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise BoardTransportError(f"board transport configuration is unreadable: {path}") from exc
    needs_mode_repair = False
    if mode is not None:
        configured = _parse(path, require_private=False)
        if legacy is not None and configured != legacy:
            raise BoardTransportError(
                "board transport mismatch: board-transport.env and legacy runtime.env disagree; "
                "reconcile the container and configuration explicitly"
            )
        needs_mode_repair = bool(mode & 0o077)
        if needs_mode_repair and legacy is None:
            raise BoardTransportError(
                "board transport configuration permissions are too broad and its contents are unconfirmed; "
                "confirm the complete legacy runtime tuple before repairing it"
            )
    elif legacy is None and not allow_default:
        raise BoardTransportError(
            "board transport is missing and legacy runtime.env has no complete Kanboard tuple; "
            "refuse to guess or rotate the live transport"
        )
    else:
        configured = legacy or DEFAULT_TRANSPORT
    try:
        ignore_changed = state_repo.ensure_ignored(
            path.parent, f"/{TRANSPORT_FILE}", dry_run=dry_run,
        ) if (path.parent / ".git").exists() else False
    except state_repo.StateRepoError as exc:
        raise BoardTransportError(f"board transport ignore lifecycle failed: {exc}") from exc
    if mode is not None:
        if needs_mode_repair and not dry_run:
            try:
                path.chmod(0o600)
            except OSError as exc:
                raise BoardTransportError(f"could not secure board transport configuration: {exc}") from None
        return TransportOutcome(
            configured, mode_repaired=needs_mode_repair, ignore_added=ignore_changed,
        )
    if not dry_run:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, "".join(f"{key}={value}\n" for key, value in configured.as_environ().items()))
            path.chmod(0o600)
        except OSError as exc:
            raise BoardTransportError(f"could not write board transport configuration: {exc}") from None
    return TransportOutcome(
        configured, "imported-legacy" if legacy is not None else "created-default",
        ignore_added=ignore_changed,
    )


def ensure_from_runtime_values(
    instance_dir: Path | str,
    *,
    legacy_values: Mapping[str, str],
    runtime_env: Path | str | None = None,
    dry_run: bool = False,
    allow_default: bool = False,
) -> TransportOutcome:
    """Reconcile caller-validated legacy values and retire their exact file entries."""
    path = Path(runtime_env) if runtime_env is not None else Path(instance_dir) / "runtime.env"
    outcome = ensure(
        instance_dir, legacy_values=legacy_values, dry_run=dry_run, allow_default=allow_default,
    )
    legacy_keys = set(TRANSPORT_ENV).intersection(legacy_values)
    if legacy_keys:
        # After a successful, equality-checked import the legacy tuple has no
        # authority. Preserve unrelated runtime lines and comments verbatim.
        raw = path.read_text(encoding="utf-8")
        retained = [line for line in raw.splitlines(keepends=True)
                    if line.split("=", 1)[0] not in legacy_keys]
        if not dry_run:
            write_text_atomic(path, "".join(retained))
        outcome = replace(outcome, legacy_retired=True)
    return outcome


def findings(instance_dir: Path | str) -> list[str]:
    """Public, non-secret transport health evidence for status and doctor."""
    path = transport_path(instance_dir)
    # A pre-transport checkout has no lifecycle marker yet; it is not an unhealthy
    # configured installation. Once the durable ignore entry exists, absence is a finding.
    if not path.exists() and not path.is_symlink() and not state_repo.is_ignored(path.parent, f"/{TRANSPORT_FILE}"):
        return []
    try:
        resolve(instance_dir)
    except BoardTransportError as exc:
        return [f"board transport configuration: {exc}"]
    return []


__all__ = [
    "BoardTransport", "BoardTransportError", "DEFAULT_TOKEN", "DEFAULT_TRANSPORT", "TransportOutcome", "ensure",
    "ensure_from_runtime_values", "findings", "resolve", "resolve_for_environ", "transport_path",
]
