"""Supported fresh-install and Git-checkpoint recovery flow.

The private instance repository is the only portable input. This module turns its normalized
checkpoint into a new local data plane and then calls the same materializer ``secretary upgrade``
uses.

The secret store opens before anything reads ``runtime.env``, because on a clean host that file
does not exist yet: it is what the store writes once the recovery phrase rebuilds the
installation key. Without the phrase the recovery still brings back everything that needs no
credentials and reports which secrets stayed locked or went missing.

It deliberately does not install Kanboard or Orca: their package transport and supported versions
are product decision gates, so a missing runtime is reported before any live state is written.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from secretary import _proc, state_repo
from secretary._fsutil import (
    publish_component_entries,
    publish_state_atomic,
    write_json,
    write_text_atomic,
)
from secretary.automations import OrcaAutomationClient, workspaces_root
from secretary.board_transport import (
    BoardTransport,
    BoardTransportError,
    ensure_from_runtime_values,
    transport_path,
)
from secretary.config import validate_instance
from secretary.data import init_layout, manifest_for
from secretary.host_apply import (
    LiveOrcaRegistrar,
    SystemdUnitInstaller,
    resolve_runtime_owner,
)
from secretary.restore import (
    RestoreError,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_findings,
)
from secretary.runtime_env import (
    RuntimeEnvError,
    RuntimeEnvMissing,
    instance_runtime_env_path,
    read_runtime_env,
)
from secretary.secret_recover import SecretRecovery, recover_secrets
from secretary.secret_store import (
    SecretStoreError,
    is_initialized,
    key_path,
    normalize_phrase,
)
from secretary.state_repo import StateRepoError
from secretary.tasks import KanboardClient, TaskError, TaskReader
from secretary.upgrade import (
    STEPS,
    GitError,
    UpgradeContext,
    UpgradeResult,
    _set_runtime_owner,
    default_product_root,
    run_steps,
    step_host,
)
from triggered_agents.runtime.paths import PRODUCT_DIRNAME, PRODUCT_ENV
from triggered_agents.runtime.shared_state import resolve_pipeline_state_dir

CHECKPOINT_BOARD = ("cards.ndjson", "sprints.ndjson", "events.ndjson", "export.json")
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


@dataclass(frozen=True)
class PipelineStateMaterialization:
    records: int
    changed: bool


def _run(
    argv: list[str], *, label: str, timeout: int = 120, cwd: Path | None = None,
) -> str:
    # Installation clones before there is an instance checkout to cross into, so it
    # takes the environment half of the instance-repository boundary on its own.
    environment = state_repo.git_env()
    try:
        completed = _proc.run(argv, timeout=timeout, env=environment, cwd=cwd)
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
    """Give root-created installation files safe ownership for the runtime user."""
    try:
        _set_runtime_owner(path, name)
    except GitError as exc:
        raise InstallError(str(exc)) from None


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
    # This checkout belongs to the runtime user.  Do not make a root Git process trust it: Git
    # loads repository configuration before every command, including executable fsmonitor hooks.
    # state_repo crosses to the owner before Git starts.
    try:
        origin = state_repo.git(target, ["remote", "get-url", "origin"], label="inspect instance remote").strip()
    except state_repo.StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if origin != remote:
        raise InstallError("existing target belongs to a different instance remote")
    if not recovery:
        # Only a checkout explicitly prepared by `bootstrap` may continue into its
        # first install. runtime.env alone is normal state of every live installation.
        if not (target / ".secretary-bootstrap").is_file():
            raise InstallError(
                f"target {target} already contains an installation; choose --recover or use the "
                "separate adopt workflow"
            )
    try:
        dirty = state_repo.git(target, ["status", "--porcelain"], label="inspect instance checkout")
    except state_repo.StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if dirty:
        raise InstallError("instance checkout has local changes; recovery will not overwrite them")
    if not dry_run:
        try:
            state_repo.git(target, ["fetch", "--quiet", "origin"], label="fetch instance remote")
            state_repo.git(target, ["merge", "--ff-only", "@{u}"], label="fast-forward instance checkout")
        except state_repo.StateRepoError as exc:
            raise InstallError(str(exc)) from None
    return "reused checkpoint checkout"


def _runtime_env_file(instance_dir: Path, override: str | None) -> Path:
    """The env file this installation runs on, override included."""
    return instance_runtime_env_path(instance_dir, override)


def _recovery_phrase(args: argparse.Namespace, instance_dir: Path) -> str | None:
    """Read the phrase the same way a secret value is read: never from argv.

    A phrase on the command line lands in the process table and the shell history, and one in the
    environment is inherited by everything this command starts, so the only inputs are a file,
    standard input and a non-echoing terminal prompt. No flag and no terminal means no phrase.
    """
    path = getattr(args, "recovery_phrase_file", None)
    if path:
        source = Path(path).expanduser()
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InstallError(f"could not read the recovery phrase file: {exc}") from None
        return _clean_phrase(raw, f"{source} holds no recovery phrase")
    if getattr(args, "recovery_phrase_stdin", False):
        return _clean_phrase(sys.stdin.read(), "no recovery phrase on standard input")
    if is_initialized(instance_dir) and not key_path(instance_dir).exists() and sys.stdin.isatty():
        answer = getpass.getpass("Recovery phrase (empty to continue without it): ")
        if answer.strip():
            return _clean_phrase(answer, "no recovery phrase entered")
    return None


def _clean_phrase(raw: str, empty: str) -> str:
    try:
        return normalize_phrase(raw)
    except SecretStoreError:
        raise InstallError(empty) from None


def _open_secret_store(
    instance_dir: Path, runtime_env: Path, *, phrase: str | None, dry_run: bool
) -> SecretRecovery:
    """Open the store before anything asks for credentials.

    Which file `runtime-env` materializes into belongs to the installation being recovered, not to
    the host default, so the resolution override is pinned to this target for the duration of the
    write. That is also what keeps a recovery drill off a live installation's env file.
    """
    try:
        with _runtime_environment({"SECRETARY_RUNTIME_ENV_FILE": str(runtime_env)}):
            return recover_secrets(instance_dir, phrase=phrase, dry_run=dry_run)
    except (SecretStoreError, StateRepoError) as exc:
        raise InstallError(f"secret store: {exc}") from None


def _secret_store_step(recovery: SecretRecovery) -> tuple[str, str]:
    if not recovery.store_present:
        return "skipped", "no secret store in the instance repo"
    if not recovery.unlocked:
        return "unchanged", recovery.summary()
    return "changed" if recovery.changed else "unchanged", recovery.summary()


def _add_secret_steps(result: InstallResult, recovery: SecretRecovery) -> None:
    """One line per secret that did not come back, ids and targets only."""
    for status, entries in (("locked", recovery.locked), ("missing", recovery.missing)):
        for entry in entries:
            where = entry.get("path") or entry.get("target") or "not materialized"
            result.add(
                f"secret:{entry['id']}", status, f"{entry.get('environment', '-')} -> {where}"
            )
    for path in recovery.withheld:
        result.add(f"secret-file:{path}", "withheld", "a secret this file needs is not readable")


def _blocked_by_secrets(
    cause: InstallError, recovery: SecretRecovery, runtime_env: Path
) -> InstallError:
    """Say what is still closed instead of asking for a hand-written file."""
    reason = (
        str(cause)
        if runtime_env.exists()
        else f"{runtime_env} is not there, and the store is what writes it"
    )
    lines = [f"recovery is incomplete: {recovery.summary()}", f"  cause: {reason}"]
    lines.extend(recovery.render())
    return InstallError("\n".join(lines))


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


def check_prerequisites(
    transport: BoardTransport, instance_dir: Path, installation_user: str | None = None,
) -> None:
    if shutil.which("orca") is None:
        raise InstallError(
            "Orca is not installed; install a supported Orca runtime before secretary recovery"
        )
    # The pinned Electron AppImage deliberately refuses to start as root.  The
    # installation command is allowed to run as root, but its CLI probe must
    # have the same uid as the service it is checking.
    if os.geteuid() == 0 and installation_user:
        _run(["runuser", "--user", installation_user, "--", "orca", "--version"], label="inspect Orca")
    else:
        _run(["orca", "--version"], label="inspect Orca")
    try:
        TaskReader(KanboardClient(transport, instance_dir)).list()
    except TaskError as exc:
        raise InstallError(f"Kanboard prerequisite failed: {exc.message}") from None


def _valid_existing_layout(data_dir: Path) -> bool:
    try:
        actual = json.loads((data_dir / "data-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return actual == manifest_for(data_dir)


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def materialize_checkpoint(
    instance_dir: Path, data_dir: Path, *, dry_run: bool = False,
) -> tuple[int, int]:
    """Validate the checkpoint and optionally publish it into the local layout."""
    bootstrap_evidence = False
    if data_dir.exists() and any(data_dir.iterdir()):
        # Bootstrap records the Orca unit before checkpoint materialization so
        # the first full reconcile can prove ownership.  That one evidence file
        # is compatible with an otherwise empty data root.
        entries = {entry.name for entry in data_dir.iterdir()}
        bootstrap_evidence = entries == {"host-managed.json"}
        if not bootstrap_evidence and not _valid_existing_layout(data_dir):
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
        # A checkpoint written before sprints joined the board export carries no
        # sprints.ndjson; its export.json declares no sprint count either, and the
        # next tick writes both.
        sprint_lines = [
            line
            for line in _read_optional(board_source / "sprints.ndjson").splitlines()
            if line.strip()
        ]
        cards = [json.loads(line) for line in card_lines]
        sprints = [json.loads(line) for line in sprint_lines]
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
    declared_sprints = board_export.get("sprint_count")
    if any(not isinstance(sprint, dict) for sprint in sprints) or (
        declared_sprints is not None and declared_sprints != len(sprints)
    ):
        raise InstallError("private checkpoint sprint count does not match sprints.ndjson")
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

    if dry_run:
        return len(cards), run_count

    if not data_dir.exists() or not any(data_dir.iterdir()) or bootstrap_evidence:
        init_layout(data_dir)

    board_target = data_dir / "board"
    runs_target = data_dir / "runs"
    board_target.mkdir(parents=True, exist_ok=True)
    runs_target.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".checkpoint-board-", dir=board_target) as staging_raw:
            staging = Path(staging_raw)
            write_json(staging / "cards.json", {"version": 1, "cards": cards})
            write_json(staging / "sprints.json", {"version": 1, "sprints": sprints})
            for name in CHECKPOINT_BOARD:
                source = board_source / name
                if source.is_file():
                    write_text_atomic(staging / name, source.read_text(encoding="utf-8"))
            publish_component_entries(
                staging,
                board_target,
                [
                    "cards.json",
                    "sprints.json",
                    *[n for n in CHECKPOINT_BOARD if (staging / n).is_file()],
                ],
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


def _restored_run_journals(runs_source: Path) -> dict[Path, list[tuple[int, str]]]:
    """Rebuild the JSONL files whose records the checkpoint normalizes."""
    grouped: dict[Path, list[tuple[int, object]]] = {}
    try:
        lines = (runs_source / "runs.ndjson").read_text(encoding="utf-8").splitlines()
        for raw in lines:
            if not raw.strip():
                continue
            entry = json.loads(raw)
            source = entry.get("source") if isinstance(entry, dict) else None
            line = entry.get("line") if isinstance(entry, dict) else None
            if not isinstance(source, str) or not source or not isinstance(line, int) or line < 1:
                raise ValueError
            relative = Path(source)
            if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl":
                raise ValueError
            grouped.setdefault(relative, []).append((line, entry.get("record")))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise InstallError("private checkpoint contains invalid run journal records") from None

    journals: dict[Path, list[tuple[int, str]]] = {}
    for relative, records in grouped.items():
        ordered = sorted(records, key=lambda item: item[0])
        numbers = [number for number, _ in ordered]
        if len(numbers) != len(set(numbers)):
            raise InstallError(
                f"private checkpoint has duplicate run journal lines for {relative.as_posix()}"
            )
        try:
            journals[relative] = [
                (number, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                for number, record in ordered
            ]
        except (TypeError, ValueError):
            raise InstallError(
                f"private checkpoint contains an unserializable run journal record for {relative.as_posix()}"
            ) from None
    return journals


def _render_restored_journal(records: list[tuple[int, str]]) -> str:
    """Keep original physical line numbers; blank source lines are meaningful offsets."""
    rendered: list[str] = []
    previous = 0
    for number, record in records:
        rendered.append("\n" * (number - previous - 1))
        rendered.append(record)
        previous = number
    return "".join(rendered)


def _live_run_journals(state_dir: Path) -> dict[Path, list[str]]:
    """Parse the current journal into the same canonical record spelling as a checkpoint."""
    journals: dict[Path, list[str]] = {}
    try:
        if not state_dir.is_dir():
            return journals
        for path in state_dir.rglob("*.jsonl"):
            if not path.is_file():
                continue
            relative = path.relative_to(state_dir)
            journals[relative] = [
                json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True) + "\n"
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise InstallError(f"could not read live pipeline state at {state_dir}: {exc}") from None
    return journals


def materialize_pipeline_state(
    instance_dir: Path, state_dir: Path, *, dry_run: bool = False,
) -> PipelineStateMaterialization:
    """Put canonical run journals back where the dispatcher checkpoint reads them.

    Recovery restores ``state/runs`` into the data plane, but the dispatcher exports from its
    pipeline worktree. This bridge is intentionally narrow: it restores only the JSONL content the
    canonical checkpoint carries, and refuses to overwrite a different non-empty live journal.
    """
    try:
        runs_source = Path(instance_dir).expanduser().resolve() / "state" / "runs"
        state_dir = Path(state_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not resolve pipeline state paths: {exc}") from None
    journals = _restored_run_journals(runs_source)
    existing = _live_run_journals(state_dir)
    for relative, canonical in journals.items():
        live = existing.get(relative, [])
        canonical_records = [record for _, record in canonical]
        if live and live[:len(canonical_records)] != canonical_records:
            raise InstallError(
                f"live pipeline state at {state_dir} does not extend the checkpoint; refusing to overwrite it"
            )
    records = sum(len(journal) for journal in journals.values())
    if dry_run:
        return PipelineStateMaterialization(records=records, changed=False)
    try:
        created = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
        # A valid live extension is newer than the checkpoint and must survive a
        # retry. Only absent/empty journals receive the reconstructed prefix.
        writes = [
            (state_dir / relative, _render_restored_journal(records))
            for relative, records in journals.items()
            if not existing.get(relative)
        ]
        if writes:
            publish_state_atomic(writes)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not materialize live pipeline state: {exc}") from None
    return PipelineStateMaterialization(records=records, changed=created or bool(writes))


def pipeline_state_path(runtime_home: Path) -> Path:
    """The dispatcher-owned state source below the installation user's worktree."""
    return resolve_pipeline_state_dir(workspaces_root(runtime_home))


def materialize_host(
    instance: Path,
    product_root: Path,
    host_fixture: Path | None = None,
    installation_user: str | None = None,
    before_host: Callable[[UpgradeContext], None] | None = None,
):
    report = validate_instance(instance)
    if not report.ok:
        raise InstallError("invalid instance config: " + "; ".join(map(str, report.errors)))
    try:
        installation_user, runtime_home = resolve_runtime_owner(instance, installation_user)
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(),
        orca=LiveOrcaRegistrar(installation_user),
        automations=OrcaAutomationClient(installation_user),
        host_fixture=host_fixture,
        pull=False,
        report=report,
        runtime_user=installation_user,
        runtime_home=runtime_home,
    )
    # The steps resolve their own paths against `runtime_home`; HOME is exported for the
    # subprocesses they start, which read the environment and not this context.
    with _runtime_environment({"HOME": str(runtime_home)}):
        if before_host is None:
            result = run_steps(context)
        else:
            host_index = STEPS.index(step_host)
            prepared = run_steps(context, steps=STEPS[:host_index])
            if not prepared.ok:
                failed = prepared.steps[-1]
                raise InstallError(f"materializer {failed.name} failed: {failed.detail}")
            before_host(context)
            finished = run_steps(context, steps=STEPS[host_index:])
            result = UpgradeResult(steps=[*prepared.steps, *finished.steps])
    if not result.ok:
        failed = result.steps[-1]
        raise InstallError(f"materializer {failed.name} failed: {failed.detail}")
    return result


def provision_project_checkouts(
    bindings: list[dict[str, object]], installation_user: str | None,
) -> int:
    """Clone missing registered checkouts without touching an existing path."""
    cloned = 0
    for binding in bindings:
        raw_target = binding.get("repo")
        if not isinstance(raw_target, str) or not raw_target:
            raise InstallError("project registry entry has no checkout path")
        target = Path(raw_target).expanduser()
        if target.exists():
            if not target.is_dir() or not (target / ".git").exists():
                raise InstallError(f"project checkout target is not a Git repository: {target}")
            continue
        remote = binding.get("remote")
        if not isinstance(remote, str) or not remote:
            raise InstallError(
                f"project {binding.get('id')!s} is missing its recovery remote"
            )
        branch = binding.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise InstallError("project registry entry has no default branch")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{target.name}.clone-", dir=target.parent
        ) as temporary:
            staging = Path(temporary) / "checkout"
            _run(
                [
                    "git", "clone", "--branch", branch, "--single-branch", "--",
                    remote, str(staging),
                ],
                label=f"clone project {binding.get('id')!s}",
                timeout=600,
            )
            os.replace(staging, target)
        _set_installation_owner(target, installation_user)
        cloned += 1
    return cloned


def provision_codex_home(product_root: Path, installation_user: str | None) -> int:
    """Seed non-secret Codex runtime files while preserving login state."""
    if not installation_user:
        return 0
    account = pwd.getpwnam(installation_user)
    target = Path(account.pw_dir) / ".config" / "orca" / "codex-runtime-home" / "home"
    source = product_root / "packaging" / "codex-home"
    changed = 0
    for name in ("AGENTS.md", "config.toml"):
        destination = target / name
        if destination.exists():
            continue
        try:
            contents = (source / name).read_text(encoding="utf-8")
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(destination, contents)
        except (OSError, RuntimeError) as exc:
            raise InstallError(f"could not provision managed CODEX_HOME: {exc}") from None
        changed += 1
    _set_installation_owner(target, installation_user)
    return changed


def _validated_instance(instance_dir: Path):
    report = validate_instance(instance_dir)
    if not report.ok:
        raise InstallError("invalid cloned instance: " + "; ".join(map(str, report.errors)))
    return report


# Files no product checkout is without. An install materializes from the checkout it selects, and
# the selection can miss: the operator ran the command out of a candidate tree without naming it,
# or `TA_SECRETARY_REPO` still points at a checkout that has since moved.
PRODUCT_MARKERS = (Path("packaging") / "codex-home", Path("skills") / "manifest.toml")


def _product_root(args: argparse.Namespace) -> Path:
    """The checkout this install materializes from, refused here if it is not one.

    Named, else configured, else the home default — never the checkout running this module. A path
    that holds no product would otherwise surface as an ENOENT from whichever step read it first.
    """
    if args.product_root:
        root = Path(args.product_root).expanduser().resolve()
        source = "--product-root"
    else:
        root = default_product_root()
        source = f"{PRODUCT_ENV} or the {PRODUCT_DIRNAME} default under this account's home"
    missing = [str(marker) for marker in PRODUCT_MARKERS if not (root / marker).exists()]
    if missing:
        raise InstallError(
            f"not a product checkout: {root} (from {source}) has no {missing[0]}; "
            "name the checkout to install with --product-root"
        )
    return root


def _restore_without_credentials(
    args: argparse.Namespace, target: Path, result: InstallResult
) -> None:
    """Recover everything that does not go through Kanboard.

    A locked store costs the operator their credentials, not their installation. What is left undone
    is named as skipped rather than quietly attempted with half a configuration, and the caller then
    fails with the secret report.
    """
    report = _validated_instance(target)
    assert report.data_dir is not None
    data_dir = report.data_dir
    cards, runs = materialize_checkpoint(target, data_dir, dry_run=args.dry_run)
    if args.dry_run:
        result.add(
            "checkpoint",
            "would-change",
            f"would materialize {cards} board card(s), {runs} run record(s)",
        )
        return
    _set_installation_owner(data_dir, args.installation_user)
    result.add("checkpoint", "changed", f"{cards} board card(s), {runs} run record(s)")
    host = report.host if isinstance(report.host, dict) else {}
    threads = host.get("memory_threads", 1)
    count = rebuild_memory_index(
        data_dir, target, threads=threads if isinstance(threads, int) else None
    )
    result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
    cloned = provision_project_checkouts(report.bindings, args.installation_user)
    seeded = provision_codex_home(_product_root(args), args.installation_user)
    result.add(
        "runtime",
        "changed" if cloned or seeded else "unchanged",
        f"{cloned} project checkout(s) cloned, {seeded} CODEX_HOME file(s) seeded",
    )
    result.add("board", "skipped", "requires an available board backend after locked secret recovery")
    result.add("host", "skipped", "requires full recovery before host materialization")


def install(args: argparse.Namespace) -> InstallResult:
    result = InstallResult()
    target = Path(args.instance_dir).expanduser().resolve()
    recovery = bool(args.recover)
    if args.adopt:
        result.add("mode", "failed", "full live-host adoption is not supported by this flow")
        return result
    try:
        # bootstrap creates this user before the first install. Its stamp is also
        # checked by _clone_or_reuse, so it is the narrow exception to the usual
        # refusal to touch an existing installation user.
        bootstrap_checkout = (target / ".secretary-bootstrap").is_file()
        _ensure_installation_user(
            args.installation_user,
            recovery=recovery or bootstrap_checkout,
            dry_run=args.dry_run,
        )
        result.add(
            "installation-user",
            "unchanged" if recovery or bootstrap_checkout else (
                "would-change" if args.dry_run else "changed"
            ),
            args.installation_user,
        )
        detail = _clone_or_reuse(args.instance_remote, target, recovery=recovery, dry_run=args.dry_run)
        result.add(
            "instance-checkout",
            "unchanged" if detail.startswith("reused") else (
                "would-change" if args.dry_run else "changed"
            ),
            detail,
        )
        if detail.startswith("cloned"):
            _set_installation_owner(target, args.installation_user)
        if args.dry_run and not target.exists():
            result.add("secret-store", "skipped", "available only after clone")
            result.add("runtime-env", "skipped", "available only after clone")
            return result
        # The store opens before anything reads runtime.env, because on a clean
        # host that file is the store's output and does not exist yet.
        runtime_env = _runtime_env_file(target, args.runtime_env)
        secrets = _open_secret_store(
            target,
            runtime_env,
            phrase=_recovery_phrase(args, target),
            dry_run=args.dry_run,
        )
        result.add("secret-store", *_secret_store_step(secrets))
        _add_secret_steps(result, secrets)

        runtime_loaded = True
        try:
            values = read_runtime_env(target, args.runtime_env)
        except RuntimeEnvMissing as exc:
            # A legacy board-only catalog is inert after this migration. It
            # must not force a recovery phrase merely to recreate transport.
            runtime_loaded = False
            runtime_required = any(
                entry.get("target") == "runtime-env"
                for entry in (*secrets.locked, *secrets.missing)
            )
            if not runtime_required:
                values = {}
            else:
                _restore_without_credentials(args, target, result)
                raise _blocked_by_secrets(exc, secrets, runtime_env) from None
        except RuntimeEnvError as exc:
            raise InstallError(str(exc)) from None
        try:
            transport_outcome = ensure_from_runtime_values(
                target,
                legacy_values=values,
                runtime_env=runtime_env,
                dry_run=args.dry_run,
                allow_default=detail.startswith(("cloned", "would clone")),
            )
        except BoardTransportError as exc:
            raise InstallError(str(exc)) from None
        if not args.dry_run:
            try:
                canonical_runtime_env = runtime_env.resolve() == target.resolve() / "runtime.env"
            except OSError:
                canonical_runtime_env = False
            if canonical_runtime_env:
                _set_installation_owner(runtime_env, args.installation_user)
            _set_installation_owner(transport_path(target), args.installation_user)
            _set_installation_owner(target / ".gitignore", args.installation_user)
            _set_installation_owner(target / ".git", args.installation_user)
        transport = transport_outcome.transport
        result.add(
            "board-transport",
            "would-change" if args.dry_run and transport_outcome.changed else (
                "changed" if transport_outcome.changed else "unchanged"
            ),
            transport_outcome.render(dry_run=args.dry_run),
        )
        result.add(
            "runtime-env",
            "unchanged" if runtime_loaded else "skipped",
            "host-only runtime configuration loaded" if runtime_loaded else "not required by this installation",
        )
        with _runtime_environment({**values, "SECRETARY_INSTANCE": str(target)}):
            check_prerequisites(transport, target, args.installation_user)
            result.add("prerequisites", "unchanged", "Kanboard and Orca are reachable")
            report = _validated_instance(target)
            assert report.data_dir is not None
            data_dir = report.data_dir
            cards, runs = materialize_checkpoint(target, data_dir, dry_run=args.dry_run)
            if args.dry_run:
                result.add(
                    "checkpoint",
                    "would-change",
                    f"would materialize {cards} board card(s), {runs} run record(s)",
                )
                result.add("board", "would-change", f"would restore {cards} card(s) and verify parity")
                result.add("memory", "would-change", "would rebuild the index from checkpoint facts")
                result.add("host", "would-change", "would run the host materializer")
                result.add("status", "skipped", "preview made no recovery changes")
                return result
            _set_installation_owner(data_dir, args.installation_user)
            result.add("checkpoint", "changed", f"{cards} board card(s), {runs} run record(s)")
            # The checkpoint only contains cards. The board itself is derived host
            # state and must exist before restore can prove card parity.
            from secretary.bootstrap import ensure_pipeline_board
            ensure_pipeline_board(target)
            restored = import_normalized_board(data_dir, instance=target)
            result.add("board", "changed", f"{restored} card(s) at parity")
            host = report.host if isinstance(report.host, dict) else {}
            threads = host.get("memory_threads", 1)
            count = rebuild_memory_index(
                data_dir, target, threads=threads if isinstance(threads, int) else None
            )
            result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
            product_root = _product_root(args)
            cloned = provision_project_checkouts(report.bindings, args.installation_user)
            seeded = provision_codex_home(product_root, args.installation_user)
            result.add(
                "runtime",
                "changed" if cloned or seeded else "unchanged",
                f"{cloned} project checkout(s) cloned, {seeded} CODEX_HOME file(s) seeded",
            )
            restored_runs = 0
            pipeline_state_changed = False

            def restore_pipeline_source(context: UpgradeContext) -> None:
                nonlocal restored_runs, pipeline_state_changed
                state_path = pipeline_state_path(context.runtime_home or Path.home())
                restored = materialize_pipeline_state(
                    target, state_path, dry_run=False,
                )
                restored_runs = restored.records
                pipeline_state_changed = restored.changed
                _set_installation_owner(state_path.parent, args.installation_user)

            host_result = materialize_host(
                target,
                product_root,
                Path(args.host_fixture).expanduser().resolve() if args.host_fixture else None,
                args.installation_user,
                before_host=restore_pipeline_source,
            )
            mark_reconcile_applied(data_dir)
            changed = sum(step.status == "changed" for step in host_result.steps)
            result.add(
                "host",
                "changed" if changed else "unchanged",
                f"materializer complete ({changed} changed step(s))",
            )
            result.add(
                "pipeline-state",
                "changed" if pipeline_state_changed else "unchanged",
                f"materialized {restored_runs} run record(s)",
            )
            findings = restore_findings(data_dir)
            if findings:
                raise InstallError("status findings: " + "; ".join(findings))
            if not recovery:
                (target / ".secretary-bootstrap").unlink(missing_ok=True)
            result.add(
                "status",
                "unchanged",
                "board, memory and operational configuration are ready"
                if secrets.complete
                else f"board and memory are ready, but {secrets.summary()}",
            )
            _set_installation_owner(data_dir, args.installation_user)
            _set_installation_owner(target, args.installation_user)
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
        phrase = parser.add_mutually_exclusive_group()
        phrase.add_argument(
            "--recovery-phrase-file",
            help="read the recovery phrase from this file; never pass it on the command line",
        )
        phrase.add_argument(
            "--recovery-phrase-stdin",
            action="store_true",
            help="read the recovery phrase from standard input",
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
