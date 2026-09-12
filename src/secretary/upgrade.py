"""``secretary upgrade``: pull a new product version and re-materialize the host.

One materializer and three entry points into it: ``upgrade`` pulls the new version and then runs
it, a fresh install runs it against an empty host, recovery runs it against a half-built one.
Nothing about a step knows which of the three called it, which is what makes them stay identical.

Every step is idempotent and reports one of ``changed``/``unchanged``/``skipped``/``failed``. A
failed step stops the run: later steps assume the earlier ones landed. ``--dry-run`` runs the
same decisions and performs no writes.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import stat
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from secretary import _proc, role_skills, state_repo
from secretary.automations import (
    AutomationError,
    OrcaAutomationClient,
    apply_automations,
    load_specs,
    workspaces_root,
)
from secretary.board.migrate import migrate_instance
from secretary.board.provision import provision as provision_board_store
from secretary.board.provision import verify_roles as verify_board_store_roles
from secretary.board.store import BoardStoreError, ensure_ignored, store_path
from secretary.board_transport import (
    BoardTransportError,
    ensure_from_runtime_values,
    transport_path,
)
from secretary.checkpoint import CheckpointPusher
from secretary.config import DataDirError, validate_instance
from secretary.head_registry import (
    HeadRegistryConfigError,
    assert_snapshot_current,
    canonical_heads,
    canonical_path,
    installed_heads,
    materialize_snapshot,
    record_source,
    snapshot_path,
    source_path,
)
from secretary.host import (
    FixtureHostSource,
    LiveHostSource,
    build_expectations,
    strict_manifest,
)
from secretary.host_apply import (
    ApplyInputs,
    HostCommandError,
    LiveOrcaRegistrar,
    OrcaRegistrar,
    SystemdUnitInstaller,
    UnitInstaller,
    apply_host,
    resolve_packaged,
    resolve_runtime_owner,
)
from secretary.memory.client_config import ClientConfigError, reconcile_clients
from secretary.memory.health import MemoryProbeError, probe_memory
from secretary.memory.pack import MemoryPackError, load_product_pack, materialize_product_pack
from secretary.projects.availability import ProjectAvailability
from secretary.runtime_env import RuntimeEnvError, RuntimeEnvMissing, read_runtime_env
from secretary.web.health import WebProbeError, probe_web, target_from_unit
from secretary.web.server import LoopbackOnly
from triggered_agents.runtime.paths import configured_product_root

MEMORY_COMPONENT = "memory"
WEB_COMPONENT = "web"
# Changes here require restarting the memory service even if its unit is unchanged.
MEMORY_CODE_PATHS = ("secretary/", "pyproject.toml", "uv.lock", "requirements.txt")
DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock", "requirements.txt")
# The bundled JSON Schemas are product data a long-lived process reads from the checkout through
# `importlib.resources` *after* it started, while its validation callables were loaded at start. So
# a schema move is its own named restart reason, not a detail of "code changed": it is the half of
# the split that makes an unrestarted process fail on files its own build shipped (secretary-1624).
SCHEMA_PATHS = ("secretary/schemas/",)
# How long a restarted web transport is given to answer one bounded loopback read.
WEB_PROBE_TIMEOUT_SECONDS = 20.0
RUFF_VERSION_RE = re.compile(r"^ruff==([^;\s]+)$")


@dataclass
class StepResult:
    name: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class UpgradeContext:
    """Everything the steps share, resolved once before the first of them runs."""

    instance_path: Path
    product_root: Path
    base_branch: str
    dry_run: bool
    units: UnitInstaller
    orca: OrcaRegistrar
    automations: OrcaAutomationClient
    host_fixture: Path | None = None
    pull: bool = True
    report: Any = None
    changed_paths: tuple[str, ...] = ()
    code_changed: bool = False
    schemas_changed: bool = False
    unit_changed: bool = False
    web_unit_changed: bool = False
    # Product-pack changes require incremental memory reconciliation.
    memory_pack_changed: bool = False
    memory_pack: Any = None
    # Resolve home-relative artifacts from the installation owner, not the invoker.
    runtime_user: str | None = None
    runtime_home: Path | None = None
    project_availability: ProjectAvailability = field(default_factory=ProjectAvailability)
    # Recovery may finish safe local work after retaining a checkpoint whose
    # remote publication failed. Every other caller keeps publication required.
    publication_policy: str = "required"
    pull_result: StepResult | None = None
    handoff_before: str | None = None
    handoff_after: str | None = None
    pulled_before: str | None = None
    pulled_after: str | None = None


@dataclass
class UpgradeResult:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)

    @property
    def changed(self) -> bool:
        return any(step.status == "changed" for step in self.steps)

    def render(self) -> str:
        lines = ["secretary upgrade"]
        for step in self.steps:
            suffix = f": {step.detail}" if step.detail else ""
            lines.append(f"  {step.status:9} {step.name}{suffix}")
        lines.append("status: " + ("ok" if self.ok else "failed"))
        return "\n".join(lines)


class GitError(RuntimeError):
    """A git command failed. The message carries git's reason, not a traceback."""


def _git(root: Path, args: list[str], timeout: int = 120) -> str:
    try:
        # Scrub inherited Git state so `-C root` selects this checkout.
        result = _proc.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
            timeout=timeout,
            env=state_repo.git_env(),
        )
    except FileNotFoundError:
        raise GitError("git not found") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out") from None
    except OSError:
        raise GitError(f"git {args[0]} could not run") from None
    if result.returncode != 0:
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(f"git {args[0]}: {reason[0] if reason else 'failed'}")
    return (result.stdout or "").strip()


def fast_forward(root: Path, base_branch: str) -> tuple[str, str]:
    """Fetch and fast-forward one checkout. Returns ``(before, after)``.

    Strictly ``--ff-only``: a checkout with local commits or a diverged history is left exactly as
    found and the caller hears why. Nothing in an upgrade may discard work that is only on this host.
    """
    before = _git(root, ["rev-parse", "HEAD"])
    _git(root, ["fetch", "--quiet", "origin", base_branch])
    _git(root, ["merge", "--ff-only", f"origin/{base_branch}"])
    return before, _git(root, ["rev-parse", "HEAD"])


def _changed_paths(root: Path, before: str, after: str) -> tuple[str, ...]:
    if before == after:
        return ()
    return tuple(_git(root, ["diff", "--name-only", f"{before}..{after}"]).splitlines())


def _touches(changed: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for path in changed for prefix in prefixes)


def step_pull(context: UpgradeContext) -> StepResult:
    if context.pull_result is not None:
        return context.pull_result
    if not context.pull:
        if context.handoff_before and context.handoff_after:
            return StepResult(
                "pull",
                "changed",
                f"{context.handoff_before[:12]} -> {context.handoff_after[:12]}; running pulled schedule",
            )
        return StepResult("pull", "skipped", "--no-pull")
    try:
        dirty = _git(context.product_root, ["status", "--porcelain"])
        if dirty:
            return StepResult("pull", "failed", "product checkout has uncommitted changes")
        if context.dry_run:
            _git(context.product_root, ["fetch", "--quiet", "origin", context.base_branch])
            head = _git(context.product_root, ["rev-parse", "HEAD"])
            target = _git(context.product_root, ["rev-parse", f"origin/{context.base_branch}"])
            if head == target:
                return StepResult("pull", "unchanged", head[:12])
            return StepResult("pull", "changed", f"{head[:12]} -> {target[:12]} (not applied)")
        before, after = fast_forward(context.product_root, context.base_branch)
    except GitError as exc:
        return StepResult("pull", "failed", str(exc))
    if before == after:
        return StepResult("pull", "unchanged", after[:12])
    context.changed_paths = _changed_paths(context.product_root, before, after)
    context.code_changed = _touches(context.changed_paths, MEMORY_CODE_PATHS)
    context.schemas_changed = _touches(context.changed_paths, SCHEMA_PATHS)
    context.pulled_before = before
    context.pulled_after = after
    return StepResult("pull", "changed", f"{before[:12]} -> {after[:12]}")


def _snapshot_install(venv_python: Path) -> bool:
    """Is the product installed into this venv as a copy rather than as the checkout itself?

    A snapshot install is a silent liability: nothing that follows moves it, so the venv keeps
    answering with whatever the code looked like when it was taken. An editable install cannot drift
    that way, so finding a snapshot is itself a reason to reinstall, whether or not a dependency
    manifest moved.
    """
    for dist_info in (venv_python.parent.parent / "lib").glob("python*/site-packages/secretary-*.dist-info"):
        try:
            direct_url = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return not bool(direct_url.get("dir_info", {}).get("editable"))
    return True


def _declared_ruff_version(product_root: Path) -> str:
    """Read the exact Ruff version the product's ``dev`` extra promises."""
    try:
        pyproject = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read the dev Ruff pin: {exc}") from None
    if not isinstance(dependencies, list):
        raise TypeError("the dev extra is not a dependency list")
    for dependency in dependencies:
        if isinstance(dependency, str) and (match := RUFF_VERSION_RE.fullmatch(dependency)):
            return match.group(1)
    raise ValueError("the dev extra declares no exact Ruff pin")


def _ruff_runtime_problem(product_root: Path, venv_python: Path) -> str:
    """Why this venv cannot run the Ruff version its checkout declares, if any."""
    try:
        expected = _declared_ruff_version(product_root)
    except (TypeError, ValueError) as exc:
        return str(exc)
    executable = venv_python.parent / "ruff"
    if not executable.is_file():
        return f"the pinned Ruff {expected} is missing"
    try:
        result = _proc.run([str(executable), "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return f"the pinned Ruff {expected} cannot run"
    if result.returncode != 0:
        return f"the pinned Ruff {expected} cannot run"
    actual = (result.stdout or result.stderr).strip()
    if actual != f"ruff {expected}":
        return f"the installed Ruff reports {actual or 'no version'}, not {expected}"
    return ""


def step_dependencies(context: UpgradeContext) -> StepResult:
    venv_python = context.product_root / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return StepResult("dependencies", "skipped", "no .venv in the product checkout")
    snapshot = _snapshot_install(venv_python)
    manifest_moved = _touches(context.changed_paths, DEPENDENCY_PATHS)
    ruff_problem = _ruff_runtime_problem(context.product_root, venv_python)
    if not snapshot and not manifest_moved and not ruff_problem:
        return StepResult("dependencies", "unchanged", "no dependency manifest moved")
    reasons = []
    if snapshot:
        reasons.append("the venv holds a snapshot install")
    if manifest_moved:
        reasons.append("a dependency manifest moved")
    if ruff_problem:
        reasons.append(ruff_problem)
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult(
            "dependencies", "changed", f"would reinstall the product dev extra into .venv: {reason}"
        )
    try:
        _proc.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                f"{context.product_root}[dev]",
            ],
            timeout=900,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return StepResult("dependencies", "failed", f"pip install exited {exc.returncode}")
    except (OSError, subprocess.TimeoutExpired):
        return StepResult("dependencies", "failed", "pip install could not run")
    context.code_changed = True
    return StepResult("dependencies", "changed", f"installed the product dev extra into .venv: {reason}")


def step_dependency_provenance(context: UpgradeContext) -> StepResult:
    """Prove core imports come from the selected production installation environment."""
    venv = context.product_root / ".venv"
    python = venv / "bin" / "python"
    if not python.is_file():
        path = store_path(context.instance_path)
        if path.exists() or path.is_symlink():
            return StepResult(
                "dependency-provenance",
                "failed",
                "a configured board store requires the selected product checkout's production venv",
            )
        return StepResult("dependency-provenance", "skipped", "no .venv in the product checkout")
    program = (
        "import importlib.util,json,pathlib,sys;"
        "names=('secretary','psycopg','sqlalchemy','alembic');"
        "print(json.dumps({'prefix':sys.prefix,'origins':{n:str(pathlib.Path(importlib.util.find_spec(n).origin).resolve()) for n in names}}))"
    )
    try:
        completed = _proc.run([str(python), "-P", "-c", program], timeout=60)
        if completed.returncode:
            raise ValueError("the import probe failed")
        evidence = json.loads(completed.stdout)
        origins = evidence["origins"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "could not import secretary, psycopg, SQLAlchemy and Alembic from the production venv",
        )
    root = context.product_root.resolve()
    try:
        Path(origins["secretary"]).resolve().relative_to(root)
        for name in ("psycopg", "sqlalchemy", "alembic"):
            Path(origins[name]).resolve().relative_to(venv.resolve())
    except (ValueError, TypeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "production imports escaped the selected product root or its venv",
        )
    detail = ", ".join(f"{name}={origins[name]}" for name in origins)
    return StepResult("dependency-provenance", "unchanged", detail)


def step_memory_clients(context: UpgradeContext) -> StepResult:
    """Materialize PO bridge entries without changing provider login state."""
    if not (context.product_root / ".venv").is_dir():
        return StepResult("memory-clients", "skipped", "no .venv in the product checkout")
    if context.runtime_home is None:
        return StepResult("memory-clients", "failed", "installation runtime home is unresolved")
    data_dir = context.report.data_dir
    if data_dir is None:
        return StepResult("memory-clients", "failed", "instance data directory is unresolved")
    try:
        result = reconcile_clients(
            context.product_root,
            context.runtime_home,
            data_dir,
            dry_run=context.dry_run,
        )
    except ClientConfigError as exc:
        return StepResult("memory-clients", "failed", str(exc))
    if not context.dry_run:
        managed = context.runtime_home / ".config" / "orca" / "codex-runtime-home" / "home" / "config.toml"
        user_codex = context.runtime_home / ".codex" / "config.toml"
        claude = context.runtime_home / ".claude.json"
        try:
            _set_runtime_directory_owner(user_codex.parent, context.runtime_user)
            for path in (managed, user_codex, claude):
                _set_runtime_owner(path, context.runtime_user)
        except GitError as exc:
            return StepResult("memory-clients", "failed", str(exc))
    if not result.changed:
        return StepResult("memory-clients", "unchanged", "Claude and Codex PO bridge entries current")
    action = "would reconcile" if context.dry_run else "reconciled"
    return StepResult("memory-clients", "changed", f"{action} {result.changed} client config(s)")


def _role_skills_manifest(context: UpgradeContext) -> Path:
    """The skill registry of the checkout being installed, which is not always the running one."""
    return role_skills.product_manifest_path(context.product_root)


def step_registries(context: UpgradeContext) -> StepResult:
    """Read every registry this upgrade materializes from, before anything is written.

    The steps that follow write in order — head snapshot, role worktrees, skills and entry points,
    host — and each reads operator-written configuration that can be malformed, so finding that out
    at the third write leaves a host half-moved. Parsing is not enough for the skill registry: a
    manifest whose declared ``SKILL.md`` is absent, whose target roots overlap, or whose entry point
    collides parses cleanly and is refused by `sync` after the head snapshot has been written, so the
    whole plan is decided here against the same manifests and home the later steps use.
    """
    manifest = _role_skills_manifest(context)
    try:
        registry = role_skills.load_registry(context.instance_path, product_manifest=manifest)
        problems = role_skills.unmaterializable(registry, context.runtime_home)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return StepResult("registries", "failed", f"skill registry: {exc}")
    if problems:
        return StepResult(
            "registries",
            "failed",
            f"skill registry: {problems[0]}",
        )
    try:
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        canonical_heads(context.product_root, context.instance_path)
    except HeadRegistryConfigError as exc:
        return StepResult("registries", "failed", str(exc))
    try:
        context.memory_pack = load_product_pack(context.product_root)
    except MemoryPackError as exc:
        return StepResult("registries", "failed", f"memory pack: {exc}")
    sources = ", ".join(str(source.path) for source in registry.sources)
    return StepResult("registries", "unchanged", f"{sources}, {canonical}, and memory pack are readable")


def step_memory_pack(context: UpgradeContext) -> StepResult:
    """Materialize the shipped pack only after `registries` validated its source."""
    pack = context.memory_pack
    try:
        if pack is None:
            pack = load_product_pack(context.product_root)
        data_dir = context.report.data_dir
        if data_dir is None:
            return StepResult("memory-pack", "failed", "instance has no resolved data directory")
        result = materialize_product_pack(
            pack,
            instance_dir=context.instance_path,
            data_dir=data_dir,
            dry_run=context.dry_run,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_export_check=lambda memory_dir: _assert_memory_export_readable(
                memory_dir, context.runtime_user
            ),
        )
    except MemoryPackError as exc:
        return StepResult("memory-pack", "failed", str(exc))
    context.memory_pack_changed = result.changed
    if not result.changed:
        return StepResult("memory-pack", "unchanged", "installed digest matches product pack")
    verb = "would reconcile" if context.dry_run else "reconciled"
    return StepResult(
        "memory-pack",
        "changed",
        f"{verb} {result.added} added, {result.updated} updated, {result.deleted} deleted, {result.retained} retained",
    )


def step_role_skills(context: UpgradeContext) -> StepResult:
    """Materialize the product skills of the checkout being installed, plus this installation's."""
    manifest = _role_skills_manifest(context)
    try:
        before = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if before["ok"]:
        return StepResult("role-skills", "unchanged", f"{len(before['targets'])} targets in sync")
    pending = len(before["missing"]) + len(before["drift"]) + len(before["entry_points"])
    if before["config_errors"] or before["source_missing"]:
        return StepResult(
            "role-skills", "failed", "manifest is not usable: overlapping roots or a missing source skill"
        )
    if context.dry_run:
        return StepResult("role-skills", "changed", f"would sync {pending} skill copies")
    try:
        after = role_skills.sync(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if not after["after"]["ok"]:
        return StepResult("role-skills", "failed", "sync ran but the audit is still red")
    return StepResult("role-skills", "changed", f"synced {pending} skill copies")


def step_head_registry(context: UpgradeContext) -> StepResult:
    """Keep the installation snapshot derived from whichever registry is this host's canon.

    The installation's own ``heads/heads.toml`` when it owns one, else the product's portable
    default. The pin next to the snapshot records which of the two won, plus the checkout and
    revision, and the live tick validates the pin against the snapshot.
    """
    target = snapshot_path(context.instance_path)
    try:
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        changed = materialize_snapshot(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
        )
        repinned = record_source(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
        )
        if not context.dry_run:
            # The installation account must own recovery files before it commits them.
            _set_runtime_owner(target, context.runtime_user)
            _set_runtime_owner(source_path(context.instance_path), context.runtime_user)
    except (HeadRegistryConfigError, GitError) as exc:
        return StepResult("head-registry", "failed", str(exc))
    if not changed and not repinned:
        return StepResult("head-registry", "unchanged", f"{target} matches {canonical}")
    verb = "would regenerate" if context.dry_run else "regenerated"
    what = target if changed else source_path(context.instance_path)
    if changed and repinned:
        what = f"{target} and {source_path(context.instance_path)}"
    return StepResult("head-registry", "changed", f"{verb} {what}")


def step_instance_packing(context: UpgradeContext) -> StepResult:
    """Keep the private instance repo's packing controls bounded and local."""
    try:
        drifted = state_repo.configure_packing_controls(context.instance_path, dry_run=context.dry_run)
    except state_repo.StateRepoError as exc:
        return StepResult("instance-packing", "failed", str(exc))
    if not drifted:
        return StepResult("instance-packing", "unchanged", "local Git packing controls match")
    action = "would set" if context.dry_run else "set"
    return StepResult(
        "instance-packing",
        "changed",
        f"{action} local Git packing controls: {', '.join(drifted)}",
    )


def step_publish_head_registry(context: UpgradeContext) -> StepResult:
    """Commit and publish the installed head pair as one recovery-canon update."""
    if context.dry_run:
        return StepResult("head-registry-checkpoint", "skipped", "--dry-run made no recovery publication")
    try:
        instance = state_repo.require_repo(context.instance_path)
        with state_repo.state_repo_lock(instance):
            commit = state_repo.commit(
                instance,
                state_repo.HEADS_PATHSPEC,
                state_repo.HEADS_CHECKPOINT_MESSAGE,
            )
            tracked = state_repo.git(
                instance,
                ["ls-files", "--", *state_repo.HEADS_PATHSPEC],
                label="inspect head registry recovery pair",
            ).split()
            missing = [path for path in state_repo.HEADS_PATHSPEC if path not in tracked]
            if missing:
                raise state_repo.StateRepoError(
                    "head registry recovery pair is not tracked by the instance repo: " + ", ".join(missing)
                )
    except state_repo.StateRepoError as exc:
        return StepResult("head-registry-checkpoint", "failed", str(exc))

    outcome = CheckpointPusher(instance).push()
    status = str(outcome.get("status") or "failed")
    if status not in ("pushed", "unchanged"):
        reason = str(outcome.get("reason") or "remote publication did not complete")
        retained = f"; local checkpoint {commit}" if commit else "; local recovery pair remains committed"
        return StepResult(
            "head-registry-checkpoint",
            "degraded" if context.publication_policy == "recovery-degraded" else "failed",
            f"head registry checkpoint {status}: {reason}{retained}",
        )
    detail = f"published {commit or outcome.get('last_push_commit', '')[:12]}".rstrip()
    return StepResult("head-registry-checkpoint", "changed" if commit else "unchanged", detail)


def desired_role_worktrees(product_root: Path, home: Path | None = None) -> list[Path]:
    """Every derived role worktree shipped by this product, present or absent."""
    root = workspaces_root(home) / "secretary"
    agents = product_root / "src" / "triggered_agents" / "agents"
    try:
        names = sorted(entry.name for entry in agents.iterdir() if (entry / "automation.toml").is_file())
    except OSError:
        return []
    return [root / name for name in names]


def _set_runtime_owner(path: Path, runtime_user: str | None) -> None:
    """Repair root-created runtime files without traversing links or hardlinks."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None

    def assign(candidate: Path) -> None:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            return
        os.chown(candidate, account.pw_uid, account.pw_gid, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return
        with os.scandir(candidate) as children:
            for child in children:
                assign(Path(child.path))

    try:
        assign(path)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _assert_memory_export_readable(memory_dir: Path, runtime_user: str | None) -> None:
    """Require the daemon account to own readable export files before activation."""
    # Root publication must prove runtime-account ownership.
    if not runtime_user or os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    for name in ("export.ndjson", "export.json", "manifest.json"):
        path = memory_dir / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise GitError(f"could not inspect memory export {path}: {exc}") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GitError(f"memory export is not a regular file: {path}")
        if info.st_uid != account.pw_uid or not info.st_mode & stat.S_IRUSR:
            raise GitError(f"memory export is not readable by runtime user {runtime_user}: {path}")


def _set_runtime_directory_owner(path: Path, runtime_user: str | None) -> None:
    """Make a created workspace ancestor traversable and writable without walking siblings."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise GitError(f"workspace ancestor is not a real directory: {path}")
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _workspace_owner_dirs(worktree: Path) -> tuple[Path, ...]:
    """The exact parents this materializer can create for a role worktree."""
    secretary_root = worktree.parent
    workspace_root = secretary_root.parent
    roots = [workspace_root, secretary_root]
    if workspace_root.name == "workspaces" and workspace_root.parent.name == "orca":
        roots.insert(0, workspace_root.parent)
    return tuple(roots)


def _worktree_git_dir(worktree: Path) -> Path | None:
    """Return the linked-worktree administrative directory named by its .git file."""
    try:
        line = (worktree / ".git").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not line.startswith("gitdir: "):
        return None
    return Path(line.removeprefix("gitdir: ")).expanduser().resolve()


def step_worktrees(context: UpgradeContext) -> StepResult:
    worktrees = desired_role_worktrees(context.product_root, context.runtime_home)
    if not worktrees:
        return StepResult("role-worktrees", "skipped", "the product ships no role worktrees")
    created: list[str] = []
    moved: list[str] = []
    stuck: list[str] = []
    # Sudo may create worktree metadata the runtime user must own.
    try:
        _set_runtime_owner(worktrees[0].parent, context.runtime_user)
        for parent in _workspace_owner_dirs(worktrees[0]):
            _set_runtime_directory_owner(parent, context.runtime_user)
    except GitError as exc:
        return StepResult("role-worktrees", "failed", str(exc))
    for worktree in worktrees:
        try:
            _set_runtime_owner(worktree, context.runtime_user)
            admin = _worktree_git_dir(worktree)
            if admin is not None:
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if not (worktree / ".git").exists():
            if worktree.exists() and any(worktree.iterdir()):
                stuck.append(f"{worktree.name}: target exists and is not a managed worktree")
                continue
            if context.dry_run:
                created.append(worktree.name)
                continue
            try:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                for parent in _workspace_owner_dirs(worktree):
                    _set_runtime_directory_owner(parent, context.runtime_user)
                _git(
                    context.product_root,
                    ["worktree", "add", "--detach", str(worktree), "HEAD"],
                    timeout=300,
                )
                _set_runtime_owner(worktree, context.runtime_user)
                admin = _worktree_git_dir(worktree)
                if admin is None:
                    raise GitError(f"could not locate Git administration for {worktree}")
                _set_runtime_owner(worktree.parent, context.runtime_user)
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
            except (GitError, OSError) as exc:
                stuck.append(f"{worktree.name}: {exc}")
                continue
            created.append(worktree.name)
            continue
        try:
            if context.dry_run:
                head = _git(worktree, ["rev-parse", "HEAD"])
                _git(worktree, ["fetch", "--quiet", "origin", context.base_branch])
                if head != _git(worktree, ["rev-parse", f"origin/{context.base_branch}"]):
                    moved.append(worktree.name)
                continue
            before, after = fast_forward(worktree, context.base_branch)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if before != after:
            moved.append(worktree.name)
    if stuck:
        return StepResult("role-worktrees", "failed", "; ".join(stuck))
    if not moved and not created:
        return StepResult("role-worktrees", "unchanged", f"{len(worktrees)} worktrees current")
    details = []
    if created:
        details.append("created " + ", ".join(created))
    if moved:
        details.append("updated " + ", ".join(moved))
    return StepResult("role-worktrees", "changed", "; ".join(details))


def step_host(context: UpgradeContext) -> StepResult:
    report = context.report
    assert report.data_dir is not None
    manifest = report.data_dir / "host-managed.json"
    try:
        packaged = resolve_packaged(
            report.instance,
            context.product_root / "packaging" / "systemd",
            product_root=context.product_root,
            instance_path=context.instance_path,
            data_dir=report.data_dir,
            runtime_user=context.runtime_user,
        )
    except (DataDirError, HostCommandError, ValueError) as exc:
        return StepResult("host", "failed", str(exc))
    expected = build_expectations(report.bindings, report.host, availability=context.project_availability)
    source = (
        FixtureHostSource(context.host_fixture)
        if context.host_fixture
        else LiveHostSource(orca_user=context.runtime_user)
    )
    collected = source.collect(expected)
    if collected.errors:
        reasons = "; ".join(f"{kind}: {reason}" for kind, reason in sorted(collected.errors.items()))
        return StepResult("host", "failed", f"host inventory unavailable: {reasons}")
    managed, error = strict_manifest(manifest)
    if error:
        return StepResult("host", "failed", error)
    result = apply_host(
        ApplyInputs(
            instance=report.instance,
            bindings=report.bindings,
            inventory=collected.inventory,
            managed=managed,
            manifest_path=manifest,
            packaged=packaged,
            runtime_user=context.runtime_user,
            project_availability=context.project_availability,
        ),
        units=context.units,
        orca=context.orca,
        dry_run=context.dry_run,
    )
    pending = [change for change in result.changes if change.action not in {"unchanged", "deferred"}]
    deferred = [change for change in result.changes if change.action == "deferred"]
    if result.conflicts:
        names = ", ".join(change.name for change in result.conflicts)
        return StepResult("host", "failed", f"unowned names in our namespace: {names}")
    if result.errors:
        return StepResult("host", "failed", "; ".join(result.errors))
    context.unit_changed = any(
        change.kind == "unit" and change.name.startswith(_component_unit_prefix(report, MEMORY_COMPONENT))
        for change in pending
    )
    # Both web units count: the front is `PartOf=` the transport, so a change to either is a change
    # the pair has to be restarted for, and restarting the transport is what restarts the pair.
    context.web_unit_changed = any(
        change.kind == "unit" and change.name.startswith(_component_unit_prefix(report, WEB_COMPONENT))
        for change in pending
    )
    if not pending:
        detail = f"{len(result.changes)} resources reconciled"
        if deferred:
            detail += f"; {len(deferred)} unavailable project registrations deferred"
        return StepResult("host", "unchanged", detail)
    detail = ", ".join(f"{change.action} {change.name}" for change in pending)
    return StepResult("host", "changed", detail)


def _component_unit_prefix(report: Any, component: str) -> str:
    """The installation's unit-name prefix for one component's units.

    Deliberately not closed with a `.`: the web component ships `secretary-web.service` *and*
    `secretary-web-front.service`, and both belong to it.
    """
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    return f"{prefix}{component}" if isinstance(prefix, str) else component


def _memory_unit_prefix(report: Any) -> str:
    return f"{_component_unit_prefix(report, MEMORY_COMPONENT)}."


def step_automations(context: UpgradeContext) -> StepResult:
    try:
        specs = load_specs(context.product_root, home=context.runtime_home)
        changes, _ = apply_automations(specs, context.automations, dry_run=context.dry_run)
    except AutomationError as exc:
        return StepResult("automations", "failed", str(exc))
    pending = [change for change in changes if change.action != "unchanged"]
    if not pending:
        return StepResult("automations", "unchanged", f"{len(changes)} automations current")
    detail = ", ".join(
        f"{change.action} {change.name}" + (f" ({', '.join(change.drifted)})" if change.drifted else "")
        for change in pending
    )
    return StepResult("automations", "changed", detail)


def step_memory(context: UpgradeContext) -> StepResult:
    report = context.report
    unit = f"{_memory_unit_prefix(report)}service"
    if not context.units.is_active(unit):
        reason = "service is not active"
    elif context.unit_changed:
        reason = "unit file changed"
    elif context.code_changed:
        reason = "product code or dependencies changed"
    elif context.memory_pack_changed:
        reason = "memory pack export changed"
    else:
        return StepResult("memory", "unchanged", "serving the current code")
    if context.dry_run:
        return StepResult("memory", "changed", f"would restart {unit}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    try:
        data_dir = report.data_dir
        if data_dir is None:
            return StepResult(
                "memory", "failed", "authenticated probe: instance has no resolved data directory"
            )
        probe_memory(
            data_dir,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_user=context.runtime_user,
        )
    except (MemoryProbeError, GitError) as exc:
        return StepResult("memory", "failed", f"authenticated probe failed: {exc}")
    return StepResult("memory", "changed", f"restarted and authenticated {unit}: {reason}")


def step_web(context: UpgradeContext) -> StepResult:
    """Make the long-lived web process coherent with what this upgrade just materialized.

    The web transport was the one supported long-lived process an upgrade did not own. That is not
    a gap you can see from a step list: `secretary upgrade` reported `ok`, the checkout was current,
    the unit was current, and `secretary-web.service` went on running the callables it had imported
    a day earlier. On 2026-09-11 that process answered every route with an empty reply for nineteen
    hours, because `importlib.resources` reads the bundled schemas from the checkout *now* while the
    validator that reads them was loaded *then*: a new relative `$ref` against an old registry-less
    validator (secretary-1624). Timestamps could not fix that and were never the defect; the process
    was. So the reconciliation is a restart, and it is here, after everything the new process needs.

    Three properties are load-bearing:

    **It is last of the materializing steps.** A restart is the moment the old code stops being the
    code that answers, so everything the new process reads has to be in place first: the checkout
    (`step_pull`), the installed dependencies and the bundled schemas that came with them
    (`step_dependencies`), and the unit itself (`step_host`). `run_steps` stops at the first failed
    step, so a failed prerequisite means this step does not run at all and the run is failed — it
    can never be the case that something reported the web as serving current code over a failed
    prerequisite.

    **A restart without a read is not evidence.** systemd accepting `restart` says nothing about
    whether the new process bound its socket or whether it can answer. So a restart is followed by
    one bounded loopback GET against the address the *installed unit* names, and a probe that does
    not come back 200 fails the step: an upgrade that cannot read the transport it just restarted
    does not get to say the transport is current.

    **The unit is optional and this step never starts it.** The web is not on every installation,
    and a stopped transport is a state an operator chose — an upgrade that quietly published a
    dashboard because it found a unit file would be making that choice for them. So an uninstalled
    or inactive unit is reported as skipped, with which of the two it was, and nothing is started.
    That is the one place this step deliberately differs from `step_memory`, which starts a stopped
    memory service because the pipeline cannot run without it.

    Only `secretary-web.service` is restarted. `secretary-web-front.service` is `PartOf=` it and
    comes along, which is also why a change to either unit file is a reason to restart this one.
    """
    report = context.report
    unit = f"{_component_unit_prefix(report, WEB_COMPONENT)}.service"
    installed = context.units.installed(unit)
    if installed is None:
        return StepResult("web", "skipped", f"{unit} is not installed; this host serves no web transport")
    if not context.units.is_active(unit):
        return StepResult(
            "web", "skipped", f"{unit} is installed but not active; an upgrade does not start it"
        )
    reasons = []
    if context.web_unit_changed:
        reasons.append("a web unit file changed")
    if context.schemas_changed:
        reasons.append("bundled schemas changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    if not reasons:
        return StepResult(
            "web",
            "unchanged",
            f"{unit} already runs this checkout, these schemas and these dependencies",
        )
    try:
        target = target_from_unit(installed)
    except LoopbackOnly as refused:
        return StepResult("web", "failed", f"{unit} does not serve a loopback address: {refused}")
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("web", "changed", f"would restart {unit} and probe {target.url}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", f"restarting {unit} failed: {exc}")
    try:
        status = probe_web(target, timeout_seconds=WEB_PROBE_TIMEOUT_SECONDS)
    except WebProbeError as exc:
        return StepResult("web", "failed", f"{unit} restarted but the loopback probe failed: {exc}")
    return StepResult("web", "changed", f"restarted {unit} and probed {target.url} -> {status}: {reason}")


def step_verify(context: UpgradeContext) -> StepResult:
    """Re-plan against the host we just wrote. A second pass must be a no-op."""
    if context.dry_run:
        return StepResult("verify", "skipped", "--dry-run made no changes to verify")
    probe = replace(context, dry_run=True, pull=False)
    result = step_host(probe)
    if result.failed:
        return StepResult("verify", "failed", f"host is still not reconciled: {result.detail}")
    if result.status == "changed":
        return StepResult("verify", "failed", f"reconcile is not idempotent: {result.detail}")
    try:
        audit = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=_role_skills_manifest(context),
            home=context.runtime_home,
        )
    except (OSError, ValueError) as exc:
        return StepResult("verify", "failed", str(exc))
    if not audit["ok"]:
        return StepResult("verify", "failed", "role skills are still out of sync")
    try:
        assert_snapshot_current(context.instance_path, context.product_root)
        installed_heads(context.instance_path)
    except HeadRegistryConfigError as exc:
        return StepResult("verify", "failed", str(exc))
    try:
        dirty = state_repo.status(context.instance_path, state_repo.HEADS_PATHSPEC)
    except state_repo.StateRepoError as exc:
        return StepResult("verify", "failed", str(exc))
    if dirty:
        return StepResult(
            "verify", "failed", f"head registry recovery pair remains dirty: {dirty.splitlines()[0]}"
        )
    return StepResult("verify", "unchanged", "host reconciled and role skills in sync")


def step_board_transport(context: UpgradeContext) -> StepResult:
    """Migrate old runtime values once, or create the deterministic local config."""
    try:
        try:
            values = read_runtime_env(context.instance_path, require_ignored=False)
        except RuntimeEnvMissing:
            values = {}
        outcome = ensure_from_runtime_values(
            context.instance_path,
            legacy_values=values,
            runtime_env=context.instance_path / "runtime.env",
            dry_run=context.dry_run,
        )
    except (BoardTransportError, RuntimeEnvError) as exc:
        return StepResult("board-transport", "failed", str(exc))
    if not context.dry_run:
        try:
            _set_runtime_owner(context.instance_path / "runtime.env", context.runtime_user)
            _set_runtime_owner(transport_path(context.instance_path), context.runtime_user)
            _set_runtime_owner(context.instance_path / ".gitignore", context.runtime_user)
            _set_runtime_owner(context.instance_path / ".git", context.runtime_user)
        except GitError as exc:
            return StepResult("board-transport", "failed", str(exc))
    return StepResult(
        "board-transport",
        "unchanged" if not outcome.changed else "would-change" if context.dry_run else "changed",
        outcome.render(dry_run=context.dry_run),
    )


def step_board_store(context: UpgradeContext) -> StepResult:
    """Bring a configured board store to the schema this build ships (§7.4).

    Three outcomes, and the first of them is the one that matters today. An installation with no
    `board-store.env` has no store to migrate — that is every installation until the card that
    provisions one lands — so the step is an explicit no-op and the upgrade proceeds exactly as it
    did before this step existed. It is not a warning and it never stops the run.

    With a complete file it reconciles the file's git exclusion, connects as `secretary_owner` and
    applies what Alembic owes, naming both. With a file that is present but partial, unreadable,
    unreachable or **tracked by the instance repository** it fails with that reason: a store that
    is configured and broken is not something an upgrade may walk past, because the next step it
    would walk to is a service restart.

    The exclusion is reconciled here and enforced in `board_store.resolve`, so this step is where
    an operator *sees* it and not the only place it happens.

    It runs after `step_dependencies` because SQLAlchemy, Alembic and the driver have to be
    installed before it can connect, which is exactly where §7.4 places it.
    """
    path = store_path(context.instance_path)
    if not path.exists() and not path.is_symlink():
        return StepResult("board-store", "skipped", "no board-store.env; the board store is not configured")
    try:
        lifecycle = ensure_ignored(context.instance_path, dry_run=context.dry_run)
        revisions = migrate_instance(context.instance_path, dry_run=context.dry_run)
    except BoardStoreError as exc:
        return StepResult("board-store", "failed", str(exc))
    prefix = f"{lifecycle.render(dry_run=context.dry_run)}; " if lifecycle.changed else ""
    if not revisions:
        return StepResult(
            "board-store",
            "would-change"
            if lifecycle.changed and context.dry_run
            else "changed"
            if lifecycle.changed
            else "unchanged",
            f"{prefix}board store schema is already current",
        )
    listed = ", ".join(revisions)
    if context.dry_run:
        return StepResult(
            "board-store", "would-change", f"{prefix}would apply board store migrations {listed}"
        )
    return StepResult("board-store", "changed", f"{prefix}applied board store migrations {listed}")


def step_board_store_provision(context: UpgradeContext) -> StepResult:
    """Reconcile the container only for an installation carrying the lifecycle marker."""
    try:
        outcome = provision_board_store(context.instance_path, dry_run=context.dry_run)
    except BoardStoreError as exc:
        return StepResult("board-store-provision", "failed", str(exc))
    if outcome is None:
        return StepResult(
            "board-store-provision", "skipped", "no board-store.env; PostgreSQL is not provisioned"
        )
    return StepResult(
        "board-store-provision",
        "would-change"
        if context.dry_run and outcome.changed
        else "changed"
        if outcome.changed
        else "unchanged",
        outcome.render(dry_run=context.dry_run),
    )


def step_board_store_roles(context: UpgradeContext) -> StepResult:
    path = store_path(context.instance_path)
    if not path.exists() and not path.is_symlink():
        return StepResult("board-store-roles", "skipped", "PostgreSQL is not provisioned")
    if context.dry_run:
        return StepResult("board-store-roles", "skipped", "--dry-run does not test role logins")
    try:
        verify_board_store_roles(context.instance_path)
    except BoardStoreError as exc:
        return StepResult("board-store-roles", "failed", str(exc))
    return StepResult("board-store-roles", "unchanged", "owner/app/read role boundary verified")


# Validate registries before any mutating materialization step.
STEPS: tuple[Callable[[UpgradeContext], StepResult], ...] = (
    step_pull,
    step_registries,
    step_memory_pack,
    step_board_transport,
    step_dependencies,
    step_dependency_provenance,
    step_board_store_provision,
    step_board_store,
    step_board_store_roles,
    step_memory_clients,
    step_head_registry,
    step_instance_packing,
    step_publish_head_registry,
    step_worktrees,
    step_role_skills,
    step_host,
    step_automations,
    step_memory,
    # Last of the materializing steps: a restart is the moment the new code becomes the code that
    # answers, so it follows the checkout, the dependencies, the schemas and the unit.
    step_web,
    step_verify,
)


def run_steps(context: UpgradeContext, steps=STEPS) -> UpgradeResult:
    result = UpgradeResult()
    for step in steps:
        outcome = step(context)
        result.steps.append(outcome)
        if outcome.failed:
            break
    return result


def running_product_root() -> Path:
    """The checkout this module was imported from.

    For reading what this process itself ships. Never for deciding what to install: that is
    `default_product_root`.
    """
    return Path(__file__).resolve().parents[2]


def default_product_root() -> Path:
    """The checkout an install or upgrade materializes when nothing names one.

    The configured one, or ``~/secretary`` — never the checkout the running module happens to sit in.
    A candidate checkout is a normal place to run ``secretary upgrade`` from, and installing whatever
    executed the command would make the caller's working directory decide the product version.
    ``--product-root`` and ``TA_SECRETARY_REPO`` still win, in that order.
    """
    return configured_product_root()


def run_upgrade(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print(f"secretary upgrade: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 2
    product_root = Path(args.product_root).expanduser() if args.product_root else default_product_root()
    # Resolve the checkout owner before materializing home-relative paths.
    instance_path = report.instance_path.parent
    try:
        runtime_user, runtime_home = resolve_runtime_owner(instance_path, getattr(args, "runtime_user", None))
    except ValueError as exc:
        print(f"secretary upgrade: {exc}")
        return 2
    # Run Orca as the runtime user; root has no Orca runtime.
    orca_user = runtime_user if os.geteuid() == 0 else None
    context = UpgradeContext(
        instance_path=instance_path,
        product_root=product_root,
        base_branch=args.base_branch,
        dry_run=args.dry_run,
        units=SystemdUnitInstaller(),
        orca=LiveOrcaRegistrar(orca_user),
        automations=OrcaAutomationClient(orca_user),
        host_fixture=Path(args.host_fixture) if args.host_fixture else None,
        pull=not args.no_pull,
        report=report,
        runtime_user=runtime_user,
        runtime_home=runtime_home,
    )
    handoff = os.environ.get("SECRETARY_UPGRADE_HANDOFF")
    if handoff:
        try:
            marker = json.loads(handoff)
            before = str(marker["before"])
            after = str(marker["after"])
            if _git(product_root, ["rev-parse", "HEAD"]) != after:
                raise ValueError("checkout no longer matches the pulled revision")
            if _git(product_root, ["status", "--porcelain"]):
                raise ValueError("checkout became dirty during pulled-code handoff")
            changed_paths = marker.get("changed_paths")
            if not isinstance(changed_paths, list) or not all(
                isinstance(path, str) for path in changed_paths
            ):
                raise ValueError("handoff has no valid changed-path set")
        except (GitError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"secretary upgrade: invalid pulled-code handoff: {exc}")
            return 1
        context.pull = False
        context.handoff_before = before
        context.handoff_after = after
        context.changed_paths = tuple(changed_paths)
        context.code_changed = _touches(context.changed_paths, MEMORY_CODE_PATHS)
    elif context.pull and not context.dry_run:
        pulled = step_pull(context)
        if pulled.failed:
            result = UpgradeResult([pulled])
            print(result.render())
            return 1
        if pulled.status == "changed":
            before = context.pulled_before
            after = context.pulled_after
            if before is None or after is None:
                print("secretary upgrade: pull did not record the before/after revisions")
                return 1
            try:
                _exec_pulled_upgrade(
                    args,
                    product_root,
                    before=before,
                    after=after,
                    changed_paths=context.changed_paths,
                )
            except OSError as exc:
                print(f"secretary upgrade: could not run pulled revision {after[:12]}: {exc}")
                return 1
            raise AssertionError("exec returned unexpectedly")
        context.pull_result = pulled
    result = run_steps(context)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok" if result.ok else "failed",
                    "dry_run": context.dry_run,
                    "steps": [
                        {"name": step.name, "status": step.status, "detail": step.detail}
                        for step in result.steps
                    ],
                },
                sort_keys=True,
                indent=2,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1


def _exec_pulled_upgrade(
    args: Any,
    product_root: Path,
    *,
    before: str,
    after: str,
    changed_paths: tuple[str, ...],
) -> None:
    """Replace the import-bound process with the exact checkout revision it just pulled."""
    python = product_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"the pulled checkout has no production interpreter at {python}")
    argv = [
        str(python),
        "-P",
        "-m",
        "secretary",
        "upgrade",
        "--instance",
        str(args.instance),
        "--no-pull",
        "--base-branch",
        str(args.base_branch),
        "--product-root",
        str(product_root),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if getattr(args, "runtime_user", None):
        argv.extend(("--runtime-user", str(args.runtime_user)))
    if getattr(args, "host_fixture", None):
        argv.extend(("--host-fixture", str(args.host_fixture)))
    if args.json:
        argv.append("--json")
    environment = dict(os.environ)
    environment["SECRETARY_UPGRADE_HANDOFF"] = json.dumps(
        {"before": before, "after": after, "changed_paths": list(changed_paths)}
    )
    os.execve(python, argv, environment)


def add_upgrade_command(subparsers) -> None:
    upgrade = subparsers.add_parser(
        "upgrade",
        help="pull the current product version and re-materialize this installation",
    )
    upgrade.add_argument("--instance", required=True, help="path to an instance dir or instance.yaml")
    upgrade.add_argument("--dry-run", action="store_true", help="decide every step but write nothing")
    upgrade.add_argument("--no-pull", action="store_true", help="re-materialize without moving the checkout")
    upgrade.add_argument("--base-branch", default="main")
    upgrade.add_argument(
        "--product-root",
        help="product checkout to upgrade (defaults to TA_SECRETARY_REPO, else ~/secretary)",
    )
    upgrade.add_argument(
        "--runtime-user",
        help="account this installation belongs to, whose home every home-relative path is "
        "materialized under (default: the owner of the instance checkout)",
    )
    upgrade.add_argument("--host-fixture", metavar="DIR", help=argparse.SUPPRESS)
    upgrade.add_argument("--json", action="store_true", help="emit the step report as JSON")
    upgrade.set_defaults(handler=run_upgrade)
