"""Shared bounded observation of the installation's supported system manager.

Packaged services run as the installation owner; their manager and bus are system-wide.
Neither the shell account nor its user bus selects a different installation contour.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ummanu import _proc


@dataclass(frozen=True)
class CommandResult:
    ran: bool
    returncode: int
    stdout: str
    stderr: str
    reason: str = ""


@dataclass(frozen=True)
class SystemdObservation:
    runtime_user: str | None = None
    timeout_seconds: float = 10

    def run(self, cmd: list[str]) -> CommandResult:
        argv = [cmd[0], "--system", *cmd[1:]] if cmd[0] == "systemctl" else cmd
        try:
            result = _proc.run(argv, timeout=self.timeout_seconds)
        except FileNotFoundError:
            return CommandResult(False, -1, "", "", f"{cmd[0]} not found")
        except subprocess.TimeoutExpired:
            return CommandResult(False, -1, "", "", f"{cmd[0]} timed out after {self.timeout_seconds}s")
        except OSError:
            return CommandResult(False, -1, "", "", f"{cmd[0]} could not run")
        return CommandResult(True, result.returncode, result.stdout or "", result.stderr or "")


def observation_error(
    result: CommandResult, *, states: set[str] | None = None, allow_empty_match: bool = False
) -> str:
    """Accept native status exits only with a recognized state, never an empty failed probe.

    Diagnostics are classified rather than echoed: stderr can contain private paths or environment.
    A reported user-bus failure is still unavailable; it does not select a user manager.
    """
    detail = ""
    if not result.ran:
        detail = result.reason
    elif result.stderr.strip():
        detail = f"systemctl exited {result.returncode} with a diagnostic"
    elif states is not None and (
        result.stdout.strip() not in states or result.returncode not in {0, 1, 3, 4}
    ):
        detail = f"systemctl runtime status unavailable (exit {result.returncode})"
    elif (
        states is None
        and result.returncode != 0
        and not (allow_empty_match and result.returncode == 1 and not result.stdout.strip())
    ):
        detail = f"systemctl exited {result.returncode}"
    if detail and result.ran:
        # Only failed observations carry diagnostics. Successful stdout includes arbitrary unit
        # names and property values; recognized nonzero status output is also ordinary data.
        diagnostic = (result.stderr + " " + result.stdout).lower()
        if any(
            phrase in diagnostic
            for phrase in (
                "failed to connect",
                "failed to get bus connection",
                "not been booted with systemd",
            )
        ):
            detail = "manager/bus connection failed"
    if detail:
        return f"system manager/bus unavailable: {detail}"
    return ""


ENABLED_STATES = {
    "enabled",
    "enabled-runtime",
    "linked",
    "linked-runtime",
    "alias",
    "masked",
    "masked-runtime",
    "static",
    "disabled",
    "indirect",
    "generated",
    "transient",
    "not-found",
}
ACTIVE_STATES = {
    "active",
    "reloading",
    "inactive",
    "failed",
    "activating",
    "deactivating",
    "maintenance",
    "unknown",
}
