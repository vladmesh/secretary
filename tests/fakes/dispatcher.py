from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary.checkpoint import CheckpointResult
from secretary.dispatcher import (
    STOPPED_BY_DISPATCHER,
    STOPPED_BY_REVIEW_FREEZE,
    CommandHostRuntime,
    HostError,
    LaunchedHead,
    _continuation_note,
    _report_nudge_prompt,
)
from secretary.dispatcher_gate import GateResult
from secretary.dispatcher_heartbeat import run_heartbeat_identity
from secretary.dispatcher_launcher import claude_launch_model, role_launch_env
from secretary.dispatcher_observer import OBSERVER_HEAD_FALLBACK
from secretary.dispatcher_types import HeadLaunchAborted, ReviewLaunch
from secretary.dispatcher_watchdog import head_run_process_status as _head_run_process_status
from secretary.dispatcher_watchdog import pid_file_path
from secretary.dispatcher_worker_lifecycle import head_run_binding
from secretary.routing_journal import HeadRun, head_run_from_profile
from secretary.sprints import SPRINT_BOARD_NAME
from secretary.tasks import TaskError
from tests.head_registry import write_installed_pair
from triggered_agents.runtime.head import operations as head_ops


def _legacy_unbound_v1_run(run_json: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Give a production-shaped Codex HeadRun its exact, still-unbound v1 descriptor."""
    run = head_ops.HeadRun.from_json(run_json)
    # CommandHostRuntime preflights Codex with the profile's resolved model before it writes this
    # source.  The fixture's generic head run omits a model, which cannot be a clean fan-out
    # attestation, so give this isolated production-shaped source the real profile fact.
    run = replace(
        run,
        role="worker",
        spec=replace(run.spec, model="gpt-5.6-terra"),
    )
    run_id, fingerprint = head_run_binding(run.to_json())
    source = {
        "version": 1,
        "kind": "codex_session_event_jsonl",
        "state": "unbound",
        "run_id": run_id,
        "head_run_fingerprint": fingerprint,
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
        "root": str(root.resolve(strict=False)),
        "baseline": [],
    }
    return run.with_fanout_policy({
        "version": 1,
        "state": "allowed",
        "terminal_state": "clean",
        "run_id": run.run_id,
        "role": run.role,
        "model": run.spec.model or "",
        "binary_path": "/test/codex",
        "binary_digest": "0" * 64,
        "cli_version": "test-codex",
        "tool_schema_digest": "0" * 64,
        "provider_schema_verdict": "no_callable_child_spawn_surface",
        "events": [],
        "provider_source_required": True,
        "provider_source": source,
    }).to_json()


def _configure_production_shaped_codex_relaunch(host: Any, *, root: Path) -> None:
    """Make the fake's next Codex rework retain the real preflight/launch HeadRun handoff."""
    def preflight(
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        run = head_ops.HeadRun(
            run_id=run_id,
            spec=head_ops.HeadSpec(
                profile_id=head, adapter="codex", model="gpt-5.6-terra",
            ),
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
        return head_ops.HeadRun.from_json(_legacy_unbound_v1_run(
            run.to_json(), root=root / run_id,
        ))

    real_restart = host.restart_worker

    def restart(task: dict, record, *, heartbeat_run_id: str = "") -> LaunchedHead:
        launched = real_restart(task, record, heartbeat_run_id=heartbeat_run_id)
        preflight_run = head_ops.HeadRun.from_json(record.launch_intent["head_run"])
        reported = preflight_run.rebound(launched.handle, leaf=launched.leaf).working()
        host._write_head_pid("worker", task["ref"], head_run=reported.to_json(), leaf=launched.leaf)
        return replace(launched, head_run=reported.to_json())

    host.preflight_codex_run = preflight
    host.restart_worker = restart

class FakeKanboard:
    def __init__(self) -> None:
        self.instance_dir = Path(tempfile.gettempdir())
        self.calls: list[tuple[str, dict]] = []
        self.columns = [
            {"id": 1, "title": "Issues"},
            {"id": 2, "title": "Ready"},
            {"id": 3, "title": "In progress"},
            {"id": 4, "title": "Validate"},
            {"id": 7, "title": "Assessment"},
            {"id": 5, "title": "Blocked"},
            {"id": 6, "title": "Done"},
        ]
        self.tasks = [
            {
                "id": 12,
                "reference": "secretary-510-pilot",
                "title": "Pilot",
                "description": "pilot spec",
                "column_id": 2,
                "position": 1,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
            {
                "id": 13,
                "reference": "secretary-510-neighbor",
                "title": "Neighbor",
                "description": "do not claim",
                "column_id": 2,
                "position": 2,
                "swimlane_id": 4,
                "date_creation": 1720000000,
                "date_modification": 1720000000,
            },
        ]
        self.metadata = {
            12: {"project": "secretary", "task_type": "code", "slug": "pilot"},
            13: {"project": "secretary", "task_type": "code", "slug": "neighbor"},
        }
        self.comments: dict[int, list[dict]] = {12: [], 13: []}
        # The sprint entities live on their own Kanboard board (`Secretary sprints`, project id 8),
        # so a card is never readable as a sprint and an empty sprint board is the default.
        self.sprints: list[dict] = []
        self.now = 1720000000

    def add_sprint(self, reference: str, *, status: str = "open", **metadata: object) -> dict:
        task_id = 100 + len(self.sprints)
        sprint = {
            "id": task_id,
            "reference": reference,
            "title": metadata.get("sprint_goal", "sprint"),
            "description": "",
            "column_id": 1,
            "position": len(self.sprints) + 1,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        }
        self.sprints.append(sprint)
        self.metadata[task_id] = {
            "sprint_goal": "ship the thing",
            "sprint_definition_of_done": "the thing ships",
            "sprint_repositories": '["secretary"]',
            "sprint_status": status,
            "sprint_current_task": "",
            **{key: str(value) for key, value in metadata.items()},
        }
        self.comments.setdefault(task_id, [])
        return sprint

    def add_record(
        self, task_id: int, reference: str, title: str, metadata: dict, *, closed: bool = False,
    ) -> None:
        """A Product or Issue row in the Pipeline's Issues column, as the real board carries it."""
        self.tasks.append({
            "id": task_id,
            "reference": reference,
            "title": title,
            "description": "",
            "column_id": 1,
            "position": task_id,
            "swimlane_id": 4,
            "is_active": 0 if closed else 1,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        })
        self.metadata[task_id] = dict(metadata)
        self.comments[task_id] = []

    def _pool(self, project_id: object) -> list[dict]:
        return self.sprints if int(project_id or 0) == 8 else self.tasks

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "getProjectByName":
            return {"id": 8} if params.get("name") == SPRINT_BOARD_NAME else {"id": 7}
        if method == "getColumns":
            return self.columns
        if method == "getActiveSwimlanes":
            return [{"id": 4, "name": "Secretary"}]
        if method == "getAllTasks":
            status = params.get("status_id")
            if status not in {0, 1}:
                return []
            pool = self.sprints if int(params.get("project_id") or 0) == 8 else self.tasks
            return [
                task for task in pool
                if (int(task.get("is_active", task.get("status", 1)) or 0) != 0) == (status == 1)
            ]
        if method == "getTaskByReference":
            pool = self.sprints if int(params.get("project_id") or 0) == 8 else self.tasks
            return next((task for task in pool if task["reference"] == params["reference"]), None)
        if method == "getTaskMetadata":
            return self.metadata[int(params["task_id"])]
        if method == "saveTaskMetadata":
            self.metadata[int(params["task_id"])].update(params["values"])
            return True
        if method == "moveTaskPosition":
            task = next(task for task in self.tasks if int(task["id"]) == int(params["task_id"]))
            task["column_id"] = params["column_id"]
            self.now += 1
            task["date_modification"] = self.now
            return True
        if method == "createComment":
            self.now += 1
            self.comments[int(params["task_id"])].append(
                {"date_creation": self.now, "comment": params["content"]}
            )
            return len(self.comments[int(params["task_id"])])
        if method == "getAllComments":
            return self.comments[int(params["task_id"])]
        if method == "createTask":
            # Sprint rows are written this way by `SprintWriter.create`: a row first, its
            # reference last, which is the order the create's recovery depends on.
            pool = self._pool(params.get("project_id"))
            task_id = max(
                [int(task["id"]) for task in self.tasks + self.sprints] + [11]
            ) + 1
            pool.append({
                "id": task_id,
                "reference": "",
                "title": params.get("title", ""),
                "description": params.get("description", ""),
                "column_id": params.get("column_id", 1),
                "position": len(pool) + 1,
                "swimlane_id": params.get("swimlane_id", 0),
                "date_creation": self.now,
                "date_modification": self.now,
            })
            self.metadata[task_id] = {}
            self.comments[task_id] = []
            return task_id
        if method == "closeTask":
            # A sprint close archives the cards its dispositions take off the contract, so
            # this fake answers the archival write the same way the board does.
            task = next(
                task for task in self.tasks + self.sprints
                if int(task["id"]) == int(params["task_id"])
            )
            task["is_active"] = 0
            self.now += 1
            task["date_modification"] = self.now
            return True
        if method == "updateTask":
            task = next(
                task for task in self.tasks + self.sprints
                if int(task["id"]) == int(params["id"])
            )
            for field in ("reference", "title", "description"):
                if field in params:
                    task[field] = params[field]
            self.now += 1
            task["date_modification"] = self.now
            return True
        raise AssertionError(method)


# The head snapshot the sprint entity resolves a declared observer against. It is the
# installation's own registry, not the dispatcher's catalog, and a sprint may not be opened on a
# profile that is missing from it.
SPRINT_HEAD_SNAPSHOT = "\n".join([
    "resources:",
    "  openai-sub:",
    "    account: openai-subscription",
    "  claude-sub:",
    "    account: claude-subscription",
    "profiles:",
    "  codex-observer:",
    "    adapter: codex",
    "    resource: openai-sub",
    "  claude-observer:",
    "    adapter: claude",
    "    resource: claude-sub",
    "role_defaults:",
    "  new_card: codex-observer",
    "  reviewer: codex-observer",
    "  observer: codex-observer",
    "",
])


class TwoOpenSprintAdmission:
    """Open the two sprints the pilot setting admits, through `SprintWriter.create` itself.

    A dispatcher fixture reads sprint rows the way production does, so the rows it reads have to
    be rows admission produced: the setting is written before either create, the products, issues
    and project registry the create validates against are seeded, and the pair is disjoint on
    product, reservation and repository.  Each sprint declares its own observer: `observer` is the
    first sprint's and `second_observer` the second's, which defaults to none for the scenarios
    that only need one head.  A scenario that needs a broken declaration corrupts the persisted
    value afterwards, which is the only way a live installation reaches one.

    Mixed into a fixture that owns `self.board` (a `FakeKanboard`) and `self.data_dir`.
    """

    FIRST = "sprint:1"
    SECOND = "sprint:2"
    # Two reserved projects each, so either sprint still has a card to claim once its first one
    # is in flight.
    RESERVATIONS = {FIRST: ["secretary", "fourth"], SECOND: ["other", "third"]}

    def sprint_instance(self) -> Path:
        """The installation directory the sprint entity validates and reads its limit from."""
        return self.data_dir / "registry" / "instance"

    def admit_two_open_sprints(self, *, observer: dict, second_observer: dict | None = None):
        from secretary.sprint_observer import none_choice
        from secretary.sprints import (
            SprintReader,
            SprintWriter,
            instance_open_sprint_limit,
        )

        instance = self.sprint_instance()
        (instance / "projects").mkdir(parents=True, exist_ok=True)
        for project in ("secretary", "other", "third", "fourth"):
            (instance / "projects" / f"{project}.yaml").write_text(
                f"id: {project}\n", encoding="utf-8",
            )
        write_installed_pair(instance, SPRINT_HEAD_SNAPSHOT)
        # The setting is in force before either create runs: it is what the second one is
        # admitted by, and admission reads it live.
        (instance / "instance.yaml").write_text("open_sprint_limit: 2\n", encoding="utf-8")
        self.assertEqual(instance_open_sprint_limit(instance), 2)
        self.board.add_record(20, "product:secretary", "Secretary", {
            "record_type": "product", "product_id": "secretary",
            "product_projects": json.dumps(["secretary", "fourth"]),
        })
        self.board.add_record(21, "product:other", "Other", {
            "record_type": "product", "product_id": "other",
            "product_projects": json.dumps(["other", "third"]),
        })
        self.board.add_record(22, "issue:secretary", "Secretary issue", {
            "record_type": "issue", "issue_product": "secretary", "issue_kind": "feature",
            "issue_priority": "P1",
        })
        self.board.add_record(23, "issue:other", "Other issue", {
            "record_type": "issue", "issue_product": "other", "issue_kind": "feature",
            "issue_priority": "P1",
        })
        writer = SprintWriter(self.board, data_dir=self.data_dir, instance=instance)
        roots = self.data_dir / "repos"
        for reference, product, issue, request in (
            (self.FIRST, "secretary", "issue:secretary", "admit-first-sprint"),
            (self.SECOND, "other", "issue:other", "admit-second-sprint"),
        ):
            writer.create(
                role="po", actor="operator", goal=f"goal of {reference}",
                definition_of_done="done when the pair is proven",
                reference=reference, product=product, issues=[issue],
                projects=self.RESERVATIONS[reference],
                repositories=[str(roots / product)],
                observer=observer if reference == self.FIRST else (
                    second_observer if second_observer is not None else none_choice()
                ),
                request_id=request,
            )
        self.assertEqual(
            sorted(
                sprint["ref"]
                for sprint in SprintReader(self.board).list(statuses={"open"}, create=False)
            ),
            [self.FIRST, self.SECOND],
        )
        return writer

    def sprint_row_id(self, reference: str) -> int:
        return int(next(row for row in self.board.sprints if row["reference"] == reference)["id"])

    def rewrite_observer(self, reference: str, value: str) -> None:
        """Break the persisted declaration of an already-open sprint, as decay does."""
        self.board.metadata[self.sprint_row_id(reference)]["sprint_observer"] = value

    def link_pair_cards(self) -> None:
        """One card of each sprint's two reserved projects, all Ready."""
        self.board.metadata[12]["sprint_ref"] = self.FIRST
        self.board.metadata[13]["project"] = "other"
        self.board.metadata[13]["sprint_ref"] = self.SECOND
        # `fourth-1` sits ahead of `third-1` in the claim order, so a tick that holds the first
        # sprint back records the skip and the other sprint's claim in the same pass.
        self.add_pair_card(14, "fourth-1", project="fourth", sprint=self.FIRST)
        self.add_pair_card(15, "third-1", project="third", sprint=self.SECOND)

    def add_pair_card(self, task_id: int, reference: str, *, project: str, sprint: str) -> None:
        self.board.tasks.append({
            "id": task_id, "reference": reference, "title": reference, "description": "spec",
            "column_id": 2, "position": task_id, "swimlane_id": 4,
            "date_creation": 1720000000, "date_modification": 1720000000,
        })
        self.board.metadata[task_id] = {
            "project": project, "task_type": "code", "slug": reference, "sprint_ref": sprint,
        }
        self.board.comments[task_id] = []


class FakeCatalog:
    def __init__(
        self,
        adapter: dict | None = None,
        *,
        default_branch: str = "",
        instance_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter or {}
        self._default_branch = default_branch
        # Checkpoint freshness reads the instance repo; the default is deliberately
        # not a repo, so tests that do not care read back empty git fields.
        self.instance_dir = instance_dir or Path("/nonexistent-instance")
        # A trimmed stand-in for heads.yaml: enough profiles to tell two families apart in the
        # routing journal, including one that pins no model at all.
        self.profiles = {
            "codex": {"adapter": "codex", "model": "gpt-5.6-terra", "effort": "default", "resource": "openai-sub"},
            "codex-reviewer": {
                "adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra", "resource": "openai-sub",
            },
            "claude-opus": {"adapter": "claude", "model": "opus", "resource": "claude-sub"},
            "claude-default": {"adapter": "claude", "resource": "claude-sub"},
        }
        self.resources = {
            "openai-sub": {"account": "openai-subscription"},
            "claude-sub": {"account": "claude-subscription"},
        }
        self.profiles["codex-observer"] = {
            "adapter": "codex", "model": "gpt-5.6-terra", "effort": "extra",
            "resource": "openai-sub", "codex_mode": "tui",
        }
        # Mutable, like the role_defaults block of heads.yaml: an operator can re-point a role
        # while cards are in flight.
        self.role_defaults = {
            "new_card": "codex", "reviewer": "codex-reviewer", "observer": "codex-observer",
        }

    def default_branch(self, project: str, override: str | None) -> str:
        # Same precedence as InstanceCatalog: card override, then the binding, then "main".
        return override or self.binding(project).get("default_branch") or "main"

    def adapter(self, project: str) -> dict:
        return self._adapter

    def worker_head(self, task: dict) -> str:
        # Routing overrides resolve ahead of the role default, as in InstanceCatalog: the resolved
        # head is written to the board at claim and re-resolved on adoption, so a fake that always
        # answers "codex" would hide an override that never propagates.
        return str(task.get("routing", {}).get("head_override") or self.role_defaults["new_card"])

    def review_head(self, task: dict) -> str:
        return str(
            task.get("routing", {}).get("review_head_override") or self.role_defaults["reviewer"]
        )

    def head_profile(self, head: str) -> dict:
        # The registry entry behind a head, as InstanceCatalog answers it: prompt delivery resolves
        # the adapter through this, and an unknown head is an error rather than an empty profile.
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return self.profiles[head]

    def head_fallback(self, head: str) -> list[str]:
        # Same rule as InstanceCatalog: the chain is whatever the registry writes down, and an
        # unknown head is an error rather than an empty chain, so the claim-time walk can tell
        # "this head names no stand-in" from "this head does not exist".
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        chain = self.profiles[head].get("fallback")
        return [str(entry) for entry in chain] if isinstance(chain, list) else []

    def claimed_worker_head(self, task: dict) -> str:
        # Same rule as InstanceCatalog: the head the claim wrote onto the card wins over whatever
        # the override and the role default say now, and a claimed head that has left the registry
        # stops the bring-up instead of falling back to the current default.
        return self._claimed_head(task, "resolved_worker_head", self.worker_head)

    def claimed_review_head(self, task: dict) -> str:
        return self._claimed_head(task, "resolved_review_head", self.review_head)

    def _claimed_head(self, task: dict, key: str, current) -> str:
        claimed = task.get("routing", {}).get(key)
        if not claimed:
            return current(task)
        head = str(claimed)
        if head not in self.profiles:
            raise HostError(f"head {head!r} recorded at claim is unavailable")
        return head

    def head_run(
        self, task: dict, *, role: str, head: str = "", workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """Mirror InstanceCatalog.head_run over a four-profile registry: `codex` for the worker,
        `codex-reviewer` for the reviewer, `claude-opus` as the other family and `claude-default` as
        the profile that pins no model. Same rule as the real catalog: the head comes from the
        bring-up, its configuration from the registry as it reads right now, and only the caller's
        own record can say the claim reached this head by walking a chain."""
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = str(override or self.role_defaults["new_card"])
        else:
            override = routing.get("review_head_override")
            asked = str(override or self.role_defaults["reviewer"])
        launched = str(head) if head else asked
        # Same rule as InstanceCatalog: a head the claim reached by walking a chain says so, and
        # anything else that differs from the asked head is the record's older decision.
        source = (
            ("fallback" if failover else "record")
            if launched != asked
            else ("card" if override else "role_default")
        )
        profile = self.profiles.get(launched, {"adapter": "codex", "resource": "openai-sub"})
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            # Same as InstanceCatalog: a claude profile that pins no model leaves the choice to the
            # CLI, and the snapshot names the model that CLI resolves at this bring-up.
            model, model_source = claude_launch_model(
                profile, workspace=workspace, env=role_launch_env(role)
            )
        return head_run_from_profile(
            role=role,
            head=launched,
            head_source=source,
            profile=profile,
            resources=self.resources,
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        # Same rule as InstanceCatalog: the observer's own role_defaults key, with a named fallback
        # profile rather than the worker's default.
        head = str(self.role_defaults.get("observer") or OBSERVER_HEAD_FALLBACK)
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return head

    def observer_profile(self, head: str) -> dict:
        # Same rule as InstanceCatalog: one lookup for a head a sprint declared, no fallback. A
        # profile that has left the registry makes the sprint unrunnable, and the fence says so.
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return self.profiles[head]

    def observer_run(self, head: str, *, workspace: str = "") -> HeadRun:
        profile = self.profiles.get(head, {"adapter": "codex", "resource": "openai-sub"})
        return head_run_from_profile(
            role="observer",
            head=head,
            head_source="role_default",
            profile=profile,
            resources=self.resources,
        )

    def binding(self, project: str) -> dict:
        # `orca_binding` is required of every enabled binding, so the double carries one too. Here
        # it spells the project the same way; the projects where it does not have their own tests.
        binding = {"repo": f"/home/dev/{project}", "orca_binding": project}
        if self._default_branch:
            binding["default_branch"] = self._default_branch
        return binding


class FakeHost:
    def __init__(self, root: Path, catalog: FakeCatalog | None = None) -> None:
        self.root = root
        # The real host snapshots the head at bring-up and hands the record back; the fake goes
        # through the same catalog so the routing journal sees real configurations here too.
        self.catalog = catalog or FakeCatalog()
        # Ordered log of every host call. The per-method lists below answer "did it happen"; this
        # answers "in what order", which some invariants depend on (complete_green must push from
        # the workspace before teardown removes it).
        self.calls: list[str] = []
        self.prepared: list[str] = []
        self.prepare_requires_existing: list[bool] = []
        # Every launch this fake performs gets its own head run identity, numbered in order.
        self.head_runs = 0
        # The production runtime installs this exact-run ingress immediately after a Codex launch
        # intent is durable.  Most dispatcher fixtures use non-source HeadRuns, so the double
        # records the hand-off without inventing a provider journal event.
        self.codex_provider_ingresses: list[str] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.torn_down: list[str] = []
        self.completed: list[str] = []
        self.fail_prepare_reason = ""
        # A bring-up failure the caller has to read for more than its message, the worker twin of
        # `fail_observer_error`: a HeadLaunchAborted carrying the pane that stayed up.
        self.fail_prepare_error: Exception | None = None
        self.fail_result_reason = ""
        self.fail_review_error: Exception | None = None
        # Recovery retries a busy reviewer nudge against the launch intent's existing HeadRun.
        # Keep that operation independently scriptable: it is neither another split nor a worker
        # freeze, and tests use the call log to prove the ordering.
        self.fail_review_delivery_retry_error: Exception | None = None
        self.review_delivery_retry_evidence: dict | None = None
        self.review_delivery_retries: list[str] = []
        # A production reviewer can receive its prompt before a later freeze fails.  Tests that
        # exercise that boundary give the fake the same completed metadata-only receipt.
        self.review_launch_delivery_evidence: dict[str, object] = {}
        # Failure hooks for host calls the real runtime can fail on: a rework workspace removed
        # out of band, a merge push the remote rejects, an orca terminal inventory that errors.
        self.fail_restart_reason = ""
        # The relaunch twin of `fail_prepare_error`: a rework or respawn bring-up whose failure the
        # caller has to read for more than its message, e.g. a head pane that was not ready.
        self.fail_restart_error: Exception | None = None
        self.fail_complete_reason = ""
        self.worker_status_result: dict | None = None
        self.review_status_result: dict | None = None
        self.worker_status_error: Exception | None = None
        self.review_status_error: Exception | None = None
        # The provider cursor the fake's bound progress seam answers with. Tests advance
        # it to model a working transcript; the default is one unchanged value, i.e.
        # admitted Quiet between ticks.
        self.provider_cursor = "fake:unchanged"
        # Mechanical gate results consumed FIFO; empty means the default green (ci: none / passing).
        self.gate_results: list[GateResult] = []
        self.gate_calls: list[str] = []
        self.gate_error: Exception | None = None
        # Reviewer pane bookkeeping (secretary-651): which handle each review was split off, which
        # reviewer panes were closed on their own, and the commit the checkout reports. `commit` is
        # what start_review pins; reassign it to model a checkout that moved under a green verdict.
        self.split_from: list[str] = []
        self.stopped_reviews: list[str] = []
        self.review_stop_initiators: list[str] = []
        self.commit = "c0ffee1234567890"
        self.instance_publish_recoveries: set[tuple[str, str]] = set()
        # Observer heads (secretary-793): which sprints got one, which handles were stopped, and
        # the pid the fake heartbeat writes. os.getpid() is a live process, so the default launch
        # reads as alive; point it at a free pid to model a head that died.
        self.observers: list[str] = []
        # The sprint binding each bring-up handed the head, in launch order.
        self.observer_identities: list[dict[str, str]] = []
        self.observer_nudges: list[str] = []
        self.stopped_observers: list[str] = []
        # workspace -> live terminal handle, the inventory Orca answers `terminal list` from.
        self.observer_terminals: dict[str, str] = {}
        self.observer_pid = os.getpid()
        # Work liveness is separate from the pid.  Tests can make a live TUI report a completed,
        # stale queue without pretending the process has died.
        self.observer_status_result: dict | None = None
        self.fail_observer_reason = ""
        # A bring-up failure the caller has to read for more than its message, e.g. an
        # ObserverLaunchAborted that carries the handle of a terminal that stayed up.
        self.fail_observer_error: Exception | None = None
        # Orca refusing to close an observer pane: the head must be assumed alive afterwards.
        self.fail_stop_observer_reason = ""
        # The pid a worker/reviewer bring-up writes to its heartbeat file, the way the real
        # launcher's `with_pid_heartbeat` wrapper does. Launch-intent recovery reads it, so a fake
        # that never wrote one would make every intent look like a head that never came up. None
        # models a runtime that writes no heartbeat at all.
        self.head_pid: int | None = os.getpid()
        # Stop refusals (secretary-820). A stop the host will not confirm must never be followed by
        # a replacement head, and these are how a test makes one refuse.
        self.fail_stop_workspace_reason = ""
        self.fail_stop_head_reason = ""
        self.stop_initiators: list[tuple[str, str]] = []
        self.fail_stop_review_reason = ""
        self.fail_freeze_worker_reason = ""
        self.fail_retain_worker_reason = ""
        # Most fixture cards use the ordinary exec profile, which has no conversation to resume.
        # Tests that model a retained Codex TUI clear this explicitly.
        self.fail_resume_worker_reason = "retained worker session cannot accept a continuation"
        # The bounded report prompt (secretary-1172). It goes to the same live conversations a
        # continuation does, so `fail_resume_worker_reason` decides addressability for both; this
        # one fails a delivery into a head that *is* addressable, which is the refused/ambiguous
        # send. Every prompt actually delivered is recorded, so a test can prove there was one.
        self.fail_report_prompt_reason = ""
        self.report_prompts: list[str] = []
        self.retained_workers: list[str] = []
        self.resumed_workers: list[str] = []
        # The prompt each wake carried, built the way the real host builds it.
        self.resumed_continuations: list[str] = []
        # A retained session the heartbeat can no longer confirm as suspended: set False to model
        # the head dying while the reviewer judged its checkout.
        self.retained_worker_alive = True
        # A retained session whose process is *provably* gone (`known and not alive`), not merely
        # unconfirmable: set True to model orca having lost the head entirely, where there is
        # nothing left to freeze before the reviewer takes the checkout.
        self.worker_retained_gone = False
        # A dispatcher death in the gap between the round's document reaching disk and the head
        # being woken or launched. Both bring-ups write the document and then, separately, wake or
        # launch, so both can be interrupted there. Fires once and clears itself, so the tick that
        # recovers runs the same path for real.
        self.crash_after_task_doc: BaseException | None = None

    def _write_task_doc(
        self, task: dict, workspace: Path, attempt_id: str, generation: int, decision: str = ""
    ) -> None:
        """Write the TASK.md this bring-up would hand the worker, from the real builder.

        The fake owns no copy of the document: a test that wants to know which report round the
        worker was actually given reads it out of the checkout, the way the worker does.
        """
        workspace.mkdir(parents=True, exist_ok=True)
        # Same order as the real host, and the real code: the round's body files go before the
        # document that names the new one is written.
        CommandHostRuntime._clear_report_bodies(self, task["ref"])  # type: ignore[arg-type]
        document = CommandHostRuntime._worker_task_doc(
            self,  # type: ignore[arg-type]
            task,
            task.get("workspace", {}).get("base_branch") or "main",
            attempt_id,
            generation,
            decision,
        )
        (workspace / "TASK.md").write_text(document, encoding="utf-8")
        if self.crash_after_task_doc is not None:
            crash, self.crash_after_task_doc = self.crash_after_task_doc, None
            raise crash

    def _write_head_pid(
        self,
        kind: str,
        reference: str,
        *,
        head_run: dict | None = None,
        leaf: str = "",
        run_id: str = "",
    ) -> None:
        path = Path(pid_file_path(kind, reference))
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.head_pid is None:
            path.unlink(missing_ok=True)
            return
        identity = run_heartbeat_identity(
            head_run or {"run_id": run_id}, role=kind, task=f"card:{reference}", leaf=leaf,
        )
        if self.head_pid > 0 and Path(f"/proc/{self.head_pid}/stat").exists():
            stat = Path(f"/proc/{self.head_pid}/stat").read_text(encoding="utf-8")
            starttime = stat[stat.rfind(")") + 2:].split()[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        else:
            # Death is checked before the kernel identity, so a valid-shaped record can model an
            # exited head without depending on a recycled or still-present /proc directory.
            starttime = "0"
            boot_id = "dead-process"
        identity.update({
            "version": 1,
            "pid": self.head_pid,
            "boot_id": boot_id,
            "proc_starttime_ticks": starttime,
        })
        path.write_text(json.dumps(identity), encoding="utf-8")

    def prepare_worker(
        self,
        task: dict,
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
        generation: int = 0,
        failover: bool = False,
        heartbeat_run_id: str = "",
    ) -> dict[str, str]:
        self.calls.append("prepare_worker")
        self.prepare_requires_existing.append(require_existing_workspace)
        if self.fail_prepare_error is not None:
            if isinstance(self.fail_prepare_error, HeadLaunchAborted):
                # A bring-up that failed with its terminal already open: the head is running, so
                # its heartbeat is there for recovery to find, exactly as after a real launch.
                self._write_head_pid(
                    "worker",
                    task["ref"],
                    run_id=heartbeat_run_id,
                    leaf=self.fail_prepare_error.leaf,
                )
            raise self.fail_prepare_error
        if self.fail_prepare_reason:
            raise HostError(self.fail_prepare_reason)
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self._write_task_doc(task, workspace, attempt_id, generation)
        self.prepared.append(task["ref"])
        launched = self._launched(
            f"term:{worker_id}", head, task, "worker", failover=failover, run_id=heartbeat_run_id
        )
        self._write_head_pid("worker", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        return {
            "workspace": str(workspace),
            "handle": launched.handle,
            "leaf": launched.leaf,
            "base_branch": task.get("workspace", {}).get("base_branch") or "main",
            "run": launched.run,
            # The real host always carries this bounded receipt, even when noop mode has no pane
            # and therefore no delivery facts to record yet.
            "delivery_evidence": {},
            # The head's own run, as `spawn` returns it (secretary-1412).
            "head_run": dict(launched.head_run),
        }

    def observer_workspace(self, reference: str) -> str:
        return str(self.root / "observers" / reference.replace(":", "-"))

    def configure_codex_provider_ingress(self, run, *, persist, stop, block) -> None:
        self.codex_provider_ingresses.append(run.run_id)

    def poll_codex_provider_ingress(self, run) -> None:
        return None

    def provider_progress(self, _task, record, kind) -> dict[str, str]:
        """A fake provider's opaque cursor is still explicitly bound to its HeadRun.

        The default answer re-reads one unchanged cursor: an admitted Quiet, which is
        what a real rollout produces between ticks when nothing advanced. Tests model
        advancement or darkness by scripting their own evidence.
        """
        run = record.review_head_run if kind == "review" else record.worker_head_run
        run_id, fingerprint = head_run_binding(run)
        if not run_id:
            return {"state": "unavailable", "reason": "fake has no persisted HeadRun"}
        return {
            "state": "observed", "admission": "accepted", "source": "fake-bound-session",
            "source_fingerprint": "f" * 32, "cursor": self.provider_cursor,
            "head_run_id": run_id, "head_run_fingerprint": fingerprint,
        }

    def _synthetic_status(self, task: dict, record, kind: str) -> dict | None:
        """Derive the vitality sources for a scripted status answer.

        A test that scripts ``worker_status_result``/``review_status_result`` spells the
        derived booleans the watchdog consumes. The vitality decision additionally needs
        the sources those booleans were derived from, so the fake derives them here the
        way the real ``command_terminal_status`` does: the raw classification is read
        through ``head_run_process_status`` against this record's pid file, and provider
        progress comes from the same bound-cursor seam. Fixtures that spell
        ``pid_status``/``provider_progress`` themselves (the vitality wiring tests)
        pass through verbatim.
        """
        scripted = getattr(self, f"{kind}_status_result")
        if scripted is None:
            return None
        result = dict(scripted)
        if result.get("identity_mismatch"):
            return result
        pid_file = record.worker_pid_file if kind == "worker" else record.review_pid_file
        run = record.worker_head_run if kind == "worker" else record.review_head_run
        leaf = record.worker_leaf if kind == "worker" else record.review_leaf
        if "pid_status" not in result:
            if pid_file:
                result["pid_status"] = dict(_head_run_process_status(
                    pid_file, run=run, role=kind,
                    task=f"card:{task['ref']}", leaf=leaf,
                ))
            else:
                result["pid_status"] = {
                    "known": False, "alive": False, "match": False,
                    "state": "not-yet-written",
                }
        if "provider_progress" not in result:
            result["provider_progress"] = dict(self.provider_progress(task, record, kind))
        if result.get("live") is False:
            # A scripted not-live answer models a terminal the inventory lost. The
            # classification above is what the reduction gets to see.
            result.setdefault("reason", "missing-terminal")
        return result

    def observer_provider_progress(self, record) -> dict[str, str]:
        """The observer twin of the shared exact-HeadRun progress seam."""
        run_id, fingerprint = head_run_binding(record.head_run)
        if not run_id:
            return {"state": "unavailable", "reason": "fake has no persisted observer HeadRun"}
        return {
            "state": "observed", "admission": "accepted", "source": "fake-bound-session",
            "source_fingerprint": "f" * 32, "cursor": "fake:unchanged",
            "head_run_id": run_id, "head_run_fingerprint": fingerprint,
        }

    def observer_pid_file(self, reference: str) -> str:
        return str(self.root / "observers" / f"{reference.replace(':', '-')}.pid")

    def prepare_observer(
        self, sprint: dict, head: str, *, prompt: str, identity: dict[str, str] | None = None,
        heartbeat_run_id: str = "",
    ) -> dict:
        self.calls.append("prepare_observer")
        self.observer_identities.append(dict(identity or {}))
        if self.fail_observer_error is not None:
            raise self.fail_observer_error
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        reference = str(sprint["ref"])
        workspace = Path(self.observer_workspace(reference))
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "SPRINT.md").write_text(prompt, encoding="utf-8")
        self.observers.append(reference)
        pid_file = Path(self.observer_pid_file(reference))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        handle = f"observer:{reference}"
        leaf = f"leaf:{handle}"
        head_run = head_ops.HeadRun(
            run_id=heartbeat_run_id or "fake-observer-run",
            spec=head_ops.HeadSpec(
                profile_id=head, adapter="codex", model="gpt-5.6-terra"
            ),
            workspace=str(workspace),
            task_ref=head_ops.TaskRef.sprint(reference),
            role="observer",
            handle=handle,
            leaf=leaf,
            pid_file=str(pid_file),
        ).to_json()
        observer_identity = run_heartbeat_identity(
            head_run, role="observer", task=f"sprint:{reference}", leaf=leaf,
        )
        if self.observer_pid > 0 and Path(f"/proc/{self.observer_pid}/stat").exists():
            stat = Path(f"/proc/{self.observer_pid}/stat").read_text(encoding="utf-8")
            observer_identity.update({
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                "proc_starttime_ticks": stat[stat.rfind(")") + 2:].split()[19],
            })
        else:
            observer_identity.update({"boot_id": "dead-process", "proc_starttime_ticks": "0"})
        observer_identity.update({"version": 1, "pid": self.observer_pid})
        pid_file.write_text(json.dumps(observer_identity), encoding="utf-8")
        # Like Orca: the terminal is findable by its workspace, which is how a head whose handle
        # was lost with its tick still gets stopped.
        self.observer_terminals[str(workspace)] = handle
        return {
            "workspace": str(workspace),
            "handle": handle,
            "leaf": leaf,
            "pid_file": str(pid_file),
            # Like the real host: a bring-up that puts a prompt in front of the head says so, and
            # hands back what the delivery boundary saw doing it.
            "prompt_delivered": True,
            "delivery_evidence": {
                "subject": "observer-launch",
                "handle": handle,
                "stage": "acknowledged",
                "payload_bytes": len(prompt.encode("utf-8")),
            },
            "run": self.catalog.observer_run(head, workspace=str(workspace)).to_json(),
            "head_run": head_run,
        }

    def observer_status(self, _record) -> dict:
        if self.observer_status_result is not None:
            return dict(self.observer_status_result)
        return {"last_activity": time.time(), "idle": False}

    def nudge_observer(self, record) -> str:
        self.calls.append("nudge_observer")
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        self.observer_nudges.append(str(record.sprint))
        # Like the real host, this confirms terminal acceptance only. The later durable resume
        # closes the observer delivery during normal reconciliation.
        return "accepted"

    def stop_observer(self, record) -> None:
        self.calls.append("stop_observer")
        if self.fail_stop_observer_reason:
            raise HostError(self.fail_stop_observer_reason)
        handle = record.handle or self.observer_terminals.get(str(record.workspace) or "", "")
        self.observer_terminals.pop(str(record.workspace) or "", None)
        if handle:
            self.stopped_observers.append(handle)

    def pane_leaf(self, workspace: str, handle: str) -> str:
        return f"leaf:{handle}"

    def start_review(self, task: dict, record) -> ReviewLaunch:
        self.calls.append("start_review")
        if self.fail_review_error is not None:
            raise self.fail_review_error
        self.reviews.append(task["ref"])
        # Mirror the real host: the reviewer gets its own pane and the worker head is shut down,
        # pinning the commit the reviewer judges.
        self.split_from.append(record.handle)
        launched = self._launched(
            f"review:{task['ref']}", record.review_head, task, "reviewer", record.workspace,
            failover=bool(record.preferred_review_head),
            delivery_evidence=dict(self.review_launch_delivery_evidence),
            run_id=str((record.launch_intent or {}).get("run_id") or ""),
        )
        self._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        try:
            if record.worker_continuation.retained and self.worker_retained_vanished(record):
                # Mirror the real host: a retained worker whose session is provably gone leaves
                # nothing to freeze, so the reviewer takes the checkout it left rather than the
                # launch aborting forever over a head that will never confirm suspended.
                pass
            elif record.worker_continuation.retained:
                # Mirror the real host: a retained worker is already suspended, so the reviewer
                # judges a checkout nothing is editing without ending that conversation.
                self.confirm_worker_retained(record)
            else:
                self.freeze_worker(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not go: the real host hands the pane
            # back with the failure rather than reporting a bring-up that left nothing running.
            raise HeadLaunchAborted(
                f"worker freeze failed: {exc}",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=dict(launched.delivery_evidence),
                # The pane is up, so the run of the head in it travels with the failure: the
                # adoption that follows continues that run rather than opening a new identity
                # for a reviewer this launch did start (secretary-1414).
                head_run=dict(launched.head_run),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=launched.leaf,
            commit=self.commit,
            run=launched.run,
            head_run=dict(launched.head_run),
            delivery_evidence=dict(launched.delivery_evidence),
            fallback_reason=str(getattr(self, "review_launch_fallback_reason", "")),
        )

    def nudge_review_delivery(self, task: dict, record, intent: dict) -> dict:
        """Fake the direct document retry on the one reviewer this intent already owns."""
        self.calls.append("nudge_review_delivery")
        self.review_delivery_retries.append(task["ref"])
        if self.fail_review_delivery_retry_error is not None:
            raise self.fail_review_delivery_retry_error
        run = head_ops.HeadRun.from_json(dict(intent["head_run"])).working()
        return {
            "handle": str(intent.get("handle") or run.handle),
            "leaf": str(intent.get("leaf") or run.leaf),
            "head_run": run.to_json(),
            "delivery_evidence": self.review_delivery_retry_evidence or {
                "subject": "reviewer-launch",
                "handle": str(intent.get("handle") or run.handle),
                "stage": "acknowledged",
                "turn_confirmed": True,
                "readiness_state": "ready",
            },
        }

    def restart_worker(self, task: dict, record, *, heartbeat_run_id: str = "") -> LaunchedHead:
        self.calls.append("restart_worker")
        if self.fail_restart_error is not None:
            if isinstance(self.fail_restart_error, HeadLaunchAborted):
                # The pane stayed up, so the head's heartbeat is there for recovery to find.
                self._write_head_pid(
                    "worker",
                    task["ref"],
                    run_id=heartbeat_run_id,
                    leaf=self.fail_restart_error.leaf,
                )
            raise self.fail_restart_error
        if self.fail_restart_reason:
            raise HostError(self.fail_restart_reason)
        self._write_task_doc(
            task, Path(record.workspace), record.attempt_id, record.report_generation,
            record.report_decision,
        )
        self.prepared.append(task["ref"])
        launched = self._launched(
            f"rework:{task['ref']}", record.head, task, "worker",
            failover=bool(record.preferred_head),
            run_id=heartbeat_run_id,
        )
        self._write_head_pid("worker", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        return launched

    def _launched(
        self, handle: str, head: str, task: dict, role: str, workspace: str = "",
        failover: bool = False, delivery_evidence: dict[str, object] | None = None, run_id: str = "",
    ) -> LaunchedHead:
        leaf = f"leaf:{handle}"
        return LaunchedHead(
            handle=handle,
            head=head,
            run=self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            ).to_json(),
            leaf=leaf,
            delivery_evidence=dict(delivery_evidence or {}),
            # The head's own run, as `spawn` hands it back on the real host (secretary-1412). The
            # fake opens no pane, but it does report an identity: what a bring-up owes the record
            # is that this head can be named afterwards, and a fake that answered `{}` could not
            # show a recovery continuing the same run.
            head_run=self._head_run(handle, head, task, role, workspace, leaf, run_id=run_id),
        )

    def _head_run(
        self, handle: str, head: str, task: dict, role: str, workspace: str, leaf: str, *, run_id: str = ""
    ) -> dict:
        self.head_runs += 1
        return head_ops.HeadRun(
            run_id=run_id or f"run-{role}-{self.head_runs}",
            spec=head_ops.HeadSpec(profile_id=head, adapter="codex"),
            workspace=workspace or str(self.root / f"{task['ref']}-pilot"),
            task_ref=head_ops.TaskRef.card(task["ref"]),
            handle=handle,
            leaf=leaf,
            pid_file=pid_file_path("review" if role == "reviewer" else "worker", task["ref"]),
        ).to_json()

    def worker_status(self, task: dict, record) -> dict:
        self.calls.append("worker_status")
        if self.worker_status_error is not None:
            raise self.worker_status_error
        synthetic = self._synthetic_status(task, record, "worker")
        if synthetic is not None:
            return synthetic
        # No scripted answer: derive the same live shape the real status seam produces,
        # so the vitality reduction sees this record's true classification and bound
        # provider cursor instead of an evidence-free "live".
        pid_file = record.worker_pid_file
        run = record.worker_head_run
        leaf = record.worker_leaf
        pid_status = (
            dict(_head_run_process_status(
                pid_file, run=run, role="worker",
                task=f"card:{task['ref']}", leaf=leaf,
            ))
            if pid_file else {
                "known": False, "alive": False, "match": False,
                "state": "not-yet-written",
            }
        )
        return {
            "known": True,
            "live": bool(pid_status.get("alive")) if pid_status.get("known") else True,
            "reason": "live" if pid_status.get("alive") else (
                "process-exited" if pid_status.get("state") == "dead" else "live"
            ),
            "pid_confirmed": bool(pid_status.get("match") and pid_status.get("alive")),
            "last_activity": time.time(),
            "pid_status": pid_status,
            "provider_progress": dict(self.provider_progress(task, record, "worker")),
        }

    def review_status(self, task: dict, record) -> dict:
        self.calls.append("review_status")
        if self.review_status_error is not None:
            raise self.review_status_error
        live = task["ref"] in self.reviews
        synthetic = self._synthetic_status(task, record, "review")
        if synthetic is not None:
            return synthetic
        if not live:
            return {"known": True, "live": False, "reason": "missing-terminal"}
        pid_file = record.review_pid_file
        run = record.review_head_run
        leaf = record.review_leaf
        pid_status = (
            dict(_head_run_process_status(
                pid_file, run=run, role="review",
                task=f"card:{task['ref']}", leaf=leaf,
            ))
            if pid_file else {
                "known": False, "alive": False, "match": False,
                "state": "not-yet-written",
            }
        )
        return {
            "known": True,
            "live": bool(pid_status.get("alive")) if pid_status.get("known") else True,
            "reason": "live",
            "pid_confirmed": bool(pid_status.get("match") and pid_status.get("alive")),
            "last_activity": time.time(),
            "pid_status": pid_status,
            "provider_progress": dict(self.provider_progress(task, record, "review")),
        }

    def verify_worker_result(self, task: dict, record) -> None:
        self.calls.append("verify_worker_result")
        if self.fail_result_reason:
            raise HostError(self.fail_result_reason)

    def gate_check(self, task: dict, record) -> GateResult:
        self.calls.append("gate_check")
        self.gate_calls.append(task["ref"])
        if self.gate_error is not None:
            raise self.gate_error
        if self.gate_results:
            scripted = self.gate_results.pop(0)
            # A scripted gate answer may be the absence of one: an exception in the queue is
            # raised where the real gate would have raised it.
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        return GateResult("green", "gate green")

    def restore_workspace(self, task: dict, worker: str) -> str:
        self.calls.append("restore_workspace")
        return str(self.root / worker)

    def complete_green(self, task: dict, record) -> None:
        self.calls.append("complete_green")
        if self.fail_complete_reason:
            raise HostError(self.fail_complete_reason)
        self.completed.append(task["ref"])

    def stop(self, record) -> None:
        self.calls.append("stop")
        self.stopped.append(record.worker)
        self._kill_head("worker", record)
        self._kill_head("review", record)

    def stop_workspace(self, record) -> None:
        """The confirmed twin of `stop`: a refusal reaches the caller (secretary-820)."""
        self.calls.append("stop_workspace")
        if self.fail_stop_workspace_reason:
            raise HostError(self.fail_stop_workspace_reason)
        self.stop(record)

    @contextlib.contextmanager
    def committing(self, flush):
        """The real host's durable-commit seam (secretary-1412), lent for the caller's span.

        The fake performs no host I/O, so it never commits mid-operation; it still has to accept
        the loan, because the tick and the freeze hand it out unconditionally and a host that
        could not take it would be a host the production paths cannot use.
        """
        previous = getattr(self, "commit_state", None)
        self.commit_state = flush
        try:
            yield
        finally:
            self.commit_state = previous

    def stop_head(self, record, kind: str, initiator: str = "dispatcher") -> None:
        # The initiator the real host records on the run (secretary-1412). Kept in the call log so
        # a test can say not only that a head was stopped but who this dispatcher said stopped it.
        self.calls.append(f"stop_head:{kind}")
        self.stop_initiators.append((kind, initiator))
        if self.fail_stop_head_reason:
            raise HostError(self.fail_stop_head_reason)
        handle = record.review_handle if kind == "review" else record.handle
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        if not handle and not leaf and not pid_file:
            raise HostError(f"{kind} head has neither a pane handle nor a pid heartbeat")
        self._kill_head(kind, record)

    def freeze_worker(self, record) -> None:
        self.calls.append("freeze_worker")
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if record.handle or record.worker_leaf or record.worker_pid_file:
            self.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

    def retain_worker(self, record) -> None:
        self.calls.append("retain_worker")
        if self.fail_retain_worker_reason:
            raise HostError(self.fail_retain_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("worker session is unavailable for retention")
        if not record.handle:
            # Like the real host: a head with no pane is unaddressable, so there is nothing to
            # retain and the caller stops it instead.
            raise HostError("worker session has no addressable pane to retain")
        self.retained_workers.append(record.handle)

    def worker_retained_alive(self, record) -> bool:
        if not record.worker_continuation.retained:
            return False
        return bool(self.retained_worker_alive and (record.handle or record.worker_pid_file))

    def worker_retained_vanished(self, record) -> bool:
        if not record.worker_continuation.retained:
            return False
        return bool(self.worker_retained_gone)

    def confirm_worker_retained(self, record) -> None:
        self.calls.append("confirm_worker_retained")
        # `fail_freeze_worker_reason` is the knob for "the host cannot vouch that this worker is
        # not writing". Suspending it for the reviewer instead of stopping it does not change what
        # a reviewer launch needs to hear before it takes the checkout.
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def worker_addressable(self, record) -> bool:
        # The real host asks whether this head is a live provider conversation: a pane handle plus
        # an adapter that has one. The fixture's exec profile has neither, and that is exactly what
        # `fail_resume_worker_reason` models here.
        return bool(record.handle) and not self.fail_resume_worker_reason

    def prompt_worker_report(self, task: dict, record) -> None:
        self.calls.append("prompt_worker_report")
        if self.fail_report_prompt_reason:
            raise HostError(self.fail_report_prompt_reason)
        if not self.worker_addressable(record):
            raise HostError("worker session cannot accept a report prompt")
        if not record.worker_pid_file and not record.handle:
            raise HostError("worker session exited")
        # Unlike a continuation, this writes no document and clears no body file: the round the
        # head is being pointed back at is the one it already has.
        self.report_prompts.append(
            _report_nudge_prompt(record.report_generation, task["ref"])
        )

    def resume_worker(self, task: dict, record) -> None:
        self.calls.append("resume_worker")
        if self.fail_resume_worker_reason:
            raise HostError(self.fail_resume_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("retained worker session exited")
        # Same order as the real host: the round's document is on disk before the suspended
        # conversation is woken, and the prompt that wakes it names that same round.
        self._write_task_doc(
            task, Path(record.workspace), record.attempt_id, record.report_generation,
            record.report_decision,
        )
        self.resumed_continuations.append(
            head_ops.NudgePointer.at_document(
                str(Path(record.workspace) / "TASK.md"),
                _continuation_note(record.report_generation, record.report_decision),
            ).text
        )
        self.resumed_workers.append(record.handle)

    def _kill_head(self, kind: str, record) -> None:
        """Drop the heartbeat of a stopped head, the way a closed pty tree does.

        Without this a stop would leave a pid file that still names this live test process, and
        every later liveness read would answer that the head the test just stopped is running.
        """
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if pid_file:
            Path(pid_file).unlink(missing_ok=True)

    def stop_review(self, record, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        self.calls.append("stop_review")
        # Who ended this reviewer, as the runtime named it. The real host writes it onto the
        # record's run; this double only has to prove the caller passed one, which is what the
        # initiator-per-path assertions read.
        self.review_stop_initiators.append(initiator)
        if not record.review_handle and not record.review_leaf and not record.review_pid_file:
            return
        if self.fail_stop_review_reason:
            raise HostError(self.fail_stop_review_reason)
        if record.review_handle:
            self.stopped_reviews.append(record.review_handle)
        self._kill_head("review", record)

    def head_commit(self, record) -> str:
        self.calls.append("head_commit")
        return self.commit

    def is_instance_publish_recovery(self, task: dict, record, reviewed_commit: str, current_commit: str) -> bool:
        self.calls.append("is_instance_publish_recovery")
        return (reviewed_commit, current_commit) in self.instance_publish_recoveries

    def teardown(self, record) -> None:
        self.calls.append("teardown")
        self.stop(record)
        self.torn_down.append(record.worker)


class FakeCheckpoint:
    def __init__(self, outcome: CheckpointResult | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    def write(self) -> CheckpointResult:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakePusher:
    def __init__(self, outcome: dict | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def push(self, state: dict | None = None) -> dict:
        self.calls.append(dict(state or {}))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {**(state or {}), **self.outcome}


class FakeSprints:
    """The sprint facts the card cycle asks about, and nothing else.

    `show` answers what a card's sprint declares, which is what decides whether a verdict parks.
    `list` stays empty on purpose: the observer *head* lifecycle is reconciled from it, and these
    tests are about the cards, not about the head that watches them.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def list(self, *args, **kwargs) -> list[dict]:
        return []

    def show(self, reference: str, **kwargs) -> dict:
        if reference not in self.rows:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.rows[reference]



__all__ = [
    "FakeCatalog", "FakeCheckpoint", "FakeHost", "FakeKanboard", "FakePusher", "FakeSprints",
    "TwoOpenSprintAdmission", "_configure_production_shaped_codex_relaunch", "_legacy_unbound_v1_run",
]
