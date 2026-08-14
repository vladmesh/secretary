from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary.dispatcher import LaunchedHead
from secretary.dispatcher_worker_lifecycle import head_run_binding
from secretary.tasks import TaskError
from triggered_agents.runtime.head import operations as head_ops
from tests.test_dispatcher import FakeCatalog, FakeHost, FakeKanboard, TwoOpenSprintAdmission


def _legacy_unbound_v1_run(run_json: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Give a production-shaped Codex HeadRun its exact, still-unbound v1 descriptor."""
    run = head_ops.HeadRun.from_json(run_json)
    run = replace(run, role="worker", spec=replace(run.spec, model="gpt-5.6-terra"))
    run_id, fingerprint = head_run_binding(run.to_json())
    source = {
        "version": 1, "kind": "codex_session_event_jsonl", "state": "unbound",
        "run_id": run_id, "head_run_fingerprint": fingerprint,
        "workspace": str(Path(run.workspace).resolve(strict=False)), "role": run.role,
        "task_ref": run.task_ref.to_json(), "root": str(root.resolve(strict=False)), "baseline": [],
    }
    return run.with_fanout_policy({
        "version": 1, "state": "allowed", "terminal_state": "clean", "run_id": run.run_id,
        "role": run.role, "model": run.spec.model or "", "binary_path": "/test/codex",
        "binary_digest": "0" * 64, "cli_version": "test-codex", "tool_schema_digest": "0" * 64,
        "provider_schema_verdict": "no_callable_child_spawn_surface", "events": [],
        "provider_source_required": True, "provider_source": source,
    }).to_json()


def _configure_production_shaped_codex_relaunch(host: Any, *, root: Path) -> None:
    """Make the fake's next Codex rework retain the real preflight/launch HeadRun handoff."""
    def preflight(head: str, *, role: str, workspace: str, task_ref: head_ops.TaskRef,
                  pid_file: str, run_id: str) -> head_ops.HeadRun:
        run = head_ops.HeadRun(
            run_id=run_id, spec=head_ops.HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
            workspace=workspace, task_ref=task_ref, role=role, pid_file=pid_file,
        )
        return head_ops.HeadRun.from_json(_legacy_unbound_v1_run(run.to_json(), root=root / run_id))

    real_restart = host.restart_worker

    def restart(task: dict, record, *, heartbeat_run_id: str = "") -> LaunchedHead:
        launched = real_restart(task, record, heartbeat_run_id=heartbeat_run_id)
        preflight_run = head_ops.HeadRun.from_json(record.launch_intent["head_run"])
        reported = preflight_run.rebound(launched.handle, leaf=launched.leaf).working()
        host._write_head_pid("worker", task["ref"], head_run=reported.to_json(), leaf=launched.leaf)
        return replace(launched, head_run=reported.to_json())

    host.preflight_codex_run = preflight
    host.restart_worker = restart


class FakeSprints:
    """The sprint facts the card cycle asks about, and nothing else."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def list(self, *args, **kwargs) -> list[dict]:
        return []

    def show(self, reference: str, **kwargs) -> dict:
        if reference not in self.rows:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.rows[reference]


__all__ = ["FakeCatalog", "FakeHost", "FakeKanboard", "FakeSprints", "TwoOpenSprintAdmission", "_configure_production_shaped_codex_relaunch", "_legacy_unbound_v1_run"]
