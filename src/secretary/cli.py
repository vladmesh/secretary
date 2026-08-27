from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from secretary.backup import create_backups, verify_backup
from secretary.board_transport import findings as _board_transport_findings
from secretary.check_commands import add_check_subcommands
from secretary.checkpoint import (
    PUSH_INTERVAL_SECONDS,
    checkpoint_snapshot,
    render_checkpoint_lines,
)
from secretary.config import DataDirError, instance_data_dir, load_config, validate, validate_instance
from secretary.data import (
    KANBOARD_DATA_PATH,
    export_all,
    export_artifacts,
    export_board,
    export_memory,
    export_runs,
    export_transcripts,
    init_layout,
    raw_kanboard_dump,
)
from secretary.dispatcher_commands import (
    add_dispatcher_subcommands,
    add_head_status_command,
    add_pause_commands,
)
from secretary.dispatcher_pause import ProductionPause
from secretary.gate import run_gate
from secretary.head_health import (
    PROBE_BROKEN,
    PROBE_TTL_SECONDS,
    HeadHealth,
    HeadReadiness,
    run_probe,
)
from secretary.head_registry import HeadRegistryConfigError, installed_heads
from secretary.host import (
    CollectResult,
    FixtureHostSource,
    KindDiff,
    LiveHostSource,
    build_doctor_expectations,
    build_plan,
    inventory,
    load_managed_manifest,
    plan_changes,
)
from secretary.host_apply import resolve_installed_packaged
from secretary.host_commands import add_reconcile_subcommands
from secretary.installation import add_install_commands
from secretary.knowledge_write import (
    KnowledgeError,
    KnowledgeValidationError,
    list_knowledge_documents,
    write_knowledge_document,
)
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
from secretary.onboarding import DEFAULT_INSTANCE, project_add, render_artifact
from secretary.product_issue_commands import add_product_issue_subcommands
from secretary.provision import apply_provision_result, render_result, start_provision
from secretary.restore import RestoreError, _target, restore_findings
from secretary.restore_commands import add_restore_subcommands, run_memory_reindex
from secretary.role_skills import add_role_skills_subcommands
from secretary.secret_commands import add_secret_subcommands
from secretary.secret_store import store_findings as _secret_store_findings
from secretary.session import run_shell
from secretary.sprint_commands import add_sprint_subcommands
from secretary.state_repo import StateRepoError
from secretary.status import collect_status
from secretary.task_commands import add_task_subcommands
from secretary.upgrade import add_upgrade_command

PUSH_INTERVAL_MINUTES = int(PUSH_INTERVAL_SECONDS // 60)
NOT_IMPLEMENTED = "not implemented in Phase 1 skeleton"
MEMORY_EXIT_VALIDATION = 2
MEMORY_EXIT_PERMISSION = 3
MEMORY_EXIT_LOCKED = 4


@dataclass
class DoctorInspection:
    """Single read-only evaluation shared by doctor renderers."""

    findings: list[dict[str, object]]
    unavailable: bool
    restore: list[str]
    dispatcher: list[str]
    checkpoint: list[str]
    secret_store: list[str]
    board_transport: list[str]
    resource_probes: list[HeadReadiness]
    expected: object | None = None
    collected: CollectResult | None = None
    diffs: dict[str, KindDiff] | None = None


class StructuredArgumentParser(argparse.ArgumentParser):
    """Keep public command validation in the same JSON envelope as handlers."""

    def error(self, message: str) -> None:
        self.exit(2, json.dumps({"error": {"code": "usage", "message": message}}) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = StructuredArgumentParser(prog="secretary")
    subparsers = parser.add_subparsers(dest="command")
    add_dispatcher_subcommands(subparsers)
    add_pause_commands(subparsers)
    add_head_status_command(subparsers)
    add_check_subcommands(subparsers)

    doctor = subparsers.add_parser("doctor", help="inspect an instance without changing the host")
    doctor.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    _add_instance(doctor, help="path to an instance dir or instance.yaml")
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
    doctor.add_argument("--json", action="store_true", help="print structured findings")
    doctor.set_defaults(handler=run_doctor)

    status = subparsers.add_parser("status", help="show the current installation state")
    _add_instance(status, help="path to an instance dir or instance.yaml")
    status.add_argument("--json", action="store_true", help="print the stable JSON status schema")
    status.add_argument("--offline", action="store_true", help="do not inspect the live host")
    status.add_argument("--host-fixture", metavar="DIR", help="read a fixture host inventory")
    status.set_defaults(handler=run_status)

    add_upgrade_command(subparsers)
    add_install_commands(subparsers)
    add_role_skills_subcommands(subparsers)
    add_sprint_subcommands(subparsers)

    reconcile = subparsers.add_parser("reconcile", help="render or apply the host plan")
    reconcile_subcommands = reconcile.add_subparsers(dest="reconcile_command")
    add_reconcile_subcommands(reconcile_subcommands)
    reconcile.set_defaults(handler=not_implemented("reconcile"))

    data = subparsers.add_parser("data", help="manage the secretary-data layout")
    data_subcommands = data.add_subparsers(dest="data_command")

    data_init = data_subcommands.add_parser("init", help="create secretary-data and its manifest")
    _add_instance(
        data_init,
        data_dir=True,
        help="path to an instance dir or instance.yaml",
        data_dir_help="override instance.yaml data_dir",
    )
    data_init.set_defaults(handler=run_data_init)

    raw_dump = data_subcommands.add_parser(
        "raw-kanboard-dump",
        help="copy the live Kanboard storage into secretary-data/board",
    )
    _add_instance(
        raw_dump,
        data_dir=True,
        help="path to an instance dir or instance.yaml",
        data_dir_help="override instance.yaml data_dir",
    )
    raw_dump.add_argument("--container", default="cp-kanboard")
    raw_dump.add_argument("--source-path", default=KANBOARD_DATA_PATH)
    raw_dump.set_defaults(handler=run_raw_kanboard_dump)

    export = data_subcommands.add_parser(
        "export",
        help="write board, memory, runs and transcript exports into secretary-data",
    )
    _add_instance(export, data_dir=True)
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
    _add_instance(export_board_command, data_dir=True)
    export_board_command.set_defaults(handler=run_export_board)

    export_memory_command = data_subcommands.add_parser(
        "export-memory",
        help="write secretary-data/memory facts and export.ndjson",
    )
    _add_instance(export_memory_command, data_dir=True)
    export_memory_command.set_defaults(handler=run_export_memory)

    export_runs_command = data_subcommands.add_parser(
        "export-runs",
        help="write secretary-data/runs state exports",
    )
    _add_instance(export_runs_command, data_dir=True)
    export_runs_command.add_argument(
        "--state-dir",
        default=str(Path.home() / "orca" / "workspaces" / "secretary" / "pipeline" / "state" / "pipeline"),
    )
    export_runs_command.set_defaults(handler=run_export_runs)

    export_transcripts_command = data_subcommands.add_parser(
        "export-transcripts",
        help="write secretary-data/transcripts inventory",
    )
    _add_instance(export_transcripts_command, data_dir=True)
    export_transcripts_command.add_argument("--root", action="append", dest="roots")
    export_transcripts_command.add_argument("--copy", action="store_true")
    export_transcripts_command.set_defaults(handler=run_export_transcripts)

    export_artifacts_command = data_subcommands.add_parser(
        "export-artifacts",
        help="write secretary-data/artifacts inventory and task docs",
    )
    _add_instance(export_artifacts_command, data_dir=True)
    export_artifacts_command.set_defaults(handler=run_export_artifacts)
    data.set_defaults(handler=not_implemented("data"))

    backup = subparsers.add_parser("backup", help="create or verify backups")
    backup_subcommands = backup.add_subparsers(dest="backup_command")

    backup_create = backup_subcommands.add_parser("create")
    _add_instance(backup_create, data_dir=True)
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
    backup_verify.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as findings",
    )
    backup_verify.set_defaults(handler=run_backup_verify)
    backup.set_defaults(handler=not_implemented("backup"))

    add_restore_subcommands(subparsers)

    project = subparsers.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command")
    project_add = project_subcommands.add_parser("add")
    project_add.add_argument("path_or_url")
    project_add.add_argument("--dry-run", action="store_true")
    project_add.add_argument(
        "--re-onboard",
        action="store_true",
        help=(
            "take an enabled binding back down to a disabled draft: keep plane, policy, remote "
            "and orca_binding, drop the canonical adapter, and require provision and gate again"
        ),
    )
    _add_env_instance(
        project_add,
        help=f"instance directory (default: SECRETARY_INSTANCE or {DEFAULT_INSTANCE})",
    )
    project_add.set_defaults(handler=run_project_add)
    provision_start = project_subcommands.add_parser("provision-start")
    provision_start.add_argument("project_id")
    _add_env_instance(
        provision_start,
        help=f"instance directory (default: SECRETARY_INSTANCE or {DEFAULT_INSTANCE})",
    )
    provision_start.set_defaults(handler=run_project_provision_start)
    provision_apply = project_subcommands.add_parser("provision-apply")
    provision_apply.add_argument("project_id")
    provision_apply.add_argument("--result")
    _add_env_instance(
        provision_apply,
        help=f"instance directory (default: SECRETARY_INSTANCE or {DEFAULT_INSTANCE})",
    )
    provision_apply.set_defaults(handler=run_project_provision_apply)
    gate = project_subcommands.add_parser("gate")
    gate.add_argument("project_id")
    _add_env_instance(gate)
    gate.set_defaults(handler=run_project_gate)
    project.set_defaults(handler=not_implemented("project"))

    add_task_subcommands(subparsers)
    add_product_issue_subcommands(subparsers)

    shell = subparsers.add_parser(
        "shell",
        help="launch an interactive secretary head with the full runtime env",
    )
    shell.add_argument(
        "--head",
        "-H",
        default=None,
        help="head profile or adapter (claude/codex/hermes or any heads.toml profile id); "
        "default claude-default",
    )
    shell.add_argument(
        "--workspace",
        default=None,
        help="workspace dir for codex directory trust (default: current dir)",
    )
    shell.add_argument(
        "--env-file",
        default=None,
        help="runtime env file to load (default: instance runtime.env)",
    )
    shell.add_argument(
        "--print",
        dest="print_command",
        action="store_true",
        help="print the resolved launch command and exit without starting the head",
    )
    shell.set_defaults(handler=run_shell)

    memory = subparsers.add_parser("memory", help="manage the memory journal")
    memory_subcommands = memory.add_subparsers(dest="memory_command")
    memory_verify = memory_subcommands.add_parser(
        "verify",
        help="verify instance memory canon, derived export and index parity",
    )
    _add_instance(memory_verify, data_dir=True)
    memory_verify.set_defaults(handler=run_memory_verify)

    memory_reindex = memory_subcommands.add_parser(
        "reindex", help="rebuild the derived memory index from the local journal"
    )
    _add_instance(memory_reindex)
    memory_reindex.set_defaults(handler=run_memory_reindex)

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

    knowledge = subparsers.add_parser(
        "knowledge", help="write long recoverable documents into state/knowledge"
    )
    knowledge_subcommands = knowledge.add_subparsers(dest="knowledge_command")
    knowledge_write = knowledge_subcommands.add_parser(
        "write",
        help="commit one markdown document into state/knowledge under the writer lock",
    )
    _add_instance(knowledge_write)
    knowledge_write.add_argument("--actor", required=True)
    knowledge_write.add_argument("--path", required=True, help="document path relative to state/knowledge")
    knowledge_write.add_argument("--file", required=True, help="source markdown file")
    knowledge_write.add_argument("--message", help="commit subject; defaults to the document path")
    knowledge_write.set_defaults(handler=run_knowledge_write)

    knowledge_list = knowledge_subcommands.add_parser(
        "list", help="list documents currently in state/knowledge"
    )
    _add_instance(knowledge_list)
    knowledge_list.set_defaults(handler=run_knowledge_list)
    knowledge.set_defaults(handler=not_implemented("knowledge"))

    add_secret_subcommands(subparsers)

    return parser


def add_memory_write_common(parser: argparse.ArgumentParser) -> None:
    _add_instance(parser, data_dir=True)
    parser.add_argument("--actor", required=True)


def _add_instance(
    parser: argparse.ArgumentParser,
    *,
    data_dir: bool = False,
    help: str | None = None,
    data_dir_help: str | None = None,
) -> None:
    parser.add_argument("--instance", required=True, help=help)
    if data_dir:
        parser.add_argument("--data-dir", help=data_dir_help)


def _add_env_instance(parser: argparse.ArgumentParser, *, help: str | None = None) -> None:
    parser.add_argument(
        "--instance",
        default=os.environ.get("SECRETARY_INSTANCE", DEFAULT_INSTANCE),
        help=help,
    )


def run_doctor(args: argparse.Namespace) -> int:
    instance_path = Path(args.instance)
    report = validate_instance(instance_path)

    if args.json:
        return run_doctor_json(args, report)

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
    cache_dir = _memory_cache_dir(report)
    print(f"memory model cache: {cache_dir}")
    if _is_temporary_directory(cache_dir):
        print("warning: memory model cache is in a temporary directory and can be cleaned unexpectedly")
    if report.warnings:
        print(f"warnings: {len(report.warnings)}")
        for warning in report.warnings:
            print(f"  {warning}")

    inspection = collect_doctor_inspection(report, args)
    print_restore_status(report, findings=inspection.restore)

    inspect_host = inspection.collected is not None
    if inspect_host:
        print_host_inventory(
            report, args, expected=inspection.expected, collected=inspection.collected, diffs=inspection.diffs
        )
        _print_external_orca_runtime(inspection.expected, inspection.collected)

    print_background_automations(inspect=inspect_host)

    print_dispatcher_status(
        report, inspection.collected, inspect_live=not args.offline, findings=inspection.dispatcher
    )
    print_resource_probes(inspection.resource_probes)
    print_checkpoint_status(report, findings=inspection.checkpoint)
    print_secret_store_status(report, findings=inspection.secret_store)
    if inspection.board_transport:
        print("board transport findings:")
        for finding in inspection.board_transport:
            print(f"  {finding}")

    print("host changes: none")
    if inspection.unavailable:
        # A kind could not be inspected, so this is not a clean "all matched".
        print("status: host inventory incomplete")
        return 2
    if inspection.findings:
        warning_only = all(finding["code"] == "config_warning" for finding in inspection.findings)
        print("status: warnings" if warning_only else "status: findings")
        return 1
    print("status: ok")
    return 0


def _memory_cache_dir(report) -> Path:
    """Return the product-owned persistent fastembed cache location."""
    assert report.data_dir is not None
    return report.data_dir / "memory" / "fastembed-cache"


def _is_temporary_directory(path: Path) -> bool:
    """Whether a cache path sits below a system temporary directory."""
    resolved = path.resolve(strict=False)
    for temporary in (Path("/tmp"), Path("/var/tmp")):
        try:
            resolved.relative_to(temporary)
            return True
        except ValueError:
            pass
    return False


def run_status(args: argparse.Namespace) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        payload = {
            "schema_version": 1,
            "error": "invalid_instance",
            "findings": [str(error) for error in report.errors],
        }
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print("secretary status: invalid instance config")
        return 2
    snapshot = collect_status(report, host_fixture=args.host_fixture, offline=args.offline)
    if args.json:
        print(json.dumps(snapshot, sort_keys=True))
        return 0
    print(f"Secretary status: {snapshot['installation']['name'] or 'unnamed'}")
    print(f"active attempts: {len(snapshot['dispatcher']['active_attempts'])}")
    canon = snapshot["installation"]["head_registry"]
    if canon["error"]:
        print(f"head registry: {canon['error']}")
    else:
        owner = canon["canonical_owner"] or "unknown"
        print(
            f"head registry: {canon['canonical']} ({owner}-owned), "
            f"built from {canon['product_root']} @ {canon['revision']}"
        )
    observers = snapshot["dispatcher"]["observers"]
    live = sum(1 for observer in observers if observer["alive"])
    print(f"sprint observers: {live} live of {len(observers)}")
    sprint_status = snapshot["installation"]["sprints"]
    if sprint_status["error"]:
        print(f"sprints: unavailable ({sprint_status['error']['message']})")
    else:
        stopped = sum(sprint["status"] == "stopped" for sprint in sprint_status["items"])
        stale = sum(not sprint["resume_freshness"]["fresh"] for sprint in sprint_status["items"])
        print(f"sprints: {len(sprint_status['items'])}, {stopped} stopped, {stale} resume errors")
    print(
        f"memory facts: {snapshot['memory']['fact_count'] if snapshot['memory']['fact_count'] is not None else 'unknown'}"
    )
    print(f"checkpoint lag: {snapshot['checkpoint']['lag_minutes']} min")
    return 0


def run_doctor_json(args: argparse.Namespace, report) -> int:
    """Structured counterpart to doctor without changing its default transcript."""
    if not report.ok:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": False,
                    "findings": [
                        {"code": "config_invalid", "message": str(error)} for error in report.errors
                    ],
                },
                sort_keys=True,
            )
        )
        return 1 if args.dry_run else 2
    snapshot = collect_status(report, host_fixture=args.host_fixture, offline=args.offline)
    inspection = collect_doctor_inspection(report, args)
    payload = {
        "schema_version": 1,
        "ok": not inspection.findings,
        "findings": inspection.findings,
        "status": snapshot,
    }
    print(json.dumps(payload, sort_keys=True))
    if inspection.unavailable:
        return 2
    return 1 if inspection.findings else 0


def collect_doctor_inspection(report, args: argparse.Namespace) -> DoctorInspection:
    """Collect invariant failures once for the text and JSON doctor renderers."""
    findings: list[dict[str, object]] = []
    restore = _restore_findings(report)
    findings.extend({"code": "restore_problem", "message": finding} for finding in restore)
    inspect_host = not args.offline and (not args.dry_run or args.host or args.host_fixture)
    collected: CollectResult | None = None
    expected = None
    diffs = None
    unavailable = False
    if inspect_host:
        expected, collected, diffs = collect_host_inventory(report, args)
        for kind, reason in collected.errors.items():
            findings.append({"code": "host_inventory_unavailable", "kind": kind, "message": reason})
        unavailable = bool(collected.errors)
        for kind, diff in diffs.items():
            if kind in collected.errors:
                continue
            findings.extend(
                {"code": "missing_on_host", "kind": kind, "name": name} for name in diff.missing_on_host
            )
            findings.extend(
                {"code": "unmanaged_on_host", "kind": kind, "name": name} for name in diff.unmanaged_on_host
            )
        findings.extend(
            {"code": "unit_runtime", "message": finding}
            for finding in _unit_runtime_findings(expected, collected)
        )
    dispatcher = dispatcher_findings(report, collected, inspect_live=not args.offline)
    checkpoint = checkpoint_findings(report)
    secret_store = secret_store_findings(report)
    board_transport = _board_transport_findings(report.instance_path.parent)
    resource_probes = resource_probe_readiness(report, inspect_live=not args.offline)
    findings.extend({"code": "dispatcher", "message": finding} for finding in dispatcher)
    findings.extend({"code": "checkpoint", "message": finding} for finding in checkpoint)
    findings.extend({"code": "secret_store", "message": finding} for finding in secret_store)
    findings.extend({"code": "board_transport", "message": finding} for finding in board_transport)
    findings.extend(
        {"code": "resource_probe", "resource": readiness.resource, "message": _probe_finding(readiness)}
        for readiness in resource_probes
        if readiness.status == PROBE_BROKEN
    )
    if args.strict:
        findings.extend({"code": "config_warning", "message": str(warning)} for warning in report.warnings)
    return DoctorInspection(
        findings,
        unavailable,
        restore,
        dispatcher,
        checkpoint,
        secret_store,
        board_transport,
        resource_probes,
        expected,
        collected,
        diffs,
    )


def print_restore_status(report, *, findings: list[str] | None = None) -> list[str]:
    findings = _restore_findings(report) if findings is None else findings
    if findings:
        print("restore findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def _restore_findings(report) -> list[str]:
    if report.data_dir is None:
        return []
    try:
        _, data_dir, _ = _target(report.instance_path)
    except RestoreError:
        return []
    if not (data_dir / "restore-state.json").is_file():
        return []
    return restore_findings(data_dir)


def run_project_add(args: argparse.Namespace) -> int:
    code, artifact = project_add(
        args.path_or_url, args.instance, dry_run=args.dry_run, re_onboard=args.re_onboard
    )
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


def print_dispatcher_status(
    report,
    collected_host: CollectResult | None,
    *,
    inspect_live: bool,
    findings: list[str] | None = None,
) -> bool:
    if report.data_dir is None:
        return False
    data_dir = report.data_dir
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    production_phase = str(production.get("phase") or "new")
    production_owner = str(production.get("owner") or "")

    if not production:
        return False

    owner_state = "production-owner" if production_owner else "unowned"

    findings = (
        dispatcher_findings(report, collected_host, inspect_live=inspect_live)
        if findings is None
        else findings
    )
    print()
    print("dispatcher ownership: read-only")
    print(f"  state: {owner_state}")
    print(f"  production phase: {production_phase}")
    print(f"  production owner: {production_owner or '(none)'}")
    pause = ProductionPause(data_dir).summary()
    if pause.get("paused"):
        print(
            f"  pause: {pause['mode']} since {pause.get('since') or '(unknown)'} by {pause.get('actor') or '(unknown)'}"
        )
    else:
        print("  pause: none")

    if findings:
        print("dispatcher findings:")
        for finding in findings:
            print(f"  {finding}")
    return bool(findings)


def dispatcher_findings(report, collected_host: CollectResult | None, *, inspect_live: bool) -> list[str]:
    if report.data_dir is None:
        return []
    data_dir = report.data_dir
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    if not production:
        return []
    # Unresolved divergences are read from the state snapshot itself, not the live host, so they
    # surface under --offline too: an operator diagnosing a broken host still needs to see them.
    findings: list[str] = _divergence_findings(production)
    if not inspect_live:
        return findings
    if not str(production.get("owner") or ""):
        findings.append("production owner fence is missing")
    findings.extend(_production_host_findings(report, data_dir, collected_host))
    return findings


def _divergence_findings(production: dict[str, object]) -> list[str]:
    """Every controlled divergence still open in the production state snapshot.

    Reconciliation (`secretary/dispatcher_production.py`) closes a divergence once its card leaves
    the active dispatcher cycle, so one still open here is either tied to a card still in flight or
    is genuinely stuck and needs an operator.
    """
    raw = production.get("controlled_divergences")
    items = raw if isinstance(raw, list) else []
    findings: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") == "closed":
            continue
        ref = str(item.get("pilot_ref") or "?")
        reason = str(item.get("reason") or "unknown")
        divergence_id = str(item.get("id") or "?")
        findings.append(f"unresolved controlled divergence {divergence_id}: ref={ref} reason={reason}")
    return findings


def resource_probe_readiness(report, *, inspect_live: bool) -> list[HeadReadiness]:
    """One verdict per resource this installation's head registry describes a probe for.

    `secretary doctor` asks a different question from the tick. The tick asks whether a claim may
    be launched; doctor asks whether the gate that answers that is working at all — because a probe
    that cannot be launched used to be indistinguishable from a resource nobody had an opinion
    about, and the claims then went through ungated and silently (`issue:6cfbbb9b`, P0).

    Read-only in both directions: a fresh verdict the dispatcher already wrote is reused rather than
    re-probed (probes cost real provider tokens), a stale or absent one is probed here without ever
    writing the dispatcher's TTL cache, and `--offline` reports only what is recorded, since a probe
    cannot be run without reaching the provider. An installation with no head snapshot yet has no
    resources to report on, and a broken snapshot is already named by `secretary status`.
    """
    if report.data_dir is None:
        return []
    instance_dir = report.instance_path.parent
    try:
        resources = installed_heads(instance_dir).get("resources", {})
    except HeadRegistryConfigError:
        return []
    if not isinstance(resources, dict):
        return []
    recorded = HeadHealth(None, report.data_dir).snapshot()
    now = time.time()
    verdicts: list[HeadReadiness] = []
    for name in sorted(resources):
        entry = resources[name] if isinstance(resources[name], dict) else {}
        probe = str(entry.get("probe") or "")
        if not probe:
            continue
        cached = _recorded_readiness(str(name), recorded, now)
        if cached is not None:
            verdicts.append(cached)
        elif inspect_live:
            verdicts.append(run_probe(str(name), probe, now))
    return verdicts


def _recorded_readiness(resource: str, recorded: dict[str, object], now: float) -> HeadReadiness | None:
    """The dispatcher's own verdict for this resource while it is still inside the probe TTL."""
    entry = recorded.get(resource)
    if not isinstance(entry, dict):
        return None
    try:
        checked_at = float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return None
    if now - checked_at >= PROBE_TTL_SECONDS:
        return None
    return HeadReadiness(
        resource, str(entry.get("status") or "unknown"), str(entry.get("reason") or ""), checked_at, True
    )


def _probe_finding(readiness: HeadReadiness) -> str:
    """Name a broken probe as the gating failure it is, not as a red resource.

    A red resource is an ordinary operational fact and is printed above without becoming a finding.
    This one is a defect of the installation: while it lasts, every claim on this resource was
    allowed without the health gate ever having an opinion.
    """
    return (
        f"resource {readiness.resource} probe cannot run ({readiness.reason}); "
        "claims on this resource are not gated by health until it is repaired"
    )


def print_resource_probes(readiness: list[HeadReadiness]) -> list[HeadReadiness]:
    """The probe of every resource, and separately the ones that could not be run at all."""
    if not readiness:
        return readiness
    print()
    print("resource probes: read-only")
    for verdict in readiness:
        age = " (recorded)" if verdict.cached else ""
        print(f"  {verdict.resource}: {verdict.status}{age} - {verdict.reason}")
    broken = [verdict for verdict in readiness if verdict.status == PROBE_BROKEN]
    if broken:
        print("resource probe findings:")
        for verdict in broken:
            print(f"  {_probe_finding(verdict)}")
    return readiness


def print_checkpoint_status(report, *, findings: list[str] | None = None) -> list[str]:
    """Checkpoint freshness: docs/RECOVERY.md, "Observability".

    Last commit, last push, lag, the gate's blocking reason and the
    `remote diverged` alarm.
    """
    if report.data_dir is None:
        return []
    data_dir = report.data_dir
    production = _load_dispatcher_state(data_dir / "dispatcher" / "production-state.json")
    if "checkpoint" not in production and "checkpoint_push" not in production:
        return []

    snapshot = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=production.get("checkpoint"),
        push_state=production.get("checkpoint_push"),
    )
    print()
    print("checkpoint freshness: read-only")
    for line in render_checkpoint_lines(snapshot):
        print(f"  {line}")

    findings = checkpoint_findings(report) if findings is None else findings
    if findings:
        print("checkpoint findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def checkpoint_findings(report) -> list[str]:
    if report.data_dir is None:
        return []
    production = _load_dispatcher_state(report.data_dir / "dispatcher" / "production-state.json")
    if "checkpoint" not in production and "checkpoint_push" not in production:
        return []
    snapshot = checkpoint_snapshot(
        report.instance_path.parent,
        write_state=production.get("checkpoint"),
        push_state=production.get("checkpoint_push"),
    )
    findings: list[str] = []
    if snapshot["remote_diverged"]:
        findings.append(f"remote diverged: {snapshot['push_reason'] or 'push stopped, resolve by hand'}")
    if snapshot["blocked_reason"]:
        findings.append(f"checkpoint gate blocked: {snapshot['blocked_reason']}")
    lag = snapshot["lag_minutes"]
    if isinstance(lag, int) and lag > 2 * PUSH_INTERVAL_MINUTES:
        findings.append(f"checkpoint lag is {lag} min, past the {PUSH_INTERVAL_MINUTES} min RPO")
    return findings


def print_secret_store_status(report, *, findings: list[str] | None = None) -> list[str]:
    """Secret store health: catalog/values consistency and installation key health."""
    findings = secret_store_findings(report) if findings is None else findings
    if findings:
        print()
        print("secret store findings:")
        for finding in findings:
            print(f"  {finding}")
    return findings


def secret_store_findings(report) -> list[str]:
    return list(_secret_store_findings(report.instance_path.parent))


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
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    desired = build_plan(report.instance, report.bindings, packaged=packaged)
    managed, error = load_managed_manifest(data_dir / "host-managed.json")
    if error:
        return ["production dispatcher managed manifest unavailable: " + error]
    changes = plan_changes(desired, collected_host.inventory, managed, prefix)
    findings = []
    for change in changes:
        if not change.logical_id.startswith("systemd:dispatcher:production"):
            continue
        if change.action == "unchanged":
            continue
        findings.append(f"production dispatcher managed unit mismatch: {change.action} {change.name}")
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
        exports = export_all(data_dir, _instance_dir(args.instance), copy_transcripts=args.copy_transcripts)
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
        result = export_board(data_dir, instance_dir=Path(args.instance).expanduser())
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
        result = export_memory(data_dir, _instance_dir(args.instance))
    except RuntimeError as exc:
        print(f"secretary data export-memory: {exc}")
        return 1
    print(f"memory facts: {result.count}")
    print(f"export: {result.path}")
    print("status: ok")
    return 0


def run_memory_verify(args: argparse.Namespace) -> int:
    data_dir = _data_dir_from_args(args, validate_tree=True)
    if data_dir is None:
        return 1
    try:
        result = verify_memory_journal(data_dir, _instance_dir(args.instance))
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
            _instance_dir(args.instance),
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
            _instance_dir(args.instance),
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


def run_knowledge_write(args: argparse.Namespace) -> int:
    try:
        result = write_knowledge_document(
            _instance_dir(args.instance),
            document=args.path,
            actor=args.actor,
            source_file=Path(args.file),
            message=args.message,
        )
    except KnowledgeValidationError as exc:
        _print_json({"ok": False, "op": "write", "error": "validation", "message": str(exc)})
        return MEMORY_EXIT_VALIDATION
    except (KnowledgeError, StateRepoError) as exc:
        _print_json({"ok": False, "op": "write", "error": "runtime", "message": str(exc)})
        return 1
    _print_json(
        {
            "ok": True,
            "op": "write",
            "document": result.document,
            "path": str(result.path),
            "commit": result.commit,
            "actor": result.actor,
            "changed": result.changed,
        }
    )
    return 0


def run_knowledge_list(args: argparse.Namespace) -> int:
    try:
        documents = list_knowledge_documents(_instance_dir(args.instance))
    except (KnowledgeError, StateRepoError) as exc:
        _print_json({"ok": False, "op": "list", "error": "runtime", "message": str(exc)})
        return 1
    _print_json({"ok": True, "op": "list", "documents": list(documents)})
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
    from secretary.cli_output import print_json

    print_json(payload)


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
    result = verify_backup(Path(args.archive))
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


def print_host_inventory(
    report,
    args: argparse.Namespace,
    *,
    expected=None,
    collected: CollectResult | None = None,
    diffs: dict[str, KindDiff] | None = None,
) -> tuple[bool, bool, CollectResult]:
    """Print the read-only host inventory: matched / missing / unmanaged per kind.

    Returns True if any kind could not be inspected (reported as unavailable).
    """
    if expected is None or collected is None or diffs is None:
        expected, collected, diffs = collect_host_inventory(report, args)

    print()
    print("host inventory: read-only")
    for kind in ("projects", "units", "orca repos"):
        reason = collected.errors.get(kind)
        if reason:
            print(f"{kind}:")
            print(f"  unavailable: {reason}")
        else:
            _print_kind(kind, diffs[kind])
    parity_findings = any(
        diff.missing_on_host or diff.unmanaged_on_host
        for kind, diff in diffs.items()
        if kind not in collected.errors
    )
    runtime_findings = _print_unit_runtime(expected, collected)
    return bool(collected.errors), parity_findings or runtime_findings, collected


def collect_host_inventory(report, args: argparse.Namespace):
    source = FixtureHostSource(Path(args.host_fixture)) if args.host_fixture else LiveHostSource()
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    expected = build_doctor_expectations(
        report.instance,
        report.bindings,
        packaged=packaged,
        data_dir=report.data_dir,
    )
    collected = source.collect(expected)
    return expected, collected, inventory(expected, collected.inventory)


def _print_kind(kind: str, diff: KindDiff) -> None:
    print(f"{kind}:")
    print(f"  matched: {_join(diff.matched)}")
    print(f"  missing-on-host: {_join(diff.missing_on_host)}")
    print(f"  unmanaged-on-host: {_join(diff.unmanaged_on_host)}")


def _print_unit_runtime(expected, collected: CollectResult) -> bool:
    """Report required enabled/active state separately from unit-file parity."""
    if "units" in collected.errors:
        return False
    findings = _unit_runtime_findings(expected, collected)
    if findings:
        print("unit runtime findings:")
        for finding in findings:
            print(f"  {finding}")
    return bool(findings)


def _unit_runtime_findings(expected, collected: CollectResult) -> list[str]:
    if "units" in collected.errors:
        return []
    findings: list[str] = []
    for name, (need_enabled, need_active) in sorted(expected.unit_runtime.items()):
        state = collected.inventory.unit_states.get(name)
        if state is None:
            continue
        enabled, active = state
        if need_enabled and enabled != "enabled":
            findings.append(f"{name}: expected enabled, got {enabled}")
        if need_active and active != "active":
            findings.append(f"{name}: expected active, got {active}")
    return findings


def _print_external_orca_runtime(expected, collected: CollectResult) -> None:
    """Show the host-owned runtime separately from Secretary's unit parity.

    A real systemd never leaves this lookup unset: `systemctl is-enabled`/`is-active` on a unit
    that does not exist still exit non-zero with ``not-found``/``inactive`` on stdout, not an
    exception or stderr (verified against systemd 255). So ``state is None`` cannot happen through
    `LiveHostSource`; the reachable "absent" signal is `enabled == "not-found"`. The `state is
    None` case is kept only as a defensive fallback for a `HostSource` that omits the entry
    entirely (e.g. a hand-authored fixture).
    """
    name = expected.external_runtime
    if not name or "units" in collected.errors:
        return
    state = collected.inventory.unit_states.get(name)
    if state is None or state[0] == "not-found":
        print("Orca runtime: absent (external, not managed by Secretary)")
        return
    enabled, active = state
    print(f"Orca runtime: external {name}, enabled={enabled}, active={active}")


def print_background_automations(*, inspect: bool) -> None:
    """Read-only view of the background-role Orca automations (curator/retro/steward) as managed
    resources, mirroring the host inventory: which shipped ``automation.toml`` specs are currently
    reconciled on the live host and which have drifted or are not provisioned yet.

    Product-level (the specs are the same for every instance), so unlike the other doctor sections
    it takes no instance ``report``.

    ``secretary upgrade`` owns them — created and repointed from the spec, matched by ``name`` so
    the automation id stays stable across re-provisions — the same way ``reconcile apply`` owns the
    packaged timers. Doctor only reports their state so an operator can see a role a
    provisioning/recovery run has not caught up on. Like ``missing-on-host`` in the host inventory,
    a missing or drifted automation is printed for visibility but does not by itself flip doctor's
    exit code: that stays reserved for a kind that could not be inspected at all. Best-effort — an
    unreadable Orca inventory prints as unavailable, never as "every role missing".
    """
    from secretary.automations import (
        AutomationError,
        OrcaAutomationClient,
        load_specs,
        plan_automations,
    )

    # The automations this process ships, not the ones a configured checkout would: doctor
    # reports on the code it is running.
    from secretary.upgrade import running_product_root

    specs = load_specs(running_product_root())
    if not specs:
        return
    print()
    print("background automations: read-only")
    if not inspect:
        print("  not inspected")
        return
    try:
        live = OrcaAutomationClient().list()
    except AutomationError as exc:
        print(f"  unavailable: {exc}")
        return
    for change in plan_automations(specs, live):
        if change.action == "unchanged":
            print(f"  {change.name}: managed")
        elif change.action == "create":
            print(f"  {change.name}: missing (not provisioned)")
        else:  # repoint
            print(f"  {change.name}: drifted ({', '.join(change.drifted)})")


def _join(names: list[str]) -> str:
    return ", ".join(names) if names else "(none)"


def _instance_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _instance_dir(value: str) -> Path:
    """The private repo root. `--instance` may name it or its instance.yaml."""
    return _instance_path(value).parent


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

    try:
        return instance_data_dir(_instance_path(args.instance))
    except DataDirError as exc:
        print(f"secretary data: cannot resolve instance data_dir: {exc}")
        return None


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"secretary {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler
