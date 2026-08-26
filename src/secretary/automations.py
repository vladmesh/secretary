"""Materialize Orca automations from the product's ``automation.toml`` specs.

Each scheduled role ships one spec describing *how* it is dispatched: the prompt,
the provider, the precheck, whether Orca's own trigger is allowed to fire, and
which worktree the run happens in. Host bindings (automation ids, repo ids) are
not in the spec; they are resolved here against the live Orca inventory and the
automation is edited in place, so its id — and anything keyed to that id — stays
stable across upgrades.

An automation is matched by name and then compared on its whole context, not
just its prompt. Two automations can share a prompt, a provider and a precheck
while one of them runs in a worktree that no longer exists, which is precisely
the drift an upgrade has to repair: comparing prompt alone declares that
automation correct and leaves it pointed at the stale path forever.

Trigger handling is deliberately asymmetric. When a spec names an explicit cron
or RRULE we compare it to what Orca stores and repoint on a difference. When it
names a preset ("hourly", "daily") we send the preset but do not treat Orca's
normalized expansion as drift, because we cannot predict that expansion and
would otherwise rewrite the automation on every single run.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRESETS = frozenset({"hourly", "daily", "weekdays", "weekly"})


@dataclass(frozen=True)
class AutomationSpec:
    """Desired state for one Orca automation, resolved against this host."""

    name: str
    prompt: str
    provider: str
    precheck: str
    reuse_session: bool
    trigger: str
    enabled: bool
    workspace: str
    repo: str
    workspace_mode: str = "existing"

    @property
    def trigger_is_explicit(self) -> bool:
        return bool(self.trigger) and self.trigger not in PRESETS


@dataclass(frozen=True)
class AutomationChange:
    name: str
    action: str  # create | repoint | unchanged
    drifted: tuple[str, ...] = ()
    automation_id: str = ""


class AutomationError(RuntimeError):
    """An automation could not be read or applied."""


def agents_root(product_root: Path) -> Path:
    return product_root / "src" / "triggered_agents" / "agents"


def workspaces_root(home: Path | str | None = None) -> Path:
    """Where role workspaces live: the configured root, else under the named home.

    ``home`` is the installation owner's, which is not the invoking process's when a repair runs
    as root or against another account's installation. Registering root's workspace paths in an
    automation the owner then runs is how a workspace ends up somewhere nothing materialized.
    """
    configured = os.environ.get("TA_WORKSPACES_ROOT")
    if configured:
        return Path(configured)
    return Path(home if home is not None else Path.home()) / "orca" / "workspaces"


def load_specs(
    product_root: Path, repo: str | None = None, *, home: Path | str | None = None
) -> list[AutomationSpec]:
    """Read every shipped automation spec that describes an Orca automation.

    A spec marked ``dispatcher`` routes to runtime code rather than an agent
    head, so it has no Orca automation at all and is skipped. Variants
    (steward's deep sweep) get a second systemd unit, never a second
    automation, so they are skipped too.
    """
    root = agents_root(product_root)
    repo_path = repo or str(product_root)
    specs: list[AutomationSpec] = []
    try:
        entries = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return []
    for entry in entries:
        spec_path = entry / "automation.toml"
        if not spec_path.is_file():
            continue
        try:
            raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise AutomationError(
                f"{entry.name}: automation spec is unreadable: {exc.__class__.__name__}"
            ) from None
        if raw.get("dispatcher") or not raw.get("skill"):
            continue
        name = raw.get("name") or entry.name
        trigger = raw.get("trigger", {}) if isinstance(raw.get("trigger"), dict) else {}
        specs.append(
            AutomationSpec(
                name=str(name),
                prompt=str(raw["skill"]),
                provider=str(raw.get("provider", "claude")),
                precheck=str(raw.get("precheck", "")),
                reuse_session=bool(raw.get("reuse_session", False)),
                trigger=str(trigger.get("orca", "")),
                enabled=bool(trigger.get("enabled", True)),
                workspace=str(workspaces_root(home) / "secretary" / str(name)),
                repo=repo_path,
            )
        )
    return specs


def _live_fields(live: dict[str, Any]) -> dict[str, Any]:
    """The subset of an Orca automation record that a spec claims ownership of."""
    precheck = live.get("precheck")
    workspace_id = live.get("workspaceId")
    workspace = ""
    if isinstance(workspace_id, str) and "::" in workspace_id:
        workspace = workspace_id.split("::", 1)[1]
    run_context = live.get("runContext") if isinstance(live.get("runContext"), dict) else {}
    return {
        "prompt": live.get("prompt") or "",
        "provider": live.get("agentId") or "",
        "precheck": (precheck or {}).get("command", "") if isinstance(precheck, dict) else "",
        "reuse_session": bool(live.get("reuseSession")),
        "enabled": bool(live.get("enabled")),
        "workspace_mode": live.get("workspaceMode") or "",
        "workspace": _normalize(workspace),
        "repo": _normalize(str(run_context.get("path") or "")),
        "trigger": live.get("rrule") or "",
    }


def _spec_fields(spec: AutomationSpec) -> dict[str, Any]:
    return {
        "prompt": spec.prompt,
        "provider": spec.provider,
        "precheck": spec.precheck,
        "reuse_session": spec.reuse_session,
        "enabled": spec.enabled,
        "workspace_mode": spec.workspace_mode,
        "workspace": _normalize(spec.workspace),
        "repo": _normalize(spec.repo),
        "trigger": spec.trigger,
    }


def _normalize(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return path


def drifted_fields(spec: AutomationSpec, live: dict[str, Any]) -> tuple[str, ...]:
    """Which owned fields of a live automation disagree with the spec."""
    desired = _spec_fields(spec)
    actual = _live_fields(live)
    drifted = [key for key in sorted(desired) if key != "trigger" and desired[key] != actual[key]]
    if spec.trigger_is_explicit and desired["trigger"] != actual["trigger"]:
        drifted.append("trigger")
    return tuple(sorted(drifted))


def plan_automations(
    specs: Iterable[AutomationSpec], live: Iterable[dict[str, Any]]
) -> list[AutomationChange]:
    by_name: dict[str, dict[str, Any]] = {}
    for record in live:
        name = record.get("name")
        if isinstance(name, str):
            by_name[name] = record
    changes: list[AutomationChange] = []
    for spec in specs:
        record = by_name.get(spec.name)
        if record is None:
            changes.append(AutomationChange(spec.name, "create"))
            continue
        drifted = drifted_fields(spec, record)
        identifier = str(record.get("id") or "")
        action = "repoint" if drifted else "unchanged"
        changes.append(AutomationChange(spec.name, action, drifted, identifier))
    return changes


def create_argv(spec: AutomationSpec) -> list[str]:
    return ["orca", "automations", "create", "--name", spec.name] + _spec_argv(spec) + ["--json"]


def repoint_argv(spec: AutomationSpec, automation_id: str) -> list[str]:
    return ["orca", "automations", "edit", "--id", automation_id] + _spec_argv(spec) + ["--json"]


def _spec_argv(spec: AutomationSpec) -> list[str]:
    argv = [
        "--prompt",
        spec.prompt,
        "--provider",
        spec.provider,
    ]
    # Orca selectors are mutually exclusive. An existing workspace already
    # identifies its repository, while a new-per-run automation needs the repo
    # from which Orca will create a workspace.
    if spec.workspace_mode == "existing":
        argv += ["--workspace", f"path:{spec.workspace}"]
    else:
        argv += ["--repo", f"path:{spec.repo}"]
    argv += ["--workspace-mode", spec.workspace_mode]
    if spec.precheck:
        argv += ["--precheck", spec.precheck]
    if spec.trigger:
        argv += ["--trigger", spec.trigger]
    argv.append("--enabled" if spec.enabled else "--disabled")
    argv.append("--reuse-session" if spec.reuse_session else "--fresh-session")
    return argv


class OrcaAutomationClient:
    """The live Orca CLI. Every call is bounded and reports failure by name."""

    timeout_seconds = 60

    def __init__(self, user: str | None = None):
        self.user = user

    def list(self) -> list[dict[str, Any]]:
        result = self._run(["orca", "automations", "list", "--json"], "list automations")
        try:
            payload = json.loads(result)
        except ValueError:
            raise AutomationError("list automations: orca returned invalid JSON") from None
        records = payload.get("result", {}).get("automations") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise AutomationError("list automations: orca returned no automation inventory")
        return [record for record in records if isinstance(record, dict)]

    def run(self, argv: list[str], label: str) -> None:
        self._run(argv, label)

    def _run(self, argv: list[str], label: str) -> str:
        if self.user:
            argv = ["runuser", "--user", self.user, "--", *argv]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise AutomationError(f"{label}: orca not found") from None
        except subprocess.TimeoutExpired:
            raise AutomationError(f"{label}: orca timed out") from None
        except OSError:
            raise AutomationError(f"{label}: orca could not run") from None
        if result.returncode != 0:
            raise AutomationError(f"{label}: orca exited {result.returncode}")
        return result.stdout or ""


def apply_automations(
    specs: Iterable[AutomationSpec],
    client: OrcaAutomationClient,
    *,
    dry_run: bool = False,
) -> tuple[list[AutomationChange], list[str]]:
    """Create or repoint automations to match the shipped specs."""
    specs = list(specs)
    changes = plan_automations(specs, client.list())
    if dry_run:
        return changes, []
    by_name = {spec.name: spec for spec in specs}
    applied: list[str] = []
    for change in changes:
        spec = by_name[change.name]
        if change.action == "create":
            client.run(create_argv(spec), f"create automation {spec.name}")
            applied.append(f"create automation {spec.name}")
        elif change.action == "repoint":
            if not change.automation_id:
                raise AutomationError(f"repoint automation {spec.name}: orca reported no automation id")
            client.run(repoint_argv(spec, change.automation_id), f"repoint automation {spec.name}")
            applied.append(f"repoint automation {spec.name} ({', '.join(change.drifted)})")
    return changes, applied
