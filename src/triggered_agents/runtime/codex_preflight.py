"""The product's one way to make a workspace fit for an interactive Codex head to start in.

Every Codex head is a TUI, and a TUI asks about directory trust before it will take a prompt.
Nobody is sitting in front of the pane, so a head whose root codex has never seen sits on the
dialog, never answers Orca's readiness probe and never receives its prompt. The answer is written
before the pane exists rather than waited for afterwards.

That makes one ordering the contract for every interactive Codex head, whichever launcher brings
it up: **ensure trust, create the pane, wait for readiness, deliver the prompt, confirm the
turn.** The first step is here, the last three are `tui_delivery`. A preflight that fails must
fail here, with no pane created and nothing for a caller to mistake for a head that ran.

It lives in `runtime` beside `tui_delivery` because both callers need it and only one of them may
import the other. Nothing here knows about boards, roles or sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # Avoid a runtime import cycle with head.command.
    from .head.run import HeadRun

CODEX_HOME_DEFAULT = str(Path.home() / ".config" / "orca" / "codex-runtime-home" / "home")
# The file codex itself reads trust from, inside whatever CODEX_HOME the head runs with.
CODEX_CONFIG_FILE = "config.toml"
# The file codex keeps its update check in, inside the same CODEX_HOME: a `VersionInfo` of
# `latest_version`, `last_checked_at` and `dismissed_version`. Picking "Skip until next version"
# on the update modal is exactly a write of `dismissed_version = latest_version` here, which is
# why the modal can be answered before the pane exists rather than typed at afterwards.
CODEX_VERSION_FILE = "version.json"

# What `ensure_codex_update_modal_dismissed` did, as an answer a caller can record.
UPDATE_MODAL_PREVENTED = "prevented"
UPDATE_MODAL_ALREADY_DISMISSED = "already-dismissed"
UPDATE_MODAL_NOT_PENDING = "not-pending"
UPDATE_MODAL_UNPREVENTABLE = "unpreventable"

# Provider-schema protocol version, not a Codex version.
FANOUT_ATTESTATION_VERSION = 1
FANOUT_SCHEMA_ABSENT = "schema_absent"
FANOUT_SCHEMA_UNKNOWN = "schema_unknown"
FANOUT_SCHEMA_ALLOWED = "no_callable_child_spawn_surface"
FANOUT_TERMINAL_CLEAN = "clean"
FANOUT_TERMINAL_UNKNOWN = "unknown"
FANOUT_TERMINAL_VIOLATION = "violation"
CAPABILITY_ATTESTATION_KIND = "provider_capability_surface"
STRICT_LAUNCH_CONFIGURATION = {
    "multi_agent_v2": True,
    "wait_agent_enabled": False,
}

EVENT_COLLABORATION_CALL = "collaboration_call"
EVENT_CHILD_THREAD_EDGE = "child_thread_edge"
EVENT_UNKNOWN_THREAD_EDGE = "unknown_thread_edge"
EVENT_UNPARSEABLE_PROVIDER_EVENT = "unparseable_provider_event"
PROVIDER_EVENT_TYPES = (
    EVENT_COLLABORATION_CALL,
    EVENT_CHILD_THREAD_EDGE,
    EVENT_UNKNOWN_THREAD_EDGE,
    EVENT_UNPARSEABLE_PROVIDER_EVENT,
)

# Classifiers never grant allow evidence; unknown tools remain unknown.
KNOWN_COLLABORATION_TOOLS = frozenset(
    {
        "spawn_agent",
        "create_agent",
        "create_child_thread",
        "fork_thread",
        "delegate",
        "collaboration",
        "collaboration_call",
        "wait",
        "wait_agent",
    }
)


class CodexPreflightError(RuntimeError):
    """A workspace could not be made fit for an interactive Codex head to start in.

    Raised only before a pane exists, so a caller that sees it knows nothing was launched.
    """


class CodexFanoutPolicyError(CodexPreflightError):
    """The exact Codex run has no independently acceptable no-fan-out attestation."""

    def __init__(self, message: str, *, run: HeadRun) -> None:
        super().__init__(message)
        self.run = run


class CodexFanoutRecordingError(CodexPreflightError):
    """A provider-edge result could not be durably written before a consequential action."""

    def __init__(self, message: str, *, run: HeadRun, event: dict[str, Any]) -> None:
        super().__init__(message)
        self.run = run
        self.event = dict(event)


@dataclass(frozen=True)
class ProviderEventOutcome:
    """One typed provider event and the run state written before its caller acts."""

    run: HeadRun
    event: dict[str, Any]

    @property
    def terminal_state(self) -> str:
        return str(self.run.fanout_policy.get("terminal_state") or FANOUT_TERMINAL_UNKNOWN)

    @property
    def blocked(self) -> bool:
        return self.terminal_state != FANOUT_TERMINAL_CLEAN


def codex_home(profile: Mapping[str, Any]) -> str:
    """The CODEX_HOME a head with this profile runs with — and therefore the config it reads trust
    from. The launch command names the same one, so the file written here is the file that head
    will actually consult."""
    return str(profile.get("codex_home") or os.environ.get("TA_CODEX_HOME") or CODEX_HOME_DEFAULT)


def codex_trust_paths(workspace: str) -> list[str]:
    """The paths codex asks about for a head started in `workspace`.

    Both the workspace and its repository root, because codex checks the repository root of the
    directory it starts in when that directory is inside a git repo — a worktree inherits the answer
    given to the repo it was cut from — and the directory itself when it is not. Trust overrides and
    the config write are rendered from this one list.
    """
    workspace_path = Path(workspace).resolve(strict=False)
    paths = [workspace_path]
    repo_root = _codex_repository_trust_root(workspace_path)
    if repo_root is not None:
        paths.append(repo_root)
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def ensure_codex_workspace_trusted(
    profile: Mapping[str, Any],
    workspace: str,
    config: Path | None = None,
) -> None:
    """Answer the codex trust question for one workspace before a head starts in it.

    The `-c projects...trust_level` overrides the launch command carries do not reach that check
    (codex 0.145 still shows the dialog with them in place), so the answer has to live in
    `config.toml` of the CODEX_HOME the head runs with, which is where codex writes it when a human
    picks "Yes, continue".

    Both the workspace and its repository root are recorded. Trust already on file is left alone, and
    a path the file keeps at another trust level is somebody's decision, so it fails the bring-up
    with a readable reason instead of being overwritten.
    """
    config_path = config or Path(codex_home(profile)) / CODEX_CONFIG_FILE
    text = _read_codex_config(config_path)
    projects = _codex_config_projects(text, config_path)
    additions: list[str] = []
    for target in codex_trust_paths(workspace):
        entry = projects.get(target)
        if entry is None:
            additions.append(target)
            continue
        if not isinstance(entry, dict):
            raise CodexPreflightError(
                f"codex config {config_path} has a non-table project entry for {target}"
            )
        level = str(entry.get("trust_level") or "")
        if level == "trusted":
            continue
        raise CodexPreflightError(
            f"codex config {config_path} keeps {target} at trust_level {level or '(none)'!r}"
        )
    if not additions:
        return
    body = text if text.endswith("\n") or not text else f"{text}\n"
    for target in additions:
        body += f'\n[projects.{json.dumps(target)}]\ntrust_level = "trusted"\n'
    _save_codex_config(config_path, body)


def codex_version_file(profile: Mapping[str, Any]) -> Path:
    """Where the head with this profile keeps its update check."""
    return Path(codex_home(profile)) / CODEX_VERSION_FILE


def ensure_codex_update_modal_dismissed(
    profile: Mapping[str, Any],
    version_file: Path | None = None,
) -> str:
    """Answer codex's update prompt for this runtime before a head starts under it.

    Same shape and the same reason as `ensure_codex_workspace_trusted`: nobody is sitting in front
    of the pane, so a dialog that waits for a person is a head that never receives its prompt. On
    `issue:e4d6f307` this exact modal held a `codex-high` reviewer for 51 minutes with the review
    pointer swallowed, `tui-idle` satisfied throughout and the codex process at zero CPU.

    The answer written here is the file codex itself writes when a human picks "Skip until next
    version": `dismissed_version` set to the version the check found. Nothing is upgraded, nothing
    is downloaded, and no version is pinned — an upgrade is a separate, explicit action.

    Prevention is best effort by construction: the file belongs to codex, it may not exist yet on a
    fresh runtime home, and a check that runs after this write can raise the modal again. So this
    returns what it did rather than raising for a state it could not reach, and the delivery
    boundary's on-screen answer stays the guarantee. A path that is not a regular file is the one
    exception and is refused, because a bring-up must never follow a symlink somebody left there.
    """
    path = version_file or codex_version_file(profile)
    reject_symlinked_config(path, "codex version file")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Codex has not run its check under this home yet; there is no pending update to dismiss.
        return UPDATE_MODAL_NOT_PENDING
    except (OSError, UnicodeError):
        return UPDATE_MODAL_UNPREVENTABLE
    try:
        info = json.loads(raw)
    except ValueError:
        return UPDATE_MODAL_UNPREVENTABLE
    if not isinstance(info, dict):
        return UPDATE_MODAL_UNPREVENTABLE
    latest = info.get("latest_version")
    if not isinstance(latest, str) or not latest:
        return UPDATE_MODAL_NOT_PENDING
    if info.get("dismissed_version") == latest:
        return UPDATE_MODAL_ALREADY_DISMISSED
    updated = dict(info)
    updated["dismissed_version"] = latest
    try:
        _save_codex_json(path, updated)
    except CodexPreflightError:
        return UPDATE_MODAL_UNPREVENTABLE
    return UPDATE_MODAL_PREVENTED


def preflight_codex_launch(
    profile: Mapping[str, Any],
    workspace: str,
    run: HeadRun,
    *,
    schema_attestation: Mapping[str, Any] | None = None,
    binary_path: str | None = None,
    config: Path | None = None,
) -> HeadRun:
    """Prepare one exact Codex ``HeadRun`` before a pane exists.

    A Codex pane is admitted only by a current, independently captured capability surface.  Launch
    flags express the desired low-fan-out configuration but are not that proof; their observed
    acceptance is a separate required part of the same capture.
    """
    attested = attest_codex_fanout(
        profile,
        run,
        schema_attestation=schema_attestation,
        binary_path=binary_path,
    )
    if not attested.fanout_clean:
        raise CodexFanoutPolicyError(
            f"Codex capability preflight refused launch: {attested.fanout_policy.get('reason') or 'unknown evidence'}",
            run=attested,
        )
    # Persist the pre-pane baseline; shared-workspace journals are not run identity.
    try:
        attested = _with_unbound_provider_source(profile, attested)
    except OSError as exc:
        refused = _unknown_run(attested, f"cannot establish Codex provider event source baseline: {exc}")
        raise CodexFanoutPolicyError(str(refused.fanout_policy["reason"]), run=refused) from None
    try:
        ensure_codex_workspace_trusted(profile, workspace, config)
    except CodexPreflightError as exc:
        refused = _unknown_run(attested, f"workspace trust preflight failed: {exc}")
        raise CodexFanoutPolicyError(str(exc), run=refused) from None
    # The update modal remains a delivery defence in depth, not capability evidence.
    try:
        ensure_codex_update_modal_dismissed(profile)
    except CodexPreflightError:
        pass
    return attested


def attest_codex_fanout(
    profile: Mapping[str, Any],
    run: HeadRun,
    *,
    schema_attestation: Mapping[str, Any] | None = None,
    binary_path: str | None = None,
) -> HeadRun:
    """Build a conservative, run-bound provider-schema attestation without opening a pane.

    ``schema_attestation`` is expected to be a provider-schema capture, not a configuration knob: a
    canonical ``tools`` list and its digest, the observed binary digest and CLI version, model and
    role. A mapping that merely says a model did not spawn is not this shape and is recorded as
    schema-unknown.
    """
    # Launch configuration cannot promote itself to provider-schema evidence.
    raw = schema_attestation
    if raw is None:
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_ABSENT,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="no provider-schema attestation is attached to this Codex launch",
        )
    if not isinstance(raw, Mapping):
        return _unknown_run(run, "provider-schema attestation is not an object")
    schema = dict(raw)
    if schema.get("version") != FANOUT_ATTESTATION_VERSION:
        return _unknown_run(run, "provider-schema attestation has an unsupported version")
    if schema.get("kind") != CAPABILITY_ATTESTATION_KIND:
        return _unknown_run(run, "provider-schema attestation is not an independent capability-surface capture")
    if str(schema.get("run_id") or "") != run.run_id:
        return _unknown_run(run, "provider-schema attestation is not bound to this HeadRun")
    if not str(run.role).strip():
        return _unknown_run(run, "HeadRun has no role to bind provider evidence to")
    if str(schema.get("role") or "") != run.role:
        return _unknown_run(run, "provider-schema attestation role does not match HeadRun")
    model = run.spec.model or ""
    if str(schema.get("model") or "") != model:
        return _unknown_run(run, "provider-schema attestation model does not match HeadRun")
    tools = schema.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, Mapping) for tool in tools):
        return _unknown_run(run, "provider-schema attestation has no canonical tool schema")
    try:
        canonical_tools = _canonical_tools(tools)
        tool_digest = _json_digest(canonical_tools)
    except ValueError as exc:
        return _unknown_run(run, str(exc))
    if not _same_digest(schema.get("tool_schema_digest"), tool_digest):
        return _unknown_run(run, "provider-schema tool digest does not match its schema")
    try:
        observed_path, observed_digest, observed_version = _codex_cli_identity(binary_path)
    except OSError as exc:
        return _unknown_run(run, f"cannot attest Codex binary identity: {exc}")
    if str(schema.get("binary_path") or "") != observed_path:
        return _unknown_run(run, "provider-schema binary path does not match launched Codex")
    if not _same_digest(schema.get("binary_digest"), observed_digest):
        return _unknown_run(run, "provider-schema binary digest does not match launched Codex")
    if str(schema.get("cli_version") or "") != observed_version:
        return _unknown_run(run, "provider-schema CLI version does not match launched Codex")
    freshness_error = _attestation_freshness_error(schema)
    if freshness_error:
        return _unknown_run(run, freshness_error)
    config_error = _strict_configuration_error(schema)
    if config_error:
        return _unknown_run(run, config_error)
    tool_names = {
        str(tool.get("name") or "").strip().lower() for tool in tools if str(tool.get("name") or "").strip()
    }
    verdict = str(schema.get("provider_schema_verdict") or "")
    if verdict != FANOUT_SCHEMA_ALLOWED:
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_UNKNOWN,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="provider schema does not explicitly prove no callable child-spawn surface",
            binary_path=observed_path,
            binary_digest=observed_digest,
            cli_version=observed_version,
            tool_schema_digest=tool_digest,
            provider_schema_verdict=verdict,
            tool_schema=canonical_tools,
            capability_attested_at=str(schema.get("attested_at")),
            capability_expires_at=str(schema.get("expires_at")),
            strict_configuration=dict(schema.get("strict_configuration") or {}),
        )
    if _has_child_spawn_surface(tool_names):
        return _policy_run(
            run,
            state=FANOUT_SCHEMA_UNKNOWN,
            terminal_state=FANOUT_TERMINAL_UNKNOWN,
            reason="provider schema exposes a callable child-spawn surface",
            binary_path=observed_path,
            binary_digest=observed_digest,
            cli_version=observed_version,
            tool_schema_digest=tool_digest,
            provider_schema_verdict=verdict,
            tool_schema=canonical_tools,
            capability_attested_at=str(schema.get("attested_at")),
            capability_expires_at=str(schema.get("expires_at")),
            strict_configuration=dict(schema.get("strict_configuration") or {}),
        )
    return _policy_run(
        run,
        state="allowed",
        terminal_state=FANOUT_TERMINAL_CLEAN,
        reason="provider schema proves no callable child-spawn surface",
        binary_path=observed_path,
        binary_digest=observed_digest,
        cli_version=observed_version,
        tool_schema_digest=tool_digest,
        provider_schema_verdict=verdict,
        tool_schema=canonical_tools,
        capability_attested_at=str(schema.get("attested_at")),
        capability_expires_at=str(schema.get("expires_at")),
        strict_configuration=dict(schema.get("strict_configuration") or {}),
    )


class CodexProviderEventRecorder:
    """Durably append provider-edge evidence to one exact HeadRun.

    The recorder owns no pane and has no screen or transcript fallback.  Its caller turns every
    recorded edge, and every durable-write failure, into the exact run's terminal policy effect.
    """

    def __init__(
        self,
        run: HeadRun,
        persist: Callable[[HeadRun], None],
        *,
        expected_parent_thread_id: str = "",
    ) -> None:
        self.run = run
        self.persist = persist
        self.expected_parent_thread_id = str(expected_parent_thread_id or "")

    def record(
        self,
        raw_event: Any,
        *,
        source_sequence: int | str | None,
        source_location: str,
        captured_at: str | None = None,
    ) -> ProviderEventOutcome:
        event = _typed_provider_event(
            raw_event,
            expected_parent_thread_id=self.expected_parent_thread_id,
            source_sequence=source_sequence,
            source_location=source_location,
            captured_at=captured_at,
        )
        policy = dict(self.run.fanout_policy)
        events = list(policy.get("events") or [])
        events.append(event)
        policy["events"] = events
        policy["terminal_state"] = (
            FANOUT_TERMINAL_VIOLATION
            if event["policy_outcome"] == FANOUT_TERMINAL_VIOLATION
            else FANOUT_TERMINAL_UNKNOWN
        )
        policy["reason"] = f"provider event {event['type']}: {event['reason']}"
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception as exc:
            failed_policy = dict(updated.fanout_policy)
            failed_policy["terminal_state"] = FANOUT_TERMINAL_UNKNOWN
            failed_policy["reason"] = (
                f"provider event could not be durably recorded: {type(exc).__name__}: {exc}"
            )
            failed = updated.with_fanout_policy(failed_policy)
            self.run = failed
            raise CodexFanoutRecordingError(str(failed_policy["reason"]), run=failed, event=event) from None
        self.run = updated
        return ProviderEventOutcome(run=updated, event=event)


def enforce_provider_event(
    recorder: CodexProviderEventRecorder,
    raw_event: Any,
    *,
    source_sequence: int | str | None,
    source_location: str,
    stop: Callable[[HeadRun, str], None],
    block: Callable[[dict[str, Any]], None],
    captured_at: str | None = None,
) -> ProviderEventOutcome:
    """Record a provider edge before stopping and blocking its exact launched run."""
    try:
        outcome = recorder.record(
            raw_event,
            source_sequence=source_sequence,
            source_location=source_location,
            captured_at=captured_at,
        )
    except CodexFanoutRecordingError as exc:
        evidence = {
            "kind": "codex_provider_fanout",
            "state": FANOUT_TERMINAL_UNKNOWN,
            "reason": str(exc),
            "event": dict(exc.event),
            "recorder_failure": True,
        }
        stop(exc.run, str(exc))
        block(evidence)
        raise
    if outcome.blocked:
        evidence = {
            "kind": "codex_provider_fanout",
            "state": outcome.terminal_state,
            "reason": str(outcome.run.fanout_policy.get("reason") or "provider event observed"),
            "event": dict(outcome.event),
            "recorder_failure": False,
        }
        stop(outcome.run, evidence["reason"])
        block(evidence)
    return outcome


def reject_symlinked_config(config: Path, kind: str) -> None:
    """Refuse to treat anything but a regular file as a head runtime's own config.

    A bring-up rewrites installation state shared by every head on the host, so it must never follow
    a symlink or a device somebody put in that path. Public because the Claude side of the same
    bring-up writes its config under the same rule.
    """
    try:
        mode = config.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CodexPreflightError(f"cannot inspect {kind} config {config}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise CodexPreflightError(f"refusing symlinked {kind} config {config}")
    if not stat.S_ISREG(mode):
        raise CodexPreflightError(f"{kind} config {config} is not a regular file")


def _read_codex_config(config: Path) -> str:
    reject_symlinked_config(config, "codex")
    try:
        return config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None


def _codex_config_projects(text: str, config: Path) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexPreflightError(f"cannot read codex config {config}: {exc}") from None
    projects = loaded.get("projects", {})
    if not isinstance(projects, dict):
        raise CodexPreflightError(f"codex config {config} has a non-table projects value")
    return projects


def _save_codex_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace one of codex's own JSON state files atomically, under the same symlink rule."""
    _atomic_write(path, json.dumps(payload) + "\n", kind="codex version file")


def _save_codex_config(config: Path, text: str) -> None:
    """Replace the codex config with `text`, but only once it parses as the TOML codex will read.

    The new trust tables are appended to the file as it stands rather than re-rendered from a parse,
    because this file is the installation's own. Appending can produce invalid TOML if the file
    already declared `projects` in a form a table header cannot extend, so the result is parsed back
    before it replaces anything.
    """
    _codex_config_projects(text, config)
    _atomic_write(config, text, kind="codex config")


def _atomic_write(path: Path, text: str, *, kind: str) -> None:
    """Replace one file of a head runtime's own state, never following a symlink into it."""
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlinked_config(path, kind)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        reject_symlinked_config(path, kind)
        os.replace(temp_path, path)
    except OSError as exc:
        raise CodexPreflightError(f"cannot update {kind} {path}: {exc}") from None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _resolve_git_path(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _workspace_git_dir(workspace_path: Path) -> Path | None:
    dotgit = workspace_path / ".git"
    try:
        if dotgit.is_dir():
            return dotgit.resolve(strict=False)
        if dotgit.is_file():
            first = dotgit.read_text(encoding="utf-8").splitlines()[0].strip()
        else:
            return None
    except (OSError, IndexError, UnicodeError):
        return None
    if not first.startswith("gitdir:"):
        return None
    return _resolve_git_path(first.split(":", 1)[1].strip(), workspace_path)


def _git_common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    try:
        if common.is_file():
            value = common.read_text(encoding="utf-8").splitlines()[0].strip()
            if value:
                return _resolve_git_path(value, git_dir)
    except (OSError, IndexError, UnicodeError):
        pass
    return git_dir.resolve(strict=False)


def _codex_repository_trust_root(workspace_path: Path) -> Path | None:
    """Codex' TUI trust check keys linked worktrees by the common git dir's repo root."""
    git_dir = _workspace_git_dir(workspace_path)
    if git_dir is None:
        return None
    common_dir = _git_common_dir(git_dir)
    if common_dir.name != ".git":
        return None
    try:
        if git_dir != common_dir and not git_dir.is_relative_to(common_dir / "worktrees"):
            return None
    except ValueError:
        return None
    return common_dir.parent.resolve(strict=False)


def _policy_run(
    run: HeadRun,
    *,
    state: str,
    terminal_state: str,
    reason: str,
    binary_path: str = "",
    binary_digest: str = "",
    cli_version: str = "",
    tool_schema_digest: str = "",
    provider_schema_verdict: str = "",
    tool_schema: list[dict[str, Any]] | None = None,
    capability_attested_at: str = "",
    capability_expires_at: str = "",
    strict_configuration: dict[str, Any] | None = None,
) -> HeadRun:
    return run.with_fanout_policy(
        {
            "version": FANOUT_ATTESTATION_VERSION,
            "state": state,
            "terminal_state": terminal_state,
            "reason": reason,
            "run_id": run.run_id,
            "role": run.role,
            "model": run.spec.model or "",
            "binary_path": binary_path,
            "binary_digest": binary_digest,
            "cli_version": cli_version,
            "tool_schema_digest": tool_schema_digest,
            "provider_schema_verdict": provider_schema_verdict,
            "tool_schema": list(tool_schema or []),
            "capability_attested_at": capability_attested_at,
            "capability_expires_at": capability_expires_at,
            "strict_configuration": dict(strict_configuration or {}),
            "events": [],
        }
    )


def _canonical_tools(tools: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize the provider's complete callable surface before it is digested and retained."""
    rendered: list[tuple[str, dict[str, Any]]] = []
    for tool in tools:
        value = dict(tool)
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"provider-schema tool is not canonical JSON: {exc}") from None
        rendered.append((encoded, value))
    return [value for _encoded, value in sorted(rendered)]


def _attestation_freshness_error(schema: Mapping[str, Any]) -> str:
    """Require a bounded, unexpired capture rather than a timeless capability claim."""
    values: dict[str, datetime] = {}
    for name in ("attested_at", "expires_at"):
        raw = schema.get(name)
        if not isinstance(raw, str) or not raw.strip():
            return f"provider-schema attestation has no {name}"
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return f"provider-schema attestation has malformed {name}"
        if parsed.tzinfo is None:
            return f"provider-schema attestation has timezone-less {name}"
        values[name] = parsed.astimezone(UTC)
    if values["expires_at"] <= values["attested_at"]:
        return "provider-schema attestation expiry does not follow its capture"
    if values["expires_at"] <= datetime.now(UTC):
        return "provider-schema attestation is stale"
    return ""


def _strict_configuration_error(schema: Mapping[str, Any]) -> str:
    """Check the observed strict configuration without mistaking it for capability proof."""
    raw = schema.get("strict_configuration")
    if not isinstance(raw, Mapping):
        return "provider-schema attestation has no observed strict launch configuration"
    configured = raw.get("configured")
    effective = raw.get("effective")
    status = raw.get("status")
    if status != "accepted":
        return "Codex rejected strict launch configuration"
    if configured != STRICT_LAUNCH_CONFIGURATION:
        return "Codex strict launch configuration drifted from the required input"
    if effective != STRICT_LAUNCH_CONFIGURATION:
        return "Codex silently ignored strict launch configuration"
    return ""


def _with_unbound_provider_source(profile: Mapping[str, Any], run: HeadRun) -> HeadRun:
    """Attach the pre-pane source baseline used to bind the new Codex event journal.

    Session JSONL has a provider session id and parent thread id but no Secretary run id, so the
    baseline is part of the attestation: a later lifecycle can select exactly one *new* provider
    journal and never re-label an older same-workspace session as this run.
    """
    root = Path(codex_home(profile)) / "sessions"
    if root.exists() and not root.is_dir():
        raise OSError(f"Codex session root {root} is not a directory")
    baseline: list[str] = []
    if root.is_dir():
        try:
            baseline = sorted(
                str(path.resolve(strict=False)) for path in root.rglob("*.jsonl") if path.is_file()
            )
        except OSError as exc:
            raise OSError(f"cannot enumerate Codex session root {root}: {exc}") from None
    policy = dict(run.fanout_policy)
    policy["provider_source_required"] = True
    policy["provider_source"] = {
        "version": 1,
        "kind": "codex_session_event_jsonl",
        "state": "unbound",
        # Bind facts before the pane; a journal has no Secretary run identity.
        **codex_provider_source_descriptor(run),
        "root": str(root.resolve(strict=False)),
        "baseline": baseline,
    }
    return run.with_fanout_policy(policy)


def codex_provider_source_descriptor(run: HeadRun) -> dict[str, Any]:
    """The immutable launch facts every Codex provider journal keeps for its entire lifetime.

    A provider journal identifies its own session but cannot name the Secretary head that opened it.
    Source binding may append verified journal facts, but it must carry these values byte-for-value
    into every persisted bound source so later readers can reject a foreign same-workspace journal.
    """
    return {
        "run_id": run.run_id,
        "head_run_fingerprint": _head_run_fingerprint(run),
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
    }


def _head_run_fingerprint(run: HeadRun) -> str:
    stable = {
        "run_id": run.run_id,
        "workspace": run.workspace,
        "task_ref": run.task_ref.to_json(),
        "role": run.role,
        "spec": {
            "profile_id": run.spec.profile_id,
            "adapter": run.spec.adapter,
            "model": run.spec.model or "",
            "effort": run.spec.effort,
            "resource": run.spec.resource or "",
            "codex_mode": run.spec.codex_mode or "",
            "fallback": list(run.spec.fallback),
        },
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()[:32]


def _unknown_run(run: HeadRun, reason: str) -> HeadRun:
    return _policy_run(
        run,
        state=FANOUT_SCHEMA_UNKNOWN,
        terminal_state=FANOUT_TERMINAL_UNKNOWN,
        reason=reason,
    )


def _codex_cli_identity(binary_path: str | None = None) -> tuple[str, str, str]:
    """Hash and query the binary that an ordinary ``codex`` launch resolves to.

    The command renderer invokes ``codex`` by name, so accepting a different configured path here
    would bind an attestation to a binary the pane does not execute.
    """
    candidate = binary_path or shutil.which("codex")
    if not candidate:
        raise OSError("codex executable is not on PATH")
    path = Path(candidate).resolve(strict=True)
    if not path.is_file():
        raise OSError(f"codex executable {path} is not a regular file")
    digest = _file_digest(path)
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(f"cannot read Codex CLI version: {exc}") from None
    if result.returncode != 0:
        raise OSError(f"Codex CLI version command failed with {result.returncode}")
    version = (result.stdout or result.stderr or "").strip()
    if not version:
        raise OSError("Codex CLI version command returned no version")
    # Preserve the exact attested version; prefixes could misclassify future forms.
    return str(path), digest, version


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"provider schema is not canonical JSON: {exc}") from None
    return hashlib.sha256(encoded).hexdigest()


def _same_digest(supplied: Any, observed: str) -> bool:
    value = str(supplied or "").lower()
    return (
        value == observed.lower() and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
    )


def _has_child_spawn_surface(tool_names: set[str]) -> bool:
    for name in tool_names:
        compact = name.replace("-", "_")
        if name in KNOWN_COLLABORATION_TOOLS:
            return True
        if any(
            fragment in compact
            for fragment in ("spawn", "child_thread", "childagent", "subagent", "delegate")
        ):
            return True
    return False


def _typed_provider_event(
    raw_event: Any,
    *,
    expected_parent_thread_id: str,
    source_sequence: int | str | None,
    source_location: str,
    captured_at: str | None,
) -> dict[str, Any]:
    """Reduce untrusted provider input to the four durable event kinds.

    All original bytes are represented only by a canonical digest. A malformed object is still an
    event: accepting it as an empty result would be a transcript reconstruction path in disguise.
    """
    captured = captured_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    supplied_digest = (
        str(raw_event.get("_secretary_raw_event_digest") or "") if isinstance(raw_event, Mapping) else ""
    )
    raw_digest = (
        supplied_digest if re.fullmatch(r"[0-9a-f]{64}", supplied_digest) else _raw_event_digest(raw_event)
    )
    base = {
        "raw_event_digest": raw_digest,
        "source_sequence": source_sequence,
        "source_location": str(source_location or ""),
        "captured_at": captured,
        "parent_thread_id": "",
        "child_thread_id": "",
        "tool_name": "",
    }
    if source_sequence is None or not str(source_location or ""):
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event has no source sequence or location",
        )
    if not isinstance(raw_event, Mapping):
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event is not an object",
        )
    raw = dict(raw_event)
    parent = str(raw.get("parent_thread_id") or raw.get("parentThreadId") or "")
    child = str(raw.get("child_thread_id") or raw.get("childThreadId") or "")
    children = raw.get("child_thread_ids") or raw.get("childThreadIds")
    if isinstance(children, list):
        nonempty = [str(value) for value in children if str(value)]
        if len(nonempty) == 1:
            child = nonempty[0]
        elif nonempty:
            child = ",".join(nonempty)
    tool = str(raw.get("tool_name") or raw.get("tool") or raw.get("name") or "")
    event_type = str(raw.get("type") or raw.get("event_type") or "")
    base.update(parent_thread_id=parent, child_thread_id=child, tool_name=tool)
    if event_type in PROVIDER_EVENT_TYPES:
        declared = event_type
    elif tool or child:
        declared = EVENT_COLLABORATION_CALL if tool else EVENT_CHILD_THREAD_EDGE
    else:
        return dict(
            base,
            type=EVENT_UNPARSEABLE_PROVIDER_EVENT,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event has no recognised collaboration shape",
        )
    if declared == EVENT_UNPARSEABLE_PROVIDER_EVENT:
        return dict(
            base,
            type=declared,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider emitted an unparseable collaboration event",
        )
    if not expected_parent_thread_id or not parent or parent != expected_parent_thread_id:
        return dict(
            base,
            type=EVENT_UNKNOWN_THREAD_EDGE,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider event parent identity is absent or does not match this HeadRun",
        )
    if declared == EVENT_UNKNOWN_THREAD_EDGE:
        return dict(
            base,
            type=declared,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider reported an unknown parent or child thread relation",
        )
    if declared == EVENT_COLLABORATION_CALL:
        if not tool or tool.lower() not in KNOWN_COLLABORATION_TOOLS:
            return dict(
                base,
                type=EVENT_COLLABORATION_CALL,
                policy_outcome=FANOUT_TERMINAL_UNKNOWN,
                reason="provider called an unknown collaboration tool",
            )
        return dict(
            base,
            type=EVENT_COLLABORATION_CALL,
            policy_outcome=FANOUT_TERMINAL_VIOLATION,
            reason="provider collaboration call observed",
        )
    # A declared relation is a violation even with a redacted child; empty is unknown.
    if not child:
        return dict(
            base,
            type=EVENT_UNKNOWN_THREAD_EDGE,
            policy_outcome=FANOUT_TERMINAL_UNKNOWN,
            reason="provider child-thread edge has no child identity",
        )
    return dict(
        base,
        type=EVENT_CHILD_THREAD_EDGE,
        policy_outcome=FANOUT_TERMINAL_VIOLATION,
        reason="provider child-thread edge observed",
    )


def _raw_event_digest(raw_event: Any) -> str:
    try:
        return _json_digest(raw_event)
    except ValueError:
        # Never retain an untrusted repr; the fixed literal keeps the digest stable.
        return _json_digest({"unserialisable_type": type(raw_event).__name__})
