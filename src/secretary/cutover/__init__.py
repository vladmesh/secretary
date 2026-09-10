"""Restart-safe operator boundary for the Kanboard to PostgreSQL cutover.

The controller deliberately contains orchestration, not another implementation of
backup, migration, import, parity, pause, checkpoint, or board operations.  Every
completed phase is fsync'd into the installation data plane before the next phase
is entered.  A failure is durable and leaves the global freeze in place.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import shlex
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary._fsutil import stage_text
from secretary.board.backend import BoardBackendError, parse_card_backend
from secretary.config import DataDirError, instance_data_dir
from secretary.head_registry import (
    HeadRegistryConfigError,
    product_revision,
)
from secretary.head_registry import (
    read_source as read_head_source,
)
from secretary.runtime_env import RuntimeEnvError, read_runtime_env

STATE_VERSION = 1
STATE_RELATIVE = Path("cutover") / "postgres-v1.json"
LOCK_RELATIVE = Path("cutover") / "postgres-v1.lock"
ARTIFACTS_RELATIVE = Path("cutover") / "artifacts"
HISTORY_RELATIVE = Path("cutover") / "history"
BACKEND_ENV = "SECRETARY_CARD_BACKEND"
CUTOVER_ACTOR = "secretary-postgres-cutover"
CONTROLLER_ID_ENV = "SECRETARY_CUTOVER_CONTROLLER_ID"
PREIMPORT_RECOVERY_BRANCHES = frozenset(("no-cutover-effects", "kanboard-before-fingerprint"))
SUDOERS_DROPIN = Path("/etc/sudoers.d/secretary-systemctl")
SYSTEMCTL_PATH = "/usr/bin/systemctl"
SUDO_PROBE = ("systemctl", "show", "--property=Version")

PHASES = (
    "preflight",
    "current_kanboard_backup_checkpoint",
    "postgresql_provision_migration_verification",
    "global_freeze",
    "writer_quiescence_proof",
    "final_fenced_import",
    "full_parity",
    "postgresql_recovery_backup",
    "selector_activation",
    "service_reconciliation",
    "installed_protocol_acceptance",
    "post_switch_checkpoint",
    "resume_ready",
)

ABSENT_LOAD_STATE = "not-found"


@dataclass(frozen=True)
class UnitDeclaration:
    """One consumer unit of the installation, declared once for every phase.

    ``required`` is a property of the unit and not of a phase: freeze, resume,
    recovery and unit evidence all read this one table, so a unit cannot be
    mandatory where it is stopped and optional where it is started.  A component
    the installation deliberately leaves out (``host.components`` in
    ``instance.yaml``) is declared optional here; its absence is a fact this
    controller reads from systemd, never from that configuration.

    ``freeze`` and ``resume`` are the positions the unit takes in the two orders.
    They differ on purpose: the freeze stops the dispatcher before the web tier,
    and the resume brings the web tier up before the timers that write through it.
    A unit with no ``resume`` position is only stopped, because its timer starts it.
    """

    name: str
    required: bool
    freeze: int
    resume: int | None


DECLARED_UNITS = (
    UnitDeclaration("secretary-dispatcher-production.timer", required=True, freeze=1, resume=3),
    UnitDeclaration("secretary-dispatcher-production.service", required=True, freeze=2, resume=None),
    UnitDeclaration("secretary-web-front.service", required=True, freeze=3, resume=2),
    UnitDeclaration("secretary-web.service", required=True, freeze=4, resume=1),
    UnitDeclaration("secretary-steward.timer", required=False, freeze=5, resume=4),
    UnitDeclaration("secretary-steward.service", required=False, freeze=6, resume=None),
    UnitDeclaration("secretary-steward-deep-sweep.timer", required=False, freeze=7, resume=5),
    UnitDeclaration("secretary-steward-deep-sweep.service", required=False, freeze=8, resume=None),
    UnitDeclaration("secretary-retro.timer", required=False, freeze=9, resume=6),
    UnitDeclaration("secretary-retro.service", required=False, freeze=10, resume=None),
    UnitDeclaration("secretary-curator.timer", required=True, freeze=11, resume=7),
    UnitDeclaration("secretary-curator.service", required=True, freeze=12, resume=None),
)
STOP_UNITS = tuple(
    declaration.name for declaration in sorted(DECLARED_UNITS, key=lambda item: item.freeze)
)
START_UNITS = tuple(
    declaration.name
    for declaration in sorted(
        (item for item in DECLARED_UNITS if item.resume is not None),
        key=lambda item: item.resume,
    )
)
WRITER_COMMANDS = (
    " web-run ",
    " task ",
    " sprint ",
    " product ",
    " issue ",
    " checkpoint",
    " backup create",
    " board import",
    " dispatcher production-",
)


class CutoverError(RuntimeError):
    """A bounded refusal which is safe to print to an operator."""


class CommandFailed(CutoverError):
    """A command that ran and exited non-zero; its stdout stays readable for the diagnosis."""

    def __init__(self, message: str, *, exit_code: int, output: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.output = output


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _instance_dir(path: Path) -> Path:
    expanded = path.expanduser()
    candidate = expanded if expanded.is_dir() else expanded.parent
    return candidate.resolve(strict=False)


def _safe_regular(path: Path, label: str, *, may_be_absent: bool = False) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if may_be_absent:
            return None
        raise CutoverError(f"{label} is missing: {path}") from None
    except OSError as exc:
        raise CutoverError(f"cannot inspect {label}: {exc}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CutoverError(f"{label} must be a regular file, not a symlink")
    if info.st_mode & 0o022:
        raise CutoverError(f"{label} is broadly writable")
    return info


def _safe_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CutoverError(f"cannot inspect {label}: {exc}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CutoverError(f"{label} must be a directory, not a symlink")
    if info.st_mode & 0o022:
        raise CutoverError(f"{label} is broadly writable")
    return info


def _safe_optional_directory(path: Path, label: str) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CutoverError(f"cannot inspect {label}: {exc}") from None
    _safe_directory(path, label)
    return True


@dataclass(frozen=True)
class Paths:
    instance: Path
    data: Path

    @property
    def runtime_env(self) -> Path:
        return self.instance / "runtime.env"

    @property
    def state(self) -> Path:
        return self.data / STATE_RELATIVE

    @property
    def lock(self) -> Path:
        return self.data / LOCK_RELATIVE

    @property
    def artifacts(self) -> Path:
        return self.data / ARTIFACTS_RELATIVE

    @property
    def history(self) -> Path:
        return self.data / HISTORY_RELATIVE


def resolve_paths(instance_arg: str) -> Paths:
    if not instance_arg:
        raise CutoverError("cutover requires an explicit non-default --instance")
    instance = _instance_dir(Path(instance_arg))
    _safe_directory(instance, "instance directory")
    _safe_regular(instance / "instance.yaml", "instance configuration")
    try:
        data = instance_data_dir(instance)
    except DataDirError as exc:
        raise CutoverError(str(exc)) from None
    _safe_directory(data, "installation data directory")
    _safe_regular(instance / "runtime.env", "runtime.env")
    _safe_regular(data / STATE_RELATIVE, "cutover state", may_be_absent=True)
    _safe_regular(data / LOCK_RELATIVE, "cutover lock", may_be_absent=True)
    _safe_optional_directory(data / HISTORY_RELATIVE, "cutover history directory")
    return Paths(instance, data)


def _provenance(paths: Paths, expected_revision: str) -> dict[str, Any]:
    try:
        pin = read_head_source(paths.instance)
    except HeadRegistryConfigError as exc:
        raise CutoverError(str(exc)) from None
    if not isinstance(pin, dict):
        raise CutoverError("installed head registry has no source provenance")
    installed_revision = str(pin.get("revision") or "")
    product_root = Path(str(pin.get("product_root") or "")).expanduser().resolve(strict=False)
    source_revision = product_revision(product_root)
    if not expected_revision or len(expected_revision) != 40:
        raise CutoverError("--expected-revision must be one exact 40-character product revision")
    if installed_revision != expected_revision or source_revision != expected_revision:
        raise CutoverError(
            "installed/source provenance mismatch: run upgrade, then plan again with the installed revision"
        )
    from secretary.dispatch.runtime_provenance import ProductionRuntime

    runtime = ProductionRuntime.current(product_root).probe()
    if not runtime.valid:
        raise CutoverError(runtime.refusal("postgres-cutover-preflight"))
    origin = Path(runtime.import_origin).resolve(strict=False)
    if not origin.is_relative_to(product_root):
        raise CutoverError("runtime import does not originate in the installed product checkout")
    return {
        "installed_revision": installed_revision,
        "source_revision": source_revision,
        "product_root": str(product_root),
        "runtime": runtime.as_dict(),
    }


def _backend(paths: Paths) -> str:
    try:
        values = read_runtime_env(paths.instance)
        return parse_card_backend(values.get(BACKEND_ENV))
    except (RuntimeEnvError, BoardBackendError) as exc:
        raise CutoverError(str(exc)) from None


def _source_evidence(paths: Paths) -> dict[str, Any]:
    from secretary.board.import_board import BoardImportError, run

    try:
        report = run(paths.instance, data_dir=paths.data, dry_run=True)
    except (BoardImportError, RuntimeError) as exc:
        raise CutoverError(f"Kanboard source preflight failed: {exc}") from None
    if not report.parity.get("ok"):
        raise CutoverError("Kanboard source does not produce a complete parity-clean import plan")
    consistency = report.source_consistency
    fingerprint = consistency.get("before")
    if not consistency.get("matched") or not isinstance(fingerprint, str) or not fingerprint:
        raise CutoverError("Kanboard source preflight returned no stable source fingerprint")
    return {
        "fingerprint": fingerprint,
        "consistency": consistency,
        "counts": report.counts,
        "audit": report.discrepancy_count,
        "parity": report.parity,
    }


def _privileged_argv(command: list[str]) -> list[str]:
    """Every privileged host command of this controller uses the installer's sudo contour."""
    from secretary.host_apply import SystemdUnitInstaller

    return SystemdUnitInstaller().argv(command)


def _runtime_identity() -> tuple[str, str]:
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError:
        return str(os.getuid()), str(os.getgid())
    try:
        group = grp.getgrgid(entry.pw_gid).gr_name
    except KeyError:
        group = str(entry.pw_gid)
    return entry.pw_name, group


def _sudo_systemctl_probe() -> dict[str, Any]:
    """Prove non-interactive sudo for systemctl by reading, never by changing a unit."""
    user, _group = _runtime_identity()
    argv = _privileged_argv(list(SUDO_PROBE))
    evidence: dict[str, Any] = {
        "requirement": f"sudo -n systemctl for {user}",
        "command": argv,
        "user": user,
    }
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            **evidence,
            "satisfied": False,
            "detail": f"sudo -n systemctl could not run: {type(exc).__name__}",
        }
    if result.returncode:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        reason = lines[-1][:200] if lines else f"exit {result.returncode}"
        return {**evidence, "satisfied": False, "detail": f"sudo -n systemctl failed: {reason}"}
    return {**evidence, "satisfied": True, "detail": f"sudo -n systemctl works for {user}"}


def _root_commands(compose: dict[str, Any], probe: dict[str, Any]) -> list[str]:
    from secretary.board.provision import COMPOSE_TEXT

    user, group = _runtime_identity()
    commands: list[str] = []
    if not compose["satisfied"]:
        path = Path(compose["path"])
        commands.append(f"install -d -m 0755 -o root -g root {path.parent}")
        commands.append(
            f"install -m 0600 -o {user} -g {group} /dev/stdin {path} <<'COMPOSE'\n{COMPOSE_TEXT}COMPOSE"
        )
    if not probe["satisfied"]:
        commands.append(
            f"echo '{user} ALL=(root) NOPASSWD: {SYSTEMCTL_PATH}' "
            f"| install -m 0440 -o root -g root /dev/stdin {SUDOERS_DROPIN}"
        )
        commands.append(f"visudo -cf {SUDOERS_DROPIN}")
    return commands


PRECONDITION_ITEMS = ("compose", "sudo_systemctl", "units", "pipeline_pause", "doctor")
# Every phase that calls `backup create`, with the freeze owner it declares.  The first one takes
# the freeze itself; the later ones join the freeze `global_freeze` took as the controller.
BACKUP_PAUSE_PHASES = (
    ("current_kanboard_backup_checkpoint", None),
    ("postgresql_recovery_backup", CUTOVER_ACTOR),
    ("post_switch_checkpoint", CUTOVER_ACTOR),
)
DOCTOR_OFFLINE = ("doctor", "--json", "--offline")


def _phase_complete(state: dict[str, Any] | None, name: str) -> bool:
    if state is None:
        return False
    return state.get("phases", {}).get(name, {}).get("status") == "complete"


def _pause_holder(pause: dict[str, Any]) -> str:
    return (
        f"paused ({pause.get('mode') or 'unknown mode'}) by actor {pause.get('actor') or 'unknown'}: "
        f"{pause.get('reason') or 'no reason recorded'}"
    )


def _pipeline_pause_precondition(paths: Paths, state: dict[str, Any] | None) -> dict[str, Any]:
    """Whether the pipeline pause lets every backup phase still ahead of this apply run.

    The pause is read the way `backup create` reads it and judged by its own `freeze_refusal`,
    for the next phase that will call it and with the freeze owner that phase declares.  The
    Kanboard checkpoint backup takes the freeze itself, so before it any pause refuses --
    including the controller's own freeze, which `recover` deliberately leaves in place.  The
    later backups join the cutover freeze; while `global_freeze` is still ahead a running
    pipeline passes as well, because that phase takes the freeze first and a repeated freeze
    keeps whoever already holds it.  Nothing here pauses or resumes the pipeline.
    """
    from secretary.backup import freeze_refusal, pipeline_pause

    report: dict[str, Any] = {
        "requirement": "a pipeline pause every remaining backup phase can run under",
        "source": "secretary pause-status, judged by backup create",
        "phase": None,
        "freeze_owner": None,
        "pause": None,
        "resume_command": None,
    }
    pending = [item for item in BACKUP_PAUSE_PHASES if not _phase_complete(state, item[0])]
    if not pending:
        return {
            **report,
            "satisfied": True,
            "detail": "every backup phase is complete; no remaining phase reads the pipeline pause",
        }
    phase, owner = pending[0]
    report.update(phase=phase, freeze_owner=owner)
    try:
        pause = pipeline_pause(paths.instance)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return {
            **report,
            "satisfied": False,
            "detail": f"the pipeline pause could not be read the way backup create reads it: {exc}",
        }
    report["pause"] = pause
    freeze_ahead = not _phase_complete(state, "global_freeze")
    if owner and freeze_ahead and not pause.get("paused"):
        return {
            **report,
            "satisfied": True,
            "detail": f"the pipeline is running; global_freeze takes the cutover freeze before {phase}",
        }
    refusal = freeze_refusal(pause, owner)
    if refusal is None:
        holder = f"is {_pause_holder(pause)}" if pause.get("paused") else "is not paused"
        return {**report, "satisfied": True, "detail": f"the pipeline {holder}; {phase} can run under it"}
    if pause.get("paused") and (owner is None or freeze_ahead):
        resume = f"secretary resume --instance {shlex.quote(str(paths.instance))}"
        report["resume_command"] = resume
        detail = (
            f"the pipeline is {_pause_holder(pause)}, and {phase} would refuse it: {refusal}. "
            "The controller never lifts a pause, and recover leaves its freeze in place on purpose; "
            f"lift it explicitly with `{resume}`, then rerun the identical apply command"
        )
    else:
        holder = _pause_holder(pause) if pause.get("paused") else "not paused"
        detail = (
            f"{phase} runs under the freeze global_freeze took as {CUTOVER_ACTOR} and would refuse: "
            f"{refusal} (the pipeline is {holder}). Neither lifting nor taking a pause by hand "
            "repairs that; inspect `secretary cutover status` and recover with its RECOVER token"
        )
    return {**report, "satisfied": False, "detail": detail}


def _doctor_finding(finding: Any) -> str:
    """One doctor finding in doctor's own words: its code, message and remaining fields."""
    if not isinstance(finding, dict):
        return str(finding)
    parts = [str(finding["message"])] if finding.get("message") else []
    parts += [f"{key}={finding[key]}" for key in sorted(finding) if key not in ("code", "message")]
    code = str(finding.get("code") or "finding")
    return f"{code}: {'; '.join(parts)}" if parts else code


def _doctor_findings(output: str) -> list[str]:
    try:
        document = json.loads(output)
    except (TypeError, ValueError):
        return []
    findings = document.get("findings") if isinstance(document, dict) else None
    return [_doctor_finding(item) for item in findings] if isinstance(findings, list) else []


def _doctor_offline(paths: Paths) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the doctor `installed_protocol_acceptance` runs, and judge it the way that phase does.

    Clean means `_secretary` accepts the run: exit zero and a JSON document that is not an error
    document.  The apply precondition and the acceptance phase both ask this one function, so
    a doctor that passes the precondition cannot fail acceptance on the same findings.  The
    findings of a refusal are read from doctor's own JSON output, as doctor named them; the
    controller repairs none of them.  Returns the report and, when clean, the command evidence.
    """
    command = shlex.join(_secretary_argv(paths, *DOCTOR_OFFLINE))
    report: dict[str, Any] = {
        "requirement": "a clean secretary doctor --json --offline",
        "command": command,
        "findings": [],
    }
    try:
        evidence = _secretary(paths, *DOCTOR_OFFLINE)
    except CutoverError as exc:
        findings = _doctor_findings(exc.output) if isinstance(exc, CommandFailed) else []
        if findings:
            detail = (
                f"secretary doctor --offline reports {len(findings)} finding(s), and "
                "installed_protocol_acceptance fails on them:\n"
                + "\n".join(f"  - {finding}" for finding in findings)
                + "\n  The controller repairs none of them; they are operator steps on this "
                f"installation. Reproduce with: {command}"
            )
        else:
            detail = f"secretary doctor --offline is not clean: {exc}. Reproduce with: {command}"
        return {**report, "satisfied": False, "findings": findings, "detail": detail}, None
    return {**report, "satisfied": True, "detail": "secretary doctor --offline reports no findings"}, evidence


def _doctor_precondition(paths: Paths, state: dict[str, Any] | None) -> dict[str, Any]:
    if _phase_complete(state, "installed_protocol_acceptance"):
        return {
            "requirement": "a clean secretary doctor --json --offline",
            "command": shlex.join(_secretary_argv(paths, *DOCTOR_OFFLINE)),
            "findings": [],
            "satisfied": True,
            "detail": "installed_protocol_acceptance is complete; no remaining phase runs doctor",
        }
    report, _evidence = _doctor_offline(paths)
    return report


def _privileged_preconditions(compose_path: Path | None = None, *, paths: Paths) -> dict[str, Any]:
    """Read-only report on what apply needs the installation to already have.

    The controller owns none of it: it proves the Compose definition that
    provisioning needs is already installed as shipped, that its own systemd
    calls will be authorised, and that every unit it must not run without is
    installed, then prints the root commands for whatever is missing.  It never
    writes the file, the sudo rule or a unit, and reading a LoadState changes
    nothing.

    Two facts of the installation sit beside them because a phase that has not
    run yet would otherwise find them in the middle of the window: a pipeline
    pause a remaining backup would refuse, and a doctor --offline that the
    acceptance phase would fail on.  Both are read with the commands those
    phases use, judged against the cutover state as it stands, and neither is
    repaired: no resume, no secret-catalog edit.
    """
    from secretary.board.provision import DEFAULT_COMPOSE_PATH, inspect_compose

    path = compose_path if compose_path is not None else DEFAULT_COMPOSE_PATH
    installed = inspect_compose(path)
    compose = {
        "requirement": "root-installed board-store Compose definition",
        "path": str(path),
        "status": installed.status,
        "satisfied": installed.ok,
        "detail": installed.describe(),
    }
    probe = _sudo_systemctl_probe()
    inventory = inventory_units()
    units = {
        "requirement": "installed required consumer units",
        "satisfied": not inventory.missing_required,
        "detail": inventory.describe(),
        **inventory.evidence(),
    }
    state = _read_state(paths)
    report: dict[str, Any] = {
        "compose": compose,
        "sudo_systemctl": probe,
        "units": units,
        "pipeline_pause": _pipeline_pause_precondition(paths, state),
        "doctor": _doctor_precondition(paths, state),
    }
    report["satisfied"] = all(report[item]["satisfied"] for item in PRECONDITION_ITEMS)
    report["root_commands"] = _root_commands(compose, probe)
    return report


def _require_privileged_preconditions(
    compose_path: Path | None = None, *, paths: Paths
) -> dict[str, Any]:
    report = _privileged_preconditions(compose_path, paths=paths)
    if report["satisfied"]:
        return report
    missing = [report[item]["detail"] for item in PRECONDITION_ITEMS if not report[item]["satisfied"]]
    message = (
        "privileged apply preconditions are not met; the controller verifies them and "
        "performs no root step, resume or repair:\n" + "\n".join(f"- {detail}" for detail in missing)
    )
    if report["root_commands"]:
        message += "\nRun as root, then rerun the identical apply command:\n" + "\n".join(
            report["root_commands"]
        )
    raise CutoverError(message)


def build_plan(
    paths: Paths,
    expected_revision: str,
    *,
    _active_identity: str | None = None,
) -> dict[str, Any]:
    canonical = _read_state(paths)
    if canonical is not None and canonical.get("identity") != _active_identity:
        eligibility = _successor_eligibility(canonical)
        raise CutoverError(
            "canonical cutover identity is still present; inspect status or recover it before planning "
            f"a successor ({eligibility['reason']})"
        )
    backend = _backend(paths)
    if backend != "kanboard":
        raise CutoverError(f"new cutover plan requires backend kanboard, found {backend}")
    recovered_history = _read_recovered_history(paths)
    pending_release = [
        item
        for item in recovered_history
        if item.get("successor_release", {}).get("status") == "pending"
    ]
    if pending_release:
        raise CutoverError(
            "successor canonical release is awaiting its immutable release receipt; "
            "rerun the exact prepare-successor command shown by status"
        )
    evidence = {
        "version": STATE_VERSION,
        "instance": str(paths.instance),
        "data_dir": str(paths.data),
        "expected_revision": expected_revision,
        "backend": backend,
        "provenance": _provenance(paths, expected_revision),
        "source": _source_evidence(paths),
        "phases": list(PHASES),
        "recovered_predecessors": [
            {
                "identity": item["state"]["identity"],
                "plan_id": item["state"]["plan_id"],
                "archive_sha256": item["archive_sha256"],
                **(
                    {
                        "successor_preparation": {
                            "archived_database": item["state"]["successor_preparation"]["database"],
                            "dump": item["state"]["successor_preparation"]["dump"],
                        }
                    }
                    if isinstance(item["state"].get("successor_preparation"), dict)
                    else {}
                ),
            }
            for item in recovered_history
        ],
    }
    plan_id = _sha(evidence)
    return {**evidence, "plan_id": plan_id, "confirmation": f"CUTOVER-{plan_id[:16]}"}


def _read_state(paths: Paths) -> dict[str, Any] | None:
    _safe_regular(paths.state, "cutover state", may_be_absent=True)
    try:
        payload = json.loads(paths.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise CutoverError(f"cutover state is unreadable: {type(exc).__name__}") from None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise CutoverError("cutover state has an unsupported version or shape")
    return payload


def _write_state(paths: Paths, payload: dict[str, Any]) -> None:
    paths.state.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    # The state contains evidence, never credentials.  Board writers may run as
    # a different uid from the privileged operator, so they must be able to
    # traverse and read this fail-closed fence.  Only its owner may mutate it.
    paths.state.parent.chmod(0o755)
    _safe_directory(paths.state.parent, "cutover state directory")
    staged = stage_text(paths.state, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        staged.chmod(0o644)
        os.replace(staged, paths.state)
        descriptor = os.open(paths.state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CutoverError(f"could not publish cutover state: {exc}") from None
    finally:
        staged.unlink(missing_ok=True)


def _read_recovered_history(paths: Paths) -> list[dict[str, Any]]:
    """Read immutable pre-write recovery evidence that precedes a fresh plan."""
    if not _safe_optional_directory(paths.history, "cutover history directory"):
        return []
    history: list[dict[str, Any]] = []
    for path in sorted(paths.history.glob("postgres-v1-*.json")):
        _safe_regular(path, "recovered cutover state")
        try:
            raw = path.read_bytes()
            state = json.loads(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise CutoverError(f"recovered cutover state is unreadable: {type(exc).__name__}") from None
        if (
            not isinstance(state, dict)
            or state.get("version") != STATE_VERSION
            or state.get("status") != "recovered-frozen"
            or not isinstance(state.get("identity"), str)
            or not isinstance(state.get("plan_id"), str)
        ):
            raise CutoverError("recovered cutover state has an unsupported version or shape")
        if path != _recovered_archive_path(paths, state):
            raise CutoverError("recovered cutover archive name does not match its plan identity")
        history.append(
            {
                "path": str(path),
                "archive_sha256": hashlib.sha256(raw).hexdigest(),
                "state": state,
            }
        )
    from secretary.cutover.successor import resolve_history

    return resolve_history(paths, history)


def _recovered_archive_path(paths: Paths, state: dict[str, Any]) -> Path:
    plan_id = state.get("plan_id")
    if (
        not isinstance(plan_id, str)
        or len(plan_id) != 64
        or any(character not in "0123456789abcdef" for character in plan_id)
    ):
        raise CutoverError("cutover state has an invalid plan identity")
    return paths.history / f"postgres-v1-{plan_id}.json"


def _successor_eligibility(state: dict[str, Any] | None) -> dict[str, Any]:
    """Whether recovery may archive this identity and release the canonical slot."""
    if state is None:
        return {
            "eligible": True,
            "reason": "canonical-slot-available",
            "final_import_complete": False,
        }
    phases = state.get("phases") if isinstance(state.get("phases"), dict) else {}
    import_phase = phases.get("final_fenced_import")
    import_entered = "final_fenced_import" in phases
    import_status = import_phase.get("status") if isinstance(import_phase, dict) else None
    if import_entered and not isinstance(import_phase, dict):
        import_status = "invalid"
    imported = import_status == "complete"
    status = state.get("status")
    branch = state.get("recovery", {}).get("branch")
    evidence = {
        "eligible": False,
        "reason": "recovery-not-preimport-terminal",
        "final_import_complete": imported,
        "final_import_status": import_status,
        "canonical_status": status,
        "recovery_branch": branch,
    }
    if status == "resume-ready":
        return {**evidence, "reason": "completed-cutover-terminal"}
    # Import effect takes precedence over the first-application-write marker:
    # the occupied target makes another import unsafe even if no later event exists.
    if imported:
        return {**evidence, "reason": "final-import-effect-terminal"}
    if import_entered:
        return {**evidence, "reason": "final-import-occupancy-uncertain-terminal"}
    if state.get("first_sql_write") is not None:
        return {**evidence, "reason": "sql-write-or-audit-uncertainty-terminal"}
    if branch == "postgres-only":
        return {**evidence, "reason": "postgres-only-recovery-terminal"}
    if status == "recovered-frozen" and branch in PREIMPORT_RECOVERY_BRANCHES:
        return {**evidence, "eligible": True, "reason": "recovered-before-final-import"}
    return evidence


def _publish_recovered_archive(paths: Paths, state: dict[str, Any]) -> Path:
    """Publish one immutable recovered identity, idempotently across a crash."""
    archive = _recovered_archive_path(paths, state)
    if not _safe_optional_directory(paths.history, "cutover history directory"):
        try:
            paths.history.mkdir(mode=0o755, parents=True)
        except OSError as exc:
            raise CutoverError(f"could not create cutover history: {exc}") from None
    _safe_directory(paths.history, "cutover history directory")
    expected = json.dumps(state, indent=2, sort_keys=True) + "\n"
    try:
        archive.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CutoverError(f"could not inspect recovered cutover archive: {exc}") from None
    else:
        _safe_regular(archive, "recovered cutover state")
        try:
            existing = archive.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CutoverError(f"could not inspect recovered cutover archive: {exc}") from None
        if existing != expected:
            raise CutoverError("recovered cutover archive conflicts with canonical evidence")
        descriptor = os.open(paths.history, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return archive
    staged = stage_text(archive, expected)
    try:
        staged.chmod(0o444)
        # The installation lock serializes controllers, while link's EEXIST
        # guarantee also prevents an external race from overwriting evidence.
        os.link(staged, archive)
        descriptor = os.open(paths.history, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CutoverError(f"could not publish recovered cutover archive: {exc}") from None
    finally:
        staged.unlink(missing_ok=True)
    return archive


def _release_recovered_identity(paths: Paths, state: dict[str, Any]) -> dict[str, Any]:
    """Archive a safe pre-write recovery, then release the canonical plan slot."""
    eligibility = _successor_eligibility(state)
    state["successor_eligibility"] = eligibility
    if not eligibility["eligible"]:
        raise CutoverError(
            f"cutover identity is not eligible for a successor: {eligibility['reason']}"
        )
    successor = state.setdefault(
        "successor",
        {
            "archive": str(_recovered_archive_path(paths, state)),
            "prepared_at": _now(),
            "canonical_slot": "released-after-archive",
        },
    )
    if successor.get("archive") != str(_recovered_archive_path(paths, state)):
        raise CutoverError("recovered cutover successor evidence is inconsistent")
    _write_state(paths, state)
    archive = _publish_recovered_archive(paths, state)
    current = _read_state(paths)
    if current != state:
        raise CutoverError("canonical cutover state changed before successor publication")
    try:
        paths.state.unlink()
        descriptor = os.open(paths.state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise CutoverError(f"could not release recovered canonical cutover state: {exc}") from None
    return {**state, "archived_state": str(archive), "successor_ready": True}


def _finish_recovery(paths: Paths, state: dict[str, Any]) -> dict[str, Any]:
    """Publish terminal recovery and release only a genuinely pre-import attempt."""
    eligibility = _successor_eligibility(state)
    state["successor_eligibility"] = eligibility
    if eligibility["eligible"]:
        return _release_recovered_identity(paths, state)
    _write_state(paths, state)
    return state


class CutoverLock:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.handle: Any = None

    def __enter__(self) -> None:
        self.paths.lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_directory(self.paths.lock.parent, "cutover state directory")
        self.handle = self.paths.lock.open("a+", encoding="utf-8")
        os.chmod(self.paths.lock, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise CutoverError("another cutover controller holds the installation-wide lock") from None

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _run(argv: list[str], *, env: dict[str, str] | None = None, capture_full: bool = False) -> dict[str, Any]:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=600, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CutoverError(f"could not run {Path(argv[0]).name}: {type(exc).__name__}") from None
    if result.returncode:
        detail = (result.stderr or result.stdout or "failed").strip().splitlines()[-1]
        raise CommandFailed(
            f"{Path(argv[0]).name} failed: {detail[:500]}",
            exit_code=result.returncode,
            output=result.stdout or "",
        )
    output = (result.stdout or "").strip()
    evidence = {
        "command": [Path(argv[0]).name, *argv[1:]],
        "exit_code": 0,
        "output": output[:4000],
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    if capture_full:
        evidence["_captured_output"] = output
    return evidence


def _secretary_argv(paths: Paths, *args: str) -> list[str]:
    return [sys.executable, "-P", "-m", "secretary", *args, "--instance", str(paths.instance)]


def _secretary(paths: Paths, *args: str) -> dict[str, Any]:
    evidence = _run(_secretary_argv(paths, *args), capture_full=True)
    output = evidence.pop("_captured_output")
    if not output:
        raise CutoverError("secretary command returned empty success evidence")
    try:
        document = json.loads(output)
    except (TypeError, ValueError):
        raise CutoverError("secretary command returned non-JSON success evidence") from None
    if not isinstance(document, (dict, list)):
        raise CutoverError("secretary command returned a non-document JSON value")
    if isinstance(document, dict) and "error" in document:
        raise CutoverError("secretary command returned an error document with exit zero")
    return {**evidence, "document": document}


def _sql_event_count(paths: Paths) -> dict[str, Any]:
    import sqlalchemy as sa

    from secretary.board import migrate, store

    config = store.resolve(paths.instance)
    engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("read")))
    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT count(*), max(committed_at) FROM board_events WHERE committed = true"
            ).one()
        return {"committed_events": int(row[0]), "latest_committed_at": str(row[1] or "")}
    except sa.exc.SQLAlchemyError as exc:
        raise CutoverError(f"could not inspect committed SQL audit evidence: {exc}") from None
    finally:
        engine.dispose()


def _command_document(evidence: dict[str, Any], label: str) -> dict[str, Any] | list[Any]:
    document = evidence.get("document")
    if not isinstance(document, (dict, list)):
        raise CutoverError(f"{label} returned no machine-readable document")
    return document


def _artifact(paths: Paths, name: str, body: str) -> Path:
    paths.artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = paths.artifacts / name
    target.write_text(body, encoding="utf-8")
    target.chmod(0o600)
    return target


def _acceptance_context(sprints: dict[str, Any] | list[Any]) -> tuple[str | None, str | None]:
    """The open sprint the acceptance canary can borrow, or nothing when none is open.

    An installation between sprints is the ordinary shape of a maintenance window, not a
    refusal: without an open sprint the acceptance phase opens and closes its own canary
    sprint on a registered project (see `_acceptance_project`).
    """
    if not isinstance(sprints, dict):
        raise CutoverError("sprint list returned an invalid acceptance document")
    rows = sprints.get("sprints", {}).get("items", [])
    if not isinstance(rows, list):
        raise CutoverError("sprint list returned an invalid items collection")
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "open":
            continue
        projects = row.get("reservations") or []
        if isinstance(projects, list) and projects and row.get("ref"):
            return str(row["ref"]), str(projects[0])
    return None, None


def _acceptance_project(paths: Paths) -> str:
    """One registered project the canary sprint may reserve while no sprint is open."""
    from secretary.product_issues import registered_projects
    from secretary.tasks import TaskError

    try:
        registered = sorted(registered_projects(paths.instance))
    except TaskError as exc:
        raise CutoverError(f"installed acceptance could not read the project registry: {exc}") from None
    if not registered:
        raise CutoverError("installed acceptance needs at least one registered project for its canary sprint")
    # The product's own project reads best in the canary's evidence; any registered one will do.
    return "secretary" if "secretary" in registered else registered[0]


def _serve_backend(backend: str | None) -> None:
    """The one place the controller changes which card backend *this process* serves.

    `secretary/board/backend.py` decides once per process on purpose, and that invariant stands:
    an ordinary command that read cards from one backend must not write them to the other because
    something re-exported the variable between its two halves.  The controller is the single
    caller that legitimately crosses the boundary — it is the thing performing the switch — and
    `reset_card_backend()` is the sanctioned way to say so.  Exporting the name without it is the
    09.09 defect: `postgresql_recovery_backup` set `postgres`, `create_backups` asked the switch
    and got the `kanboard` the earlier phases had already decided, and the phase took a Kanboard
    backup where the recovery point had to be the SQL one.  `None` restores "no name exported",
    which `parse_card_backend` reads as the default.
    """
    from secretary.board.backend import reset_card_backend

    if backend is None:
        os.environ.pop(BACKEND_ENV, None)
    else:
        os.environ[BACKEND_ENV] = backend
    reset_card_backend()


@contextmanager
def _serving_backend(backend: str) -> Iterator[None]:
    """Serve `backend` for the body, then put back exactly what was there: name and decision.

    Symmetric by construction, which is the half the inline pairs kept losing.  A phase that
    borrows PostgreSQL before `selector_activation` has activated it restores the environment on
    the way out and must restore the process decision with it, or the next phase inherits a
    `postgres` reader under a `kanboard` selector.
    """
    before = os.environ.get(BACKEND_ENV)
    _serve_backend(backend)
    try:
        yield
    finally:
        _serve_backend(before)


def _set_backend(paths: Paths, backend: str) -> dict[str, Any]:
    parse_card_backend(backend)
    path = paths.runtime_env
    info = _safe_regular(path, "runtime.env")
    assert info is not None
    body = path.read_text(encoding="utf-8")
    lines = body.splitlines(keepends=True)
    found = 0
    output: list[str] = []
    for line in lines:
        raw = line.rstrip("\r\n")
        if raw.startswith(f"{BACKEND_ENV}="):
            found += 1
            if found > 1:
                raise CutoverError(f"runtime.env contains duplicate {BACKEND_ENV} entries")
            ending = line[len(raw) :]
            output.append(f"{BACKEND_ENV}={backend}{ending or os.linesep}")
        else:
            output.append(line)
    if not found:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += os.linesep
        output.append(f"{BACKEND_ENV}={backend}{os.linesep}")
    staged = stage_text(path, "".join(output))
    try:
        os.chmod(staged, stat.S_IMODE(info.st_mode))
        os.chown(staged, info.st_uid, info.st_gid)
        os.replace(staged, path)
    except OSError as exc:
        raise CutoverError(f"could not atomically activate {backend}: {exc}") from None
    finally:
        staged.unlink(missing_ok=True)
    return {"backend": backend, "path": str(path), "mode": oct(stat.S_IMODE(info.st_mode))}


def _systemctl(action: str, units: tuple[str, ...]) -> dict[str, Any]:
    evidence = []
    for unit in units:
        evidence.append(_run(_privileged_argv(["systemctl", action, unit])))
    return {"action": action, "units": list(units), "privileged": "sudo -n", "results": evidence}


def _unit_load_state(unit: str) -> str:
    """Ask systemd what it has installed for one unit: read-only and without sudo.

    A read that fails is not an absence.  The unit keeps its place in the freeze
    and resume composition and the failure becomes its recorded state, so an
    unreadable systemd fails loudly on the command that needs the unit instead
    of quietly shrinking the window.
    """
    try:
        status = _run(["systemctl", "show", unit, "--property=LoadState"])
    except CutoverError as exc:
        return f"unreadable: {exc}"[:200]
    for line in status["output"].splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "LoadState":
            return value.strip() or "unknown"
    return "unknown"


@dataclass(frozen=True)
class UnitInventory:
    """The LoadState of every declared unit, as this installation actually has it.

    The installed units are the truth about what may be stopped, started and
    proven; an instance configuration that disabled a component is not consulted,
    because the two can disagree.  Nothing here mutates a unit.
    """

    load_states: dict[str, str]

    def state(self, unit: str) -> str:
        return self.load_states.get(unit, "unknown")

    @property
    def absent(self) -> tuple[str, ...]:
        return tuple(
            declaration.name
            for declaration in DECLARED_UNITS
            if self.state(declaration.name) == ABSENT_LOAD_STATE
        )

    @property
    def present(self) -> tuple[str, ...]:
        return tuple(
            declaration.name
            for declaration in DECLARED_UNITS
            if self.state(declaration.name) != ABSENT_LOAD_STATE
        )

    @property
    def missing_required(self) -> tuple[str, ...]:
        absent = set(self.absent)
        return tuple(
            declaration.name
            for declaration in DECLARED_UNITS
            if declaration.required and declaration.name in absent
        )

    def installed(self, units: tuple[str, ...]) -> tuple[str, ...]:
        """A declared order, minus every unit this installation does not have."""
        return tuple(unit for unit in units if self.state(unit) != ABSENT_LOAD_STATE)

    def stop_units(self) -> tuple[str, ...]:
        return self.installed(STOP_UNITS)

    def start_units(self) -> tuple[str, ...]:
        return self.installed(START_UNITS)

    def describe(self) -> str:
        if self.missing_required:
            return "required units are not installed: " + ", ".join(self.missing_required)
        if self.absent:
            return "optional units absent from this installation: " + ", ".join(self.absent)
        return "every declared unit is installed"

    def evidence(self) -> dict[str, Any]:
        """Why a unit did not take part in a phase, recorded beside what did."""
        required = {declaration.name: declaration.required for declaration in DECLARED_UNITS}
        return {
            "source": "systemctl show --property=LoadState",
            "load_states": dict(self.load_states),
            "stop": list(self.stop_units()),
            "start": list(self.start_units()),
            "excluded": [
                {"unit": name, "load_state": self.state(name), "required": required[name]}
                for name in self.absent
            ],
        }


def inventory_units() -> UnitInventory:
    """Read the LoadState of every declared unit; the one way phases learn what exists."""
    return UnitInventory(
        {declaration.name: _unit_load_state(declaration.name) for declaration in DECLARED_UNITS}
    )


def _restart_consumers() -> dict[str, Any]:
    """Every recovery branch reconciles the installed consumers, not a fixed list.

    Recovery is the controller's second entrance: its own command, run after an apply that
    did not finish, with the apply seam and its `_require_privileged_preconditions` gate
    behind it rather than in front.  A unit that was installed when those preconditions
    passed can be gone by the time this runs, and inventorying excludes every `not-found`
    unit — including a required one.  A silently excluded required unit is a recovery that
    reports success without bringing the web tier back, which is what the `systemctl
    restart` of the fixed list used to fail loudly on.  So the exclusion of a required unit
    refuses here instead, naming the unit and the LoadState that excluded it.

    The refusal is raised before the first restart, so it changes no unit; the caller
    writes no state document for a recovery that did not reconcile, and every durable
    effect the branch already made — the selector in runtime.env, the provisioned and
    migrated store — stands and is idempotent under the identical rerun.
    """
    inventory = inventory_units()
    if inventory.missing_required:
        raise CutoverError(
            "recovery cannot reconcile an installation that is missing a required unit; "
            "no unit was restarted and the durable cutover state is unchanged:\n"
            + "\n".join(
                f"- {name}: LoadState={inventory.state(name)}"
                for name in inventory.missing_required
            )
            + "\nInstall the unit as root, then rerun the identical recover command."
        )
    return {
        "services": _systemctl("restart", inventory.start_units()),
        "inventory": inventory.evidence(),
    }


def _service_evidence(units: tuple[str, ...], product_root: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for unit in units:
        status = _run(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState,ActiveState,SubState,ExecMainPID,FragmentPath",
            ]
        )
        if "LoadState=loaded" not in status["output"] or "ActiveState=active" not in status["output"]:
            raise CutoverError(f"reconciled unit is not loaded and active: {unit}")
        definition_unit = f"{unit.removesuffix('.timer')}.service" if unit.endswith(".timer") else unit
        definition = _run(["systemctl", "cat", definition_unit])
        if unit != "secretary-web-front.service" and product_root not in definition["output"]:
            raise CutoverError(f"unit {unit} does not execute the installed product root")
        evidence.append({"unit": unit, "status": status, "definition": definition})
    return evidence


def _reconcile_postgres(paths: Paths) -> dict[str, Any]:
    from secretary.board import migrate
    from secretary.board.provision import provision, verify_roles

    store = provision(paths.instance)
    migrations = migrate.migrate_instance(paths.instance)
    verify_roles(paths.instance)
    return {
        "actions": list(store.actions if store else ()),
        "migrations": list(migrations),
        "migration_head": migrate.head_revision(),
        "roles": "verified",
    }


def _writer_processes() -> list[dict[str, Any]]:
    own = {os.getpid(), os.getppid()}
    found: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in own:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8")
        except (OSError, UnicodeError):
            continue
        padded = f" {command.strip()} "
        if "secretary" in padded and any(token in padded for token in WRITER_COMMANDS):
            found.append({"pid": int(entry.name), "command": command[:240]})
    return found


class Operations:
    """Production phase implementations, kept injectable for crash/failure rehearsal."""

    def __init__(
        self,
        paths: Paths,
        state: dict[str, Any],
        *,
        provision_options: dict[str, Any] | None = None,
        checkpoint_options: dict[str, Any] | None = None,
    ) -> None:
        self.paths = paths
        self.state = state
        # Tests use an isolated Compose project and file; production deliberately
        # takes the provisioner's supported defaults.
        self.provision_options = dict(provision_options or {})
        self.checkpoint_options = dict(checkpoint_options or {})

    def preflight(self) -> dict[str, Any]:
        current = build_plan(
            self.paths,
            self.state["expected_revision"],
            _active_identity=self.state["identity"],
        )
        if current["plan_id"] != self.state["plan_id"]:
            raise CutoverError("plan is stale; inspect status and create a new plan before freezing")
        return {"plan_id": current["plan_id"], "provenance": current["provenance"]}

    def current_kanboard_backup_checkpoint(self) -> dict[str, Any]:
        from secretary.backup import create_backups
        from secretary.checkpoint import CheckpointWriter

        checkpoint = CheckpointWriter(
            self.paths.data, self.paths.instance, **self.checkpoint_options
        ).write()
        if checkpoint.status == "blocked":
            raise CutoverError(f"Kanboard checkpoint refused: {checkpoint.reason}")
        backups = create_backups(self.paths.instance, data_dir=self.paths.data, backup_kinds=("full",))
        return {
            "checkpoint": checkpoint.to_json(),
            "archives": [str(item.archive) for item in backups],
            "archive_manifests": [item.manifest for item in backups],
        }

    def postgresql_provision_migration_verification(self) -> dict[str, Any]:
        from secretary.board import migrate
        from secretary.board.provision import provision, verify_roles

        outcome = provision(self.paths.instance, allow_create=True, **self.provision_options)
        applied = migrate.migrate_instance(self.paths.instance)
        verify_roles(self.paths.instance)
        return {
            "actions": list(outcome.actions if outcome else ()),
            "migrations": list(applied),
            "migration_head": migrate.head_revision(),
            "roles": "verified",
        }

    def global_freeze(self) -> dict[str, Any]:
        frozen = _secretary(
            self.paths,
            "pause",
            "freeze",
            "--actor",
            CUTOVER_ACTOR,
            "--reason",
            self.state["reason"],
        )
        inventory = inventory_units()
        stopped = _systemctl("stop", inventory.stop_units())
        return {"pause": frozen, "services": stopped, "inventory": inventory.evidence()}

    def writer_quiescence_proof(self) -> dict[str, Any]:
        survivors = _writer_processes()
        if survivors:
            raise CutoverError(f"writer quiescence refused; {len(survivors)} writer process(es) survive")
        source = _source_evidence(self.paths)
        planned = self.state.get("planned_source", {}).get("fingerprint")
        if not planned or source["fingerprint"] != planned:
            raise CutoverError("Kanboard source moved after planning; refusing fenced import")
        return {"survivors": [], "controller_pid": os.getpid(), "source": source}

    def final_fenced_import(self) -> dict[str, Any]:
        from secretary.board.import_board import run

        self.paths.artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.paths.artifacts / f"import-{self.state['plan_id']}.json"
        report = run(
            self.paths.instance,
            data_dir=self.paths.data,
            dry_run=False,
            report_path=target,
        )
        return {"report": str(target), "import": report.as_dict()}

    def full_parity(self) -> dict[str, Any]:
        imported = self.state["phases"]["final_fenced_import"]["evidence"]["import"]
        parity = imported.get("parity", {})
        if not parity.get("ok"):
            raise CutoverError("full PostgreSQL parity failed")
        consistency = imported.get("source_consistency", {})
        if not consistency.get("matched"):
            raise CutoverError("final Kanboard source fence did not match")
        planned = self.state.get("planned_source", {}).get("fingerprint")
        quiescent = (
            self.state.get("phases", {})
            .get("writer_quiescence_proof", {})
            .get("evidence", {})
            .get("source", {})
            .get("fingerprint")
        )
        if (
            not planned
            or not quiescent
            or consistency.get("before") != planned
            or consistency.get("before") != quiescent
        ):
            raise CutoverError("final Kanboard source did not match the planned quiescent source")
        return {
            "parity": parity,
            "counts": imported.get("counts", {}),
            "source": imported.get("source", {}),
            "migration_head": imported.get("schema_revision"),
        }

    def postgresql_recovery_backup(self) -> dict[str, Any]:
        from secretary.backup import create_backups

        # The recovery point of the whole cutover, taken before the selector moves.  Earlier
        # phases of this same process have already read Kanboard, so borrowing PostgreSQL here is
        # a change of the process decision, not only of the environment.
        with _serving_backend("postgres"):
            backups = create_backups(
                self.paths.instance,
                data_dir=self.paths.data,
                backup_kinds=("full",),
                existing_freeze_actor=CUTOVER_ACTOR,
            )
        manifests = [item.manifest for item in backups]
        if any("raw_board" in item.get("components", {}) for item in manifests):
            raise CutoverError("pre-switch recovery backup unexpectedly contains a raw Kanboard component")
        if not manifests or any("postgres_dump" not in item.get("components", {}) for item in manifests):
            raise CutoverError("pre-switch recovery backup has no PostgreSQL dump component")
        return {
            "archives": [str(item.archive) for item in backups],
            "archive_manifests": manifests,
        }

    def selector_activation(self) -> dict[str, Any]:
        baseline = _sql_event_count(self.paths)
        activated = _set_backend(self.paths, "postgres")
        # The durable selector and the process decision move through the same named switch, so
        # every phase after this one reads PostgreSQL without a fresh process.
        _serve_backend("postgres")
        return {**activated, "sql_audit_baseline": baseline}

    def service_reconciliation(self) -> dict[str, Any]:
        inventory = inventory_units()
        started = _systemctl("start", inventory.start_units())
        provenance = _provenance(self.paths, self.state["expected_revision"])
        units = _service_evidence(inventory.start_units(), provenance["product_root"])
        return {
            "services": started,
            "units": units,
            "provenance": provenance,
            "inventory": inventory.evidence(),
        }

    def installed_protocol_acceptance(self) -> dict[str, Any]:
        # Every operation below is a public executable boundary and every write
        # has a deterministic request id.  A crash may therefore resume this
        # phase without duplicating a product, issue, comment, task or event.
        status = _secretary(self.paths, "status", "--json")
        # The same judgement the apply precondition made before the window, so the two cannot
        # disagree on what counts as a clean doctor.
        doctor_report, doctor = _doctor_offline(self.paths)
        if doctor is None:
            raise CutoverError(doctor_report["detail"])
        product = _secretary(self.paths, "product", "list")
        issue = _secretary(self.paths, "issue", "list")
        sprint = _secretary(self.paths, "sprint", "list")
        task = _secretary(self.paths, "task", "list")
        sprint_ref, project = _acceptance_context(_command_document(sprint, "sprint list"))
        canary_sprint = sprint_ref is None
        if canary_sprint:
            project = _acceptance_project(self.paths)
        prefix = self.state["plan_id"][:16]
        product_id = f"cutover-{prefix}"
        body = _artifact(
            self.paths,
            f"acceptance-{self.state['plan_id']}.md",
            "PostgreSQL cutover installed-protocol acceptance.\n",
        )
        override = _artifact(
            self.paths,
            f"acceptance-override-{self.state['plan_id']}.md",
            "Isolated cutover acceptance canary while the imported sprint remains frozen.\n",
        )

        created_product = _secretary(
            self.paths,
            "product",
            "create",
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-product-{prefix}",
            "--id",
            product_id,
            "--project",
            project,
            "--title",
            "PostgreSQL cutover acceptance",
            "--description",
            "Durable installed-protocol canary",
            "--data-dir",
            str(self.paths.data),
        )
        shown_product = _secretary(self.paths, "product", "show", "--id", product_id)
        created_issue = _secretary(
            self.paths,
            "issue",
            "create",
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-issue-{prefix}",
            "--product",
            product_id,
            "--kind",
            "improvement",
            "--priority",
            "P3",
            "--title",
            "Verify PostgreSQL cutover",
            "--description",
            "Installed acceptance issue",
            "--data-dir",
            str(self.paths.data),
        )
        issue_document = _command_document(created_issue, "issue create")
        issue_ref = str(
            (issue_document.get("ref") or issue_document.get("issue", {}).get("ref"))
            if isinstance(issue_document, dict)
            else ""
        )
        if not issue_ref:
            raise CutoverError("issue create returned no issue reference")
        updated_issue = _secretary(
            self.paths,
            "issue",
            "update-priority",
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-issue-priority-{prefix}",
            "--ref",
            issue_ref,
            "--priority",
            "P2",
            "--reason",
            "exercise installed mutation",
            "--data-dir",
            str(self.paths.data),
        )
        shown_issue = _secretary(self.paths, "issue", "show", "--ref", issue_ref)
        created_sprint: dict[str, Any] | None = None
        if canary_sprint:
            created_sprint = _secretary(
                self.paths,
                "sprint",
                "create",
                "--role",
                "po",
                "--actor",
                CUTOVER_ACTOR,
                "--request-id",
                f"cutover-sprint-{prefix}",
                "--goal",
                "PostgreSQL cutover acceptance canary",
                "--definition-of-done",
                "The installed protocol writes and reads one canary card through this sprint.",
                "--product",
                product_id,
                "--issue",
                issue_ref,
                "--project",
                str(project),
                "--observer",
                "none",
                "--repository",
                str(self.paths.instance),
                "--data-dir",
                str(self.paths.data),
            )
            sprint_created = _command_document(created_sprint, "sprint create")
            created_ref = None
            if isinstance(sprint_created, dict):
                created_ref = sprint_created.get("ref") or (sprint_created.get("sprint") or {}).get("ref")
            if not created_ref:
                raise CutoverError("sprint create returned no sprint reference")
            sprint_ref = str(created_ref)
        assert sprint_ref is not None

        sprint_request = f"cutover-sprint-comment-{prefix}"
        sprint_comment_args = (
            "sprint",
            "comment",
            "--ref",
            sprint_ref,
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            sprint_request,
            "--body-file",
            str(body),
            "--data-dir",
            str(self.paths.data),
        )
        sprint_comment = _secretary(self.paths, *sprint_comment_args)
        sprint_replay = _secretary(self.paths, *sprint_comment_args)
        sprint_document = _command_document(sprint_comment, "sprint comment")
        replay_document = _command_document(sprint_replay, "sprint comment replay")
        comment_id = str(sprint_document.get("comment_id") if isinstance(sprint_document, dict) else "")
        if (
            not comment_id
            or replay_document.get("comment_id") != comment_id
            or replay_document.get("saved") is not False
        ):
            raise CutoverError(
                "sprint comment replay did not prove one durable delivery event: "
                f"first={sprint_document!r}, replay={replay_document!r}"
            )
        delivery = _secretary(
            self.paths,
            "sprint",
            "comment-delivery",
            "--ref",
            sprint_ref,
            "--comment-id",
            comment_id,
            "--data-dir",
            str(self.paths.data),
        )

        create_task = _secretary(
            self.paths,
            "task",
            "create",
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-task-{prefix}",
            "--project",
            project,
            "--type",
            "code",
            "--title",
            "PostgreSQL cutover acceptance task",
            "--state",
            "ready",
            "--sprint",
            sprint_ref,
            "--sprint-override",
            "--sprint-override-reason-file",
            str(override),
            "--data-dir",
            str(self.paths.data),
        )
        create_document = _command_document(create_task, "task create")
        reference = str(
            create_document.get("task", {}).get("ref") if isinstance(create_document, dict) else ""
        )
        if not reference:
            raise CutoverError("task create returned no task reference")
        claim = _secretary(
            self.paths,
            "task",
            "claim",
            "--ref",
            reference,
            "--role",
            "dispatcher",
            "--actor",
            CUTOVER_ACTOR,
            "--worker",
            f"cutover-{prefix}",
            "--request-id",
            f"cutover-task-claim-{prefix}",
            "--data-dir",
            str(self.paths.data),
        )
        release = _secretary(
            self.paths,
            "task",
            "move",
            "--ref",
            reference,
            "--role",
            "dispatcher",
            "--actor",
            CUTOVER_ACTOR,
            "--to",
            "ready",
            "--reason-file",
            str(body),
            "--request-id",
            f"cutover-task-release-{prefix}",
            "--data-dir",
            str(self.paths.data),
        )
        comment_args = (
            "task",
            "comment",
            "--ref",
            reference,
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-task-comment-{prefix}",
            "--body-file",
            str(body),
            "--data-dir",
            str(self.paths.data),
        )
        first = _secretary(self.paths, *comment_args)
        replay = _secretary(self.paths, *comment_args)
        first_document = _command_document(first, "task comment")
        replay_task_document = _command_document(replay, "task comment replay")
        if not isinstance(first_document, dict) or not isinstance(replay_task_document, dict):
            raise CutoverError("task comment replay returned invalid documents")
        if (
            first_document.get("event_id") != replay_task_document.get("event_id")
            or replay_task_document.get("replayed") is not True
        ):
            raise CutoverError("task comment replay did not prove one committed event")
        completed = _secretary(
            self.paths,
            "task",
            "move",
            "--ref",
            reference,
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--to",
            "done",
            "--reason-file",
            str(body),
            "--request-id",
            f"cutover-task-done-{prefix}",
            "--sprint-override",
            "--sprint-override-reason-file",
            str(override),
            "--data-dir",
            str(self.paths.data),
        )
        archived = _secretary(
            self.paths,
            "task",
            "archive",
            "--ref",
            reference,
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--reason-file",
            str(body),
            "--request-id",
            f"cutover-task-archive-{prefix}",
            "--data-dir",
            str(self.paths.data),
        )
        post_close = _secretary(
            self.paths,
            "task",
            "comment",
            "--ref",
            reference,
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-task-post-close-{prefix}",
            "--body-file",
            str(body),
            "--data-dir",
            str(self.paths.data),
        )
        closed_issue = _secretary(
            self.paths,
            "issue",
            "close",
            "--role",
            "po",
            "--actor",
            CUTOVER_ACTOR,
            "--request-id",
            f"cutover-issue-close-{prefix}",
            "--ref",
            issue_ref,
            "--reason",
            "resolved",
            "--data-dir",
            str(self.paths.data),
        )
        task_read = _secretary(self.paths, "task", "show", "--ref", reference)
        web_task = _secretary(self.paths, "web-read", "task", "--json", "--ref", reference, "--events", "50")
        web_request = _secretary(
            self.paths, "web-read", "request", "--json", "--request-id", f"cutover-task-comment-{prefix}"
        )
        web_commands = _secretary(self.paths, "web-read", "commands", "--json", "--limit", "50")
        web_system = _secretary(self.paths, "web-read", "system", "--json")
        closed_sprint: dict[str, Any] | None = None
        if canary_sprint:
            decisions = _artifact(
                self.paths,
                f"acceptance-sprint-decisions-{self.state['plan_id']}.yaml",
                "issues:\n"
                f"  - ref: {issue_ref}\n"
                "    verdict: already_closed\n"
                "    actual: resolved\n"
                "    reason: the acceptance flow closed its canary issue after the card was archived\n",
            )
            closeout = _artifact(
                self.paths,
                f"acceptance-sprint-closeout-{self.state['plan_id']}.md",
                "# PostgreSQL cutover acceptance canary\n\n"
                "Opened and closed by `secretary cutover apply` inside installed_protocol_acceptance "
                "because no sprint was open during the window. Its only card was created, claimed, "
                "released, commented, moved to Done and archived through the installed protocol.\n",
            )
            closed_sprint = _secretary(
                self.paths,
                "sprint",
                "close",
                "--role",
                "po",
                "--actor",
                CUTOVER_ACTOR,
                "--request-id",
                f"cutover-sprint-close-{prefix}",
                "--ref",
                sprint_ref,
                "--reason",
                "cutover acceptance canary complete",
                "--decisions-file",
                str(decisions),
                "--closeout-file",
                str(closeout),
                "--data-dir",
                str(self.paths.data),
            )
        tick = _secretary(self.paths, "dispatcher", "production-tick", "--probe", "--host-mode", "noop")
        audit = _sql_event_count(self.paths)
        return {
            "reads": {
                "status": status,
                "doctor": doctor,
                "product": product,
                "issue": issue,
                "sprint": sprint,
                "task": task,
            },
            "product": {"id": product_id, "created": created_product, "shown": shown_product},
            "issue": {
                "ref": issue_ref,
                "created": created_issue,
                "updated": updated_issue,
                "shown": shown_issue,
                "closed": closed_issue,
            },
            "sprint": {
                "ref": sprint_ref,
                "canary": canary_sprint,
                "created": created_sprint,
                "comment": sprint_comment,
                "replay": sprint_replay,
                "delivery": delivery,
                "closed": closed_sprint,
            },
            "write": {
                "task_ref": reference,
                "request_id": f"cutover-task-comment-{prefix}",
                "created": create_task,
                "claim": claim,
                "release": release,
                "first": first,
                "replay": replay,
                "completed": completed,
                "archived": archived,
                "post_close_comment": post_close,
                "read": task_read,
            },
            "web": {"system": web_system, "task": web_task, "commands": web_commands, "request": web_request},
            "dispatcher_probe": tick,
            "sql_audit": audit,
        }

    def post_switch_checkpoint(self) -> dict[str, Any]:
        from secretary.backup import create_backups
        from secretary.checkpoint import CheckpointWriter

        with _serving_backend("postgres"):
            checkpoint = CheckpointWriter(
                self.paths.data, self.paths.instance, **self.checkpoint_options
            ).write()
            if checkpoint.status == "blocked":
                raise CutoverError(f"PostgreSQL checkpoint refused: {checkpoint.reason}")
            backups = create_backups(
                self.paths.instance,
                data_dir=self.paths.data,
                backup_kinds=("full",),
                existing_freeze_actor=CUTOVER_ACTOR,
            )
        manifests = [item.manifest for item in backups]
        if any("raw_board" in item.get("components", {}) for item in manifests):
            raise CutoverError("post-switch backup unexpectedly contains a raw Kanboard component")
        if not manifests or any("postgres_dump" not in item.get("components", {}) for item in manifests):
            raise CutoverError("post-switch full backup has no PostgreSQL dump component")
        preserved = None
        acceptance = self.state.get("phases", {}).get("installed_protocol_acceptance", {}).get("evidence", {})
        task_ref = acceptance.get("write", {}).get("task_ref")
        if task_ref:
            preserved = _secretary(self.paths, "task", "show", "--ref", str(task_ref))
        return {
            "checkpoint": checkpoint.to_json(),
            "archives": [str(x.archive) for x in backups],
            "archive_manifests": manifests,
            "acceptance_preserved": preserved,
        }

    def resume_ready(self) -> dict[str, Any]:
        return {
            "ready": True,
            "pipeline": "frozen",
            "instruction": "inspect evidence, then run secretary resume explicitly",
        }


def _new_state(plan: dict[str, Any], actor: str, reason: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "identity": f"postgres-{plan['plan_id'][:20]}",
        "plan_id": plan["plan_id"],
        "expected_revision": plan["expected_revision"],
        "actor": actor,
        "reason": reason,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "applying",
        "backend_before": "kanboard",
        "planned_source": plan.get("source", {}),
        "first_sql_write": None,
        "phases": {},
    }


def _validate_mutation(args: argparse.Namespace, plan: dict[str, Any] | None = None) -> None:
    if not args.actor.strip() or not args.reason.strip():
        raise CutoverError("mutating cutover modes require non-empty --actor and --reason")
    if not args.confirm.strip():
        raise CutoverError("mutating cutover modes require --confirm")
    if plan is not None and args.confirm != plan["confirmation"]:
        raise CutoverError("confirmation token does not match the current plan")


def _refresh_first_sql_write(paths: Paths, state: dict[str, Any]) -> None:
    """Persist the irreversible boundary, treating an unreadable audit as uncertainty."""
    if state.get("first_sql_write") is not None:
        return
    activation = state.get("phases", {}).get("selector_activation", {})
    if activation.get("status") != "complete":
        return
    baseline = activation.get("evidence", {}).get("sql_audit_baseline", {})
    try:
        current = _sql_event_count(paths)
    except Exception as exc:  # noqa: BLE001 - uncertainty must itself cross the durable boundary
        reason = str(exc) if isinstance(exc, CutoverError) else type(exc).__name__
        current = {"unavailable": True, "reason": reason}
        crossed = True
    else:
        crossed = current.get("committed_events", 0) > baseline.get("committed_events", 0)
    if crossed:
        state["first_sql_write"] = {"recorded_at": _now(), "evidence": current}


def apply_cutover(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    # Every precondition -- the privileged install steps, the pipeline pause and
    # doctor --offline -- is proven before the lock, the first state write and
    # the first phase, so a new run leaves no state document behind and a retry
    # leaves the durable one exactly as it was.
    _require_privileged_preconditions(paths=paths)
    with CutoverLock(paths):
        state = _read_state(paths)
        if state is None:
            plan = build_plan(paths, args.expected_revision)
            _validate_mutation(args, plan)
            state = _new_state(plan, args.actor.strip(), args.reason.strip())
            _write_state(paths, state)
        else:
            _validate_mutation(args)
            if state.get("status") in {"resume-ready", "recovered-frozen"}:
                raise CutoverError(
                    "terminal cutover identity cannot be applied again; create a fresh plan and identity"
                )
            if state.get("expected_revision") != args.expected_revision:
                raise CutoverError("existing cutover state belongs to a different revision")
            if args.confirm != f"CUTOVER-{state['plan_id'][:16]}":
                raise CutoverError("confirmation token does not match the existing cutover identity")
            if state.get("actor") != args.actor.strip() or state.get("reason") != args.reason.strip():
                raise CutoverError("retry must use the cutover identity's original actor and reason")
            activated = state.get("phases", {}).get("selector_activation", {}).get("status") == "complete"
            expected_backend = "postgres" if activated else "kanboard"
            if _backend(paths) != expected_backend:
                raise CutoverError(
                    f"durable phase evidence expects backend {expected_backend}; refusing ambiguous continuation"
                )
            _provenance(paths, args.expected_revision)
        # This is the single apply-entry boundary for both a new run and every
        # supported retry.  Publish it before constructing Operations or
        # entering any phase so a failed/failed-frozen retry cannot traverse
        # the freeze with the durable foreign-writer barrier disarmed.
        state["status"] = "applying"
        state["controller_pid"] = os.getpid()
        state["updated_at"] = _now()
        _write_state(paths, state)
        old_controller = os.environ.get(CONTROLLER_ID_ENV)
        os.environ[CONTROLLER_ID_ENV] = state["identity"]
        operations = Operations(paths, state)
        try:
            for name in PHASES:
                if state["phases"].get(name, {}).get("status") == "complete":
                    continue
                state["phases"][name] = {"status": "running", "started_at": _now()}
                state["updated_at"] = _now()
                _write_state(paths, state)
                try:
                    evidence = getattr(operations, name)()
                except Exception as exc:  # noqa: BLE001 - every phase failure must become durable evidence
                    reason = str(exc) if isinstance(exc, CutoverError) else f"{type(exc).__name__}: {exc}"
                    _refresh_first_sql_write(paths, state)
                    state["phases"][name] = {
                        **state["phases"][name],
                        "status": "failed",
                        "failed_at": _now(),
                        "reason": reason[:1000],
                    }
                    state["status"] = "failed-frozen" if "global_freeze" in state["phases"] else "failed"
                    state["updated_at"] = _now()
                    _write_state(paths, state)
                    raise CutoverError(f"phase {name} failed: {reason}") from None
                state["phases"][name] = {
                    **state["phases"][name],
                    "status": "complete",
                    "completed_at": _now(),
                    "revision": args.expected_revision,
                    "evidence": evidence,
                }
                if name == "installed_protocol_acceptance":
                    _refresh_first_sql_write(paths, state)
                state["updated_at"] = _now()
                _write_state(paths, state)
        finally:
            if old_controller is None:
                os.environ.pop(CONTROLLER_ID_ENV, None)
            else:
                os.environ[CONTROLLER_ID_ENV] = old_controller
        state["status"] = "resume-ready"
        state["updated_at"] = _now()
        _write_state(paths, state)
        return state


def _frozen_source_unchanged(paths: Paths, state: dict[str, Any]) -> dict[str, Any]:
    proof = _source_evidence(paths)
    quiescence = state.get("phases", {}).get("writer_quiescence_proof", {}).get("evidence", {})
    expected = quiescence.get("source", {}).get("fingerprint")
    if not expected or proof.get("fingerprint") != expected:
        raise CutoverError("frozen Kanboard source moved; refusing rollback")
    return proof


def recover_cutover(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    _validate_mutation(args)
    with CutoverLock(paths):
        state = _read_state(paths)
        if state is None:
            raise CutoverError("there is no durable cutover state to recover")
        if state.get("expected_revision") != args.expected_revision:
            raise CutoverError("recovery revision does not match the cutover identity")
        expected_token = f"RECOVER-{state['plan_id'][:16]}"
        if args.confirm != expected_token:
            raise CutoverError(f"recovery confirmation token must be {expected_token}")
        _provenance(paths, args.expected_revision)
        if state.get("status") == "recovered-frozen":
            return _finish_recovery(paths, state)
        activated = state.get("phases", {}).get("selector_activation", {}).get("status") == "complete"
        freeze_status = state.get("phases", {}).get("global_freeze", {}).get("status")
        import_entered = "final_fenced_import" in state.get("phases", {})
        if freeze_status is None and not import_entered and not activated:
            # No old writer was durably declared stopped, no target was imported
            # and no selector changed.  There is no cutover effect to roll back.
            state["recovery"] = {
                "branch": "no-cutover-effects",
                "actor": args.actor,
                "reason": args.reason,
                "completed_at": _now(),
                "evidence": {"backend": _backend(paths), "frozen": False},
            }
            state["status"] = "recovered-frozen"
            state["updated_at"] = _now()
            return _finish_recovery(paths, state)
        if not import_entered and not activated and not state.get("first_sql_write"):
            if _backend(paths) != "kanboard":
                raise CutoverError("early recovery expected the unchanged Kanboard selector")
            source = _source_evidence(paths)
            state["recovery"] = {
                "branch": "kanboard-before-fingerprint",
                "actor": args.actor,
                "reason": args.reason,
                "completed_at": _now(),
                "evidence": {
                    "source": source,
                    "freeze_phase_status": freeze_status,
                    **_restart_consumers(),
                },
            }
            state["status"] = "recovered-frozen"
            state["updated_at"] = _now()
            return _finish_recovery(paths, state)
        if activated:
            baseline = state["phases"]["selector_activation"]["evidence"]["sql_audit_baseline"]
            try:
                current = _sql_event_count(paths)
            except CutoverError as exc:
                # Unavailable SQL cannot prove the pre-write branch.  Record the
                # uncertainty as irreversible and stay on PostgreSQL.
                current = {"unavailable": True, "reason": str(exc)}
                state["first_sql_write"] = state.get("first_sql_write") or {
                    "recorded_at": _now(),
                    "evidence": current,
                }
            else:
                if current["committed_events"] > baseline["committed_events"]:
                    state["first_sql_write"] = state.get("first_sql_write") or {
                        "recorded_at": _now(),
                        "evidence": current,
                    }
        if state.get("first_sql_write") is not None:
            if _backend(paths) != "postgres":
                _set_backend(paths, "postgres")
            # Recovery is the second entrance that changes which backend the installation
            # serves, and it moves the durable selector and the process decision through the
            # same named switch apply does.  The durable write is conditional because the
            # selector may already be activated; the decision is not, because the process
            # that reconciles the store must serve what runtime.env now names either way.
            _serve_backend("postgres")
            evidence = {"postgresql": _reconcile_postgres(paths), **_restart_consumers()}
            branch = "postgres-only"
        else:
            source = _frozen_source_unchanged(paths, state)
            _set_backend(paths, "kanboard")
            _serve_backend("kanboard")
            evidence = {"source": source, **_restart_consumers()}
            branch = "kanboard-before-first-write"
        state["recovery"] = {
            "branch": branch,
            "actor": args.actor,
            "reason": args.reason,
            "completed_at": _now(),
            "evidence": evidence,
        }
        state["status"] = "recovered-frozen"
        state["updated_at"] = _now()
        return _finish_recovery(paths, state)


def _render(payload: dict[str, Any], *, pretty: bool = True) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))


def run_cutover(args: argparse.Namespace) -> int:
    try:
        if args.cutover_command == "prepare-successor" and not Path(args.instance).is_absolute():
            raise CutoverError("prepare-successor requires an absolute --instance path")
        paths = resolve_paths(args.instance)
        if args.cutover_command == "plan":
            plan = build_plan(paths, args.expected_revision)
            # The preconditions describe the host, not the plan, so they are
            # reported beside the identity instead of inside the hashed
            # evidence: a saved confirmation token has to survive the operator
            # installing the missing root steps.  Plan only shows them.
            _render({**plan, "privileged_preconditions": _privileged_preconditions(paths=paths)})
        elif args.cutover_command == "status":
            state = _read_state(paths)
            recovered_history = _read_recovered_history(paths)
            successor_preparation = None
            if state is not None:
                from secretary.cutover.successor import status_probe

                successor_preparation = status_probe(
                    paths.instance, state, _backend(paths), artifacts=paths.artifacts
                )
            else:
                pending = [
                    item
                    for item in recovered_history
                    if item.get("successor_release", {}).get("status") == "pending"
                ]
                if len(pending) == 1:
                    from secretary.cutover.successor import history_status

                    successor_preparation = history_status(pending[0])
            _render(
                {
                    "state": state,
                    "recovered_history": recovered_history,
                    "successor_eligibility": _successor_eligibility(state),
                    "backend": _backend(paths),
                    "successor_preparation": successor_preparation,
                    "recovery_confirmation": (
                        f"RECOVER-{state['plan_id'][:16]}" if state is not None else None
                    ),
                }
            )
        elif args.cutover_command == "apply":
            _render(apply_cutover(args, paths))
        elif args.cutover_command == "recover":
            _render(recover_cutover(args, paths))
        elif args.cutover_command == "prepare-successor":
            from secretary.cutover.successor import prepare

            _render(prepare(args, paths))
        else:
            raise CutoverError("cutover subcommand required")
    except CutoverError as exc:
        _render({"ok": False, "error": str(exc)})
        return 1
    return 0


def add_cutover_subcommands(subparsers: Any) -> None:
    cutover = subparsers.add_parser(
        "cutover",
        help="plan, inspect, apply, recover or prepare a successor PostgreSQL board-store target",
    )
    commands = cutover.add_subparsers(dest="cutover_command")
    for name in ("plan", "status", "apply", "recover", "prepare-successor"):
        command = commands.add_parser(name)
        command.add_argument("--instance", required=True)
        if name != "status":
            command.add_argument("--expected-revision", required=True)
        if name in ("apply", "recover", "prepare-successor"):
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--confirm", required=True)
        command.set_defaults(handler=run_cutover)
    cutover.set_defaults(handler=run_cutover)


__all__ = [
    "ARTIFACTS_RELATIVE",
    "BACKEND_ENV",
    "HISTORY_RELATIVE",
    "LOCK_RELATIVE",
    "PHASES",
    "STATE_RELATIVE",
    "STATE_VERSION",
    "CutoverError",
    "CutoverLock",
    "Operations",
    "Paths",
    "add_cutover_subcommands",
    "apply_cutover",
    "build_plan",
    "recover_cutover",
    "resolve_paths",
    "run_cutover",
]
