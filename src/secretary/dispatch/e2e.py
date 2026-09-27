"""The e2e check a project adapter declares, and the GitHub calls that dispatch and identify its run.

A project declares one e2e check in its adapter, beside its mechanical gate (secretary-1795):

    validation:
      ci: github
      e2e:
        workflow: e2e.yml         # the workflow file name, or its numeric id, in the project's repository
        inputs: {suite: mega}     # optional static `workflow_dispatch` inputs
        deadline: 6h              # optional; how long the run may take, 6h when absent
        candidate_input: sha      # optional; the input that receives the candidate SHA

`parse_e2e` is the one reading of it, and a malformed declaration raises
:class:`AdapterE2eDeclarationError`, a typed adapter error: the adapter read fails (`InstanceCatalog.
adapter`), so the card's gate fails with the reason, instead of the stage being skipped. An adapter
with no `e2e` key reads as None and nothing changes. `e2e` needs `ci: github`: only the github gate
publishes the candidate branch the workflow is dispatched on.

The run is identified by a **dispatch id**. The dispatcher passes a fresh id as the input
:data:`DISPATCH_ID_INPUT` and looks it up among the workflow's `workflow_dispatch` runs on the card's
branch, in the run's title. The declared workflow therefore has to take that input and put it in its
`run-name`:

    on:
      workflow_dispatch:
        inputs:
          secretary_dispatch_id: {required: true}
    run-name: e2e ${{ inputs.secretary_dispatch_id }}

A workflow that does not declare the input is refused by GitHub at dispatch (HTTP 422), which Blocks
the card with GitHub's answer; one that does not put it in its `run-name` is never identified, which
Blocks the card when the identification window runs out.

Everything here is host I/O through the gate's `_backend_call`/`_gh_api`, so a question that got no
answer is a `GateTransportError`, never a verdict. The stage (`dispatch/e2e_stage.py`) decides what
each answer does to the card.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from secretary.board.wait_card import WaitSpecError, parse_duration
from secretary.dispatch.gate import _HTTP_STATUS_RE, _backend_call, _failed_log, _gh_api, _LogFragment
from secretary.dispatch.helpers import _tail
from secretary.dispatch.types import GateTransportError, HostError

#: The `workflow_dispatch` input that carries the dispatch id, and must appear in the run's `run-name`.
DISPATCH_ID_INPUT = "secretary_dispatch_id"
DEFAULT_DEADLINE = "6h"
#: How long after its intent a dispatched run may stay unidentified before the card is Blocked.
E2E_IDENTIFY_SECONDS = max(60, int(os.environ.get("SECRETARY_E2E_IDENTIFY_SECONDS", str(15 * 60))))

_KEYS = frozenset({"workflow", "inputs", "deadline", "candidate_input"})
_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml$")
_INPUT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,99}$")
#: Conclusions of a failed job whose steps and log are the evidence of a red run.
_FAILED_JOB_CONCLUSIONS = frozenset({"failure", "timed_out"})
_RUNS_JQ = "[.workflow_runs[] | {id, display_title, name, head_sha, html_url, status, created_at}]"
_JOBS_JQ = (
    "[.jobs[] | {name, conclusion, html_url, "
    'steps: [(.steps // [])[] | select(.conclusion == "failure" or .conclusion == "timed_out") | .name]}]'
)


class AdapterE2eDeclarationError(HostError):
    """An adapter's `validation.e2e` is malformed; the message names the adapter and what is wrong."""

    def __init__(self, adapter: str, problem: str) -> None:
        super().__init__(f"adapter {adapter or '(unnamed)'} declares a malformed validation.e2e: {problem}")
        self.adapter = adapter
        self.problem = problem


@dataclass(frozen=True)
class E2eDeclaration:
    """A well-formed `validation.e2e`."""

    workflow: str
    inputs: tuple[tuple[str, str], ...] = ()
    deadline: str = DEFAULT_DEADLINE
    candidate_input: str = ""

    def dispatch_inputs(self, dispatch_id: str, sha: str) -> dict[str, str]:
        """Every input one dispatch sends: the static ones, the candidate SHA if declared, the id."""
        inputs = dict(self.inputs)
        if self.candidate_input:
            inputs[self.candidate_input] = sha
        inputs[DISPATCH_ID_INPUT] = dispatch_id
        return inputs


def _input_value(name: str, value: Any, adapter: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise AdapterE2eDeclarationError(
        adapter,
        f"inputs.{name} is a {type(value).__name__}; a workflow_dispatch input is a string, number or boolean",
    )


def _input_name(value: Any, what: str, adapter: str) -> str:
    if not isinstance(value, str) or not _INPUT_NAME_RE.match(value):
        raise AdapterE2eDeclarationError(adapter, f"{what} {value!r} is not a workflow input name")
    if value == DISPATCH_ID_INPUT:
        raise AdapterE2eDeclarationError(
            adapter, f"{what} {value!r} is the dispatch id input the dispatcher sets itself"
        )
    return value


def parse_e2e(validation: Any, *, adapter: str = "") -> E2eDeclaration | None:
    """The adapter's e2e declaration, None when `validation` declares none, or a typed error."""
    if not isinstance(validation, Mapping) or "e2e" not in validation:
        return None
    raw = validation["e2e"]
    if not isinstance(raw, Mapping):
        raise AdapterE2eDeclarationError(adapter, "it is not a mapping with a workflow")
    unknown = sorted(str(key) for key in raw if key not in _KEYS)
    if unknown:
        raise AdapterE2eDeclarationError(
            adapter, f"unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(_KEYS))})"
        )
    if str(validation.get("ci") or "none") != "github":
        raise AdapterE2eDeclarationError(
            adapter,
            f"it needs ci: github, not {validation.get('ci') or 'none'!r}: only the github gate publishes "
            "the candidate branch the workflow is dispatched on",
        )
    workflow = raw.get("workflow")
    if isinstance(workflow, int) and not isinstance(workflow, bool) and workflow > 0:
        workflow = str(workflow)
    if not isinstance(workflow, str) or not (workflow.isdigit() or _WORKFLOW_FILE_RE.match(workflow)):
        raise AdapterE2eDeclarationError(
            adapter, f"workflow {workflow!r} is neither a workflow file name like e2e.yml nor a workflow id"
        )
    inputs_raw = raw.get("inputs", {})
    if inputs_raw is None:
        inputs_raw = {}
    if not isinstance(inputs_raw, Mapping):
        raise AdapterE2eDeclarationError(adapter, "inputs is not a mapping of input names to values")
    inputs = tuple(
        (_input_name(name, "input", adapter), _input_value(str(name), value, adapter))
        for name, value in inputs_raw.items()
    )
    deadline = raw.get("deadline", DEFAULT_DEADLINE)
    try:
        parse_duration(str(deadline), "deadline")
    except WaitSpecError as exc:
        raise AdapterE2eDeclarationError(adapter, str(exc)) from None
    candidate = raw.get("candidate_input")
    candidate_input = "" if candidate is None else _input_name(candidate, "candidate_input", adapter)
    if candidate_input and candidate_input in dict(inputs):
        raise AdapterE2eDeclarationError(
            adapter, f"candidate_input {candidate_input!r} is also a static input; it takes the candidate SHA"
        )
    return E2eDeclaration(workflow, inputs, str(deadline).strip(), candidate_input)


def declared_e2e(host: Any, project: str) -> E2eDeclaration | None:
    """This project's e2e declaration as its adapter is read now."""
    adapter_fn = getattr(host.catalog, "adapter", None)
    adapter = adapter_fn(project) if callable(adapter_fn) else None
    if not isinstance(adapter, Mapping):
        return None
    return parse_e2e(adapter.get("validation"), adapter=project)


class DispatchRefused(HostError):
    """GitHub answered the dispatch and refused it: no workflow, no `workflow_dispatch`, no access."""


def dispatch_workflow(
    host: Any, repo: str, declaration: E2eDeclaration, *, branch: str, dispatch_id: str, sha: str
) -> None:
    """`POST .../workflows/{workflow}/dispatches` on the candidate branch, with the dispatch id.

    Returns when GitHub accepted it. :class:`DispatchRefused` when GitHub answered with a refusal;
    `GateTransportError` when no answer came back (which says nothing about whether it was accepted),
    and for a rate limit, which is GitHub declining to answer now rather than refusing the workflow.
    """
    args = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/actions/workflows/{declaration.workflow}/dispatches",
        "-f",
        f"ref={branch}",
    ]
    for name, value in declaration.dispatch_inputs(dispatch_id, sha).items():
        args += ["-f", f"inputs[{name}]={value}"]
    completed = _backend_call(host, args, "e2e workflow dispatch")
    if completed.returncode == 0:
        return
    text = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
    status = _HTTP_STATUS_RE.search(text)
    code = (status.group(1) or status.group(2)) if status else ""
    if code == "429" or "rate limit" in text.lower():
        raise GateTransportError(f"e2e workflow dispatch was rate limited: {text}")
    raise DispatchRefused(f"GitHub refused the dispatch of {declaration.workflow} on {branch}: {text}")


def find_run(host: Any, repo: str, workflow: str, *, branch: str, dispatch_id: str) -> dict[str, Any] | None:
    """The workflow's `workflow_dispatch` run on `branch` whose title carries `dispatch_id`, or None.

    `GateTransportError` when GitHub did not answer; `HostError` when it answered with an error.
    """
    path = (
        f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={branch}&per_page=100"
    )
    runs = _gh_api(host, path, jq=_RUNS_JQ)
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        title = f"{run.get('display_title') or ''} {run.get('name') or ''}"
        run_id = run.get("id")
        if dispatch_id in title and isinstance(run_id, int) and run_id > 0:
            return run
    return None


@dataclass(frozen=True)
class RedEvidence:
    """What a red run shows: its failed jobs with their failed steps, and one bounded log fragment."""

    jobs: tuple[tuple[str, tuple[str, ...]], ...]
    fragment: _LogFragment
    note: str = ""


def red_evidence(host: Any, repo: str, run_id: int, run_url: str) -> RedEvidence:
    """The failed jobs and steps of a concluded run, and the gate's `--log-failed` fragment of the first.

    Never raises: evidence that cannot be read degrades to a note, it does not undo the red verdict.
    """
    note = ""
    jobs: list[tuple[str, tuple[str, ...]]] = []
    try:
        listed = _gh_api(host, f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", jq=_JOBS_JQ)
    except HostError as exc:
        listed, note = [], f"the run's jobs could not be read: {_tail(str(exc), 5)}"
    for job in listed if isinstance(listed, list) else []:
        if isinstance(job, dict) and str(job.get("conclusion") or "") in _FAILED_JOB_CONCLUSIONS:
            steps = tuple(str(step) for step in job.get("steps") or [] if str(step))
            jobs.append((str(job.get("name") or "?"), steps))
    first = jobs[0][0] if jobs else ""
    fragment = _failed_log(host, repo, {"name": first, "html_url": run_url})
    return RedEvidence(tuple(jobs), fragment, note)


__all__ = [
    "DEFAULT_DEADLINE",
    "DISPATCH_ID_INPUT",
    "E2E_IDENTIFY_SECONDS",
    "AdapterE2eDeclarationError",
    "DispatchRefused",
    "E2eDeclaration",
    "RedEvidence",
    "declared_e2e",
    "dispatch_workflow",
    "find_run",
    "parse_e2e",
    "red_evidence",
]
