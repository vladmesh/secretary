"""Restart-safe operator boundary for the Kanboard to PostgreSQL cutover.

The controller deliberately contains orchestration, not another implementation of
backup, migration, import, parity, pause, checkpoint, or board operations.  Every
completed phase is fsync'd into the installation data plane before the next phase
is entered.  A failure is durable and leaves the global freeze in place.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
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
BACKEND_ENV = "SECRETARY_CARD_BACKEND"
CUTOVER_ACTOR = "secretary-postgres-cutover"

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

STOP_UNITS = (
    "secretary-dispatcher-production.timer",
    "secretary-dispatcher-production.service",
    "secretary-web-front.service",
    "secretary-web.service",
    "secretary-steward.timer",
    "secretary-steward.service",
    "secretary-steward-deep-sweep.timer",
    "secretary-steward-deep-sweep.service",
    "secretary-retro.timer",
    "secretary-retro.service",
    "secretary-curator.timer",
    "secretary-curator.service",
)
START_UNITS = (
    "secretary-web.service",
    "secretary-web-front.service",
    "secretary-dispatcher-production.timer",
    "secretary-steward.timer",
    "secretary-steward-deep-sweep.timer",
    "secretary-retro.timer",
    "secretary-curator.timer",
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
    return {
        "fingerprint": report.source.get("fingerprint"),
        "consistency": report.source_consistency,
        "counts": report.counts,
        "audit": report.discrepancy_count,
        "parity": report.parity,
    }


def build_plan(paths: Paths, expected_revision: str) -> dict[str, Any]:
    backend = _backend(paths)
    if backend != "kanboard":
        raise CutoverError(f"new cutover plan requires backend kanboard, found {backend}")
    evidence = {
        "version": STATE_VERSION,
        "instance": str(paths.instance),
        "data_dir": str(paths.data),
        "expected_revision": expected_revision,
        "backend": backend,
        "provenance": _provenance(paths, expected_revision),
        "source": _source_evidence(paths),
        "phases": list(PHASES),
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
    paths.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe_directory(paths.state.parent, "cutover state directory")
    staged = stage_text(paths.state, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        staged.chmod(0o600)
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


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=600, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CutoverError(f"could not run {Path(argv[0]).name}: {type(exc).__name__}") from None
    if result.returncode:
        detail = (result.stderr or result.stdout or "failed").strip().splitlines()[-1]
        raise CutoverError(f"{Path(argv[0]).name} failed: {detail[:500]}")
    output = (result.stdout or "").strip()
    return {
        "command": [Path(argv[0]).name, *argv[1:]],
        "exit_code": 0,
        "output": output[:4000],
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _secretary(paths: Paths, *args: str) -> dict[str, Any]:
    return _run([sys.executable, "-P", "-m", "secretary.cli", *args, "--instance", str(paths.instance)])


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


def _acceptance_task_ref(paths: Paths) -> str:
    import sqlalchemy as sa

    from secretary.board import migrate, store

    config = store.resolve(paths.instance)
    engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("read")))
    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql("SELECT task_ref FROM tasks ORDER BY task_ref LIMIT 1").first()
        if row is None:
            raise CutoverError("installed protocol acceptance needs at least one imported task")
        return str(row[0])
    except sa.exc.SQLAlchemyError as exc:
        raise CutoverError(f"could not select the protocol acceptance task: {exc}") from None
    finally:
        engine.dispose()


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
        evidence.append(_run(["systemctl", action, unit]))
    return {"action": action, "units": list(units), "results": evidence}


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

    def __init__(self, paths: Paths, state: dict[str, Any]) -> None:
        self.paths = paths
        self.state = state

    def preflight(self) -> dict[str, Any]:
        current = build_plan(self.paths, self.state["expected_revision"])
        if current["plan_id"] != self.state["plan_id"]:
            raise CutoverError("plan is stale; inspect status and create a new plan before freezing")
        return {"plan_id": current["plan_id"], "provenance": current["provenance"]}

    def current_kanboard_backup_checkpoint(self) -> dict[str, Any]:
        from secretary.backup import create_backups
        from secretary.checkpoint import CheckpointWriter

        checkpoint = CheckpointWriter(self.paths.data, self.paths.instance).write()
        if checkpoint.status == "blocked":
            raise CutoverError(f"Kanboard checkpoint refused: {checkpoint.reason}")
        backups = create_backups(self.paths.instance, data_dir=self.paths.data, backup_kinds=("full",))
        return {
            "checkpoint": checkpoint.as_dict(),
            "archives": [str(item.archive) for item in backups],
            "archive_manifests": [item.manifest for item in backups],
        }

    def postgresql_provision_migration_verification(self) -> dict[str, Any]:
        from secretary.board import migrate
        from secretary.board.provision import provision, verify_roles

        outcome = provision(self.paths.instance, allow_create=True)
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
        stopped = _systemctl("stop", STOP_UNITS)
        return {"pause": frozen, "services": stopped}

    def writer_quiescence_proof(self) -> dict[str, Any]:
        survivors = _writer_processes()
        if survivors:
            raise CutoverError(f"writer quiescence refused; {len(survivors)} writer process(es) survive")
        source = _source_evidence(self.paths)
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
        return {
            "parity": parity,
            "counts": imported.get("counts", {}),
            "source": imported.get("source", {}),
            "migration_head": imported.get("schema_revision"),
        }

    def postgresql_recovery_backup(self) -> dict[str, Any]:
        from secretary.backup import create_backups

        env_before = os.environ.get(BACKEND_ENV)
        os.environ[BACKEND_ENV] = "postgres"
        try:
            backups = create_backups(
                self.paths.instance,
                data_dir=self.paths.data,
                backup_kinds=("full",),
                existing_freeze_actor=CUTOVER_ACTOR,
            )
        finally:
            if env_before is None:
                os.environ.pop(BACKEND_ENV, None)
            else:
                os.environ[BACKEND_ENV] = env_before
        return {
            "archives": [str(item.archive) for item in backups],
            "archive_manifests": [item.manifest for item in backups],
        }

    def selector_activation(self) -> dict[str, Any]:
        from secretary.board.backend import reset_card_backend

        baseline = _sql_event_count(self.paths)
        activated = _set_backend(self.paths, "postgres")
        os.environ[BACKEND_ENV] = "postgres"
        reset_card_backend()
        return {**activated, "sql_audit_baseline": baseline}

    def service_reconciliation(self) -> dict[str, Any]:
        started = _systemctl("start", START_UNITS)
        provenance = _provenance(self.paths, self.state["expected_revision"])
        units = _service_evidence(START_UNITS, provenance["product_root"])
        return {"services": started, "units": units, "provenance": provenance}

    def installed_protocol_acceptance(self) -> dict[str, Any]:
        # These are public executable boundaries.  The idempotent comment is the
        # first accepted application write and its committed request/event pair is
        # the irreversible recovery boundary.  Repeating the same request proves
        # public replay without creating a second comment or event.
        status = _secretary(self.paths, "status", "--json")
        doctor = _secretary(self.paths, "doctor", "--json")
        product = _secretary(self.paths, "product", "list")
        issue = _secretary(self.paths, "issue", "list")
        sprint = _secretary(self.paths, "sprint", "list")
        task = _secretary(self.paths, "task", "list")
        reference = _acceptance_task_ref(self.paths)
        body = self.paths.artifacts / f"acceptance-{self.state['plan_id']}.md"
        body.write_text(
            "PostgreSQL cutover installed-protocol acceptance.\n",
            encoding="utf-8",
        )
        body.chmod(0o600)
        request_id = f"postgres-cutover-acceptance-{self.state['plan_id'][:24]}"
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
            request_id,
            "--body-file",
            str(body),
            "--data-dir",
            str(self.paths.data),
        )
        first = _secretary(self.paths, *comment_args)
        replay = _secretary(self.paths, *comment_args)
        tick = _secretary(self.paths, "dispatcher", "production-tick", "--probe")
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
            "write": {"task_ref": reference, "request_id": request_id, "first": first, "replay": replay},
            "dispatcher_probe": tick,
            "sql_audit": audit,
        }

    def post_switch_checkpoint(self) -> dict[str, Any]:
        from secretary.backup import create_backups
        from secretary.checkpoint import CheckpointWriter

        old = os.environ.get(BACKEND_ENV)
        os.environ[BACKEND_ENV] = "postgres"
        try:
            checkpoint = CheckpointWriter(self.paths.data, self.paths.instance).write()
            if checkpoint.status == "blocked":
                raise CutoverError(f"PostgreSQL checkpoint refused: {checkpoint.reason}")
            backups = create_backups(
                self.paths.instance,
                data_dir=self.paths.data,
                backup_kinds=("full",),
                existing_freeze_actor=CUTOVER_ACTOR,
            )
        finally:
            if old is None:
                os.environ.pop(BACKEND_ENV, None)
            else:
                os.environ[BACKEND_ENV] = old
        manifests = [item.manifest for item in backups]
        if any("kanboard_raw" in item.get("components", {}) for item in manifests):
            raise CutoverError("post-switch backup unexpectedly contains a raw Kanboard component")
        return {"checkpoint": checkpoint.as_dict(), "archives": [str(x.archive) for x in backups]}

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


def apply_cutover(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    with CutoverLock(paths):
        state = _read_state(paths)
        if state is None:
            plan = build_plan(paths, args.expected_revision)
            _validate_mutation(args, plan)
            state = _new_state(plan, args.actor.strip(), args.reason.strip())
            _write_state(paths, state)
        else:
            _validate_mutation(args)
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
        state["controller_pid"] = os.getpid()
        state["updated_at"] = _now()
        _write_state(paths, state)
        old_controller = os.environ.get("SECRETARY_CUTOVER_CONTROLLER_ID")
        os.environ["SECRETARY_CUTOVER_CONTROLLER_ID"] = state["identity"]
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
                    baseline = state["phases"]["selector_activation"]["evidence"]["sql_audit_baseline"]
                    audit = evidence["sql_audit"]
                    if audit["committed_events"] > baseline["committed_events"]:
                        state["first_sql_write"] = {"recorded_at": _now(), "evidence": audit}
                state["updated_at"] = _now()
                _write_state(paths, state)
        finally:
            if old_controller is None:
                os.environ.pop("SECRETARY_CUTOVER_CONTROLLER_ID", None)
            else:
                os.environ["SECRETARY_CUTOVER_CONTROLLER_ID"] = old_controller
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
        activated = state.get("phases", {}).get("selector_activation", {}).get("status") == "complete"
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
            evidence = {
                "postgresql": _reconcile_postgres(paths),
                "services": _systemctl("restart", START_UNITS),
            }
            branch = "postgres-only"
        else:
            source = _frozen_source_unchanged(paths, state)
            _set_backend(paths, "kanboard")
            evidence = {"source": source, "services": _systemctl("restart", START_UNITS)}
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
        _write_state(paths, state)
        return state


def _render(payload: dict[str, Any], *, pretty: bool = True) -> None:
    print(json.dumps(payload, indent=2 if pretty else None, sort_keys=True))


def run_cutover(args: argparse.Namespace) -> int:
    try:
        paths = resolve_paths(args.instance)
        if args.cutover_command == "plan":
            _render(build_plan(paths, args.expected_revision))
        elif args.cutover_command == "status":
            state = _read_state(paths)
            _render(
                {
                    "state": state,
                    "backend": _backend(paths),
                    "recovery_confirmation": (
                        f"RECOVER-{state['plan_id'][:16]}" if state is not None else None
                    ),
                }
            )
        elif args.cutover_command == "apply":
            _render(apply_cutover(args, paths))
        elif args.cutover_command == "recover":
            _render(recover_cutover(args, paths))
        else:
            raise CutoverError("cutover subcommand required")
    except CutoverError as exc:
        _render({"ok": False, "error": str(exc)})
        return 1
    return 0


def add_cutover_subcommands(subparsers: Any) -> None:
    cutover = subparsers.add_parser(
        "cutover", help="plan, inspect, apply or recover the PostgreSQL board-store cutover"
    )
    commands = cutover.add_subparsers(dest="cutover_command")
    for name in ("plan", "status", "apply", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--instance", required=True)
        if name != "status":
            command.add_argument("--expected-revision", required=True)
        if name in ("apply", "recover"):
            command.add_argument("--actor", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--confirm", required=True)
        command.set_defaults(handler=run_cutover)
    cutover.set_defaults(handler=run_cutover)


__all__ = [
    "ARTIFACTS_RELATIVE",
    "BACKEND_ENV",
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
