from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from secretary.backup import check_backup_health, create_backups, verify_backup
from secretary.config import ConfigError, load_config, validate, validate_instance
from secretary.data import (
    KANBOARD_DATA_PATH,
    export_all,
    export_artifacts,
    export_board,
    export_memory,
    export_runs,
    export_transcripts,
    import_memory_journal,
    init_layout,
    raw_kanboard_dump,
)
from secretary.dispatcher_commands import add_dispatcher_subcommands
from secretary.dispatcher_pause import FileLegacyPauseProbe
from secretary.host import (
    CollectResult,
    FixtureHostSource,
    KindDiff,
    LiveHostSource,
    build_expectations,
    build_plan,
    inventory,
    load_managed_manifest,
    plan_changes,
)
from secretary.host_commands import add_reconcile_subcommands
from secretary.gate import run_gate
from secretary.memory_journal import verify_memory_journal
from secretary.memory_write import (
    MemoryExportPublishError,
    MemoryLockError,
    MemoryPermissionError,
    MemoryValidationError,
    commit_memory_proposal,
    propose_memory_fact,
    supersede_memory_fact,
)
from secretary.offsite import check_last_fetch
from secretary.onboarding import DEFAULT_INSTANCE, project_add, render_artifact
from secretary.provision import apply_provision_result, render_result, start_provision
from secretary.task_commands import add_task_subcommands


NOT_IMPLEMENTED = "not implemented in Phase 1 skeleton"
MEMORY_EXIT_VALIDATION = 2
MEMORY_EXIT_PERMISSION = 3
MEMORY_EXIT_LOCKED = 4


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secretary")
    subparsers = parser.add_subparsers(dest="command")
    add_dispatcher_subcommands(subparsers)

    doctor = subparsers.add_parser("doctor", help="inspect an instance without changing the host")
    doctor.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="check config and data without inspecting the host",
    )
    doctor.add_argument("--host", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument(
        "--host-fixture",
        metavar="DIR",
        help="compare against a fixture host dir instead of the live host",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="treat migration warnings as findings",
    )
    doctor.set_defaults(handler=run_doctor)

    reconcile = subparsers.add_parser("reconcile", help="render a host plan without applying it")
    reconcile_subcommands = reconcile.add_subparsers(dest="reconcile_command")
    add_reconcile_subcommands(reconcile_subcommands)
    reconcile.set_defaults(handler=not_implemented("reconcile"))

    data = subparsers.add_parser("data", help="manage the secretary-data layout")
    data_subcommands = data.add_subparsers(dest="data_command")

    data_init = data_subcommands.add_parser("init", help="create secretary-data and its manifest")
    data_init.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    data_init.add_argument(
        "--data-dir",
        help="override instance.yaml data_dir",
    )
    data_init.set_defaults(handler=run_data_init)

    raw_dump = data_subcommands.add_parser(
        "raw-kanboard-dump",
        help="copy the live Kanboard storage into secretary-data/board",
    )
    raw_dump.add_argument(
        "--instance",
        required=True,
        help="path to an instance dir or instance.yaml",
    )
    raw_dump.add_argument(
        "--data-dir",
        help="override instance.yaml data_dir",
    )
    raw_dump.add_argument("--container", default="cp-kanboard")
    raw_dump.add_argument("--source-path", default=KANBOARD_DATA_PATH)
    raw_dump.set_defaults(handler=run_raw_kanboard_dump)

    export = data_subcommands.add_parser(
        "export",
        help="write board, memory, runs and transcript exports into secretary-data",
    )
    export.add_argument("--instance", required=True)
    export.add_argument("--data-dir")
    export.add_argument(
        "--copy-transcripts",
        action="store_true",
        help="copy transcript files in addition to writing the inventory",
    )
    export.set_defaults(handler=run_data_export)

    export_board_command = data_subcommands.add_parser(
        "export-board",
        help="write secretary-data/board normalized cards",
    )
    export_board_command.add_argument("--instance", required=True)
    export_board_command.add_argument("--data-dir")
    export_board_command.set_defaults(handler=run_export_board)

    export_memory_command = data_subcommands.add_parser(
        "export-memory",
        help="write secretary-data/memory facts and export.ndjson",
    )
    export_memory_command.add_argument("--instance", required=True)
    export_memory_command.add_argument("--data-dir")
    export_memory_command.add_argument("--source-dir", default="/home/dev/panelmem-kb")
    export_memory_command.set_defaults(handler=run_export_memory)

    export_runs_command = data_subcommands.add_parser(
        "export-runs",
        help="write secretary-data/runs state exports",
    )
    export_runs_command.add_argument("--instance", required=True)
    export_runs_command.add_argument("--data-dir")
    export_runs_command.add_argument(
        "--state-dir",
        default="/home/dev/orca/workspaces/triggered-agents/pipeline/state",
    )
    export_runs_command.set_defaults(handler=run_export_runs)

    export_transcripts_command = data_subcommands.add_parser(
        "export-transcripts",
        help="write secretary-data/transcripts inventory",
    )
    export_transcripts_command.add_argument("--instance", required=True)
    export_transcripts_command.add_argument("--data-dir")
    export_transcripts_command.add_argument("--root", action="append", dest="roots")
    export_transcripts_command.add_argument("--copy", action="store_true")
    export_transcripts_command.set_defaults(handler=run_export_transcripts)

    export_artifacts_command = data_subcommands.add_parser(
        "export-artifacts",
        help="write secretary-data/artifacts inventory and task docs",
    )
    export_artifacts_command.add_argument("--instance", required=True)
    export_artifacts_command.add_argument("--data-dir")
    export_artifacts_command.set_defaults(handler=run_export_artifacts)
    data.set_defaults(handler=not_implemented("data"))

    backup = subparsers.add_parser("backup", help="create or verify encrypted backups")
    backup_subcommands = backup.add_subparsers(dest="backup_command")

    backup_create = backup_subcommands.add_parser("create")
    backup_create.add_argument("--instance", required=True)
    backup_create.add_argument("--data-dir")
    backup_create.add_argument("--age-recipient")
    backup_create.add_argument(
        "--kind",
        choices=("full", "core", "both"),
        default="full",
        help="archive kind to create",
    )
    backup_create.add_argument(
        "--copy-transcripts",
        action="store_true",
        help="copy transcript files in addition to writing the inventory",
    )
    backup_create.add_argument(
        "--no-copy-transcripts",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    backup_create.set_defaults(handler=run_backup_create)

    backup_verify = backup_subcommands.add_parser("verify")
    backup_verify.add_argument("archive")
    backup_verify.add_argument("--age-identity")
    backup_verify.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as findings",
    )
    backup_verify.set_defaults(handler=run_backup_verify)
    backup.set_defaults(handler=not_implemented("backup"))

    for name in ("restore",):
        command = subparsers.add_parser(name)
        command.add_argument("args", nargs="*")
        command.set_defaults(handler=not_implemented(name))

    project = subparsers.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command")
    project_add = project_subcommands.add_parser("add")
    project_add.add_argument("path_or_url")
    project_add.add_argument("--dry-run", action="store_true")
    project_add.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help="instance directory (default: SECRETARY_INSTANCE or /home/dev/secretary-instance)",
    )
    project_add.set_defaults(handler=run_project_add)
    provision_start = project_subcommands.add_parser("provision-start")
    provision_start.add_argument("project_id")
    provision_start.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help="instance directory (default: SECRETARY_INSTANCE or /home/dev/secretary-instance)",
    )
    provision_start.set_defaults(handler=run_project_provision_start)
    provision_apply = project_subcommands.add_parser("provision-apply")
    provision_apply.add_argument("project_id")
    provision_apply.add_argument("--result")
    provision_apply.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help="instance directory (default: SECRETARY_INSTANCE or /home/dev/secretary-instance)",
    )
    provision_apply.set_defaults(handler=run_project_provision_apply)
    gate = project_subcommands.add_parser("gate")
    gate.add_argument("project_id")
    gate.add_argument("--instance", default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE))
    gate.set_defaults(handler=run_project_gate)
    project.set_defaults(handler=not_implemented("project"))

    add_task_subcommands(subparsers)

    memory = subparsers.add_parser("memory", help="manage the memory journal")
    memory_subcommands = memory.add_subparsers(dest="memory_command")
    memory_import = memory_subcommands.add_parser(
        "import",
        help="seed or sync secretary-data/memory/facts from panelmem-kb",
    )
    memory_import.add_argument("--instance", required=True)
    memory_import.add_argument("--data-dir")
    memory_import.add_argument("--from", dest="source_dir", default="/home/dev/panelmem-kb")
    memory_import.set_defaults(handler=run_memory_import)

    memory_verify = memory_subcommands.add_parser(
        "verify",
        help="verify secretary-data/memory journal, export and index parity",
    )
    memory_verify.add_argument("--instance", required=True)
    memory_verify.add_argument("--data-dir")
    memory_verify.set_defaults(handler=run_memory_verify)

    memory_propose = memory_subcommands.add_parser("propose")
    add_memory_write_common(memory_propose)
    memory_propose.add_argument("--scope", required=True)
    memory_propose.add_argument("--slug", required=True)
    memory_propose.add_argument("--file", required=True)
    memory_propose.add_argument("--source")
    memory_propose.add_argument("--tags", default="")
    memory_propose.add_argument("--pinned", action="store_true")
    memory_propose.add_argument("--supersedes", default="")
    memory_propose.set_defaults(handler=run_memory_propose)

    memory_commit = memory_subcommands.add_parser("commit")
    add_memory_write_common(memory_commit)
    memory_commit.add_argument("--propose-id", required=True)
    memory_commit.set_defaults(handler=run_memory_commit)

    memory_supersede = memory_subcommands.add_parser("supersede")
    add_memory_write_common(memory_supersede)
    memory_supersede.add_argument("--scope", required=True)
    memory_supersede.add_argument("--slug", required=True)
    memory_supersede.add_argument("--file", required=True)
    memory_supersede.add_argument("--supersedes", required=True)
    memory_supersede.add_argument("--source")
    memory_supersede.add_argument("--tags", default="")
    memory_supersede.add_argument("--pinned", action="store_true")
    memory_supersede.set_defaults(handler=run_memory_supersede)
    memory.set_defaults(handler=not_implemented("memory"))

    return parser


def add_memory_write_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True)
    parser.add_argument("--data-dir")
    parser.add_argument("--actor", required=True)


def run_doctor(args: argparse.Namespace) -> int:
    instance_path = Path(args.instance)
    report = validate_instance(instance_path)

    if not report.ok:
        print(f"secretary doctor: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 1 if args.dry_run else 2

    print("Secretary doctor report")
    print("mode: dry-run" if args.dry_run else "mode: read-only")
    print(f"instance: {report.instance_path}")
    print(f"name: {report.name or 'unnamed'}")
    print(f"projects: {report.projects}")
    print(f"adapters: {report.adapters}")
    print(f"adapter drafts: {report.adapter_drafts}")
    print(f"data manifest: {'present' if report.has_manifest else 'absent'}")
    if report.manifest_path:
        print(f"data manifest path: {report.manifest_path}")
    if report.warnings:
        print(f"warnings: {len(report.warnings)}")
        for warning in report.warnings:
            print(f"  {warning}")

    offsite_warnings, offsite_findings = print_offsite_status(instance_path)
    backup_warnings = print_backup_status(report.instance_path)

    host_incomplete = False
    collected_host: CollectResult | None = None
    if not args.offline and (not args.dry_run or args.host or args.host_fixture):
        host_incomplete, collected_host = print_host_inventory(report, args)

    dispatcher_findings = print_dispatcher_status(report, collected_host, inspect_live=not args.offline)

    print("host changes: none")
    if host_incomplete:
        # A kind could not be inspected, so this is not a clean "all matched".
        print("status: host inventory incomplete")
        return 1 if args.dry_run else 2
    if dispatcher_findings:
        print("status: findings")
        return 1
    if offsite_findings:
        print("status: findings")
        return 1
    if (report.warnings or offsite_warnings or backup_warnings) and args.strict:
        print("status: warnings")
        return 1
    print("status: ok")
    return 0


def run_project_add(args: argparse.Namespace) -> int:
    code, artifact = project_add(args.path_or_url, args.instance, dry_run=args.dry_run)
    print(render_artifact(artifact), end="")
    return code


def run_project_provision_start(args: argparse.Namespace) -> int:
    code, result = start_provision(args.instance, args.project_id)
    print(render_result(result), end="")
    return code


def run_project_provision_apply(args: argparse.Namespace) -> int:
    code, result = apply_provision_result(args.instance, args.project_id, args.result)
    print(render_result(result), end="")
    return code


def run_project_gate(args: argparse.Namespace) -> int:
    code, result = run_gate(args.instance, args.project_id)
    print(render_result(result), end="")
    return code


def print_offsite_status(instance_path: Path) -> tuple[list[str], list[str]]:
    try:
        instance = load_config(_instance_path(str(instance_path)))
    except ConfigError:
        return [], []
    if not isinstance(instance, dict):
        return [], []

    status = check_last_fetch(instance)
    if status.warnings:
        print("offsite warnings:")
        for warning in status.warnings:
            print(f"  {warning}")
    if status.findings:
        print("offsite findings:")
        for finding in status.findings:
            print(f"  {finding}")
    return status.warnings, status.findings


def print_backup_status(instance_path: Path) -> list[str]:
    data_dir = _load_data_dir(_instance_path(str(instance_path)))
    if data_dir is None:
        return []
    status = check_backup_health(data_dir)
    if status.warnings:
        print("backup warnings:")
        for warning in status.warnings:
            print(f"  {warning}")
    return status.warnings


def print_dispatcher_status(
    report,
    collected_host: CollectResult | None,
    *,
    inspect_live: bool,
) -> bool:
    data_dir_value = report.instance.get("data_dir") if isinstance(report.instance, dict) else None
    if not isinstance(data_dir_value, str) or not data_dir_value:
        return False
    data_dir = Path(data_dir_value).expanduser()
    pilot = _load_dispatcher_state(data_dir / "dispatcher" / "pilot-state.json")
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    pilot_phase = str(pilot.get("phase") or "new")
    production_phase = str(production.get("phase") or "new")
    production_owner = str(production.get("owner") or "")
    cutover_committed = pilot_phase == "cutover_committed"

    if not (pilot or production or cutover_committed):
        return False

    if cutover_committed and production_owner:
        owner_state = "production-owner"
    elif pilot_phase == "new_pilot":
        owner_state = "pilot-only"
    else:
        owner_state = "legacy-owner"

    findings: list[str] = []
    print("")
    print("dispatcher ownership: read-only")
    print(f"  state: {owner_state}")
    print(f"  pilot phase: {pilot_phase}")
    print(f"  production phase: {production_phase}")
    print(f"  production owner: {production_owner or '(none)'}")

    if cutover_committed and inspect_live:
        legacy_pause = FileLegacyPauseProbe().snapshot()
        print(f"  legacy freeze: {'confirmed' if legacy_pause.sufficient else 'not confirmed'}")
        if production_owner and not legacy_pause.sufficient:
            findings.append("double owner: production owner exists while old dispatcher hard freeze is not confirmed")
        if not production_owner:
            findings.append("production owner fence is missing after cutover")
        findings.extend(_production_host_findings(report, data_dir, collected_host))
    elif cutover_committed:
        print("  legacy freeze: not inspected")

    if findings:
        print("dispatcher findings:")
        for finding in findings:
            print(f"  {finding}")
    return bool(findings)


def _load_dispatcher_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _production_host_findings(report, data_dir: Path, collected_host: CollectResult | None) -> list[str]:
    if collected_host is None or collected_host.errors.get("units"):
        return []
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    prefix = prefix if isinstance(prefix, str) else ""
    desired = build_plan(report.instance, report.bindings)
    managed = load_managed_manifest(data_dir / "host-managed.json")
    changes = plan_changes(desired, collected_host.inventory, managed, prefix)
    findings = []
    for change in changes:
        if not change.logical_id.startswith("systemd:dispatcher:production"):
            continue
        if change.action == "unchanged":
            continue
        findings.append(
            f"production dispatcher managed unit mismatch: {change.action} {change.name}"
        )
    return findings


def run_data_init(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=False)
    if data_dir is None:
        return 1

    try:
        layout = init_layout(data_dir)
    except RuntimeError as exc:
        print(f"secretary data init: {exc}")
        return 1

    manifest = load_config(layout.manifest_path)
    errors = validate(manifest, "data-manifest", layout.manifest_path.name)
    if errors:
        print(f"secretary data init: generated invalid data manifest at {layout.manifest_path}")
        for error in errors:
            print(f"  {error}")
        return 1

    print(f"secretary-data: {layout.data_dir}")
    print(f"manifest: {layout.manifest_path}")
    print(f"created directories: {_join([str(p.relative_to(layout.data_dir)) for p in layout.created_dirs])}")
    print("status: ok")
    return 0


def run_raw_kanboard_dump(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1

    try:
        dump = raw_kanboard_dump(
            data_dir,
            container=args.container,
            source_path=args.source_path,
        )
    except RuntimeError as exc:
        print(f"secretary data raw-kanboard-dump: {exc}")
        return 1

    print(f"kanboard raw dump: {dump.dump_dir}")
    print(f"source: {dump.source}")
    print("status: ok")
    return 0


def run_data_export(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1

    try:
        exports = export_all(data_dir, copy_transcripts=args.copy_transcripts)
    except RuntimeError as exc:
        print(f"secretary data export: {exc}")
        return 1

    for name in ("board", "memory", "runs", "transcripts", "artifacts"):
        result = exports[name]
        print(f"{name}: {result.count} -> {result.path}")
    print("status: ok")
    return 0


def run_export_board(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_board(data_dir)
    except RuntimeError as exc:
        print(f"secretary data export-board: {exc}")
        return 1
    print(f"board cards: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_export_memory(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_memory(data_dir, source_dir=Path(args.source_dir))
    except RuntimeError as exc:
        print(f"secretary data export-memory: {exc}")
        return 1
    print(f"memory facts: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_memory_import(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = import_memory_journal(data_dir, source_dir=Path(args.source_dir))
    except RuntimeError as exc:
        print(f"secretary memory import: {exc}")
        return 1
    print(f"memory facts: {result.count}")
    print(f"journal: {result.facts_dir}")
    print(f"source: {result.source}")
    print(f"source head: {result.source_head}")
    print(f"journal commit: {result.commit or '(none)'}")
    print(f"changed: {'yes' if result.changed else 'no'}")
    print("status: ok")
    return 0


def run_memory_verify(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = verify_memory_journal(data_dir)
    except RuntimeError as exc:
        print(f"secretary memory verify: {exc}")
        return 1
    print(f"journal: {result.facts_dir}")
    print(f"journal commit: {result.journal_commit or '(none)'}")
    print(f"memory facts: {result.fact_count}")
    export_count = result.export_count if result.export_count is not None else "(missing)"
    index_count = result.index_count if result.index_count is not None else "(missing)"
    print(f"export facts: {export_count}")
    print(f"index facts: {index_count}")
    print(f"journal dirty: {'yes' if result.dirty else 'no'}")
    if result.findings:
        print("findings:")
        for finding in result.findings:
            print(f"  {finding}")
    print(f"status: {'ok' if result.ok else 'failed'}")
    return 0 if result.ok else 1


def run_memory_propose(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = propose_memory_fact(
            data_dir,
            actor=args.actor,
            scope=args.scope,
            slug=args.slug,
            fact_file=Path(args.file),
            source=args.source,
            tags=_split_csv(args.tags),
            pinned=args.pinned,
            supersedes=_split_csv(args.supersedes),
        )
    except Exception as exc:
        return _print_memory_error("propose", exc)
    _print_json(
        {
            "ok": True,
            "op": "propose",
            "propose_id": result.propose_id,
            "proposal": str(result.path),
            "fact": f"{result.scope_dir}/{result.slug}",
            "actor": result.actor,
            "source": result.source,
            "supersedes": list(result.supersedes),
        }
    )
    return 0


def run_memory_commit(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = commit_memory_proposal(
            data_dir,
            actor=args.actor,
            propose_id=args.propose_id,
        )
    except Exception as exc:
        return _print_memory_error("commit", exc)
    _print_memory_write_result(result)
    return 0


def run_memory_supersede(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = supersede_memory_fact(
            data_dir,
            actor=args.actor,
            scope=args.scope,
            slug=args.slug,
            fact_file=Path(args.file),
            supersedes=_split_csv(args.supersedes),
            source=args.source,
            tags=_split_csv(args.tags),
            pinned=args.pinned,
        )
    except Exception as exc:
        return _print_memory_error("supersede", exc)
    _print_memory_write_result(result)
    return 0


def _print_memory_write_result(result) -> None:
    _print_json(
        {
            "ok": True,
            "op": result.op,
            "commit": result.commit,
            "journal": str(result.facts_dir),
            "fact": result.fact,
            "actor": result.actor,
            "source": result.source,
            "changed_facts": list(result.changed_facts),
            "propose_id": result.propose_id,
        }
    )


def _print_memory_error(op: str, exc: Exception) -> int:
    if isinstance(exc, MemoryExportPublishError):
        result = exc.result
        _print_json(
            {
                "ok": False,
                "op": op,
                "error": "export",
                "message": str(exc),
                "commit": result.commit,
                "journal": str(result.facts_dir),
                "fact": result.fact,
                "actor": result.actor,
                "source": result.source,
                "changed_facts": list(result.changed_facts),
                "propose_id": result.propose_id,
            }
        )
        return 1
    if isinstance(exc, MemoryValidationError):
        code = MEMORY_EXIT_VALIDATION
        kind = "validation"
    elif isinstance(exc, MemoryPermissionError):
        code = MEMORY_EXIT_PERMISSION
        kind = "permission"
    elif isinstance(exc, MemoryLockError):
        code = MEMORY_EXIT_LOCKED
        kind = "locked"
    else:
        code = 1
        kind = "runtime"
    _print_json({"ok": False, "op": op, "error": kind, "message": str(exc)})
    return code


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def run_export_runs(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_runs(data_dir, state_dir=Path(args.state_dir))
    except RuntimeError as exc:
        print(f"secretary data export-runs: {exc}")
        return 1
    print(f"run records: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_export_transcripts(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    roots = [Path(root) for root in args.roots] if args.roots else None
    try:
        result = export_transcripts(data_dir, roots=roots, copy=args.copy)
    except RuntimeError as exc:
        print(f"secretary data export-transcripts: {exc}")
        return 1
    print(f"transcripts: {result.count}")
    print(f"inventory: {result.path}")
    print("status: ok")
    return 0


def run_export_artifacts(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = export_artifacts(data_dir)
    except RuntimeError as exc:
        print(f"secretary data export-artifacts: {exc}")
        return 1
    print(f"artifacts: {result.count}")
    print(f"inventory: {result.path}")
    print("status: ok")
    return 0


def run_backup_create(args: argparse.Namespace) -> int:
    kinds = ("core", "full") if args.kind == "both" else (args.kind,)
    try:
        results = create_backups(
            Path(args.instance),
            data_dir=Path(args.data_dir) if args.data_dir else None,
            recipient=args.age_recipient,
            copy_transcripts=args.copy_transcripts,
            backup_kinds=kinds,
        )
    except RuntimeError as exc:
        print(f"secretary backup create: {exc}")
        return 1

    for result in results:
        print(f"archive: {result.archive}")
        print(f"kind: {result.manifest.get('backup_kind', 'full')}")
        print(f"version: {result.manifest['version']}")
    print("status: ok")
    return 0


def run_backup_verify(args: argparse.Namespace) -> int:
    identity = args.age_identity or os.environ.get("SECRETARY_AGE_IDENTITY")
    result = verify_backup(
        Path(args.archive),
        identity=Path(identity).expanduser() if identity else None,
    )
    print(f"archive: {args.archive}")
    if result.manifest:
        print(f"kind: {result.manifest.get('backup_kind', 'full')}")
        print(f"version: {result.manifest.get('version', '(unknown)')}")
    if result.warnings:
        print(f"warnings: {len(result.warnings)}")
        for warning in result.warnings:
            print(f"  {warning}")
    findings = list(result.findings)
    if args.strict:
        findings.extend(result.warnings)
    if findings:
        print(f"findings: {len(findings)}")
        for finding in findings:
            print(f"  {finding}")
    if result.code == 2:
        print("status: unavailable")
        return 2
    if findings:
        print("status: findings")
        return 1
    print("status: ok")
    return 0


def print_host_inventory(report, args: argparse.Namespace) -> tuple[bool, CollectResult]:
    """Print the read-only host inventory: matched / missing / unmanaged per kind.

    Returns True if any kind could not be inspected (reported as unavailable).
    """
    if args.host_fixture:
        source = FixtureHostSource(Path(args.host_fixture))
    else:
        source = LiveHostSource()

    expected = build_expectations(report.bindings, report.host)
    collected = source.collect(expected)
    diffs = inventory(expected, collected.inventory)

    print("")
    print("host inventory: read-only")
    for kind in ("projects", "units", "orca repos"):
        reason = collected.errors.get(kind)
        if reason:
            print(f"{kind}:")
            print(f"  unavailable: {reason}")
        else:
            _print_kind(kind, diffs[kind])
    return bool(collected.errors), collected


def _print_kind(kind: str, diff: KindDiff) -> None:
    print(f"{kind}:")
    print(f"  matched: {_join(diff.matched)}")
    print(f"  missing-on-host: {_join(diff.missing_on_host)}")
    print(f"  unmanaged-on-host: {_join(diff.unmanaged_on_host)}")


def _join(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"


def _instance_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _data_dir_from_args(args: argparse.Namespace, *, validate_tree: bool) -> Path | None:
    if args.data_dir:
        return Path(args.data_dir).expanduser()

    if validate_tree:
        report = validate_instance(_instance_path(args.instance))
        if report.errors:
            print(f"secretary data: {len(report.errors)} config problem(s):")
            for error in report.errors:
                print(f"  {error}")
            return None

    data_dir = _load_data_dir(_instance_path(args.instance))
    if data_dir is None:
        print("secretary data: instance.yaml has no usable data_dir")
        return None
    return data_dir


def _load_data_dir(instance_path: Path) -> Path | None:
    from secretary.config import ConfigError

    try:
        instance = load_config(instance_path)
    except ConfigError:
        return None
    if not isinstance(instance, dict):
        return None
    data_dir = instance.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        return None
    return Path(data_dir).expanduser()


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"secretary {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler
