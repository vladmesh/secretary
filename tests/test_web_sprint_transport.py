"""The sprint form, the sprint page and the cross-origin guard, driven as a person drives them.

Every test here goes through :class:`secretary.web.app.WebApp` — the same object the socket handler
calls — over fakes of the sprint layer. There is no live installation, no board and no head
registry, and that is the point twice over: what is being pinned is the *transport*, and the layer
it calls already has its own suite (`tests/test_web_sprint_protocol.py`) proving the rules those
fakes stand in for.

The fakes are not permissive stand-ins. `FakeSprintOps` holds the two properties the transport is
built on top of and would otherwise be able to fake past: a request id owns one sprint, and a
create that fails after the row exists is repeated rather than restarted. A transport that
regenerated its request id per POST, or that answered a partial failure by offering a fresh form,
fails here.
"""

from __future__ import annotations

import json
import re
import unittest
from http.client import HTTPConnection
from threading import Thread
from typing import Any
from urllib.parse import urlencode

from secretary.web.app import ROUTES, WebApp, cross_origin_reason
from secretary.web.server import build_server
from secretary.webproto.errors import (
    OperationPending,
    OwnerConflict,
    ReadError,
    TaskNotFound,
    ValidationRefused,
)
from secretary.webproto.sprint_ops import PENDING_REASON
from secretary.webproto.sprint_requests import SPRINT_CREATE_OPERATION

OBSERVER_PROFILE = "claude-observer"
WORKER_PROFILE = "codex-product-worker"
REVIEWER_PROFILE = "claude-product-reviewer"

#: The profiles the fake registry holds, in the shape `sprint_options` publishes them.
HEADS = [
    {
        "id": OBSERVER_PROFILE,
        "label": "claude · opus · high effort",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "resource": "claude-sub",
        "observer": True,
        "observer_reason": None,
        "role_default_for": ["observer"],
    },
    {
        "id": WORKER_PROFILE,
        "label": "codex · gpt-5.6-terra · default effort",
        "adapter": "codex",
        "model": "gpt-5.6-terra",
        "effort": "default",
        "resource": "openai-sub",
        "observer": False,
        "observer_reason": "this profile is not eligible to observe",
        "role_default_for": [],
    },
    {
        "id": REVIEWER_PROFILE,
        "label": "claude · opus · high effort",
        "adapter": "claude",
        "model": "opus",
        "effort": "high",
        "resource": "claude-sub",
        "observer": True,
        "observer_reason": None,
        "role_default_for": [],
    },
]


def available() -> dict[str, Any]:
    return {"state": "available", "reason": None, "observed_at": "2026-09-06T00:00:00Z"}


def unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason, "observed_at": "2026-09-06T00:00:00Z"}


def options_document(**overrides: Any) -> dict[str, Any]:
    """A `sprint_options` document, as the read layer publishes one."""
    document = {
        "schema_version": 1,
        "kind": "sprint_options",
        "observed_at": "2026-09-06T00:00:00Z",
        "products": {
            "source": available(),
            "items": [
                {"id": "secretary", "label": "Secretary", "ref": "product:1", "projects": ["secretary"]},
                {"id": "codegen", "label": "Codegen", "ref": "product:2", "projects": ["codegen"]},
            ],
        },
        "issues": {
            "source": available(),
            "items": [
                {
                    "ref": "issue:web",
                    "label": "the web needs a sprint form",
                    "product": "secretary",
                    "kind": "feature",
                    "priority": None,
                },
                {
                    "ref": "issue:codegen",
                    "label": "codegen needs a template",
                    "product": "codegen",
                    "kind": None,
                    "priority": None,
                },
            ],
        },
        "projects": {
            "source": available(),
            "items": [
                {"id": "secretary", "label": "secretary", "reserved_by": []},
                {"id": "codegen", "label": "codegen", "reserved_by": ["sprint:7"]},
                {"id": "orca", "label": "orca", "reserved_by": None},
            ],
        },
        "heads": {
            "source": available(),
            "items": [dict(head) for head in HEADS],
            "observer": {"none": "none", "default": OBSERVER_PROFILE},
            "role_defaults": {"observer": OBSERVER_PROFILE},
            "executor_roles": ["worker", "reviewer"],
        },
    }
    document.update(overrides)
    return document


def sprint_document(
    reference: str = "sprint:1",
    *,
    launch: str = "not_started",
    observer: str | None = OBSERVER_PROFILE,
    worker: str | None = None,
    reviewer: str | None = None,
    record: dict[str, Any] | None = None,
    launch_source: dict[str, Any] | None = None,
    reason: str = "the sprint is saved and the production tick holds no observer for it yet",
    resume: dict[str, Any] | None = None,
    current_task: str | None = None,
) -> dict[str, Any]:
    """A `sprint_state` document, as the read layer publishes one."""

    def pin(profile: str | None) -> dict[str, Any]:
        return {"state": "pinned", "profile": profile} if profile else {"state": "unset"}

    declared = (
        {"state": "declared", "value": {"kind": "head", "profile": observer}, "profile": observer}
        if observer
        else {"state": "declared", "value": {"kind": "none"}, "profile": None}
    )
    return {
        "schema_version": 1,
        "kind": "sprint",
        "observed_at": "2026-09-06T00:00:00Z",
        "ref": reference,
        "sprint": {
            "source": available(),
            "value": {
                "ref": reference,
                "goal": "Make the web a client of the sprint contract",
                "definition_of_done": "the form and the page exist and are tested",
                "status": "open",
                "product": "secretary",
                "issues": ["issue:web"],
                "reservations": ["secretary"],
                "repositories": [],
                "current_task": current_task,
                "executors": {"worker": pin(worker), "reviewer": pin(reviewer)},
                "resume": resume,
                "budget": None,
                "audit": None,
            },
        },
        "observer": {
            "declared": declared,
            "launch": {
                "source": launch_source or available(),
                "state": launch,
                "reason": reason,
                "record": record,
            },
        },
    }


class FakeSprintReads:
    """The two reads, answering documents a test handed it — or refusing the way the layer does."""

    def __init__(self, options: Any = None, states: dict[str, Any] | None = None) -> None:
        self.options = options if options is not None else options_document()
        self.states = states or {}
        self.options_refusal: ReadError | None = None
        self.reads: list[str] = []

    def sprint_options(self) -> dict[str, Any]:
        self.reads.append("sprint_options")
        if self.options_refusal is not None:
            raise self.options_refusal
        return self.options

    def sprint_state(self, ref: str) -> dict[str, Any]:
        self.reads.append(f"sprint_state:{ref}")
        if ref in self.states:
            return self.states[ref]
        raise TaskNotFound(f"the board holds no sprint {ref!r}")


class FakeSprintOps:
    """`sprint_create`, with the layer's idempotency and the layer's refusals, and no board.

    Three behaviours are modelled because the transport is built on them and a laxer fake would let
    a defect through:

    * a request id owns one sprint — a repeat answers from the record and creates nothing;
    * a repeat naming other inputs is a `validation` refusal rather than somebody else's sprint;
    * `pending_once` is the partial failure: the row is written, the request does not finish, and
      the *same* request id repeated afterwards picks that sprint up.

    Everything about what a sprint may be stays a judgement of the writer, so the only rule copied
    here is the one criterion 3 is about: an observer that is not a profile of the registry is
    refused, exactly as `check_observer_profile` refuses it below this layer.
    """

    def __init__(self, reads: FakeSprintReads) -> None:
        self.reads = reads
        self.calls: list[dict[str, Any]] = []
        self.by_request: dict[str, tuple[str, str]] = {}
        self.created: list[str] = []
        self.pending_once = False
        self.refusal: ReadError | None = None

    def sprint_create(
        self,
        *,
        request_id: str,
        actor: str,
        product: str,
        goal: str,
        issues: list[str] | None = None,
        projects: list[str] | None = None,
        observer: str = "",
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        role: str = "po",
        reference: str = "",
    ) -> dict[str, Any]:
        call = {
            "request_id": request_id,
            "actor": actor,
            "role": role,
            "product": product,
            "goal": goal,
            "definition_of_done": definition_of_done,
            "issues": list(issues or []),
            "projects": list(projects or []),
            "observer": observer,
            "worker": worker,
            "reviewer": reviewer,
        }
        self.calls.append(call)
        if self.refusal is not None:
            raise self.refusal
        fingerprint = json.dumps(call, sort_keys=True)
        recorded = self.by_request.get(request_id)
        if recorded is not None:
            if recorded[1] != fingerprint:
                raise ValidationRefused(
                    f"request {request_id!r} already names another {SPRINT_CREATE_OPERATION}"
                )
            return self._document(recorded[0], request_id=request_id, created=False)
        known = {head["id"] for head in HEADS if head["observer"]}
        if observer != "none" and observer not in known:
            raise ValidationRefused(
                f"{observer!r} is not a head profile of this installation's registry"
            )
        for pinned in (worker, reviewer):
            if pinned is not None and pinned not in {head["id"] for head in HEADS}:
                raise ValidationRefused(f"{pinned!r} is not a head profile of this installation")
        made = f"sprint:{len(self.created) + 1}"
        self.created.append(made)
        self.reads.states[made] = sprint_document(
            made, observer=None if observer == "none" else observer, worker=worker, reviewer=reviewer
        )
        self.by_request[request_id] = (made, fingerprint)
        if self.pending_once:
            self.pending_once = False
            raise OperationPending(
                f"sprint {made} was created and the request that made it did not finish; repeat the "
                "same request id to pick it up rather than opening a second sprint.",
                data={
                    "reason": PENDING_REASON,
                    "action": {
                        "operation": SPRINT_CREATE_OPERATION,
                        "repeat_request": True,
                        "request_id": request_id,
                        "reference": made,
                    },
                },
            )
        return self._document(made, request_id=request_id, created=True)

    def _document(self, reference: str, *, request_id: str, created: bool) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "sprint_created",
            "observed_at": "2026-09-06T00:00:00Z",
            "request_id": request_id,
            "created": created,
            "sprint": self.reads.sprint_state(reference),
        }


class RecordingOps:
    """The product-run half, recording what was called on it and nothing else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def record(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return {"ok": True, "run": {"run_id": "pr-1"}}

        return record


class SprintTransportFixture(unittest.TestCase):
    """One application over the two fakes, driven the way a browser drives it."""

    def setUp(self) -> None:
        self.sprint_reads = FakeSprintReads()
        self.sprint_ops = FakeSprintOps(self.sprint_reads)
        self.ops = RecordingOps()
        self.app = WebApp(RecordingOps(), self.ops, self.sprint_reads, self.sprint_ops)

    # -- driving it --------------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any):
        return self.app.handle("GET", path, **kwargs)

    def submit(self, fields: Any, *, headers: Any = None, path: str = "/sprints"):
        """One form submission, encoded exactly as a browser encodes one."""
        pairs = fields.items() if isinstance(fields, dict) else fields
        flat: list[tuple[str, str]] = []
        for name, value in pairs:
            if isinstance(value, (list, tuple)):
                flat += [(name, one) for one in value]
            else:
                flat.append((name, value))
        return self.app.handle(
            "POST", path, body=urlencode(flat).encode("utf-8"), headers=headers
        )

    def text_of(self, response) -> str:
        return response.body.decode("utf-8")

    def form(self) -> str:
        response = self.get("/sprints/new")
        self.assertEqual(response.status, 200)
        return self.text_of(response)

    def request_id_of(self, markup: str) -> str:
        found = re.search(r'name="request_id" value="([^"]+)"', markup)
        self.assertIsNotNone(found, "the form carries no request id")
        assert found is not None
        return found.group(1)

    def valid(self, **overrides: Any) -> dict[str, Any]:
        fields = {
            "request_id": self.request_id_of(self.form()),
            "product": "secretary",
            "goal": "Make the web a client of the sprint contract",
            "definition_of_done": "the form and the page exist and are tested",
            "issues": ["issue:web"],
            "projects": ["secretary"],
            "observer": OBSERVER_PROFILE,
            "worker": "",
            "reviewer": "",
        }
        fields.update(overrides)
        return fields


# -- criterion 1: the routes ----------------------------------------------------------------------


class SprintRouteTests(SprintTransportFixture):
    def test_the_three_routes_are_in_the_table_naming_their_operation(self) -> None:
        table = {(route.method, route.pattern): route.operation for route in ROUTES}
        self.assertEqual(table[("GET", "/sprints/new")], "sprint_reads.sprint_options")
        self.assertEqual(table[("POST", "/sprints")], "sprint_ops.sprint_create")
        self.assertEqual(table[("GET", "/sprints/{ref}")], "sprint_reads.sprint_state")

    def test_the_form_route_is_not_read_as_a_sprint_named_new(self) -> None:
        """A literal route wins over a placeholder, so `/sprints/new` is the form and not a read."""
        self.assertEqual(self.get("/sprints/new").status, 200)
        self.assertEqual(self.sprint_reads.reads, ["sprint_options"])

    def test_an_unrouted_sprint_path_is_404_and_reaches_no_layer(self) -> None:
        for path in ("/sprints/new/again", "/sprints/sprint:1/start", "/api/sprints"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status, 404)
        self.assertEqual(self.sprint_reads.reads, [])
        self.assertEqual(self.sprint_ops.calls, [])

    def test_an_unrouted_method_on_a_sprint_path_is_405(self) -> None:
        self.assertEqual(self.get("/sprints").status, 405)
        self.assertEqual(self.submit({}, path="/sprints/new").status, 405)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_a_form_field_this_route_does_not_take_is_refused(self) -> None:
        response = self.submit(self.valid(role="steward"))
        self.assertEqual(response.status, 400)
        self.assertIn("role", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])


# -- criterion 2: the form is the installation's own catalogue -------------------------------------


class SprintFormTests(SprintTransportFixture):
    def test_the_form_offers_the_products_issues_projects_and_profiles_the_layer_answered(self) -> None:
        markup = self.form()
        for expected in (
            'value="secretary"',
            'value="codegen"',
            "issue:web",
            "the web needs a sprint form",
            "issue:codegen",
            'value="orca"',
            OBSERVER_PROFILE,
            "opus",
            "high",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, markup)

    def test_a_different_catalogue_is_a_different_form_and_nothing_is_written_into_the_page(self) -> None:
        """Criterion 2's real claim: the list is the layer's answer, not a list in this product."""
        other = options_document()
        other["products"]["items"] = [{"id": "orca", "label": "Orca", "ref": "product:9", "projects": []}]
        other["issues"]["items"] = [
            {"ref": "issue:orca", "label": "orca needs a bridge", "product": "orca", "kind": None, "priority": None}
        ]
        other["heads"]["items"] = [
            {
                "id": "hermes-observer",
                "label": "hermes · sonnet · low effort",
                "adapter": "hermes",
                "model": "sonnet",
                "effort": "low",
                "resource": "r",
                "observer": True,
                "observer_reason": None,
                "role_default_for": [],
            }
        ]
        self.sprint_reads.options = other
        markup = self.form()
        self.assertIn("issue:orca", markup)
        self.assertIn("hermes-observer", markup)
        self.assertNotIn("issue:web", markup)
        self.assertNotIn(OBSERVER_PROFILE, markup)

    def test_no_field_asks_a_person_to_type_a_profile_identifier(self) -> None:
        """Criterion 2 and 3: every head is chosen from the registry, never typed from memory."""
        markup = self.form()
        for role in ("observer", "worker", "reviewer"):
            with self.subTest(role=role):
                self.assertIn(f'<select id="{role}" name="{role}">', markup)
                self.assertNotIn(f'<input id="{role}"', markup)

    def test_a_project_an_open_sprint_holds_says_so_and_one_nobody_could_check_says_that(self) -> None:
        markup = self.form()
        self.assertIn("an open sprint already holds it: sprint:7", markup)
        self.assertIn("could not be established", markup)

    def test_an_unreadable_catalogue_section_marks_that_section_and_not_the_others(self) -> None:
        options = options_document()
        options["heads"] = {
            "source": unavailable("the head registry could not be read: no such file"),
            "items": [],
            "observer": {"none": "none", "default": None},
            "role_defaults": {},
            "executor_roles": ["worker", "reviewer"],
        }
        self.sprint_reads.options = options
        markup = self.form()
        self.assertIn("could not find out which head profiles this installation has", markup)
        self.assertIn("no such file", markup)
        self.assertIn("issue:web", markup)

    def test_a_catalogue_that_cannot_be_read_at_all_is_the_layers_status_and_not_an_empty_form(self) -> None:
        self.sprint_reads.options_refusal = TaskNotFound("this instance config does not validate")
        response = self.get("/sprints/new")
        self.assertEqual(response.status, 404)
        self.assertIn("this instance config does not validate", self.text_of(response))


# -- criterion 3: the observer ---------------------------------------------------------------------


class ObserverFieldTests(SprintTransportFixture):
    def test_each_observer_choice_shows_its_name_model_and_effort(self) -> None:
        markup = self.form()
        observer = markup.split('<select id="observer"')[1].split("</select>")[0]
        self.assertIn("claude · opus · high effort", observer)
        self.assertIn("model opus", observer)
        self.assertIn("effort high", observer)
        self.assertIn(OBSERVER_PROFILE, observer)

    def test_only_the_profiles_the_layer_calls_eligible_are_offered_as_observers(self) -> None:
        observer = self.form().split('<select id="observer"')[1].split("</select>")[0]
        self.assertIn(OBSERVER_PROFILE, observer)
        self.assertNotIn(WORKER_PROFILE, observer)

    def test_an_observer_nobody_chose_is_refused_by_name_and_opens_nothing(self) -> None:
        response = self.submit(self.valid(observer=""))
        self.assertEqual(response.status, 400)
        self.assertIn("observer", self.text_of(response))
        self.assertIn("choose the observer head", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])

    def test_a_profile_the_registry_does_not_hold_is_refused_by_the_server(self) -> None:
        """Criterion 3: refused, and never quietly replaced with a profile nobody chose."""
        response = self.submit(self.valid(observer="a-profile-that-was-deleted"))
        self.assertEqual(response.status, 400)
        markup = self.text_of(response)
        self.assertIn("a-profile-that-was-deleted", markup)
        self.assertIn("is not a head profile", markup)
        self.assertEqual(self.sprint_ops.created, [])
        # And what reached the layer was the profile that was submitted, not another one.
        self.assertEqual(self.sprint_ops.calls[-1]["observer"], "a-profile-that-was-deleted")


# -- criterion 4: the two optional pins -------------------------------------------------------------


class ExecutorPinTests(SprintTransportFixture):
    def test_both_roles_default_to_the_observer_choosing(self) -> None:
        markup = self.form()
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                select = markup.split(f'<select id="{role}"')[1].split("</select>")[0]
                self.assertIn('<option value="" selected>the observer chooses</option>', select)

    def test_the_observer_chooses_reaches_the_layer_as_nothing_said(self) -> None:
        self.assertEqual(self.submit(self.valid()).status, 303)
        call = self.sprint_ops.calls[-1]
        self.assertIsNone(call["worker"])
        self.assertIsNone(call["reviewer"])

    def test_a_chosen_profile_reaches_the_layer_as_that_profile(self) -> None:
        self.assertEqual(
            self.submit(self.valid(worker=WORKER_PROFILE, reviewer=REVIEWER_PROFILE)).status, 303
        )
        call = self.sprint_ops.calls[-1]
        self.assertEqual(call["worker"], WORKER_PROFILE)
        self.assertEqual(call["reviewer"], REVIEWER_PROFILE)

    def test_the_two_roles_are_independent(self) -> None:
        self.assertEqual(self.submit(self.valid(worker=WORKER_PROFILE)).status, 303)
        call = self.sprint_ops.calls[-1]
        self.assertEqual(call["worker"], WORKER_PROFILE)
        self.assertIsNone(call["reviewer"])

    def test_an_empty_string_never_travels_down_as_a_profile_name(self) -> None:
        """The one spelling the layer refuses rather than folds into absence. Criterion 4."""
        self.submit(self.valid())
        for call in self.sprint_ops.calls:
            self.assertNotEqual(call["worker"], "")
            self.assertNotEqual(call["reviewer"], "")


# -- criterion 5: what is missing, and what was typed ------------------------------------------------


class FormRefusalTests(SprintTransportFixture):
    def test_every_required_field_is_refused_by_name_and_by_what_is_wrong(self) -> None:
        for field, expected in (
            ("goal", "say what this sprint is for"),
            ("definition_of_done", "say what would make this sprint done"),
            ("observer", "choose the observer head"),
            ("issues", "choose at least one open issue"),
            ("projects", "choose at least one registered project"),
            ("product", "choose the product"),
        ):
            with self.subTest(field=field):
                response = self.submit(self.valid(**{field: [] if field in ("issues", "projects") else ""}))
                self.assertEqual(response.status, 400)
                markup = self.text_of(response)
                self.assertIn(field.replace("_", " "), markup)
                self.assertIn(expected, markup)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_nothing_a_person_typed_is_lost_when_the_form_comes_back(self) -> None:
        fields = self.valid(
            goal="A goal nobody should have to type twice",
            definition_of_done="A definition of done that is equally hard to retype",
            observer="",
            issues=["issue:web", "issue:codegen"],
            projects=["secretary", "orca"],
            worker=WORKER_PROFILE,
        )
        markup = self.text_of(self.submit(fields))
        self.assertIn("A goal nobody should have to type twice", markup)
        self.assertIn("A definition of done that is equally hard to retype", markup)
        self.assertIn('value="secretary" selected', markup)
        for checked in (
            'name="issues" value="issue:web" checked',
            'name="issues" value="issue:codegen" checked',
            'name="projects" value="secretary" checked',
            'name="projects" value="orca" checked',
        ):
            with self.subTest(checked=checked):
                self.assertIn(checked, markup)
        self.assertIn(f'value="{WORKER_PROFILE}" selected', markup)
        # And it comes back under the same request id, so fixing a field is still one sprint.
        self.assertEqual(self.request_id_of(markup), fields["request_id"])

    def test_a_refusal_the_layer_made_is_shown_on_the_form_with_the_values_still_in_it(self) -> None:
        self.sprint_ops.refusal = OwnerConflict("an open sprint already reserves secretary")
        fields = self.valid(goal="A goal that survives a conflict")
        response = self.submit(fields)
        self.assertEqual(response.status, 409)
        markup = self.text_of(response)
        self.assertIn("an open sprint already reserves secretary", markup)
        self.assertIn("A goal that survives a conflict", markup)
        self.assertEqual(self.request_id_of(markup), fields["request_id"])

    def test_a_refusal_shown_over_an_unreadable_catalogue_is_still_the_refusal(self) -> None:
        self.sprint_ops.refusal = OwnerConflict("an open sprint already reserves secretary")
        fields = self.valid()
        self.sprint_reads.options_refusal = TaskNotFound("the board could not be read")
        response = self.submit(fields)
        self.assertEqual(response.status, 409)
        markup = self.text_of(response)
        self.assertIn("an open sprint already reserves secretary", markup)
        self.assertIn("the board could not be read", markup)


# -- criteria 6: the sprint page ---------------------------------------------------------------------


class SprintPageTests(SprintTransportFixture):
    def page(self, document: dict[str, Any], reference: str = "sprint:1") -> str:
        self.sprint_reads.states[reference] = document
        response = self.get(f"/sprints/{reference}")
        self.assertEqual(response.status, 200)
        return self.text_of(response)

    def test_the_page_shows_what_the_sprint_was_opened_with(self) -> None:
        markup = self.page(
            sprint_document(
                worker=WORKER_PROFILE,
                current_task="secretary-1570",
                resume={"selected_step": "the transport", "next_safe_step": "write the tests"},
            )
        )
        for expected in (
            "Make the web a client of the sprint contract",
            "the form and the page exist and are tested",
            "secretary",
            "issue:web",
            OBSERVER_PROFILE,
            f"pinned to {WORKER_PROFILE}",
            "the observer chooses",
            "secretary-1570",
            "the transport",
            "write the tests",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, markup)

    def test_the_three_launch_states_are_told_apart_in_words(self) -> None:
        """Criterion 6: the words differ, so the page reads the same without colour."""
        saved = self.page(sprint_document(launch="not_started"))
        running = self.page(
            sprint_document(
                launch="running",
                reason=f"an observer head is up on {OBSERVER_PROFILE}",
                record={"head": OBSERVER_PROFILE, "alive": True, "heartbeat_state": "fresh"},
            )
        )
        blind = self.page(
            sprint_document(
                launch="unavailable",
                launch_source=unavailable("the dispatcher production state could not be read"),
                reason="the dispatcher production state could not be read",
            )
        )
        self.assertIn("saved — no observer is up for it yet", saved)
        self.assertIn("running — an observer head is up", running)
        self.assertIn("not established — this could not be read at all", blind)
        for one, others in ((saved, (running, blind)), (running, (saved, blind)), (blind, (saved, running))):
            said = one.split('class="launch')[1].split("</span>")[0]
            for other in others:
                self.assertNotIn(said, other)

    def test_a_sprint_that_declared_no_observer_does_not_read_as_one_waiting_for_one(self) -> None:
        markup = self.page(
            sprint_document(
                observer=None,
                launch="not_declared",
                reason="this sprint declares no observer, so the tick raises none for it",
            )
        )
        self.assertIn("no observer — this sprint declared none", markup)
        self.assertNotIn("saved — no observer is up for it yet", markup)

    def test_a_stopped_observer_is_neither_running_nor_never_started(self) -> None:
        markup = self.page(
            sprint_document(
                launch="stopped",
                reason="the dispatcher holds an observer record whose head is not alive",
                record={"head": OBSERVER_PROFILE, "alive": False, "heartbeat_state": "stale"},
            )
        )
        self.assertIn("stopped — an observer was raised for it and is not alive", markup)
        self.assertIn("stale", markup)

    def test_a_sprint_the_board_does_not_hold_is_a_404_page(self) -> None:
        response = self.get("/sprints/sprint:404")
        self.assertEqual(response.status, 404)
        self.assertIn("text/html", response.content_type)

    def test_the_page_escapes_what_the_layer_gave_it(self) -> None:
        document = sprint_document()
        document["sprint"]["value"]["goal"] = "<script>alert(1)</script>"
        markup = self.page(document)
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)


# -- criterion 7: one submission is one sprint --------------------------------------------------------


class IdempotentSubmissionTests(SprintTransportFixture):
    def test_a_valid_submission_opens_one_sprint_and_lands_on_its_page(self) -> None:
        response = self.submit(self.valid())
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])
        page = self.get("/sprints/sprint:1")
        self.assertEqual(page.status, 200)
        self.assertIn("Make the web a client of the sprint contract", self.text_of(page))

    def test_the_same_submission_twice_is_one_sprint(self) -> None:
        """A double click, a retry and a reconnected client are all this: the same form, again."""
        fields = self.valid()
        first = self.submit(fields)
        second = self.submit(fields)
        self.assertEqual(first.status, 303)
        self.assertEqual(second.status, 303)
        self.assertEqual(first.headers["Location"], second.headers["Location"])
        # Both submissions reached the layer -- the transport swallows neither -- and the layer
        # answered the second from the request it already owns.
        self.assertEqual(len(self.sprint_ops.calls), 2)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_the_request_id_is_the_forms_and_is_not_minted_per_post(self) -> None:
        markup = self.form()
        submitted = self.request_id_of(markup)
        self.submit(self.valid(request_id=submitted))
        self.submit(self.valid(request_id=submitted))
        self.assertEqual({call["request_id"] for call in self.sprint_ops.calls}, {submitted})
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_two_forms_opened_separately_are_two_intentions(self) -> None:
        self.assertNotEqual(self.request_id_of(self.form()), self.request_id_of(self.form()))

    def test_a_submission_with_no_request_id_is_refused_rather_than_given_a_new_one(self) -> None:
        response = self.submit(self.valid(request_id=""))
        self.assertEqual(response.status, 400)
        self.assertIn("request id", self.text_of(response))
        self.assertEqual(self.sprint_ops.calls, [])

    def test_after_a_partial_failure_the_same_submission_reaches_the_sprint_that_exists(self) -> None:
        """Criterion 7's other half: the repair is a repeat, and the page says so in those words."""
        self.sprint_ops.pending_once = True
        fields = self.valid()
        refused = self.submit(fields)
        self.assertEqual(refused.status, 503)
        markup = self.text_of(refused)
        self.assertIn("this sprint exists and the request that opened it did not finish", markup)
        self.assertIn("Submitting this form again is safe", markup)
        self.assertIn("sprint:1", markup)
        self.assertEqual(self.request_id_of(markup), fields["request_id"])

        again = self.submit(fields)
        self.assertEqual(again.status, 303)
        self.assertEqual(again.headers["Location"], "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])


# -- criterion 8: a mutation comes from this service's own pages ---------------------------------------


class CrossOriginTests(SprintTransportFixture):
    HOST = "secretary.example"

    def mutations(self) -> list[tuple[str, bytes, str]]:
        """Every mutating route, with a body it would otherwise be answered on."""
        return [
            (
                "/api/runs/start",
                json.dumps(
                    {"ref": "secretary-1", "request_id": "r", "profile": WORKER_PROFILE}
                ).encode("utf-8"),
                "run_start",
            ),
            (
                "/api/runs/review",
                json.dumps(
                    {"request_id": "r", "profile": REVIEWER_PROFILE, "worker_run_id": "pr-1"}
                ).encode("utf-8"),
                "run_review",
            ),
            ("/sprints", urlencode(list(self.valid().items()), doseq=True).encode("utf-8"), ""),
        ]

    def post(self, path: str, body: bytes, headers: dict[str, str]):
        return self.app.handle("POST", path, body=body, headers=headers)

    def test_a_browser_posting_from_another_origin_is_refused_on_every_mutating_route(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(
                    path,
                    body,
                    {"Origin": "https://attacker.example", "Host": self.HOST},
                )
                self.assertEqual(response.status, 403)
                self.assertIn("this service did not serve", self.text_of(response))
        self.assertEqual(self.ops.calls, [])
        self.assertEqual(self.sprint_ops.calls, [])
        self.assertEqual(self.sprint_ops.created, [])

    def test_a_null_origin_is_refused_too(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(path, body, {"Origin": "null", "Host": self.HOST})
                self.assertEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.calls, [])

    def test_the_services_own_pages_still_post(self) -> None:
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(
                    path, body, {"Origin": f"https://{self.HOST}", "Host": self.HOST}
                )
                self.assertNotEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_the_scheme_is_not_compared_because_the_front_terminates_tls(self) -> None:
        """A real request through the published front arrives as https origin over plain http."""
        self.assertIsNone(
            cross_origin_reason({"Origin": "https://secretary.example", "Host": "secretary.example"})
        )
        self.assertIsNone(
            cross_origin_reason({"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1:8787"})
        )

    def test_a_port_that_differs_is_another_origin(self) -> None:
        self.assertIsNotNone(
            cross_origin_reason({"Origin": "http://127.0.0.1:9999", "Host": "127.0.0.1:8787"})
        )

    def test_a_client_that_sends_no_origin_is_not_a_browser_and_keeps_working(self) -> None:
        """`secretary web-run`, curl and the OPERATIONS.md diagnostics send none. Criterion 8."""
        for path, body, _operation in self.mutations():
            with self.subTest(path=path):
                response = self.post(path, body, {"Host": self.HOST})
                self.assertNotEqual(response.status, 403)
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])
        self.assertIsNone(cross_origin_reason(None))
        self.assertIsNone(cross_origin_reason({}))

    def test_the_check_sees_the_headers_a_real_request_arrives_with(self) -> None:
        """One real socket, because the header path between `http.server` and the app is the point.

        Everything else here calls `WebApp.handle` directly, which is exactly where a check that is
        never handed the headers would still look correct. So this one goes through the server.
        """
        server = build_server(self.app, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[0], server.server_address[1]
        connection = HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        body = urlencode(list(self.valid().items()), doseq=True)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        connection.request("POST", "/sprints", body=body, headers={**headers, "Origin": "https://attacker.example"})
        refused = connection.getresponse()
        refused.read()
        self.assertEqual(refused.status, 403)
        self.assertEqual(self.sprint_ops.created, [])

        connection.request("POST", "/sprints", body=body, headers={**headers, "Origin": f"http://{host}:{port}"})
        opened = connection.getresponse()
        opened.read()
        self.assertEqual(opened.status, 303)
        self.assertEqual(opened.getheader("Location"), "/sprints/sprint%3A1")
        self.assertEqual(self.sprint_ops.created, ["sprint:1"])

    def test_a_read_is_not_asked_about_its_origin(self) -> None:
        """The check is on mutations. A GET from anywhere is still only a read of this service."""
        response = self.app.handle(
            "GET", "/sprints/new", headers={"Origin": "https://attacker.example", "Host": self.HOST}
        )
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
