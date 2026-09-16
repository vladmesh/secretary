from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPRINTS = ROOT / "src/secretary/sprints.py"
PYPROJECT = ROOT / "pyproject.toml"
SHARDS = ROOT / "tests/ci-shards.txt"
TEST = ROOT / "tests/test_sprint_write.py"


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one occurrence, found {count}: {old[:100]!r}")
    return text.replace(old, new, 1)


def replace_region(text: str, start: str, end: str, replacement: str) -> str:
    first = text.find(start)
    if first < 0:
        raise RuntimeError(f"start marker not found: {start!r}")
    second = text.find(end, first + len(start))
    if second < 0:
        raise RuntimeError(f"end marker not found: {end!r}")
    return text[:first] + replacement + text[second:]


text = SPRINTS.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from secretary.board.models import SprintState\nfrom secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex\n",
    "from secretary.board.models import SprintState\n"
    "from secretary.board.roles import Role\n"
    "from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex\n"
    "from secretary.board.sprint_write import (\n"
    "    SprintCreateIntent,\n"
    "    SprintMutationReceipt,\n"
    "    SprintReopenIntent,\n"
    "    SprintWriteSnapshot,\n"
    ")\n",
)

text = replace_region(
    text,
    "def update_active_sprint_projects(data_dir: str | Path, sprint: dict[str, Any]) -> None:\n",
    "\n\n@contextmanager\ndef _sprint_guard_index_lock",
    '''def update_active_sprint_projects(
    data_dir: str | Path, sprint: SprintAdmission | dict[str, Any]
) -> None:
    """Update one sprint's entries in the local reserved-project index."""
    with _sprint_guard_index_lock(data_dir):
        path = Path(data_dir) / _GUARD_INDEX
        index = _read_guard_index(path)
        if index is None and path.exists():
            # Rebuild stale index key spaces from the board.
            path.unlink()
            return
        admission = sprint if isinstance(sprint, SprintAdmission) else SprintAdmission.from_document(sprint)
        index = (index or SprintReservationIndex()).without_sprint(admission.ref)
        if admission.is_open:
            index = index.with_sprint(admission)
        _write_guard_index(data_dir, index)
''',
)

text = replace_region(
    text,
    "    def _create_under_admission(self, request_id: str, intent: dict[str, Any]) -> dict[str, Any]:\n",
    "\n\n    @_sql_atomic\n    def restore_create",
    '''    def _create_under_admission(self, request_id: str, intent: SprintCreateIntent) -> dict[str, Any]:
        intent_document = intent.to_document()
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_CREATED, intent=intent_document
        )
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            self._check_ownership(intent.product, list(intent.issues), list(intent.reservations))
            self._check_conflicts(intent.admission(), excluding="")
            document, committed = self._begin_create(request_id, intent)
            if committed is not None:
                return self._committed_result(SPRINT_CREATED, committed)
        return self._run_create(document, admitted=True)
''',
)

text = replace_once(
    text,
    '''        document, committed = self.transactions.existing(request_id, kind=SPRINT_CREATED, intent=intent)
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            document, committed = self._begin_create(request_id, intent)
''',
    '''        intent_document = intent.to_document()
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_CREATED, intent=intent_document
        )
        if committed is not None:
            return self._committed_result(SPRINT_CREATED, committed)
        if document is None:
            document, committed = self._begin_create(request_id, intent)
''',
)

text = replace_region(
    text,
    "    def _create_intent(\n",
    "\n    def _observer_intent(\n",
    '''    def _create_intent(
        self,
        *,
        role: str,
        actor: str,
        goal: str,
        definition_of_done: str,
        repositories: list[str],
        product: str,
        issues: list[str],
        reservations: list[str],
        reference: str,
        require_goal: bool = True,
        observer: dict[str, Any] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        status: str = "open",
        require_executable_observer: bool = True,
        canonical_repositories: bool = True,
    ) -> SprintCreateIntent:
        """The normalized request, which is both the replay key and the repair recipe.

        A repeat of the same request id carrying a different intent is another operation and is refused
        before any side effect. The observer is part of the staged intent for the same reason the
        reservations are: a repair has to write the value the caller chose, not one it picks up later.

        Repository roots are canonicalized here, where the operator declaring them is, so the absolute
        root is what the row persists and a root this host cannot resolve is refused before any board
        row, staged intent, metadata or audit event exists. Recovery canonicalizes nothing.
        """
        goal = goal.strip()
        reference = reference.strip()
        if require_goal and not goal:
            raise TaskError("validation", "create requires a non-empty goal", 2)
        if reference and not reference.startswith(SPRINT_REFERENCE_PREFIX):
            raise TaskError("validation", f"sprint reference must start with {SPRINT_REFERENCE_PREFIX}", 2)
        if reference:
            try:
                sprint_reference_number(reference)
            except BoardBackendError as exc:
                raise TaskError("validation", str(exc), 2) from None
        try:
            state = SprintState(status)
        except ValueError:
            raise TaskError("validation", f"unknown sprint status {status!r}", 2) from None
        pins = self._executor_intent(worker=worker, reviewer=reviewer)
        return SprintCreateIntent(
            role=Role(role),
            actor=actor,
            goal=goal,
            definition_of_done=definition_of_done,
            repositories=tuple(
                canonical_repository_roots(repositories)
                if canonical_repositories
                else _unique_strings(repositories)
            ),
            product=product.strip(),
            issues=tuple(_unique_strings(issues)),
            reservations=tuple(_unique_strings(reservations)),
            reference=reference,
            state=state,
            observer=self._observer_intent(observer, executable=require_executable_observer),
            worker=pins["worker"],
            reviewer=pins["reviewer"],
        )
''',
)

text = replace_region(
    text,
    "    def _begin_create(\n",
    "\n    def _run_create(",
    '''    def _begin_create(
        self, request_id: str, intent: SprintCreateIntent
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Claim the request id after the mutable preconditions have passed."""
        reference = intent.reference
        if reference:
            board_id = _sprint_board(self.client, create=False)
            if board_id is not None and self.client.call(
                "getTaskByReference", project_id=board_id, reference=reference
            ):
                raise TaskError("validation", "sprint reference already exists", 2)
            try:
                TaskReader(self.client).show(reference)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
            else:
                raise TaskError("validation", "sprint reference already belongs to a Pipeline card", 2)
        intent_document = intent.to_document()
        event = self._event(
            SPRINT_CREATED,
            intent.role.value,
            intent.actor,
            reference,
            request_id,
            {"intent": intent_document},
        )
        document, committed = self.transactions.begin(
            request_id, kind=SPRINT_CREATED, intent=intent_document, event=event
        )
        if document is None and committed is None:
            raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
        return document, committed  # type: ignore[return-value]
''',
)

text = replace_region(
    text,
    "    def _run_create(self, document: dict[str, Any], *, admitted: bool) -> dict[str, Any]:\n",
    "\n    def _compensate_create(",
    '''    def _run_create(self, document: dict[str, Any], *, admitted: bool) -> dict[str, Any]:
        """Drive the staged create to its single audit event, or leave it repairable."""
        try:
            reference = self._finish_create(document, admitted=admitted)
            sprint_document = self.reader.show(reference)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            event = document["event"]
            event["task_id"] = sprint.entity_id
            event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint.admission())
            return SprintMutationReceipt(SPRINT_CREATED, str(event["event_id"])).to_document(sprint_document)
        except TaskError as exc:
            if getattr(self.client, "backend_kind", "kanboard") == "postgres":
                raise
            answer = exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            )
            # Refuse only after compensation proves the request holds no row.
            clean = self._compensate_create(document)
            if answer and clean:
                self.transactions.discard(document)
                raise
            raise TaskError(
                "audit_pending",
                "sprint create is pending repair; retry with the same request id",
                4,
            ) from None
        except (OSError, KeyError, TypeError):
            self._compensate_create(document)
            raise TaskError(
                "audit_pending",
                "sprint create is pending repair; retry with the same request id",
                4,
            ) from None
''',
)

text = replace_once(
    text,
    '        intent = document["intent"]\n        event = document["event"]\n',
    '        intent = SprintCreateIntent.from_document(document["intent"])\n        event = document["event"]\n',
)
text = replace_once(
    text,
    '            self._check_conflicts(intent, excluding_id=staged_id)\n',
    '            self._check_conflicts(intent.admission(reference=created_ref), excluding_id=staged_id)\n',
)
text = replace_once(
    text,
    '        recorded = str(document["intent"].get("reference") or document.get("reference") or "")\n',
    '        intent = SprintCreateIntent.from_document(document["intent"])\n'
    '        recorded = intent.reference or str(document.get("reference") or "")\n',
)
text = replace_once(
    text,
    '                title=str(document["intent"]["goal"]),\n',
    '                title=SprintCreateIntent.from_document(document["intent"]).goal,\n',
)

text = replace_region(
    text,
    "    def _create_values(self, intent: dict[str, Any]) -> dict[str, str]:\n",
    "\n    def _ensure_metadata(\n",
    '''    def _create_values(self, intent: SprintCreateIntent) -> dict[str, str]:
        values = {
            "sprint_goal": intent.goal,
            "sprint_definition_of_done": intent.definition_of_done,
            "sprint_repositories": json.dumps(list(intent.repositories), separators=(",", ":")),
            "sprint_status": intent.state.value,
            "sprint_budget": _budget_json(SprintBudget.from_legacy(thresholds=self.thresholds).to_document()),
            "sprint_current_task": "",
            "sprint_resume": "",
        }
        # Written with the fields, which is before the reference publishes the row: a sprint is
        # never readable open without the observer it was opened with. A restored row that
        # carried no observer at all keeps carrying none, and the strict reader refuses it.
        if intent.observer is not None:
            values[OBSERVER_FIELD] = encode_observer(intent.observer)
        # A pinned executor is written with the rest of the fields, for the same reason: the row is
        # never readable with cards to cut under a pin the sprint was not opened with. A role the
        # operator pinned nothing on gets no field at all, which is how absence stays absence.
        for role, field in EXECUTOR_FIELDS.items():
            executor = {"worker": intent.worker, "reviewer": intent.reviewer}[role]
            if executor:
                values[field] = encode_executor(executor)
        # A restored legacy row gets no ownership keys at all; `restore` then writes
        # back exactly the fields its own export carried.
        if intent.product:
            values["sprint_product"] = intent.product
        if intent.issues:
            values["sprint_issues"] = json.dumps(list(intent.issues), separators=(",", ":"))
        if intent.reservations:
            values["sprint_reservations"] = json.dumps(list(intent.reservations), separators=(",", ":"))
        return values
''',
)

text = replace_region(
    text,
    "    def _committed_result(self, action: str, committed: dict[str, Any]) -> dict[str, Any]:\n",
    "\n    def _check_ownership(",
    '''    def _committed_result(self, action: str, committed: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(committed["ref"]))
        return SprintMutationReceipt(action, str(committed["event_id"])).to_document(sprint_document)
''',
)

text = replace_region(
    text,
    "    def _check_conflicts(\n",
    "\n    @_sql_atomic\n    def comment(\n",
    '''    def _check_conflicts(
        self,
        candidate: SprintAdmission,
        *,
        excluding: str = "",
        excluding_id: int | None = None,
    ) -> None:
        """Refuse a sprint this installation has no room, or no disjoint room, for.

        Every collision the caller can act on is reported before the generic count refusal. A sprint is
        left out of the scan only when it is proven to be the very row this transition is about — the
        row `reopen` reads by reference, or the row a staged create recorded its task id for. A matching
        reference alone proves nothing.
        """
        others = [
            sprint
            for sprint in self.reader.list(statuses={"open"}, create=False)
            if not (
                (excluding and sprint["ref"] == excluding)
                or (excluding_id is not None and _sprint_number(sprint) == excluding_id)
            )
        ]
        _refuse_open_sprint(
            candidate,
            [SprintAdmission.from_document(sprint) for sprint in others],
            limit=self._open_sprint_limit(),
        )
''',
)

text = replace_once(text, "        def mutation(sprint: dict[str, Any]) -> None:\n            task = TaskReader", "        def mutation(sprint: SprintWriteSnapshot) -> None:\n            task = TaskReader")

text = replace_region(
    text,
    "    @_sql_atomic\n    def record_budget(\n",
    "\n    def _finish_hard_budget(\n",
    '''    @_sql_atomic
    def record_budget(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        event_type: str,
        request_id: str | None = None,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "steward"})
        if event_type not in BUDGET_RECORDED_EVENT_TYPES:
            raise TaskError("validation", "unknown budget event type " + repr(event_type), 2)
        # One recording path for both families; only the charge is conditional. An uncharged type
        # can never reach the hard limit, so it never takes the typed hard-stop edge below.
        charged = event_type in BUDGET_EVENT_TYPES
        request_id = request_id or str(uuid.uuid4())
        existing = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if existing is not None:
            if existing.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            payload = existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
            if existing.get("kind") != "budget_recorded":
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            if bool(payload.get("hard_limit_stop")):
                return self._finish_hard_budget(
                    role=role,
                    actor=actor,
                    reference=reference,
                    event_type=event_type,
                    request_id=request_id,
                    source_event_id=source_event_id,
                    event=existing,
                )
            return (
                self._committed("budget_recorded", existing)
                if self.audit.committed_event(request_id)
                else self._pending("budget_recorded", existing)
            )
        before_document = self.reader.show(reference)
        before = SprintWriteSnapshot.from_document(before_document, thresholds=self.thresholds)
        before_budget = before.budget
        hard_stop = (
            charged
            and before.state is SprintState.OPEN
            and before_budget.total + 1 >= self.thresholds["hard"]
        )
        if hard_stop:
            counts = dict(before_budget.by_type)
            counts[event_type] += 1
            budget = SprintBudget.from_legacy({"by_type": counts}, thresholds=self.thresholds)
            event = self._event(
                "budget_recorded",
                role,
                actor,
                reference,
                request_id,
                {
                    "event_type": event_type,
                    "source_event_id": source_event_id or None,
                    "hard_limit_stop": True,
                    "budget": {"by_type": dict(budget.by_type)},
                },
                before,
            )
            self.audit.stage(request_id, event)
            return self._finish_hard_budget(
                role=role,
                actor=actor,
                reference=reference,
                event_type=event_type,
                request_id=request_id,
                source_event_id=source_event_id,
                event=event,
            )

        def mutation(sprint: SprintWriteSnapshot) -> None:
            budget = sprint.budget
            if charged:
                counts = dict(budget.by_type)
                counts[event_type] += 1
                normalized = SprintBudget.from_legacy({"by_type": counts}, thresholds=self.thresholds)
                values = {"sprint_budget": _budget_json(normalized.to_document())}
            else:
                uncharged = dict(budget.uncharged)
                uncharged[event_type] += 1
                values = {
                    BUDGET_UNCHARGED_FIELD: json.dumps(
                        uncharged, sort_keys=True, separators=(",", ":")
                    )
                }
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=values)

        return self._write(
            "budget_recorded",
            role,
            actor,
            reference,
            request_id,
            {
                "event_type": event_type,
                "source_event_id": source_event_id or None,
                "hard_limit_stop": False,
            },
            mutation,
        )
''',
)

text = replace_region(
    text,
    "    def _record_hard_stop(\n",
    "\n    def resume(\n",
    '''    def _record_hard_stop(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        budget_event_id: str,
        event_type: str,
        source_event_id: str,
    ) -> None:
        """Record the state transition separately from the charge that caused it."""
        stop_request_id = request_id + ":budget-hard-stop"
        if self.audit.committed_event(stop_request_id) is not None:
            return
        sprint_document = self.reader.show(reference)
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event = self._event(
            "budget_hard_stopped",
            role,
            actor,
            reference,
            stop_request_id,
            {
                "reason": "budget_hard_limit",
                "budget_event_id": budget_event_id or None,
                "event_type": event_type,
                "source_event_id": source_event_id or None,
            },
            sprint,
        )
        self.audit.stage(stop_request_id, event)
        self._record("budget_hard_stopped", event)
''',
)

text = replace_region(
    text,
    "    def resume(\n",
    "\n    @_sql_atomic\n    def close(\n",
    '''    def resume(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        entry: dict[str, Any],
        request_id: str | None = None,
        delivery_id: str = "",
        through_event: str = "",
    ) -> dict[str, Any]:
        self._role(role, {"po", "dispatcher", "observer", "steward"})
        try:
            normalized = SprintResume.from_legacy(entry, required=True, now=_now)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        if normalized is None:
            raise TaskError("validation", "resume entry must be a JSON object", 2)
        if _timestamp(normalized.recorded_at) is None:
            raise TaskError("validation", "resume recorded_at must include a timezone", 2)
        delivery_id = delivery_id.strip()
        through_event = through_event.strip()
        if bool(delivery_id) != bool(through_event):
            raise TaskError(
                "validation",
                "resume delivery acknowledgement requires both delivery_id and through_event",
                2,
            )
        if (delivery_id or through_event) and role != "observer":
            raise TaskError("role_forbidden", "only an observer resume can acknowledge delivery", 3)
        # Guard whole resumes by sprint as acknowledgements move its event cursor.
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(
            role=role,
            actor=actor,
            reference=reference,
            request_id=request_id,
        )

        return self._resume_atomic(
            role=role,
            actor=actor,
            reference=reference,
            normalized=normalized,
            request_id=request_id,
            delivery_id=delivery_id,
            through_event=through_event,
        )

    @_sql_atomic
    def _resume_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        normalized: SprintResume,
        request_id: str,
        delivery_id: str,
        through_event: str,
    ) -> dict[str, Any]:
        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call(
                "saveTaskMetadata",
                task_id=_sprint_number(sprint),
                values={"sprint_resume": json.dumps(normalized.to_document(), separators=(",", ":"))},
            )
            self.client.call(
                "createComment",
                task_id=_sprint_number(sprint),
                user_id=0,
                content="[sprint:resume]\\n" + normalized.selected_step,
            )

        payload = {"fields": list(RESUME_FIELDS)}
        if delivery_id:
            payload.update({"delivery_id": delivery_id, "through_event": through_event})
        return self._write("resume_recorded", role, actor, reference, request_id, payload, mutation)
''',
)

text = replace_region(
    text,
    "    @_sql_atomic\n    def reopen(\n",
    "\n    @_sql_atomic\n    def restore(\n",
    '''    @_sql_atomic
    def reopen(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reopen a sprint that still satisfies every rule for an open sprint.

        The other transition into `open`, so it runs the same admission order as `create` on the same
        staged-intent primitive. The observer is decided again here and is never inherited: what the row
        carries is either the value of the run that closed or migration provenance, and neither is a
        decision about the run being opened now. It is written while the sprint is still closed, so the
        row is never readable open under a value the reopening caller did not choose.
        """
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        intent = SprintReopenIntent(
            role=Role(role),
            actor=actor,
            reference=reference,
            observer=self._observer_intent(observer, executable=True),
        )
        intent_document = intent.to_document()
        with sprint_admission_lock(self.data_dir):
            document, committed = self.transactions.existing(
                request_id, kind=SPRINT_REOPENED, intent=intent_document
            )
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                sprint_document = self.reader.show(reference, include_cards=False)
                sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
                self._check_reopen(sprint, reference)
                event = self._event(
                    SPRINT_REOPENED,
                    role,
                    actor,
                    reference,
                    request_id,
                    {"intent": intent_document},
                    sprint,
                )
                document, committed = self.transactions.begin(
                    request_id, kind=SPRINT_REOPENED, intent=intent_document, event=event
                )
                if committed is not None:
                    return self._committed_result(SPRINT_REOPENED, committed)
                if document is None:
                    raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
            return self._run_reopen(document)

    def _check_reopen(self, sprint: SprintWriteSnapshot, reference: str) -> None:
        """Every rule an open sprint has to satisfy, read live before any write."""
        missing = [
            name
            for name, value in (
                ("product", sprint.product),
                ("issues", sprint.issues),
                ("reservations", sprint.reservations),
            )
            if not value
        ]
        if missing:
            raise TaskError(
                "validation",
                f"sprint {reference} predates sprint ownership and has no "
                + ", ".join(missing)
                + "; open a new sprint that owns its issues instead of reopening it",
                2,
            )
        self._check_ownership(sprint.product, list(sprint.issues), list(sprint.reservations))
        self._check_conflicts(sprint.admission(), excluding=reference)

    def _run_reopen(self, document: dict[str, Any]) -> dict[str, Any]:
        """Drive the staged reopen to its single audit event, or leave it repairable."""
        intent = SprintReopenIntent.from_document(document["intent"])
        reference = intent.reference
        try:
            sprint_document = self.reader.show(reference, include_cards=False)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            if not (document.get("progress") or {}).get("opened_done"):
                # A staged reopen held nothing while it waited for its repeat, so the
                # installation is measured again before this sprint becomes the open one.
                self._check_conflicts(sprint.admission(), excluding=reference)
            # The value the row carries now, recorded durably before the write that replaces it.
            self._record_observer_preimage(document, sprint)
            document.setdefault("progress", {})["observer_started"] = True
            self.transactions.save(document)
            if intent.observer is None:
                raise TaskError("validation", "sprint reopen intent lacks its observer", 2)
            self._transition_host(
                role=intent.role.value,
                actor=intent.actor,
                reference=reference,
                target="open",
                reason="Sprint reopened",
                request_id=str(document["request_id"]) + ":typed-reopen",
                observer=encode_observer(intent.observer),
            )
            document.setdefault("progress", {})["observer_done"] = True
            document.setdefault("progress", {})["opened_done"] = True
            self.transactions.save(document)
            sprint_document = self.reader.show(reference)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            event = document["event"]
            event["task_id"] = sprint.entity_id
            event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint.admission())
            return SprintMutationReceipt(SPRINT_REOPENED, str(event["event_id"])).to_document(sprint_document)
        except TaskError as exc:
            if getattr(self.client, "backend_kind", "kanboard") == "postgres":
                raise
            answer = exc.code in _ADMISSION_REFUSALS or (
                exc.code in {"validation", "role_forbidden"} and not document.get("progress")
            )
            if answer and self._compensate_reopen(document, reference):
                raise
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None

    def _record_observer_preimage(self, document: dict[str, Any], sprint: SprintWriteSnapshot) -> None:
        """Record what the row's observer was, once, before this reopen writes over it."""
        progress = document.setdefault("progress", {})
        if "observer_preimage" in progress:
            return
        try:
            progress["observer_preimage"] = encode_observer(sprint.observer) if sprint.observer else None
        except ValueError:
            progress["observer_preimage"] = None
        self.transactions.save(document)

    def _compensate_reopen(self, document: dict[str, Any], reference: str) -> bool:
        """Undo a refused reopen's observer write and drop its intent."""
        progress = document.get("progress") or {}
        if progress.get("opened_done"):
            return False
        try:
            sprint_document = self.reader.show(reference, include_cards=False)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            if sprint.state is SprintState.OPEN:
                return False
            if progress.get("observer_started") or progress.get("observer_done"):
                preimage = progress.get("observer_preimage")
                if not isinstance(preimage, str):
                    return False
                if (
                    self.client.call(
                        "saveTaskMetadata",
                        task_id=_sprint_number(sprint),
                        values={OBSERVER_FIELD: preimage},
                    )
                    is not True
                ):
                    return False
        except (TaskError, OSError, KeyError, TypeError, ValueError):
            return False
        document["progress"] = {}
        self.transactions.save(document)
        self.transactions.discard(document)
        return True
''',
)

text = replace_region(
    text,
    "    @_sql_atomic\n    def restore(\n",
    "\n    def _guard_observer_identity(",
    '''    @_sql_atomic
    def restore(
        self, *, reference: str, values: dict[str, str], request_id: str | None = None
    ) -> dict[str, Any]:
        """Rewrite one sprint entity's fields verbatim from a checkpoint export.

        Not a sprint mutation an operator makes, so it is not refused on status the way `comment` or
        `resume` are.
        """
        unknown = sorted(set(values) - SPRINT_METADATA)
        if unknown:
            raise TaskError("validation", "restore carries unknown sprint fields: " + ", ".join(unknown), 2)

        def mutation(sprint: SprintWriteSnapshot) -> None:
            self.client.call("saveTaskMetadata", task_id=_sprint_number(sprint), values=dict(values))

        return self._write(
            "restored", "steward", "restore", reference, request_id, {"fields": sorted(values)}, mutation
        )
''',
)

text = replace_region(
    text,
    "    def _write(\n",
    "\n\ndef _is_sprint_row(",
    '''    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any],
        mutation: Callable[[SprintWriteSnapshot], Any],
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            return self._committed(kind, committed)
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            if pending.get("record_type") == "board.protocol_event":
                raise TaskError(
                    "validation",
                    "request id belongs to a typed Sprint lifecycle occurrence; retry its protocol recovery",
                    2,
                )
            return self._pending(kind, pending)
        sprint_document = self.reader.show(reference)
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        if sprint.state in {SprintState.CLOSED, SprintState.STOPPED} and kind in {
            "current_task_set",
            "resume_recorded",
        }:
            raise TaskError("closed", "sprint is closed", 3)
        event = self._event(kind, role, actor, reference, request_id, payload, sprint)
        self.audit.stage(request_id, event)
        try:
            mutation(sprint)
        except Exception:
            if getattr(self.client, "backend_kind", "kanboard") == "postgres":
                raise
            self.audit.discard(request_id)
            raise
        return self._record(kind, event)

    def _record(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(event["ref"]))
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event["task_id"] = sprint.entity_id
        event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
        request_id = str(event["request_id"])
        self.audit.stage(request_id, event)
        event_id = self.audit.append(request_id, event)
        update_active_sprint_projects(self.data_dir, sprint.admission())
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _committed(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        sprint_document = self.reader.show(str(event["ref"]))
        event_id = self.audit.append(str(event["request_id"]), event)
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _pending(self, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        # The staged event is only retained after a successful backend mutation in the
        # simple writes. Creation stages its Kanboard id before assigning metadata.
        sprint_document = self.reader.show(str(event["ref"]))
        sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
        event["task_id"] = sprint.entity_id
        event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
        self.audit.stage(str(event["request_id"]), event)
        event_id = self.audit.append(str(event["request_id"]), event)
        update_active_sprint_projects(self.data_dir, sprint.admission())
        return SprintMutationReceipt(kind, event_id).to_document(sprint_document)

    def _event(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        payload: dict[str, Any],
        sprint: SprintWriteSnapshot | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = sprint.entity_id if isinstance(sprint, SprintWriteSnapshot) else sprint["id"] if sprint else ""
        return {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": task_id,
            "ref": reference,
            "backend": {
                "kind": getattr(self.client, "backend_kind", "kanboard"),
                "task_id": _sprint_number(sprint) if sprint else None,
                "revision": "pending",
            },
            "request_id": request_id,
            "payload": payload,
        }

    @staticmethod
    def _role(role: str, allowed: set[str]) -> None:
        if role not in allowed:
            raise TaskError("role_forbidden", "role is not permitted for this operation", 3)


def _sprint_number(sprint: SprintWriteSnapshot | dict[str, Any] | None) -> int:
    """The sprint's number, read through the same parser a card's number is read through."""
    entity = sprint.entity_id if isinstance(sprint, SprintWriteSnapshot) else (sprint or {}).get("id")
    number = entity_number("sprint", entity)
    if number is None:
        raise TaskError("backend_error", "Kanboard returned an invalid sprint", 1)
    return number
''',
)

SPRINTS.write_text(text, encoding="utf-8")

pyproject = PYPROJECT.read_text(encoding="utf-8")
pyproject = replace_once(
    pyproject,
    '    "src/secretary/board/sprint_admission.py",\n',
    '    "src/secretary/board/sprint_admission.py",\n    "src/secretary/board/sprint_write.py",\n',
)
PYPROJECT.write_text(pyproject, encoding="utf-8")

shards = SHARDS.read_text(encoding="utf-8")
shards = replace_once(
    shards,
    "unit tests/test_sprint_admission.py\n",
    "unit tests/test_sprint_admission.py\nunit tests/test_sprint_write.py\n",
)
SHARDS.write_text(shards, encoding="utf-8")

TEST.write_text(
    '''from __future__ import annotations

from unittest import TestCase

from secretary.board.models import SprintState
from secretary.board.roles import Role
from secretary.board.sprint_write import (
    SprintCreateIntent,
    SprintMutationReceipt,
    SprintReopenIntent,
    SprintWriteSnapshot,
)


class SprintWriteValueTests(TestCase):
    def test_snapshot_parses_writer_fields_and_admission(self) -> None:
        snapshot = SprintWriteSnapshot.from_document(
            {
                "id": "sprint_postgres_42",
                "ref": "sprint:42",
                "status": "stopped",
                "repositories": ["/srv/a", "/srv/b"],
                "product": "product:7",
                "issues": ["issue:8"],
                "reservations": ["secretary"],
                "budget": {"by_type": {"blocked": 2}},
                "current_task": "secretary-9",
                "observer": {"kind": "none"},
                "audit": {"updated_at": "2026-09-16T12:00:00Z"},
            }
        )
        self.assertEqual(snapshot.state, SprintState.STOPPED)
        self.assertEqual(snapshot.repositories, ("/srv/a", "/srv/b"))
        self.assertEqual(snapshot.issues, ("issue:8",))
        self.assertEqual(snapshot.budget.by_type["blocked"], 2)
        self.assertEqual(snapshot.current_task, "secretary-9")
        self.assertEqual(snapshot.updated_at, "2026-09-16T12:00:00Z")
        admission = snapshot.admission()
        self.assertEqual(admission.ref, "sprint:42")
        self.assertEqual(admission.product, "product:7")
        self.assertEqual(admission.reservations, ("secretary",))
        self.assertTrue(admission.is_open is False)

    def test_snapshot_keeps_reader_legacy_unknown_state_fallback(self) -> None:
        snapshot = SprintWriteSnapshot.from_document({"id": "sprint_kanboard_1", "status": "mystery"})
        self.assertEqual(snapshot.state, SprintState.OPEN)

    def test_create_intent_round_trips_released_document(self) -> None:
        document = {
            "role": "po",
            "actor": "owner",
            "goal": "Ship it",
            "definition_of_done": "green",
            "repositories": ["/srv/a"],
            "product": "product:1",
            "issues": ["issue:2"],
            "reservations": ["secretary"],
            "reference": "sprint:3",
            "status": "open",
            "observer": {"kind": "none"},
            "worker": None,
            "reviewer": "review-head",
        }
        intent = SprintCreateIntent.from_document(document)
        self.assertEqual(intent.role, Role.PO)
        self.assertEqual(intent.state, SprintState.OPEN)
        self.assertEqual(intent.to_document(), document)
        self.assertEqual(intent.admission().repositories, ("/srv/a",))

    def test_reopen_intent_round_trips_released_document(self) -> None:
        document = {
            "role": "po",
            "actor": "owner",
            "reference": "sprint:3",
            "observer": {"kind": "head", "profile": "observer"},
        }
        intent = SprintReopenIntent.from_document(document)
        self.assertEqual(intent.role, Role.PO)
        self.assertEqual(intent.to_document(), document)

    def test_receipt_projects_the_existing_public_document(self) -> None:
        sprint = {"ref": "sprint:3", "status": "open", "nested": {"kept": True}}
        result = SprintMutationReceipt("commented", "evt_1").to_document(sprint)
        self.assertEqual(
            result,
            {"action": "commented", "sprint": sprint, "event_id": "evt_1"},
        )
        self.assertIsNot(result["sprint"], sprint)
''',
    encoding="utf-8",
)
