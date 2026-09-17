from __future__ import annotations

from pathlib import Path


def patch_tasks(path: Path) -> bool:
    source = path.read_text()
    if "data: OutcomeRoundContext | Mapping[str, object]" in source:
        return False

    source = source.replace(
        "from collections.abc import Callable, Collection, Iterable, Iterator\n",
        "from collections.abc import Callable, Collection, Iterable, Iterator, Mapping\n",
        1,
    )
    source = source.replace(
        "from secretary.board.models import (\n",
        "from secretary.board.outcome_round_context import OutcomeRoundContext\n"
        "from secretary.board.models import (\n",
        1,
    )
    start = source.index("def _validate_outcome_round_context(data: dict[str, Any]) -> None:\n")
    end = source.index("\ndef specification_revision(", start)
    source = source[:start] + source[end + 1 :]

    old = '''    def outcome_round_context(\n        self,\n        *,\n        role: str,\n        actor: str,\n        reference: str,\n        data: dict[str, Any],\n        request_id: str,\n    ) -> dict[str, Any]:\n        """Persist one exact forward source identity before its consumer runs.\n\n        This is journal-only.  It is deliberately a typed dispatcher boundary\n        rather than comment prose: recovery reads the request id that names\n        this record and never reconstructs a worker, reviewer, or Assessment\n        identity from card history.\n        """\n        role = self._role(role, {Role.DISPATCHER})\n        _validate_outcome_round_context(data)\n        if not request_id.strip():\n            raise TaskError("validation", "outcome round context needs the request id it owns", 2)\n        return self._write(\n            "outcome_round_context",\n            role,\n            actor,\n            reference,\n            request_id,\n            dict(data),\n            lambda task: None,\n            identity=dict(data),\n        )\n'''
    new = '''    def outcome_round_context(\n        self,\n        *,\n        role: str,\n        actor: str,\n        reference: str,\n        data: OutcomeRoundContext | Mapping[str, object],\n        request_id: str,\n    ) -> dict[str, Any]:\n        """Persist one exact forward source identity before its consumer runs.\n\n        This is journal-only. The dispatcher carries the typed value; the\n        historical dictionary exists only at this audit compatibility boundary.\n        Raw mappings remain accepted for released callers and are normalized\n        once through the same model.\n        """\n        role = self._role(role, {Role.DISPATCHER})\n        try:\n            context = (\n                data\n                if isinstance(data, OutcomeRoundContext)\n                else OutcomeRoundContext.from_data(data)\n            )\n        except ValueError as exc:\n            raise TaskError("validation", str(exc), 2) from None\n        if not request_id.strip():\n            raise TaskError("validation", "outcome round context needs the request id it owns", 2)\n        payload = context.to_data()\n        return self._write(\n            "outcome_round_context",\n            role,\n            actor,\n            reference,\n            request_id,\n            payload,\n            lambda task: None,\n            identity=payload,\n        )\n'''
    if old not in source:
        raise RuntimeError("TaskWriter.outcome_round_context source drifted")
    path.write_text(source.replace(old, new, 1))
    return True


def patch_dispatcher(path: Path) -> bool:
    source = path.read_text()
    if "dict[str, OutcomeRoundContext]" in source:
        return False

    source = source.replace(
        "from secretary.board.protocol_artifacts import ArtifactOwnershipViolation, validate_rework_prerequisites\n",
        "from secretary.board.outcome_round_context import OutcomeRoundContext, OutcomeRoundPhase\n"
        "from secretary.board.protocol_artifacts import ArtifactOwnershipViolation, validate_rework_prerequisites\n",
        1,
    )

    old = '''        context = self._outcome_round_context(reference, record)\n        worker_context = context.get("worker", {})\n        attempt_id = str(worker_context.get("attempt_id") or record.attempt_id or "")\n        attempt = worker_context.get("attempt", record.attempt_round)\n        generation = worker_context.get("report_generation", record.report_generation)\n        if (\n            not attempt_id\n            or not isinstance(attempt, int)\n            or attempt < 1\n            or not isinstance(generation, int)\n            or generation < 1\n        ):\n            return None\n        reviewed = bool(context.get("review")) or bool(record.review_run)\n        revision = context.get("report", {}).get(\n            "specification_revision", worker_context.get("specification_revision")\n        )\n        if revision is not None and not isinstance(revision, str):\n            revision = None\n'''
    new = '''        context = self._outcome_round_context(reference, record)\n        worker_context = context.get("worker")\n        attempt_id = (worker_context.attempt_id if worker_context is not None else record.attempt_id) or ""\n        attempt = worker_context.attempt if worker_context is not None else record.attempt_round\n        generation = (\n            worker_context.report_generation if worker_context is not None else record.report_generation\n        )\n        if not attempt_id or attempt < 1 or generation < 1:\n            return None\n        reviewed = "review" in context or bool(record.review_run)\n        report_context = context.get("report")\n        revision = (\n            report_context.specification_revision\n            if report_context is not None\n            else worker_context.specification_revision\n            if worker_context is not None\n            else None\n        )\n'''
    if old not in source:
        raise RuntimeError("outcome obligation source drifted")
    source = source.replace(old, new, 1)
    source = source.replace(
        "        context: dict[str, dict[str, Any]],\n",
        "        context: dict[str, OutcomeRoundContext],\n",
        1,
    )

    old = '''        def one(name: str, phase: str, kind: str, marker: str) -> str:\n            if canon is None:\n                return f"attempt_outcome_lineage_missing_{name}"\n            handoff = context.get(phase, {})\n            event_id = handoff.get("source_event_id")\n            if not isinstance(event_id, str) or not event_id:\n                return f"attempt_outcome_lineage_missing_{name}"\n            event = events.get(event_id)\n            if event is None:\n                return f"attempt_outcome_lineage_dangling_{name}"\n            data = event.data\n            if event.kind.value != kind or event.ref != reference or data.get("marker") != marker:\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            if "specification_revision" not in data:\n                return f"attempt_outcome_lineage_legacy_{name}"\n            if data.get("specification_revision") != revision:\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            if phase == "decision" and data.get("assessment_visit") != handoff.get("assessment_visit"):\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            source[name] = event.event_id\n            return ""\n\n        diagnostics = [\n            one("report", "report", "card.reported", str(context.get("report", {}).get("marker") or ""))\n            if report_required\n            else "",\n            one("verdict", "verdict", "card.verdict", str(context.get("verdict", {}).get("marker") or ""))\n            if verdict_required\n            else "",\n            one("decision", "decision", "card.decided", str(context.get("decision", {}).get("marker") or ""))\n            if decision_required\n            else "",\n        ]\n'''
    new = '''        def one(name: str, phase: str, kind: str, marker: str) -> str:\n            if canon is None:\n                return f"attempt_outcome_lineage_missing_{name}"\n            handoff = context.get(phase)\n            if handoff is None or not handoff.source_event_id:\n                return f"attempt_outcome_lineage_missing_{name}"\n            event = events.get(handoff.source_event_id)\n            if event is None:\n                return f"attempt_outcome_lineage_dangling_{name}"\n            data = event.data\n            if event.kind.value != kind or event.ref != reference or data.get("marker") != marker:\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            if "specification_revision" not in data:\n                return f"attempt_outcome_lineage_legacy_{name}"\n            if data.get("specification_revision") != revision:\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            if phase == "decision" and data.get("assessment_visit") != handoff.assessment_visit:\n                return f"attempt_outcome_lineage_incompatible_{name}"\n            source[name] = event.event_id\n            return ""\n\n        report_context = context.get("report")\n        verdict_context = context.get("verdict")\n        decision_context = context.get("decision")\n        diagnostics = [\n            one(\n                "report", "report", "card.reported",\n                report_context.marker if report_context is not None else "",\n            )\n            if report_required else "",\n            one(\n                "verdict", "verdict", "card.verdict",\n                verdict_context.marker if verdict_context is not None else "",\n            )\n            if verdict_required else "",\n            one(\n                "decision", "decision", "card.decided",\n                decision_context.marker if decision_context is not None else "",\n            )\n            if decision_required else "",\n        ]\n'''
    if old not in source:
        raise RuntimeError("lineage source code drifted")
    source = source.replace(old, new, 1)
    source = source.replace(
        '''        existing_context = self._outcome_round_context(reference, record)\n        worker = existing_context.get("worker", {})\n        round_id = str(worker.get("round_id") or "")\n''',
        '''        existing_context = self._outcome_round_context(reference, record)\n        worker = existing_context.get("worker")\n        round_id = worker.round_id if worker is not None else ""\n''',
        1,
    )

    old = '''        revision = source_revision if phase != "worker" else None\n        if phase != "worker" and not freeze_source_revision and source_revision is None:\n            revision = worker.get("specification_revision") if worker else None\n        if phase == "worker":\n            revision = (\n                specification_revision(self.audit.events(reference), str(task.get("description") or ""))\n                or None\n            )\n        self.writer.outcome_round_context(\n            role="dispatcher",\n            actor=self.owner,\n            reference=reference,\n            request_id=context_request,\n            data={\n                "version": 2,\n                "phase": phase,\n                "round_id": round_id,\n                "attempt_id": worker.get("attempt_id", record.attempt_id)\n                if phase != "worker"\n                else record.attempt_id,\n                "attempt": worker.get("attempt", record.attempt_round)\n                if phase != "worker"\n                else record.attempt_round,\n                "report_generation": worker.get("report_generation", record.report_generation)\n                if phase != "worker"\n                else record.report_generation,\n                "request_ids": sorted(request_ids),\n                "assessment_visit": assessment_visit,\n                "source_event_id": source_event_id,\n                "specification_revision": revision,\n                "marker": marker,\n            },\n        )\n'''
    new = '''        revision = source_revision if phase != "worker" else None\n        if phase != "worker" and not freeze_source_revision and source_revision is None:\n            revision = worker.specification_revision if worker is not None else None\n        if phase == "worker":\n            revision = (\n                specification_revision(self.audit.events(reference), str(task.get("description") or ""))\n                or None\n            )\n        try:\n            context = OutcomeRoundContext(\n                version=2,\n                phase=OutcomeRoundPhase(phase),\n                round_id=round_id,\n                attempt_id=(\n                    worker.attempt_id\n                    if phase != "worker" and worker is not None\n                    else record.attempt_id\n                ),\n                attempt=(\n                    worker.attempt\n                    if phase != "worker" and worker is not None\n                    else record.attempt_round\n                ),\n                report_generation=(\n                    worker.report_generation\n                    if phase != "worker" and worker is not None\n                    else record.report_generation\n                ),\n                request_ids=tuple(sorted(request_ids)),\n                assessment_visit=assessment_visit,\n                source_event_id=source_event_id,\n                specification_revision=revision,\n                marker=marker,\n            )\n        except ValueError as exc:\n            raise TaskError("validation", str(exc), 2) from None\n        self.writer.outcome_round_context(\n            role="dispatcher", actor=self.owner, reference=reference,\n            request_id=context_request, data=context,\n        )\n'''
    if old not in source:
        raise RuntimeError("round-context producer source drifted")
    source = source.replace(old, new, 1)
    source = source.replace(
        '''        context = self._outcome_round_context(reference, record)\n        owner = context.get("worker" if phase == "report" else "review", {})\n        request_ids = owner.get("request_ids") if isinstance(owner, dict) else None\n        if not isinstance(request_ids, list):\n            return\n''',
        '''        context = self._outcome_round_context(reference, record)\n        owner = context.get("worker" if phase == "report" else "review")\n        if owner is None:\n            return\n        request_ids = owner.request_ids\n''',
        1,
    )

    start = source.index(
        "    def _outcome_round_context(self, reference: str, record: DispatcherRecord) -> dict[str, dict[str, Any]]:\n"
    )
    end = source.index("\n    def _outcome_usage_source(", start)
    method = '''    def _outcome_round_context(\n        self, reference: str, record: DispatcherRecord\n    ) -> dict[str, OutcomeRoundContext]:\n        """Find one unsettled durable handoff without re-estimating its identity.\n\n        The fast path keeps ordinary dispatch cheap. Adoption can lose the\n        process-local attempt id and report generation, so its fallback uses\n        only durable handoffs and excludes rounds already sealed by a lifecycle\n        effect. It never uses card comments, workspace text, event order or\n        request-id grammar to choose a source.\n        """\n        payloads: list[OutcomeRoundContext] = []\n        for event in self.audit.events(reference, kind="outcome_round_context"):\n            payload = (\n                event.get("data")\n                if event.get("record_type") == "board.protocol_event"\n                else event.get("payload")\n            )\n            if not isinstance(payload, dict) or payload.get("version") != 2:\n                continue\n            try:\n                payloads.append(OutcomeRoundContext.from_data(payload))\n            except ValueError:\n                continue\n        workers = [payload for payload in payloads if payload.phase is OutcomeRoundPhase.WORKER]\n        exact = [\n            payload for payload in workers\n            if payload.attempt_id == record.attempt_id\n            and payload.attempt == record.attempt_round\n            and payload.report_generation == record.report_generation\n        ]\n        if len(exact) == 1:\n            worker = exact[0]\n        else:\n            sealed = {\n                (data.get("attempt_id"), data.get("attempt"), data.get("report_generation"))\n                for event in self.writer.board_host.canon.events(ref=reference)\n                if isinstance((data := event.data.get("attempt_outcome_owed")), dict)\n            }\n            unsettled = [\n                payload for payload in workers\n                if (payload.attempt_id, payload.attempt, payload.report_generation) not in sealed\n            ]\n            if len(unsettled) != 1:\n                return {}\n            worker = unsettled[0]\n        context = {"worker": worker}\n        for payload in payloads:\n            if payload.phase is not OutcomeRoundPhase.WORKER and payload.round_id == worker.round_id:\n                context[payload.phase.value] = payload\n        return context\n'''
    source = source[:start] + method + source[end:]
    path.write_text(source)
    return True


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    changed = [
        patch_tasks(root / "src/secretary/tasks.py"),
        patch_dispatcher(root / "src/secretary/dispatcher.py"),
    ]
    print("A12 source transform:", "changed" if any(changed) else "already applied")


if __name__ == "__main__":
    main()
