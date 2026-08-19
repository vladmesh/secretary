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
import stat
import subprocess
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
from secretary.runtime_env import RuntimeEnvError, RuntimeEnvMissing, read_runtime_env
from triggered_agents.runtime.paths import configured_product_root

MEMORY_COMPONENT = "memory"
# A pull that touches any of these can change what the long-running memory
# service executes, so the service has to be restarted even when its unit file
# is byte-identical. Restarting only on a changed unit leaves the MCP serving
# the previous release's code, which is the opposite of re-materializing.
MEMORY_CODE_PATHS = ("secretary/", "pyproject.toml", "uv.lock", "requirements.txt")
DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock", "requirements.txt")


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
    unit_changed: bool = False
    # The account that owns the selected installation and the home its paths hang off. Everything
    # home-relative an upgrade materializes — skills, command entry points, role worktrees, the
    # workspaces an automation is registered with — resolves against this home rather than the
    # invoking process's, so a repair run as root writes what the rendered units then name.
    runtime_user: str | None = None
    runtime_home: Path | None = None


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
        # `-C root` selects the checkout only if no inherited `GIT_DIR` outranks
        # it, so an upgrade takes the same scrubbed environment as every other
        # Git child of this product.
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
    if not context.pull:
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


def step_dependencies(context: UpgradeContext) -> StepResult:
    venv_python = context.product_root / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return StepResult("dependencies", "skipped", "no .venv in the product checkout")
    snapshot = _snapshot_install(venv_python)
    if not snapshot and not _touches(context.changed_paths, DEPENDENCY_PATHS):
        return StepResult("dependencies", "unchanged", "no dependency manifest moved")
    reason = "the venv holds a snapshot install" if snapshot else "a dependency manifest moved"
    if context.dry_run:
        return StepResult("dependencies", "changed", f"would reinstall the product into .venv: {reason}")
    try:
        _proc.run(
            [str(venv_python), "-m", "pip", "install", "--quiet", "-e", str(context.product_root)],
            timeout=900,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return StepResult("dependencies", "failed", f"pip install exited {exc.returncode}")
    except (OSError, subprocess.TimeoutExpired):
        return StepResult("dependencies", "failed", "pip install could not run")
    context.code_changed = True
    return StepResult("dependencies", "changed", f"reinstalled the product into .venv: {reason}")


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
    sources = ", ".join(str(source.path) for source in registry.sources)
    return StepResult("registries", "unchanged", f"{sources} and {canonical} are readable")


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
        return StepResult("role-skills", "failed", "manifest is not usable: overlapping roots or a missing source skill")
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
    except HeadRegistryConfigError as exc:
        return StepResult("head-registry", "failed", str(exc))
    if not changed and not repinned:
        return StepResult("head-registry", "unchanged", f"{target} matches {canonical}")
    verb = "would regenerate" if context.dry_run else "regenerated"
    what = target if changed else source_path(context.instance_path)
    if changed and repinned:
        what = f"{target} and {source_path(context.instance_path)}"
    return StepResult("head-registry", "changed", f"{verb} {what}")


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
                "checkpoint(heads): publish installed head registry",
            )
            tracked = state_repo.git(
                instance,
                ["ls-files", "--", *state_repo.HEADS_PATHSPEC],
                label="inspect head registry recovery pair",
            ).split()
            missing = [path for path in state_repo.HEADS_PATHSPEC if path not in tracked]
            if missing:
                raise state_repo.StateRepoError(
                    "head registry recovery pair is not tracked by the instance repo: "
                    + ", ".join(missing)
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
            "failed",
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
    # `recover` and `upgrade` may run under sudo.  Git makes both the linked
    # checkout and its administration directory as that invoking user, while
    # the systemd units subsequently run as `runtime_user`.
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
    expected = build_expectations(report.bindings, report.host)
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
        ),
        units=context.units,
        orca=context.orca,
        dry_run=context.dry_run,
    )
    pending = [change for change in result.changes if change.action != "unchanged"]
    if result.conflicts:
        names = ", ".join(change.name for change in result.conflicts)
        return StepResult("host", "failed", f"unowned names in our namespace: {names}")
    if result.errors:
        return StepResult("host", "failed", "; ".join(result.errors))
    context.unit_changed = any(
        change.kind == "unit" and change.name.startswith(_memory_unit_prefix(report)) for change in pending
    )
    if not pending:
        return StepResult("host", "unchanged", f"{len(result.changes)} resources reconciled")
    detail = ", ".join(f"{change.action} {change.name}" for change in pending)
    return StepResult("host", "changed", detail)


def _memory_unit_prefix(report: Any) -> str:
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    return f"{prefix}{MEMORY_COMPONENT}." if isinstance(prefix, str) else f"{MEMORY_COMPONENT}."


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
    else:
        return StepResult("memory", "unchanged", "serving the current code")
    if context.dry_run:
        return StepResult("memory", "changed", f"would restart {unit}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    return StepResult("memory", "changed", f"restarted {unit}: {reason}")


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
            context.instance_path, legacy_values=values,
            runtime_env=context.instance_path / "runtime.env", dry_run=context.dry_run,
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


# `registries` runs directly after the pull and before every step that writes. `dependencies` is
# one of those: a checkout with a `.venv` and a moved dependency manifest gets `pip install -e`,
# which mutates the checkout being installed. Rejecting a malformed manifest, overlay or head canon
# after that has already left a host part-way onto a version it never finished installing.
STEPS: tuple[Callable[[UpgradeContext], StepResult], ...] = (
    step_pull,
    step_registries,
    step_board_transport,
    step_dependencies,
    step_head_registry,
    step_publish_head_registry,
    step_worktrees,
    step_role_skills,
    step_host,
    step_automations,
    step_memory,
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
    # `validate_instance` accepts either a checkout or instance.yaml; the owner is a property of
    # the checkout. Resolving it here rather than inside the host step is what makes every
    # home-relative path an upgrade materializes agree with the units it renders.
    instance_path = report.instance_path.parent
    try:
        runtime_user, runtime_home = resolve_runtime_owner(
            instance_path, getattr(args, "runtime_user", None)
        )
    except ValueError as exc:
        print(f"secretary upgrade: {exc}")
        return 2
    context = UpgradeContext(
        instance_path=instance_path,
        product_root=product_root,
        base_branch=args.base_branch,
        dry_run=args.dry_run,
        units=SystemdUnitInstaller(),
        orca=LiveOrcaRegistrar(),
        automations=OrcaAutomationClient(),
        host_fixture=Path(args.host_fixture) if args.host_fixture else None,
        pull=not args.no_pull,
        report=report,
        runtime_user=runtime_user,
        runtime_home=runtime_home,
    )
    result = run_steps(context)
    if args.json:
        print(json.dumps(
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
        ))
    else:
        print(result.render())
    return 0 if result.ok else 1


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
