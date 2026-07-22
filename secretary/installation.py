"""Supported fresh-install and Git-checkpoint recovery flow.

The private instance repository is the only portable input.  This module turns
its normalized checkpoint into a new local data plane and then calls the same
materializer used by ``secretary upgrade``.  It deliberately does not install
Kanboard or Orca: their package transport and supported versions are product
decision gates, so a missing runtime is reported before any live state is
written.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from secretary._fsutil import publish_component_entries, write_json, write_text_atomic
from secretary.automations import OrcaAutomationClient
from secretary.config import validate_instance
from secretary.data import init_layout, manifest_for
from secretary.host_apply import LiveOrcaRegistrar, SystemdUnitInstaller
from secretary.restore import (
    RestoreError,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_findings,
)
from secretary.tasks import KanboardClient, TaskError, TaskReader
from secretary.upgrade import UpgradeContext, default_product_root, run_steps


CHECKPOINT_BOARD = ("cards.ndjson", "events.ndjson", "export.json")
CHECKPOINT_RUNS = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")


class InstallError(RuntimeError):
    """A controlled install/recovery refusal."""


@dataclass
class InstallStep:
    name: str
    status: str
    detail: str = ""


@dataclass
class InstallResult:
    steps: list[InstallStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(step.status == "failed" for step in self.steps)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(InstallStep(name, status, detail))

    def render(self) -> str:
        lines = ["secretary install"]
        for step in self.steps:
            suffix = f": {step.detail}" if step.detail else ""
            lines.append(f"  {step.status:9} {step.name}{suffix}")
        lines.append("status: " + ("ok" if self.ok else "failed"))
        return "\n".join(lines)


def _run(argv: list[str], *, label: str, timeout: int = 120) -> str:
    environment = dict(os.environ)
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    environment.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError:
        raise InstallError(f"{label}: command not found") from None
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(f"{label}: command could not run") from None
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise InstallError(f"{label}: {detail[-1] if detail else f'exited {completed.returncode}'}")
    return (completed.stdout or "").strip()


def _ensure_installation_user(name: str | None, *, recovery: bool, dry_run: bool) -> None:
    if not name:
        return
    try:
        pwd.getpwnam(name)
    except KeyError:
        if dry_run:
            return
        if os.geteuid() != 0:
            raise InstallError(
                f"installation user {name!r} does not exist; create it as root, then rerun"
            ) from None
        _run(["useradd", "--create-home", "--", name], label="create installation user")
        return
    if not recovery:
        raise InstallError(
            f"installation user {name!r} already exists; choose --recover for a lost-machine "
            "restore or use the separate adopt workflow for a live installation"
        )


def _set_installation_owner(path: Path, name: str | None) -> None:
    """Give the dedicated user runtime paths created while the installer is root."""
    if not name or os.geteuid() != 0 or not path.exists():
        return
    account = pwd.getpwnam(name)
    try:
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        for child in path.rglob("*"):
            os.chown(child, account.pw_uid, account.pw_gid, follow_symlinks=False)
    except OSError as exc:
        raise InstallError(f"could not assign {path} to installation user {name}: {exc}") from None


def _clone_or_reuse(remote: str, target: Path, *, recovery: bool, dry_run: bool) -> str:
    empty_target = target.is_dir() and not any(target.iterdir())
    if not target.exists() or empty_target:
        if dry_run:
            return "would clone private instance remote"
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--", remote, str(target)], label="clone instance remote", timeout=300)
        return "cloned private instance remote"
    if not target.is_dir() or not (target / ".git").exists():
        raise InstallError(
            f"target {target} is not empty; choose --recover for the same instance or use the "
            "separate adopt workflow, no files were overwritten"
        )
    origin = _run(["git", "-C", str(target), "remote", "get-url", "origin"], label="inspect instance remote")
    if origin != remote:
        raise InstallError("existing target belongs to a different instance remote")
    if not recovery:
        raise InstallError(
            f"target {target} already contains an installation; choose --recover or use the "
            "separate adopt workflow"
        )
    if _run(["git", "-C", str(target), "status", "--porcelain"], label="inspect instance checkout"):
        raise InstallError("instance checkout has local changes; recovery will not overwrite them")
    if not dry_run:
        _run(["git", "-C", str(target), "fetch", "--quiet", "origin"], label="fetch instance remote")
        _run(["git", "-C", str(target), "merge", "--ff-only", "@{u}"], label="fast-forward instance checkout")
    return "reused checkpoint checkout"


def _read_runtime_env(instance_dir: Path, override: str | None) -> dict[str, str]:
    path = Path(override).expanduser() if override else instance_dir / "runtime.env"
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise InstallError(
            f"runtime credentials are required: create {path}, chmod 0600, then rerun with --recover"
        ) from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise InstallError("runtime.env must be a regular file, not a symlink")
    if mode & 0o077:
        raise InstallError("runtime.env permissions are too broad; run chmod 0600")
    try:
        relative = path.resolve().relative_to(instance_dir.resolve())
    except ValueError:
        relative = None
    if relative is not None:
        try:
            ignored = subprocess.run(
                ["git", "-C", str(instance_dir), "check-ignore", "--quiet", "--", str(relative)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError("could not verify that runtime.env is gitignored") from None
        if ignored.returncode != 0:
            raise InstallError("runtime.env is inside the instance checkout but is not gitignored")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise InstallError("runtime.env is unreadable") from None
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line or line.startswith("export "):
            raise InstallError(f"runtime.env line {number} must use KEY=VALUE syntax")
        key, value = line.split("=", 1)
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise InstallError(f"runtime.env line {number} has an invalid variable name")
        values[key] = value
    required = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise InstallError("runtime.env is missing required Kanboard credentials: " + ", ".join(missing))
    return values


@contextmanager
def _runtime_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_prerequisites() -> None:
    if shutil.which("orca") is None:
        raise InstallError(
            "Orca is not installed; install a supported Orca runtime before secretary recovery"
        )
    _run(["orca", "--version"], label="inspect Orca")
    try:
        TaskReader(KanboardClient()).list()
    except TaskError as exc:
        raise InstallError(f"Kanboard prerequisite failed: {exc.message}") from None


def _valid_existing_layout(data_dir: Path) -> bool:
    try:
        actual = json.loads((data_dir / "data-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return actual == manifest_for(data_dir)


def materialize_checkpoint(instance_dir: Path, data_dir: Path) -> tuple[int, int]:
    """Publish normalized checkpoint files into a newly derived local layout."""
    if data_dir.exists() and any(data_dir.iterdir()):
        if not _valid_existing_layout(data_dir):
            raise InstallError(
                f"data target {data_dir} is not an installation created by secretary; "
                "choose adopt or a clean recovery target"
            )
    board_source = instance_dir / "state" / "board"
    runs_source = instance_dir / "state" / "runs"
    for required in (board_source / "cards.ndjson", board_source / "export.json"):
        if not required.is_file():
            raise InstallError(f"private checkpoint is missing {required.relative_to(instance_dir)}")
    for name in CHECKPOINT_RUNS:
        if not (runs_source / name).is_file():
            raise InstallError(f"private checkpoint is missing state/runs/{name}")

    try:
        card_lines = [
            line
            for line in (board_source / "cards.ndjson").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        run_lines = [
            line
            for line in (runs_source / "runs.ndjson").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cards = [json.loads(line) for line in card_lines]
        for line in run_lines:
            json.loads(line)
        board_export = json.loads((board_source / "export.json").read_text(encoding="utf-8"))
        run_export = json.loads((runs_source / "export.json").read_text(encoding="utf-8"))
        claims = json.loads((runs_source / "claims.json").read_text(encoding="utf-8"))
        watermarks = json.loads((runs_source / "watermarks.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise InstallError("private checkpoint contains invalid normalized state") from None
    if not isinstance(board_export, dict) or not isinstance(run_export, dict):
        raise InstallError("private checkpoint contains invalid export metadata")
    if any(not isinstance(card, dict) for card in cards) or board_export.get("card_count") != len(cards):
        raise InstallError("private checkpoint board count does not match cards.ndjson")
    run_count = len(run_lines)
    declared_runs = run_export.get("run_record_count")
    if not isinstance(declared_runs, int) or declared_runs != run_count:
        raise InstallError("private checkpoint run count does not match runs.ndjson")
    claim_entries = claims.get("claims") if isinstance(claims, dict) else None
    watermark_entries = watermarks.get("files") if isinstance(watermarks, dict) else None
    if not isinstance(claim_entries, dict) or run_export.get("claim_count") != len(claim_entries):
        raise InstallError("private checkpoint claim count does not match claims.json")
    if not isinstance(watermark_entries, list) or run_export.get("watermark_count") != len(watermark_entries):
        raise InstallError("private checkpoint watermark count does not match watermarks.json")

    if not data_dir.exists() or not any(data_dir.iterdir()):
        init_layout(data_dir)

    board_target = data_dir / "board"
    runs_target = data_dir / "runs"
    board_target.mkdir(parents=True, exist_ok=True)
    runs_target.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".checkpoint-board-", dir=board_target) as staging_raw:
            staging = Path(staging_raw)
            write_json(staging / "cards.json", {"version": 1, "cards": cards})
            for name in CHECKPOINT_BOARD:
                source = board_source / name
                if source.is_file():
                    write_text_atomic(staging / name, source.read_text(encoding="utf-8"))
            publish_component_entries(
                staging,
                board_target,
                ["cards.json", *[n for n in CHECKPOINT_BOARD if (staging / n).is_file()]],
                "checkpoint board materialization",
            )
        with tempfile.TemporaryDirectory(prefix=".checkpoint-runs-", dir=runs_target) as staging_raw:
            staging = Path(staging_raw)
            for name in CHECKPOINT_RUNS:
                write_text_atomic(staging / name, (runs_source / name).read_text(encoding="utf-8"))
            publish_component_entries(
                staging,
                runs_target,
                list(CHECKPOINT_RUNS),
                "checkpoint runs materialization",
            )
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not materialize checkpoint: {exc}") from None
    return len(cards), run_count


def materialize_host(instance: Path, product_root: Path, host_fixture: Path | None = None):
    report = validate_instance(instance)
    if not report.ok:
        raise InstallError("invalid instance config: " + "; ".join(map(str, report.errors)))
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(),
        orca=LiveOrcaRegistrar(),
        automations=OrcaAutomationClient(),
        host_fixture=host_fixture,
        pull=False,
        report=report,
    )
    result = run_steps(context)
    if not result.ok:
        failed = result.steps[-1]
        raise InstallError(f"materializer {failed.name} failed: {failed.detail}")
    return result


def install(args: argparse.Namespace) -> InstallResult:
    result = InstallResult()
    target = Path(args.instance_dir).expanduser().resolve()
    recovery = bool(args.recover)
    if args.adopt:
        result.add("mode", "failed", "full live-host adoption is not supported by this flow")
        return result
    try:
        _ensure_installation_user(args.installation_user, recovery=recovery, dry_run=args.dry_run)
        result.add(
            "installation-user",
            "unchanged" if recovery else "changed",
            args.installation_user,
        )
        detail = _clone_or_reuse(args.instance_remote, target, recovery=recovery, dry_run=args.dry_run)
        result.add("instance-checkout", "unchanged" if detail.startswith("reused") else "changed", detail)
        if detail.startswith("cloned"):
            _set_installation_owner(target, args.installation_user)
        if args.dry_run and not target.exists():
            result.add("runtime-env", "skipped", "available only after clone")
            return result
        values = _read_runtime_env(target, args.runtime_env)
        result.add("runtime-env", "unchanged", "credentials loaded from host-only file")
        with _runtime_environment(values):
            check_prerequisites()
            result.add("prerequisites", "unchanged", "Kanboard and Orca are reachable")
            report = validate_instance(target)
            if not report.ok:
                raise InstallError("invalid cloned instance: " + "; ".join(map(str, report.errors)))
            data_dir = Path(report.instance["data_dir"]).expanduser().resolve()
            cards, runs = materialize_checkpoint(target, data_dir)
            _set_installation_owner(data_dir, args.installation_user)
            result.add("checkpoint", "changed", f"{cards} board card(s), {runs} run record(s)")
            restored = import_normalized_board(data_dir)
            result.add("board", "changed", f"{restored} card(s) at parity")
            count = rebuild_memory_index(data_dir, target)
            result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
            product_root = (
                Path(args.product_root).expanduser().resolve()
                if args.product_root
                else default_product_root()
            )
            host_result = materialize_host(
                target,
                product_root,
                Path(args.host_fixture).expanduser().resolve() if args.host_fixture else None,
            )
            mark_reconcile_applied(data_dir)
            changed = sum(step.status == "changed" for step in host_result.steps)
            result.add(
                "host",
                "changed" if changed else "unchanged",
                f"materializer complete ({changed} changed step(s))",
            )
            findings = restore_findings(data_dir)
            if findings:
                raise InstallError("status findings: " + "; ".join(findings))
            result.add("status", "unchanged", "board, memory and operational configuration are ready")
            _set_installation_owner(data_dir, args.installation_user)
    except (InstallError, RestoreError, RuntimeError) as exc:
        result.add("install", "failed", str(exc))
    return result


def run_install(args: argparse.Namespace) -> int:
    result = install(args)
    if args.json:
        print(json.dumps({
            "status": "ok" if result.ok else "failed",
            "steps": [step.__dict__ for step in result.steps],
        }, indent=2, sort_keys=True))
    else:
        print(result.render())
    return 0 if result.ok else 1


def add_install_commands(subparsers) -> None:
    def arguments(parser, *, recovery_default: bool) -> None:
        parser.add_argument("--instance-remote", required=True, help="private Git checkpoint remote")
        parser.add_argument("--instance-dir", required=True, help="local checkout destination")
        parser.add_argument("--installation-user", required=True, help="dedicated OS account")
        parser.add_argument(
            "--runtime-env",
            help="host-only credentials file (default: INSTANCE/runtime.env)",
        )
        parser.add_argument("--product-root", help="installed product checkout")
        parser.add_argument("--recover", action="store_true", default=recovery_default,
                            help="resume or recover the same installation without overwriting local changes")
        parser.add_argument(
            "--adopt",
            action="store_true",
            help="select the separate live-host adoption path",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--host-fixture", help=argparse.SUPPRESS)
        parser.set_defaults(handler=run_install)

    fresh = subparsers.add_parser("install", help="install or resume from a private instance remote")
    arguments(fresh, recovery_default=False)
    recover = subparsers.add_parser(
        "recover",
        help="recover a lost installation from a private instance remote",
    )
    arguments(recover, recovery_default=True)
