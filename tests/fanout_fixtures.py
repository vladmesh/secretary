"""Explicit provider-policy boundary fixtures for tests of later launch stages."""

from __future__ import annotations

from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef


def accepted_transport_run(
    head: str,
    *,
    role: str,
    workspace: str,
    task_ref: TaskRef,
    pid_file: str,
    run_id: str,
) -> HeadRun:
    """The independently attested run a pane/delivery test begins with.

    These suites are not asserting the provider boundary itself.  Supplying this explicit fixture
    keeps a missing schema from being implicitly treated as an allow while preserving the focused
    transport contract under test.
    """
    return HeadRun(
        run_id=run_id,
        spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
        workspace=workspace,
        task_ref=task_ref,
        role=role,
        pid_file=pid_file,
    ).with_fanout_policy(
        {
            "version": 1,
            "state": "allowed",
            "terminal_state": "clean",
            "run_id": run_id,
            "role": role,
            "model": "gpt-5.6-terra",
            "binary_path": "/test/codex",
            "binary_digest": "0" * 64,
            "cli_version": "test-codex",
            "tool_schema_digest": "0" * 64,
            "provider_schema_verdict": "no_callable_child_spawn_surface",
            "events": [],
        }
    )
