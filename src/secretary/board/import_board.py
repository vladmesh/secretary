"""Importing today's Kanboard board into the §3 schema, with a report of what did not fit.

``docs/BOARD_STORE.md`` §8 is the contract this module executes.  It reads the two Kanboard
boards through the readers the product already has, maps every record onto the tables
``secretary.board.schema`` declares, and writes a report that names *everything* the mapping
could not carry.  A record that is neither imported nor named in the report is a defect of this
module, not an acceptable rounding.

**It never writes to Kanboard.**  Every call it makes is a read (`getProjectByName`,
`getColumns`, `getActiveSwimlanes`, `getAllTasks`/`getAllTaskLinks` through
:func:`secretary.tasks.all_project_cards`, `getTaskMetadata`, `getAllComments`), and there is no
`createTask`, `updateTask`, `saveTaskMetadata`, `createComment` or `removeTask` anywhere below.

**The normalizers are the readers' own.**  ``_STATE_BY_COLUMN``, ``_KNOWN_METADATA``,
``_enum_or_default``, ``_split_heads`` and the sprint side's ``_budget``, ``_resume``,
``_source_audit`` and ``_json_list`` are imported from :mod:`secretary.tasks` and
:mod:`secretary.sprints` rather than restated here, so the importer and the reader cannot come
to disagree about what a metadata bag means.  What this module adds is only the part that has no
reader today: the §8.1 marker rule, which is deliberately *stricter* than
``tasks._normalize_comment``.

**One run, on an empty database.**  §8's mapping generates identity keys for comments, resumes
and budget events, and the board carries no stable column for any of them, so a second run has no
natural key to converge on.  Rather than duplicate rows or invent one, :func:`apply` refuses a
database that already holds board rows and names the table it found them in.  A repeat is a fresh
``migrate`` and a fresh import, which is reproducible because every row this module builds is a
pure function of the board it read.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from secretary.product_issues import (
    ISSUE_CLOSE_REASONS,
    ISSUE_KINDS,
    ISSUE_PRIORITIES,
    ISSUE_TYPE,
    META_ISSUE_CLOSED_REASON,
    META_ISSUE_KIND,
    META_ISSUE_PRIORITY,
    META_ISSUE_PRODUCT,
    META_PRODUCT_ID,
    META_PRODUCT_PROJECTS,
    META_RECORD_TYPE,
    PRODUCT_TYPE,
    product_lane_name,
)
from secretary.sprint_observer import (
    EXECUTOR_PINNED,
    OBSERVER_FIELD,
    parse_observer,
    stored_executors,
)
from secretary.sprints import (
    BUDGET_RECORDED_EVENT_TYPES,
    BUDGET_UNCHARGED_EVENT_TYPES,
    BUDGET_UNCHARGED_FIELD,
    RESUME_FIELDS,
    SPRINT_BOARD_NAME,
    SPRINT_REFERENCE_PREFIX,
    SPRINT_STATUSES,
    _budget,
    _json_list,
    _resume,
    _source_audit,
)
from secretary.tasks import (
    _COMPLEXITIES,
    _FAMILY_PREFERENCES,
    _KNOWN_METADATA,
    _ROLES,
    _STATE_BY_COLUMN,
    _TASK_TYPES,
    KanboardClient,
    _enum_or_default,
    _enum_or_none,
    _nonnegative_int,
    _null_if_empty,
    _positive_int,
    _split_heads,
    _task_metadata,
    _text,
    all_project_cards,
)
from triggered_agents.runtime.head import CODEX_LAUNCH_MODES

PIPELINE_BOARD_NAME = "Pipeline"

#: The importer's own version.  It travels in the report and in nothing else: the *schema's*
#: version is Alembic's, and inventing a second bookkeeping table beside it is what §7.4 refuses.
IMPORT_VERSION = 1

#: ``record_type`` is consumed by the mapping — §8.1 makes it table identity — so it is a known
#: key here even though ``tasks._KNOWN_METADATA`` does not list it.  Without this every one of the
#: 940 cards would carry it into ``extensions`` and §8.2's "a key on thousands of rows is a
#: missing column" alarm would fire on a key the schema already models.
TASK_KNOWN_METADATA = frozenset(_KNOWN_METADATA) | {META_RECORD_TYPE}

#: §8.1's marker vocabulary, exactly as the document spells it.  A first line that is a complete
#: ``[token]`` whose token is *not* here keeps the whole body and gets ``marker = NULL``.
MARKER_ROLES = frozenset(_ROLES)
MARKER_PREFIXES = ("report:", "review:", "decision:", "issue:")
MARKER_EXACT = frozenset({"sprint:resume", "archive", "rejected"})

_MARKER_LINE = re.compile(r"^\[([^\]\n]+)\]$")

#: The tables this import fills, in the order they are written.  A table absent from a group is
#: not forgotten: §8.4 and the report's ``expected_zero`` section say why each empty one is empty.
TABLE_ORDER = (
    "projects",
    "repositories",
    "products",
    "product_projects",
    "issues",
    "sprints",
    "sprint_repositories",
    "sprint_issues",
    "sprint_projects",
    "sprint_resumes",
    "tasks",
    "task_retry_heads",
    "task_dependencies",
    "task_supersessions",
    "task_issues",
    "requests",
    "board_events",
    "sprint_budget_events",
    "task_comments",
    "sprint_comments",
    "sprint_decisions",
)


class BoardImportError(RuntimeError):
    pass


# --- reading the source -------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRow:
    """One Kanboard row with the two things Kanboard has no bulk read for."""

    raw: dict[str, Any]
    meta: dict[str, str]
    comments: tuple[dict[str, Any], ...]

    @property
    def ref(self) -> str:
        return _text(self.raw.get("reference"))

    @property
    def task_id(self) -> int:
        identifier = _positive_int(self.raw.get("id"))
        if identifier is None:
            raise BoardImportError("Kanboard returned a row without an id")
        return identifier

    @property
    def archived(self) -> bool:
        return _nonnegative_int(self.raw.get("is_active", self.raw.get("status", 1))) == 0


@dataclass(frozen=True)
class RegistryEntry:
    project_id: str
    repo: str
    remote: str | None
    default_branch: str
    adapter: str | None
    orca_binding: str | None
    enabled: bool
    plane: str
    curator_roots: tuple[str, ...]


@dataclass(frozen=True)
class BoardSource:
    """Everything the import reads, captured once so a dry run and a real run see one board."""

    pipeline: tuple[SourceRow, ...]
    sprints: tuple[SourceRow, ...]
    pipeline_columns: dict[int, str]
    pipeline_swimlanes: dict[int, str]
    registry: tuple[RegistryEntry, ...]
    budget_records: tuple[dict[str, Any], ...]
    transaction_documents: tuple[dict[str, Any], ...]


def _board_rows(client: KanboardClient, board_name: str) -> tuple[list[SourceRow], dict[int, str], dict[int, str]]:
    board = client.call("getProjectByName", name=board_name)
    board_id = _positive_int(board.get("id")) if isinstance(board, dict) else None
    if board_id is None:
        raise BoardImportError(f"the {board_name!r} board is unavailable")
    columns = {
        identifier: _text(column.get("title"))
        for column in (client.call("getColumns", project_id=board_id) or [])
        if isinstance(column, dict) and (identifier := _positive_int(column.get("id"))) is not None
    }
    swimlanes = {
        identifier: _text(lane.get("name"))
        for lane in (client.call("getActiveSwimlanes", project_id=board_id) or [])
        if isinstance(lane, dict) and (identifier := _positive_int(lane.get("id"))) is not None
    }
    raw_rows = [row for row in all_project_cards(client, board_id) if isinstance(row, dict)]
    ids = [_positive_int(row.get("id")) for row in raw_rows]
    if any(identifier is None for identifier in ids):
        raise BoardImportError(f"the {board_name!r} board returned a row without an id")
    answers = client.call_batch(
        (method, {"task_id": identifier})
        for identifier in ids
        for method in ("getTaskMetadata", "getAllComments")
    )
    rows = []
    for index, row in enumerate(raw_rows):
        comments = answers[index * 2 + 1] or []
        if not isinstance(comments, list):
            raise BoardImportError(f"the {board_name!r} board returned invalid comments")
        rows.append(
            SourceRow(
                raw=row,
                meta=_task_metadata(answers[index * 2]),
                comments=tuple(comment for comment in comments if isinstance(comment, dict)),
            )
        )
    return rows, columns, swimlanes


def read_registry(instance: Path) -> list[RegistryEntry]:
    """The project registry files, which stay canonical (§6.2); §8.3 only projects them."""
    directory = Path(instance).expanduser() / "projects"
    if not directory.is_dir():
        raise BoardImportError(f"the project registry {directory} is unavailable")
    entries = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise BoardImportError(f"cannot read project registry entry {path.name}: {exc}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            continue
        roots = payload.get("curator_roots")
        entries.append(
            RegistryEntry(
                project_id=str(payload["id"]),
                repo=str(payload.get("repo") or ""),
                remote=_null_if_empty(payload.get("remote")),
                default_branch=str(payload.get("default_branch") or "main"),
                adapter=_null_if_empty(payload.get("adapter")),
                orca_binding=_null_if_empty(payload.get("orca_binding")),
                enabled=payload.get("enabled", True) is not False,
                plane=str(payload.get("plane") or "project"),
                curator_roots=tuple(str(root) for root in roots if isinstance(root, str))
                if isinstance(roots, list)
                else (),
            )
        )
    return entries


def read_budget_records(data_dir: Path | None) -> list[dict[str, Any]]:
    """§8.7's source for a charge's ``occurred_at``: the audit journal's ``budget_recorded`` rows.

    The journal is read and never written.  A journal that is absent is not an error — §8.7 then
    falls back to the sprint's ``updated_at`` for every charge and the report marks the timestamps
    approximate, which is exactly the case the document describes.
    """
    if data_dir is None:
        return []
    path = Path(data_dir).expanduser() / "board" / "events.ndjson"
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("kind") == "budget_recorded":
            records.append(record)
    return records


def read_transaction_documents(data_dir: Path | None) -> list[dict[str, Any]]:
    """§8.5's source for a pre-cutover sprint close's decisions, where one still exists."""
    if data_dir is None:
        return []
    directory = Path(data_dir).expanduser() / "board" / "product-issue-transactions"
    if not directory.is_dir():
        return []
    documents = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(document, dict):
            documents.append(document)
    return documents


def read_source(instance: str | Path, *, data_dir: str | Path | None = None) -> BoardSource:
    """One read of everything, so a dry run and a real run describe the same board."""
    instance_path = Path(instance).expanduser()
    client = KanboardClient.for_instance(instance_path)
    pipeline, columns, swimlanes = _board_rows(client, PIPELINE_BOARD_NAME)
    sprints, _, _ = _board_rows(client, SPRINT_BOARD_NAME)
    data_path = Path(data_dir).expanduser() if data_dir else None
    return BoardSource(
        pipeline=tuple(pipeline),
        sprints=tuple(row for row in sprints if row.ref.startswith(SPRINT_REFERENCE_PREFIX)),
        pipeline_columns=columns,
        pipeline_swimlanes=swimlanes,
        registry=tuple(read_registry(instance_path)),
        budget_records=tuple(read_budget_records(data_path)),
        transaction_documents=tuple(read_transaction_documents(data_path)),
    )


# --- the §8.1 marker rule -----------------------------------------------------------------


def parse_marker(body: str) -> tuple[str | None, str]:
    """Split a comment into §8.1's ``(marker, body)``.

    Stricter than ``tasks._normalize_comment`` on purpose, and stricter in the safe direction: a
    first line that merely *looks* like a marker keeps the whole body and gets no marker, so this
    can fail to recognize a marker but can never eat a line of prose.
    """
    head, separator, rest = body.partition("\n")
    match = _MARKER_LINE.match(head.strip())
    if match is None:
        return None, body
    token = match.group(1).strip()
    known = (
        token in MARKER_ROLES
        or token in MARKER_EXACT
        or any(token.startswith(prefix) and len(token) > len(prefix) for prefix in MARKER_PREFIXES)
    )
    if not known:
        return None, body
    return token, rest if separator else ""


def marker_token(body: str) -> str | None:
    """The bracketed first-line token, whether or not §8.1's vocabulary recognizes it."""
    match = _MARKER_LINE.match(body.partition("\n")[0].strip())
    return match.group(1).strip() if match else None


# --- the report ----------------------------------------------------------------------------


@dataclass
class ImportReport:
    """Every fact the mapping produced, machine-readable; :func:`render` is its prose half."""

    mode: str = "dry-run"
    import_version: int = IMPORT_VERSION
    schema_revision: str | None = None
    generated_at: str = ""
    source: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    expected_zero: list[dict[str, Any]] = field(default_factory=list)
    records_not_imported: list[dict[str, Any]] = field(default_factory=list)
    links_not_imported: list[dict[str, Any]] = field(default_factory=list)
    fields_without_a_column: list[dict[str, Any]] = field(default_factory=list)
    extensions_keys: list[dict[str, Any]] = field(default_factory=list)
    project_repository_mismatch: dict[str, Any] = field(default_factory=dict)
    sprints_without_recoverable_decisions: list[str] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    unrecognized_comment_markers: list[dict[str, Any]] = field(default_factory=list)
    approximate_values: list[dict[str, Any]] = field(default_factory=list)
    parity: dict[str, Any] = field(default_factory=dict)

    def record_not_imported(self, *, kind: str, ref: str, reason: str) -> None:
        self.records_not_imported.append({"kind": kind, "ref": ref, "reason": reason})

    def link_not_imported(self, *, kind: str, subject: str, target: str, reason: str) -> None:
        self.links_not_imported.append(
            {"kind": kind, "subject": subject, "target": target, "reason": reason}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_version": self.import_version,
            "schema_revision": self.schema_revision,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "source": self.source,
            "counts": self.counts,
            "discrepancies": {
                "expected_zero": self.expected_zero,
                "records_not_imported": self.records_not_imported,
                "links_not_imported": self.links_not_imported,
                "fields_without_a_column": self.fields_without_a_column,
                "extensions_keys": self.extensions_keys,
                "project_repository_mismatch": self.project_repository_mismatch,
                "sprints_without_recoverable_decisions": self.sprints_without_recoverable_decisions,
                "budget": self.budget,
                "unrecognized_comment_markers": self.unrecognized_comment_markers,
                "approximate_values": self.approximate_values,
            },
            "parity": self.parity,
        }

    @property
    def discrepancy_count(self) -> int:
        return (
            len(self.records_not_imported)
            + len(self.links_not_imported)
            + len(self.fields_without_a_column)
            + len(self.unrecognized_comment_markers)
            + len(self.budget.get("mismatches", []))
        )


def render(report: ImportReport) -> str:
    """The human half of the report.  Same facts, same run, no second computation."""
    lines = [
        (
            f"board import ({report.mode}), version {report.import_version}, "
            f"schema {report.schema_revision or 'unapplied'}"
        ),
        f"read at {report.generated_at}",
        "",
        "source:",
    ]
    lines += [f"  {name}: {value}" for name, value in sorted(report.source.items())]
    lines += ["", "rows per table:"]
    lines += [f"  {name}: {report.counts.get(name, 0)}" for name in TABLE_ORDER]
    if report.expected_zero:
        lines += ["", "expected zeroes (an empty table that is not a loss):"]
        lines += [f"  {item['table']}: {item['reason']}" for item in report.expected_zero]
    lines += ["", f"discrepancies: {report.discrepancy_count}"]
    if report.records_not_imported:
        lines += ["", f"records that did not land in the model ({len(report.records_not_imported)}):"]
        lines += [
            f"  {item['kind']} {item['ref']}: {item['reason']}" for item in report.records_not_imported
        ]
    if report.links_not_imported:
        lines += ["", f"links that did not land in the model ({len(report.links_not_imported)}):"]
        lines += [
            f"  {item['kind']} {item['subject']} -> {item['target']}: {item['reason']}"
            for item in report.links_not_imported
        ]
    if report.fields_without_a_column:
        lines += ["", "fields the schema gives no home (§8.6 and beyond):"]
        lines += [
            f"  {item['entity']}.{item['key']}: {item['rows']} row(s); {item['reason']}"
            for item in report.fields_without_a_column
        ]
    if report.extensions_keys:
        lines += ["", "metadata keys carried into tasks.extensions (§8.2), per key:"]
        lines += [f"  {item['key']}: {item['rows']} row(s)" for item in report.extensions_keys]
    mismatch = report.project_repository_mismatch
    if mismatch:
        lines += ["", "project/repository mismatch (§8.3):"]
        for name, values in sorted(mismatch.items()):
            lines.append(f"  {name.replace('_', ' ')}: {len(values)}")
            lines += [f"    {value}" for value in values]
    if report.unrecognized_comment_markers:
        lines += ["", "bracketed first lines outside §8.1's vocabulary (marker = NULL, body kept):"]
        lines += [
            f"  [{item['token']}]: {item['comments']} comment(s)"
            for item in report.unrecognized_comment_markers
        ]
    if report.budget:
        lines += ["", "budget (§8.7):"]
        lines.append(f"  counter total: {report.budget.get('counter_total')}")
        lines.append(f"  rows written:  {report.budget.get('rows')}")
        lines.append(f"  per-type sums agree: {not report.budget.get('mismatches')}")
        lines.append(f"  occurred_at approximate for {report.budget.get('approximate')} row(s)")
        for item in report.budget.get("mismatches", []):
            lines.append(f"  MISMATCH {item}")
    if report.sprints_without_recoverable_decisions:
        lines += [
            "",
            (
                "sprints closed without a recoverable decision document (§8.5): "
                f"{len(report.sprints_without_recoverable_decisions)}"
            ),
            "  " + ", ".join(report.sprints_without_recoverable_decisions),
        ]
    if report.approximate_values:
        lines += ["", "values the board does not carry, derived and marked approximate:"]
        lines += [
            f"  {item['field']}: {item['rows']} row(s); {item['reason']}"
            for item in report.approximate_values
        ]
    if report.parity:
        lines += ["", f"parity: {'PASS' if report.parity.get('ok') else 'FAIL'}"]
        for axis in ("counts", "identifiers", "links", "content"):
            checks = report.parity.get(axis, [])
            failed = [check for check in checks if not check.get("ok")]
            lines.append(f"  {axis}: {len(checks)} check(s), {len(failed)} failed")
            lines += [f"    FAIL {check['name']}: {check.get('detail')}" for check in failed]
    return "\n".join(lines) + "\n"


# --- the mapping (§8) ----------------------------------------------------------------------


@dataclass
class ImportPlan:
    """Every row the import would write, plus the report that explains what it would not.

    Two of the plan's columns are not the schema's: ``sprint_repositories.path`` and
    ``sprints.__resume`` stand in for identity columns PostgreSQL generates.  :func:`apply`
    resolves both after the rows they name exist; a dry run never has to, which is why it needs
    no database at all.
    """

    rows: dict[str, list[dict[str, Any]]]
    report: ImportReport


def _when(value: Any) -> datetime | None:
    number = _positive_int(value)
    return datetime.fromtimestamp(number, tz=UTC) if number else None


def _required_when(value: Any, fallback: datetime) -> datetime:
    return _when(value) or fallback


def _task_number_of(ref: str) -> int | None:
    _, separator, tail = ref.rpartition("-")
    if not separator or not tail.isdigit():
        return None
    return int(tail)


def _sprint_number_of(ref: str) -> int | None:
    tail = ref.removeprefix(SPRINT_REFERENCE_PREFIX)
    return int(tail) if tail.isdigit() else None


def plan(source: BoardSource, *, thresholds: dict[str, int] | None = None) -> ImportPlan:
    """Turn one read board into the rows of §3 and the report of §8.  Pure: no I/O, no clock."""
    report = ImportReport(generated_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"))
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_ORDER}

    product_rows = [row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == PRODUCT_TYPE]
    issue_rows = [row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == ISSUE_TYPE]
    card_rows = [
        row
        for row in source.pipeline
        if row.meta.get(META_RECORD_TYPE) not in (PRODUCT_TYPE, ISSUE_TYPE)
    ]

    report.source = {
        "pipeline_rows": len(source.pipeline),
        "pipeline_product_rows": len(product_rows),
        "pipeline_issue_rows": len(issue_rows),
        "pipeline_card_rows": len(card_rows),
        "pipeline_comments": sum(len(row.comments) for row in source.pipeline),
        "sprint_rows": len(source.sprints),
        "sprint_comments": sum(len(row.comments) for row in source.sprints),
        "registry_projects": len(source.registry),
        "audit_budget_records": len(source.budget_records),
        "transaction_documents": len(source.transaction_documents),
    }

    _plan_registry(source, card_rows, product_rows, rows, report)
    products = _plan_products(product_rows, rows, report)
    issues = _plan_issues(source, issue_rows, products, rows, report)
    sprints = _plan_sprints(source, products, issues, rows, report)
    tasks = _plan_tasks(source, card_rows, sprints, products, rows, report)
    _link_sprint_cursors(source, sprints, tasks, report)
    _plan_budget(source, sprints, rows, report, thresholds=thresholds)
    _plan_comments(source, tasks, sprints, issue_rows, product_rows, rows, report)
    _plan_decisions(source, sprints, tasks, rows, report)

    report.expected_zero = [
        {
            "table": "task_issues",
            "reason": "no source field exists: no metadata key holds a card's issue refs and "
            "board/kanboard.py:_card builds every Card with an empty issue_refs (§8.4, gap 1). "
            "A card's issue association is reachable through its sprint.",
        },
        {
            "table": "board_events",
            "reason": "the typed events live in the local audit journal "
            "<data>/board/events.ndjson, which is not board data and is not one of this card's "
            "sources. The journal is untouched and still holds all 25605 records; importing it "
            "is a later card. The only journal rows this import reads are the budget charges "
            "§8.7 names, and they arrive as sprint_budget_events, not as events.",
        },
    ]
    if not rows["sprint_decisions"]:
        report.expected_zero.append(
            {
                "table": "sprint_decisions",
                "reason": "no staged transaction document survives under "
                "<data>/board/product-issue-transactions/ (§8.5); the decisions are not invented, "
                "and every closed sprint is listed under "
                "'sprints closed without a recoverable decision document'.",
            }
        )
    report.counts = {name: len(rows[name]) for name in TABLE_ORDER}
    return ImportPlan(rows=rows, report=report)


def _plan_registry(
    source: BoardSource,
    card_rows: list[SourceRow],
    product_rows: list[SourceRow],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> None:
    """§3.1 and §8.3: projects and repositories to the extent of existing links, losing nothing."""
    registry = {entry.project_id: entry for entry in source.registry}
    referenced: set[str] = set()
    for row in card_rows:
        if project := _text(row.meta.get("project")):
            referenced.add(project)
    for row in source.sprints:
        referenced.update(_json_list(row.meta.get("sprint_reservations")))
    for row in product_rows:
        referenced.update(_json_list(row.meta.get(META_PRODUCT_PROJECTS)))

    for project_id in sorted(set(registry) | referenced):
        entry = registry.get(project_id)
        rows["projects"].append(
            {
                "project_id": project_id,
                "enabled": entry.enabled if entry else True,
                "plane": entry.plane if entry else "project",
                "adapter": entry.adapter if entry else None,
                "orca_binding": entry.orca_binding if entry else None,
                "registry_present": entry is not None,
            }
        )

    # §8.3 step 1-2: one row per distinct path, whether or not the registry claims it.
    by_path: dict[str, dict[str, Any]] = {}
    for entry in source.registry:
        if entry.repo and entry.repo not in by_path:
            by_path[entry.repo] = {
                "project_id": entry.project_id,
                "path": entry.repo,
                "remote": entry.remote,
                "default_branch": entry.default_branch,
                "role": "primary",
            }
    for entry in source.registry:
        for root in entry.curator_roots:
            if root not in by_path:
                by_path[root] = {
                    "project_id": entry.project_id,
                    "path": root,
                    "remote": None,
                    "default_branch": entry.default_branch,
                    "role": "curator_root",
                }
    unbound: list[str] = []
    not_a_path: list[str] = []
    sprint_paths: set[str] = set()
    for row in source.sprints:
        for path in _json_list(row.meta.get("sprint_repositories")):
            sprint_paths.add(path)
            if not path.startswith("/"):
                not_a_path.append(path)
            if path not in by_path:
                unbound.append(path)
                by_path[path] = {
                    "project_id": None,
                    "path": path,
                    "remote": None,
                    "default_branch": "main",
                    "role": "primary",
                }
    rows["repositories"] = [by_path[path] for path in sorted(by_path)]

    report.project_repository_mismatch = {
        "repository_paths_without_project": sorted(set(unbound)),
        "project_ids_without_registry_file": sorted(referenced - set(registry)),
        "registry_repositories_no_sprint_references": sorted(
            entry.repo for entry in source.registry if entry.repo and entry.repo not in sprint_paths
        ),
        "repository_entries_that_are_not_absolute_paths": sorted(set(not_a_path)),
    }


def _plan_products(
    product_rows: list[SourceRow], rows: dict[str, list[dict[str, Any]]], report: ImportReport
) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    known_projects = {row["project_id"] for row in rows["projects"]}
    for row in sorted(product_rows, key=lambda item: item.ref):
        product_id = _text(row.meta.get(META_PRODUCT_ID))
        if not product_id:
            report.record_not_imported(
                kind="product",
                ref=row.ref,
                reason="the row carries no product_id, and products.product_id is the primary key",
            )
            continue
        if product_id in products:
            report.record_not_imported(
                kind="product",
                ref=f"{row.ref} (kanboard task {row.task_id})",
                reason=f"a second board row claims product_id {product_id!r}, which is a "
                "primary key; the first row was kept",
            )
            continue
        created = _required_when(row.raw.get("date_creation"), datetime.fromtimestamp(0, tz=UTC))
        products[product_id] = {
            "product_id": product_id,
            "title": _text(row.raw.get("title")) or product_id,
            "description": _text(row.raw.get("description")),
            "state": "archived" if row.archived else "active",
            "created_at": created,
            "updated_at": _required_when(row.raw.get("date_modification"), created),
        }
        for project_id in _json_list(row.meta.get(META_PRODUCT_PROJECTS)):
            if project_id not in known_projects:
                report.link_not_imported(
                    kind="product_projects",
                    subject=f"product:{product_id}",
                    target=project_id,
                    reason="no projects row exists for this id",
                )
                continue
            rows["product_projects"].append({"product_id": product_id, "project_id": project_id})
    rows["products"] = [products[key] for key in sorted(products)]
    return products


def _plan_issues(
    source: BoardSource,
    issue_rows: list[SourceRow],
    products: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> dict[str, dict[str, Any]]:
    issues: dict[str, dict[str, Any]] = {}
    stray_keys: Counter[str] = Counter()
    misplaced_lanes = 0
    for row in sorted(issue_rows, key=lambda item: item.ref):
        issue_id = row.ref.removeprefix("issue:")
        if not issue_id or issue_id == row.ref:
            report.record_not_imported(
                kind="issue", ref=row.ref, reason="the reference is not of the form issue:<id>"
            )
            continue
        product_id = _text(row.meta.get(META_ISSUE_PRODUCT))
        kind = _text(row.meta.get(META_ISSUE_KIND))
        priority = _text(row.meta.get(META_ISSUE_PRIORITY))
        reason = _null_if_empty(row.meta.get(META_ISSUE_CLOSED_REASON))
        closed = row.archived
        refusal = None
        if product_id not in products:
            refusal = f"issue_product {product_id!r} names no imported product (issues.product_id is NOT NULL)"
        elif kind not in ISSUE_KINDS:
            refusal = f"issue_kind {kind!r} is outside the CHECK vocabulary {sorted(ISSUE_KINDS)}"
        elif priority not in ISSUE_PRIORITIES:
            refusal = f"issue_priority {priority!r} is outside the CHECK vocabulary {sorted(ISSUE_PRIORITIES)}"
        elif reason is not None and reason not in ISSUE_CLOSE_REASONS:
            refusal = f"issue_closed_reason {reason!r} is outside the CHECK vocabulary"
        elif closed != (reason is not None):
            refusal = (
                "issue_close_reason_matches_state refuses this row: "
                f"is_active={0 if closed else 1} with close reason {reason!r}"
            )
        elif not _text(row.raw.get("title")):
            refusal = "issues.title carries CHECK (title <> '')"
        if refusal is not None:
            report.record_not_imported(kind="issue", ref=row.ref, reason=refusal)
            continue
        created = _required_when(row.raw.get("date_creation"), datetime.fromtimestamp(0, tz=UTC))
        issues[issue_id] = {
            "issue_id": issue_id,
            "product_id": product_id,
            "title": _text(row.raw.get("title")),
            "description": _text(row.raw.get("description")),
            "issue_kind": kind,
            "priority": priority,
            "state": "closed" if closed else "open",
            "close_reason": reason,
            "created_at": created,
            "updated_at": _required_when(row.raw.get("date_modification"), created),
        }
        for key in row.meta:
            if key not in {
                META_RECORD_TYPE,
                META_ISSUE_PRODUCT,
                META_ISSUE_KIND,
                META_ISSUE_PRIORITY,
                META_ISSUE_CLOSED_REASON,
            }:
                stray_keys[key] += 1
        lane = source.pipeline_swimlanes.get(_positive_int(row.raw.get("swimlane_id")) or -1, "")
        if lane and lane != product_lane_name(product_id):
            misplaced_lanes += 1
    rows["issues"] = [issues[key] for key in sorted(issues)]
    for key, count in sorted(stray_keys.items()):
        report.fields_without_a_column.append(
            {
                "entity": "issue",
                "key": key,
                "rows": count,
                    "reason": (
                    "the issues table has no extensions column, so a metadata key the schema "
                    "does not name has nowhere to land; §8.2's extensions bag is tasks-only"
                ),
            }
        )
    if misplaced_lanes:
        report.fields_without_a_column.append(
            {
                "entity": "issue",
                "key": "swimlane",
                "rows": misplaced_lanes,
                "reason": "the row sits in a lane that is not its product's, and unlike tasks "
                "(§8.6) the issues table has no extensions column to keep the observed lane in",
            }
        )
    return issues


def _one_row_per_reference(rows: tuple[SourceRow, ...]) -> list[SourceRow]:
    """One row per reference, the live one winning over an archived duplicate.

    Kanboard lets two rows carry one reference — `references.py` documents the allocator defect
    that produced them — and §3 makes the reference a primary key.  Every pass over a board has
    to pick the *same* survivor or the plan and the parity checks describe different sprints, so
    the rule lives here and nowhere else.
    """
    chosen: dict[str, SourceRow] = {}
    for row in sorted(rows, key=lambda item: (item.ref, item.archived, item.task_id)):
        chosen.setdefault(row.ref, row)
    return [chosen[ref] for ref in sorted(chosen)]


def _plan_sprints(
    source: BoardSource,
    products: dict[str, dict[str, Any]],
    issues: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> dict[int, dict[str, Any]]:
    known_projects = {row["project_id"] for row in rows["projects"]}
    repository_paths = {row["path"] for row in rows["repositories"]}
    sprints: dict[int, dict[str, Any]] = {}
    approximate_closed_at = 0
    live_reservation: dict[str, int] = {}
    for row in sorted(source.sprints, key=lambda item: (item.ref, item.archived, item.task_id)):
        number = _sprint_number_of(row.ref)
        if number is None:
            report.record_not_imported(
                kind="sprint", ref=row.ref, reason="the reference is not of the form sprint:<N>"
            )
            continue
        if number in sprints:
            report.record_not_imported(
                kind="sprint",
                ref=f"{row.ref} (kanboard task {row.task_id})",
                reason="a second board row claims this sprint number; §3.3 makes it a primary "
                "key and the live row was kept",
            )
            continue
        status = row.meta.get("sprint_status")
        if status not in SPRINT_STATUSES:
            status = "open"
        created = _required_when(row.raw.get("date_creation"), datetime.fromtimestamp(0, tz=UTC))
        updated = _required_when(row.raw.get("date_modification"), created)
        closed_at = None
        if status != "open":
            closed_at = updated
            approximate_closed_at += 1
        product_id = row.meta.get("sprint_product") or ""
        if product_id and product_id not in products:
            report.link_not_imported(
                kind="sprint.product",
                subject=row.ref,
                target=product_id,
                reason="no products row exists for this id; sprints.product_id stays NULL",
            )
            product_id = ""
        pins = stored_executors(row.meta)
        observer = parse_observer(row.meta[OBSERVER_FIELD]) if OBSERVER_FIELD in row.meta else None
        if OBSERVER_FIELD in row.meta and observer is None:
            report.link_not_imported(
                kind="sprint.observer",
                subject=row.ref,
                target=row.meta[OBSERVER_FIELD][:60],
                reason="the value is not one of the four tagged observer forms; the column stays NULL",
            )
        sprints[number] = {
            "sprint_number": number,
            "goal": row.meta.get("sprint_goal", ""),
            "definition_of_done": row.meta.get("sprint_definition_of_done", ""),
            "product_id": product_id or None,
            "status": status,
            "observer": observer,
            "worker_pin": _pin(pins, "worker"),
            "reviewer_pin": _pin(pins, "reviewer"),
            "current_task_ref": None,
            "resume_id": None,
            "close_reason": None,
            "closeout_document": None,
            "source_audit": _source_audit(row.meta.get("sprint_source_audit")),
            "created_at": created,
            "updated_at": updated,
            "closed_at": closed_at,
        }
        for path in _json_list(row.meta.get("sprint_repositories")):
            if path not in repository_paths:  # pragma: no cover - _plan_registry created them all
                report.link_not_imported(
                    kind="sprint_repositories",
                    subject=row.ref,
                    target=path,
                    reason="no repositories row exists for this path",
                )
                continue
            rows["sprint_repositories"].append({"sprint_number": number, "path": path})
        for value in _json_list(row.meta.get("sprint_issues")):
            issue_id = value.removeprefix("issue:")
            if issue_id not in issues:
                report.link_not_imported(
                    kind="sprint_issues",
                    subject=row.ref,
                    target=value,
                    reason="the issue was not imported, and sprint_issues.issue_id is a foreign key",
                )
                continue
            rows["sprint_issues"].append({"sprint_number": number, "issue_id": issue_id})
        reserved = status == "open"
        for project_id in _json_list(row.meta.get("sprint_reservations")):
            if project_id not in known_projects:  # pragma: no cover - _plan_registry created them
                report.link_not_imported(
                    kind="sprint_projects",
                    subject=row.ref,
                    target=project_id,
                    reason="no projects row exists for this id",
                )
                continue
            if reserved and project_id in live_reservation:
                report.link_not_imported(
                    kind="sprint_projects",
                    subject=row.ref,
                    target=project_id,
                    reason="sprint_projects_one_live_reservation already holds this project for "
                    f"sprint:{live_reservation[project_id]}; §4 allows exactly one live reservation",
                )
                continue
            if reserved:
                live_reservation[project_id] = number
            rows["sprint_projects"].append(
                {
                    "sprint_number": number,
                    "project_id": project_id,
                    "reserved": reserved,
                    "reserved_at": created,
                    "released_at": None if reserved else closed_at,
                }
            )
        resume = _resume(row.meta.get("sprint_resume"))
        if resume is not None:
            recorded = _timestamp(resume.get("recorded_at")) or updated
            rows["sprint_resumes"].append(
                {
                    "sprint_number": number,
                    **{field_name: resume[field_name] for field_name in RESUME_FIELDS},
                    "recorded_at": recorded,
                }
            )
        elif row.meta.get("sprint_resume"):
            report.record_not_imported(
                kind="sprint_resume",
                ref=row.ref,
                reason="the stored resume is missing one of the six RESUME_FIELDS, all of which "
                "are NOT NULL columns; the raw value stays on the Kanboard row",
            )
    rows["sprints"] = [sprints[key] for key in sorted(sprints)]
    if approximate_closed_at:
        report.approximate_values.append(
            {
                "field": "sprints.closed_at",
                "rows": approximate_closed_at,
                "reason": "sprint_closed_has_time requires a time for every non-open sprint and "
                "the board stores none; the row's date_modification is used and is approximate",
            }
        )
    for name, why in (
        (
            "sprints.close_reason",
            (
                "lives in the close event payload, not on the board row (§9); it is left NULL "
                "rather than reconstructed"
            ),
        ),
        (
            "sprints.closeout_document",
            "the knowledge closeout's path is not on the board row; it is left NULL",
        ),
        ("tasks.claimed_at", "hard-coded NULL in tasks._normalize (§8.6, gap 3)"),
        (
            "tasks.resolved_worker_family / resolved_review_family",
            "never stored by any writer (§8.6, gap 4)",
        ),
        (
            "task_comments.actor_id / sprint_comments.actor_id",
            (
                "every Kanboard comment on this board carries user_id 0 and a null username, so "
                "the actor's identity is not recoverable; actor_role comes from a role marker only"
            ),
        ),
        (
            "task_comments.request_id / sprint_comments.request_id",
            (
                "a comment on the board carries no request id; the namespace is the audit "
                "journal's, which this import does not read for events"
            ),
        ),
    ):
        report.fields_without_a_column.append(
            {"entity": "structurally absent", "key": name, "rows": 0, "reason": why}
        )
    return sprints


def _pin(pins: dict[str, dict[str, Any]], role: str) -> str | None:
    state = pins.get(role) or {}
    return str(state["profile"]) if state.get("state") == EXECUTOR_PINNED else None


def _timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _plan_tasks(
    source: BoardSource,
    card_rows: list[SourceRow],
    sprints: dict[int, dict[str, Any]],
    products: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> dict[str, dict[str, Any]]:
    known_projects = {row["project_id"] for row in rows["projects"]}
    lane_of_project = {}
    for link in rows["product_projects"]:
        lane_of_project.setdefault(link["project_id"], product_lane_name(link["product_id"]))
    tasks: dict[str, dict[str, Any]] = {}
    extension_keys: Counter[str] = Counter()
    pending_links: list[tuple[str, str, str]] = []
    # Deterministic order, and the live row of a duplicated reference wins over an archived one.
    ordered = sorted(card_rows, key=lambda row: (row.ref, row.archived, row.task_id))
    for row in ordered:
        ref = row.ref
        project_id = _text(row.meta.get("project"))
        task_number = _task_number_of(ref)
        task_type = _text(row.meta.get("task_type"))
        column = source.pipeline_columns.get(_positive_int(row.raw.get("column_id")) or -1, "")
        refusal = None
        if not ref:
            refusal = "the row carries no reference, and tasks.task_ref is the primary key"
        elif ref in tasks:
            refusal = (
                f"a second board row claims reference {ref!r}; tasks.task_ref is the primary key "
                "and the live row was kept"
            )
        elif task_number is None:
            refusal = "the reference does not end in -<number>, so UNIQUE (project_id, task_number) has no value"
        elif not project_id:
            refusal = "the row carries no project metadata, and tasks.project_id is NOT NULL"
        elif project_id not in known_projects:  # pragma: no cover - _plan_registry created them
            refusal = f"project {project_id!r} has no projects row"
        elif task_type not in _TASK_TYPES:
            refusal = f"task_type {task_type!r} is outside the CHECK vocabulary {sorted(_TASK_TYPES)}"
        elif column not in _STATE_BY_COLUMN:
            refusal = f"the row sits in column {column!r}, which _STATE_BY_COLUMN does not map to a state"
        elif not _text(row.raw.get("title")):
            refusal = "tasks.title carries CHECK (title <> '')"
        if refusal is not None:
            report.record_not_imported(
                kind="card",
                ref=(f"{ref} (kanboard task {row.task_id})" if ref in tasks else ref)
                or f"task_kanboard_{row.task_id}",
                reason=refusal,
            )
            continue

        sprint_ref = _null_if_empty(row.meta.get("sprint_ref"))
        sprint_number = _sprint_number_of(sprint_ref) if sprint_ref else None
        if sprint_ref and (sprint_number is None or sprint_number not in sprints):
            report.link_not_imported(
                kind="tasks.sprint",
                subject=ref,
                target=sprint_ref,
                reason="the sprint was not imported; tasks.sprint_number stays NULL",
            )
            sprint_number = None

        extensions = {
            key: value for key, value in row.meta.items() if key not in TASK_KNOWN_METADATA
        }
        # §3.12: a value a closed vocabulary rejects is normalized away on read, and the retired
        # spelling must stay queryable rather than disappear.  The three enum columns are the only
        # places a *known* key can lose its stored value, so each of them keeps the raw string.
        for key, allowed in (
            ("complexity", _COMPLEXITIES),
            ("family_preference", _FAMILY_PREFERENCES),
            ("codex_launch_mode", CODEX_LAUNCH_MODES),
        ):
            raw_value = _null_if_empty(row.meta.get(key))
            if raw_value is not None and raw_value not in allowed:
                extensions[key] = raw_value
        lane = source.pipeline_swimlanes.get(_positive_int(row.raw.get("swimlane_id")) or -1, "")
        if lane and lane != lane_of_project.get(project_id):
            # §8.6: keep the observed lane where it disagrees with the product-derived one, so
            # product_lanes.py's finding survives the migration instead of being normalized away.
            extensions["swimlane"] = lane
        for key in extensions:
            extension_keys[key] += 1

        created = _required_when(row.raw.get("date_creation"), datetime.fromtimestamp(0, tz=UTC))
        tasks[ref] = {
            "task_ref": ref,
            "project_id": project_id,
            "task_number": task_number,
            "title": _text(row.raw.get("title")),
            "description": _text(row.raw.get("description")),
            "task_type": task_type,
            "state": _STATE_BY_COLUMN[column],
            "archived": row.archived,
            "position": _nonnegative_int(row.raw.get("position")),
            "sprint_number": sprint_number,
            "claim_worker": _null_if_empty(row.meta.get("claim")),
            "claimed_at": None,
            "slug": _null_if_empty(row.meta.get("slug")),
            "base_branch": _null_if_empty(row.meta.get("base_branch")),
            "seed_ref": _null_if_empty(row.meta.get("seed_ref")),
            "complexity": _enum_or_default(row.meta.get("complexity"), _COMPLEXITIES, "standard"),
            "family_preference": _enum_or_default(
                row.meta.get("family_preference"), _FAMILY_PREFERENCES, "auto"
            ),
            "head_override": _null_if_empty(row.meta.get("head")),
            "review_head_override": _null_if_empty(row.meta.get("review_head")),
            "resolved_worker_head": _null_if_empty(row.meta.get("resolved_head")),
            "resolved_worker_family": None,
            "resolved_review_head": _null_if_empty(row.meta.get("resolved_review_head")),
            "resolved_review_family": None,
            "routing_reason": _null_if_empty(row.meta.get("routing_reason")),
            "quota_snapshot_at": _timestamp(row.meta.get("quota_snapshot_at")),
            "codex_launch_mode": _enum_or_none(row.meta.get("codex_launch_mode"), CODEX_LAUNCH_MODES),
            "retry_same": _nonnegative_int(row.meta.get("retry_same")),
            "retry_switch": _nonnegative_int(row.meta.get("retry_switch")),
            "extensions": {"kanboard": extensions} if extensions else {},
            "created_at": created,
            "updated_at": _required_when(row.raw.get("date_modification"), created),
        }
        for ordinal, head in enumerate(_split_heads(row.meta.get("retry_heads"))):
            rows["task_retry_heads"].append({"task_ref": ref, "ordinal": ordinal, "head": head})
        if blocked_by := _null_if_empty(row.meta.get("blocked_by")):
            pending_links.append(("task_dependencies", ref, blocked_by))
        if supersedes := _null_if_empty(row.meta.get("supersedes")):
            pending_links.append(("task_supersessions", ref, supersedes))
        raw_quota = _null_if_empty(row.meta.get("quota_snapshot_at"))
        if raw_quota and tasks[ref]["quota_snapshot_at"] is None:
                report.link_not_imported(
                    kind="tasks.quota_snapshot_at",
                    subject=ref,
                    target=raw_quota,
                    reason="the stored value is not a timestamp the timestamptz column accepts",
                )

    rows["tasks"] = [tasks[key] for key in sorted(tasks)]
    for table, subject, target in pending_links:
        if target not in tasks:
            report.link_not_imported(
                kind=table,
                subject=subject,
                target=target,
                reason="the referenced card is not on the board, and the column is a foreign key "
                "into tasks(task_ref)",
            )
            continue
        if subject == target:
            report.link_not_imported(
                kind=table,
                subject=subject,
                target=target,
                reason="a card may not reference itself (no_self_dependency / no_self_supersession)",
            )
            continue
        if table == "task_dependencies":
            rows["task_dependencies"].append({"task_ref": subject, "depends_on": target})
        else:
            rows["task_supersessions"].append(
                {
                    "task_ref": subject,
                    "supersedes": target,
                    "recorded_at": tasks[subject]["updated_at"],
                }
            )
    report.extensions_keys = [
        {"key": key, "rows": count, "where": "tasks.extensions.kanboard"}
        for key, count in sorted(extension_keys.items(), key=lambda item: (-item[1], item[0]))
    ]
    return tasks


def _link_sprint_cursors(
    source: BoardSource,
    sprints: dict[int, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    report: ImportReport,
) -> None:
    """§3.3's scoped cursor: a sprint's current task must be a card of that sprint."""
    for row in _one_row_per_reference(source.sprints):
        number = _sprint_number_of(row.ref)
        if number is None or number not in sprints:
            continue
        current = _null_if_empty(row.meta.get("sprint_current_task"))
        if not current:
            continue
        card = tasks.get(current)
        if card is None or card["sprint_number"] != number:
            report.link_not_imported(
                kind="sprints.current_task_ref",
                subject=row.ref,
                target=current,
                reason="sprint_current_task_is_in_this_sprint scopes the cursor to a card this "
                "sprint holds, and this card "
                + ("was not imported" if card is None else "belongs to another sprint")
                + "; the cursor stays NULL",
            )
            continue
        sprints[number]["current_task_ref"] = current


def _plan_budget(
    source: BoardSource,
    sprints: dict[int, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
    *,
    thresholds: dict[str, int] | None = None,
) -> None:
    """§8.7: the two counter objects become rows, and the sums have to reconcile exactly."""
    journal: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in source.budget_records:
        ref = _text(record.get("ref"))
        event_type = _text((record.get("payload") or {}).get("event_type"))
        journal.setdefault((ref, event_type), []).append(record)
    for bucket in journal.values():
        bucket.sort(key=lambda record: (_text(record.get("occurred_at")), _text(record.get("event_id"))))

    counter_total = 0
    written = 0
    approximate = 0
    mismatches: list[dict[str, Any]] = []
    per_type: Counter[str] = Counter()
    claimed: set[str] = set()
    for row in _one_row_per_reference(source.sprints):
        number = _sprint_number_of(row.ref)
        if number is None or number not in sprints:
            continue
        budget = _budget(
            row.meta.get("sprint_budget"), thresholds, row.meta.get(BUDGET_UNCHARGED_FIELD)
        )
        counts = {**budget["by_type"], **budget["uncharged"]}
        for event_type in BUDGET_RECORDED_EVENT_TYPES:
            wanted = int(counts.get(event_type, 0))
            counter_total += wanted
            per_type[event_type] += wanted
            available = journal.get((row.ref, event_type), [])
            if len(available) > wanted:
                mismatches.append(
                    {
                        "sprint": row.ref,
                        "event_type": event_type,
                        "counter": wanted,
                        "audit_journal": len(available),
                        "detail": "the journal holds more charges than the counter records; the "
                        "counter is authoritative for the totals §8.7 must reconcile, so the "
                        "surplus journal records are not imported",
                    }
                )
            for index in range(wanted):
                record = available[index] if index < len(available) else None
                if record is not None:
                    request_id = _text(record.get("request_id")) or (
                        f"import:budget:{row.ref}:{event_type}:{index}"
                    )
                    occurred = _timestamp(record.get("occurred_at")) or sprints[number]["updated_at"]
                    reason = f"imported from audit journal record {_text(record.get('event_id'))}"
                else:
                    request_id = f"import:budget:{row.ref}:{event_type}:{index}"
                    occurred = sprints[number]["updated_at"]
                    reason = (
                        "imported from the sprint's counter; the audit journal holds no record "
                        "for this charge, so occurred_at is approximate"
                    )
                    approximate += 1
                if request_id in claimed:
                    request_id = f"{request_id}:{index}"
                claimed.add(request_id)
                rows["requests"].append(
                    {
                        "request_id": request_id,
                        "operation": "sprint.budget",
                        "intent": {
                            "sprint": row.ref,
                            "event_type": event_type,
                            "imported_by": f"board-import:{IMPORT_VERSION}",
                        },
                        "status": "committed",
                        "protocol": True,
                        "entity_kind": "sprint",
                        "ref": row.ref,
                        "created_at": occurred,
                        "settled_at": occurred,
                    }
                )
                rows["sprint_budget_events"].append(
                    {
                        "sprint_number": number,
                        "event_type": event_type,
                        "charged": event_type not in BUDGET_UNCHARGED_EVENT_TYPES,
                        "task_ref": None,
                        "reason": reason,
                        "request_id": request_id,
                        "occurred_at": occurred,
                    }
                )
                written += 1
    report.budget = {
        "counter_total": counter_total,
        "rows": written,
        "per_type": dict(sorted(per_type.items())),
        "approximate": approximate,
        "mismatches": mismatches,
        "sums_reconcile": counter_total == written,
    }
    if approximate:
        report.approximate_values.append(
            {
                "field": "sprint_budget_events.occurred_at",
                "rows": approximate,
                "reason": "§8.7: no audit-journal record carries this charge, so the sprint's "
                "updated_at is used and the timestamp is approximate, not observed",
            }
        )
    if written:
        report.fields_without_a_column.append(
            {
                "entity": "sprint_budget_events",
                "key": "task_ref",
                "rows": written,
                "reason": "a budget_recorded journal record names the sprint, never the card, and "
                "the counter names neither; the nullable column stays NULL (MATCH SIMPLE skips "
                "budget_card_is_in_this_sprint for it)",
            }
        )


def _plan_comments(
    source: BoardSource,
    tasks: dict[str, dict[str, Any]],
    sprints: dict[int, dict[str, Any]],
    issue_rows: list[SourceRow],
    product_rows: list[SourceRow],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> None:
    """§8.1's marker rule, applied once, to every comment on both boards."""
    unrecognized: Counter[str] = Counter()
    homeless = Counter()
    for row in source.pipeline:
        if row.meta.get(META_RECORD_TYPE) in (PRODUCT_TYPE, ISSUE_TYPE):
            if row.comments:
                homeless[row.meta[META_RECORD_TYPE]] += len(row.comments)
            continue
        if row.ref not in tasks or tasks[row.ref] is None:
            continue
        # A duplicated reference keeps one card; a comment of the row that lost is still a comment
        # of that reference, so it is imported rather than dropped.
        for comment in row.comments:
            body = _text(comment.get("comment"))
            marker, text = parse_marker(body)
            if marker is None and (token := marker_token(body)) is not None:
                unrecognized[token] += 1
            rows["task_comments"].append(
                {
                    "task_ref": row.ref,
                    "marker": marker,
                    "body": text,
                    "actor_role": marker if marker in MARKER_ROLES else None,
                    "actor_id": None,
                    "request_id": None,
                    "created_at": _required_when(
                        comment.get("date_creation"), tasks[row.ref]["updated_at"]
                    ),
                }
            )
    kept_sprint_rows = {row.task_id for row in _one_row_per_reference(source.sprints)}
    for row in source.sprints:
        number = _sprint_number_of(row.ref)
        if number is None or number not in sprints:
            continue
        if row.task_id not in kept_sprint_rows:
            if not row.comments:
                continue
            # A duplicated *card* reference is one card seen twice, so its comments merge above.
            # A duplicated *sprint* reference is two different sprints that were handed the same
            # number, and merging their journals would invent a sprint that never ran.  The
            # losing row's comments are named here instead.
            report.record_not_imported(
                kind="sprint comment",
                ref=f"{len(row.comments)} comment(s) on the archived duplicate of {row.ref}",
                reason="two board rows carry this sprint reference and §3.3 makes the number a "
                "primary key; only the live row is imported, and merging the other row's journal "
                "into it would attribute one sprint's record to another",
            )
            continue
        for comment in row.comments:
            body = _text(comment.get("comment"))
            marker, text = parse_marker(body)
            if marker is None and (token := marker_token(body)) is not None:
                unrecognized[token] += 1
            rows["sprint_comments"].append(
                {
                    "sprint_number": number,
                    "marker": marker,
                    "body": text,
                    "actor_role": marker if marker in MARKER_ROLES else None,
                    "actor_id": None,
                    "request_id": None,
                    "created_at": _required_when(
                        comment.get("date_creation"), sprints[number]["updated_at"]
                    ),
                }
            )
    report.unrecognized_comment_markers = [
        {"token": token, "comments": count}
        for token, count in sorted(unrecognized.items(), key=lambda item: (-item[1], item[0]))
    ]
    for record_type, count in sorted(homeless.items()):
        report.record_not_imported(
            kind=f"{record_type} comment",
            ref=f"{count} comment(s) across the board's {record_type} rows",
            reason="§3.7 declares exactly two comment tables, task_comments and sprint_comments, "
            "and both are foreign-keyed to their entity. A Product or an Issue is neither, so a "
            "comment on one has no table. This is a gap §2.6 does not list and the schema has to "
            "close before cutover; the comments stay on the Kanboard rows and are lost by any "
            "reader that moves to SQL.",
        )


def _plan_decisions(
    source: BoardSource,
    sprints: dict[int, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> None:
    """§8.5: materialize what a surviving transaction document holds, and invent nothing else.

    The documents are the staged sprint-close transactions `ProductIssueTransaction` writes under
    `<data>/board/product-issue-transactions/`.  One carries the close's whole decision plan under
    ``event.payload.decisions``, which is `sprint_close.plan_close_decisions`' own output — the
    same two lists, the same vocabularies, one entry per declared issue and per remaining card.
    Their lifecycle is the transaction store's, so coverage is partial by construction and a
    sprint with no surviving document is *named*, never reconstructed.
    """
    declared_issues = {
        (link["sprint_number"], link["issue_id"]) for link in rows["sprint_issues"]
    }
    recovered: set[int] = set()
    claimed: set[str] = set()
    for document in source.transaction_documents:
        event = document.get("event")
        if not isinstance(event, dict):
            continue
        number = _sprint_number_of(_text(event.get("ref")))
        if number is None or number not in sprints:
            continue
        payload = event.get("payload")
        plan_of = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(plan_of, dict):
            continue
        request_id = _text(document.get("request_id"))
        decided_at = _timestamp(event.get("occurred_at")) or sprints[number]["updated_at"]
        entries: list[dict[str, Any]] = []
        for entry in plan_of.get("issues") or []:
            if not isinstance(entry, dict):
                continue
            issue_id = _text(entry.get("ref")).removeprefix("issue:")
            if (number, issue_id) not in declared_issues:
                report.link_not_imported(
                    kind="sprint_decisions.issue",
                    subject=f"sprint:{number}",
                    target=_text(entry.get("ref")),
                    reason="decided_issue_is_declared_by_this_sprint requires the sprint to have "
                    "declared the issue, and this sprint_issues link was not imported",
                )
                continue
            entries.append(
                {
                    "subject_kind": "issue",
                    "issue_id": issue_id,
                    "task_ref": None,
                    "verdict": _text(entry.get("verdict")),
                    "actual": _null_if_empty(entry.get("actual")),
                    "reason": _text(entry.get("reason")),
                }
            )
        for entry in plan_of.get("cards") or []:
            if not isinstance(entry, dict):
                continue
            task_ref = _text(entry.get("ref"))
            card = tasks.get(task_ref)
            if card is None or card["sprint_number"] != number:
                report.link_not_imported(
                    kind="sprint_decisions.card",
                    subject=f"sprint:{number}",
                    target=task_ref,
                    reason="decided_card_is_in_this_sprint scopes the decision to a card this "
                    "sprint holds, and this card is not one",
                )
                continue
            entries.append(
                {
                    "subject_kind": "card",
                    "issue_id": None,
                    "task_ref": task_ref,
                    "verdict": _text(entry.get("verdict")),
                    "actual": _null_if_empty(entry.get("actual")),
                    "reason": _text(entry.get("reason")),
                }
            )
        if not entries:
            continue
        if request_id and request_id not in claimed:
            claimed.add(request_id)
            rows["requests"].append(
                {
                    "request_id": request_id,
                    "operation": "sprint.close",
                    "intent": document.get("intent") if isinstance(document.get("intent"), dict) else {},
                    "status": "committed",
                    "protocol": True,
                    "entity_kind": "sprint",
                    "ref": f"sprint:{number}",
                    "created_at": decided_at,
                    "settled_at": decided_at,
                }
            )
        recovered.add(number)
        for entry in entries:
            rows["sprint_decisions"].append(
                {"sprint_number": number, "request_id": request_id, "decided_at": decided_at, **entry}
            )
    report.sprints_without_recoverable_decisions = [
        f"sprint:{number}"
        for number, row in sorted(sprints.items())
        if row["status"] != "open" and number not in recovered
    ]


# --- writing (§7.1) ------------------------------------------------------------------------

#: The four transactions, each a group of entities that is one fact.  §7.1's rule is one
#: transaction per protocol mutation; an import is not a protocol mutation, so the same rule is
#: applied to the groups a mutation would have written: the registry projection, the catalogue,
#: the sprint-and-card graph whose keys are mutual, and the journal-shaped children.
TRANSACTION_GROUPS = (
    ("registry", ("projects", "repositories")),
    ("catalogue", ("products", "product_projects", "issues")),
    (
        "sprints and cards",
        (
            "sprints",
            "sprint_repositories",
            "sprint_issues",
            "sprint_projects",
            "sprint_resumes",
            "tasks",
            "task_retry_heads",
            "task_dependencies",
            "task_supersessions",
            "task_issues",
        ),
    ),
    (
        "claims, charges and comments",
        ("requests", "board_events", "sprint_budget_events", "task_comments", "sprint_comments", "sprint_decisions"),
    ),
)


def _table(name: str):
    """One §3 table, by name.

    ``schema`` is imported here rather than at module scope for the reason ``migrate`` imports
    SQLAlchemy inside its functions: the upgrade that installs the dependencies has to be able to
    start on a venv that does not have them yet, and the whole CLI imports this module to build
    its parser.  A dry run therefore needs no driver at all.
    """
    from secretary.board import schema

    return schema.metadata.tables[name]


def occupied_tables(connection: Any) -> list[tuple[str, int]]:
    """Every §3 table that already holds rows, so a repeat run can name what it found."""
    import sqlalchemy as sa

    found = []
    for name in TABLE_ORDER:
        count = connection.execute(sa.select(sa.func.count()).select_from(_table(name))).scalar_one()
        if count:
            found.append((name, int(count)))
    return found


def apply(plan_result: ImportPlan, connection: Any) -> dict[str, int]:
    """Write the plan, in four transactions, refusing a database that already holds board rows.

    The refusal is the answer §-level idempotency needs here.  Comments, resumes, budget events
    and decisions get their identity from ``GENERATED ALWAYS AS IDENTITY``, and the board carries
    no column this import could match them back on — Kanboard's comment id has no home in §3.7 —
    so a second run has nothing to converge on and would duplicate every one of them.  Refusing
    with the table it found is the only outcome that cannot be silently wrong; the repeat is
    ``migrate`` onto an empty database and this command again, which is reproducible because
    every row is a pure function of the board that was read.
    """
    import sqlalchemy as sa

    occupied = occupied_tables(connection)
    if occupied:
        detail = ", ".join(f"{name} ({count} row(s))" for name, count in occupied)
        raise BoardImportError(
            "the board store already holds imported rows and this import runs exactly once on an "
            f"empty schema: {detail}. Re-run `secretary board migrate` onto an empty database and "
            "import again; the rows this import writes carry generated identities that a second "
            "run cannot match back to the board."
        )

    rows = plan_result.rows
    written: dict[str, int] = {}
    # Reads autobegin on a SQLAlchemy `Connection`, so the implicit transaction the occupancy
    # check opened is closed before the first group opens its own.  Every `begin()` below is
    # preceded by the same rollback for the same reason: each group is its own transaction, and
    # a read between two of them must not silently join either.
    connection.rollback()

    with connection.begin():
        _insert(connection, "projects", rows["projects"], written)
        _insert(connection, "repositories", rows["repositories"], written)

    connection.rollback()
    with connection.begin():
        for name in ("products", "product_projects", "issues"):
            _insert(connection, name, rows[name], written)

    repository_id = {
        path: identifier
        for identifier, path in connection.execute(
            sa.select(_table("repositories").c.repository_id, _table("repositories").c.path)
        )
    }
    connection.rollback()
    with connection.begin():
        connection.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")
        _insert(connection, "sprints", rows["sprints"], written)
        _insert(
            connection,
            "sprint_repositories",
            [
                {"sprint_number": link["sprint_number"], "repository_id": repository_id[link["path"]]}
                for link in rows["sprint_repositories"]
            ],
            written,
        )
        for name in (
            "sprint_issues",
            "sprint_projects",
            "sprint_resumes",
            "tasks",
            "task_retry_heads",
            "task_dependencies",
            "task_supersessions",
            "task_issues",
        ):
            _insert(connection, name, rows[name], written)
        # §3.3's two cursors are DEFERRABLE INITIALLY DEFERRED precisely so they can be set in the
        # same transaction as the rows they point at, in an order nobody has to think about.
        resume_of = {
            number: identifier
            for identifier, number in connection.execute(
                sa.select(_table("sprint_resumes").c.resume_id, _table("sprint_resumes").c.sprint_number)
            )
        }
        sprints = _table("sprints")
        for row in rows["sprints"]:
            number = row["sprint_number"]
            resume_id = resume_of.get(number)
            if row["current_task_ref"] is None and resume_id is None:
                continue
            connection.execute(
                sprints.update()
                .where(sprints.c.sprint_number == number)
                .values(current_task_ref=row["current_task_ref"], resume_id=resume_id)
            )

    connection.rollback()
    with connection.begin():
        for name in (
            "requests",
            "board_events",
            "sprint_budget_events",
            "task_comments",
            "sprint_comments",
            "sprint_decisions",
        ):
            _insert(connection, name, rows[name], written)
    connection.rollback()
    return written


def _insert(connection: Any, name: str, payload: list[dict[str, Any]], written: dict[str, int]) -> None:
    if payload:
        connection.execute(_table(name).insert(), payload)
    written[name] = len(payload)


def fetch_rows(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Read the written rows back in the plan's own shape, so parity checks one comparison.

    ``sprint_repositories`` comes back keyed by path rather than by the generated
    ``repository_id``, which is the plan's spelling: parity is about the board's identifiers, and
    a surrogate key the board never had is not one of them.
    """
    import sqlalchemy as sa

    result: dict[str, list[dict[str, Any]]] = {}
    for name in TABLE_ORDER:
        table = _table(name)
        if name == "sprint_repositories":
            repositories = _table("repositories")
            statement = sa.select(table.c.sprint_number, repositories.c.path).join_from(
                table, repositories, table.c.repository_id == repositories.c.repository_id
            )
        else:
            statement = sa.select(table)
        result[name] = [dict(row) for row in connection.execute(statement).mappings()]
    return result


# --- parity --------------------------------------------------------------------------------
#
# Four axes, and each one is checked against the *board*, never against the plan's own arithmetic:
# a check that compares the importer with itself proves nothing.  Identifiers are compared
# literally — `sprint:N`, task refs, `issue:<hash>`, `product:<id>` — because "the same number of
# rows" is exactly the check that misses a renumbering.  Links are checked from both sides.
# Archived and closed records and their comments are in every one of them.


def _check(name: str, expected: Any, actual: Any, *, note: str = "") -> dict[str, Any]:
    ok = expected == actual
    check: dict[str, Any] = {"name": name, "ok": ok}
    if note:
        check["note"] = note
    if not ok:
        check["detail"] = _difference(expected, actual)
    return check


def _difference(expected: Any, actual: Any) -> str:
    if isinstance(expected, set) and isinstance(actual, set):
        missing = sorted(str(value) for value in expected - actual)[:20]
        extra = sorted(str(value) for value in actual - expected)[:20]
        return f"missing {len(expected - actual)} {missing}; unexpected {len(actual - expected)} {extra}"
    if isinstance(expected, Counter) and isinstance(actual, Counter):
        missing = sorted(str(key) for key in (expected - actual))[:20]
        extra = sorted(str(key) for key in (actual - expected))[:20]
        return f"missing {sum((expected - actual).values())} {missing}; unexpected {sum((actual - expected).values())} {extra}"
    return f"expected {expected!r}, got {actual!r}"


def parity(source: BoardSource, rows: dict[str, list[dict[str, Any]]], report: ImportReport) -> dict[str, Any]:
    """Compare the board against what was stored, on counts, identifiers, links and content."""
    refused_records = {item["ref"] for item in report.records_not_imported}
    refused_links = {(item["kind"], item["subject"], item["target"]) for item in report.links_not_imported}

    board_products = {row.ref: row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == PRODUCT_TYPE}
    board_issues = {row.ref: row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == ISSUE_TYPE}
    board_cards: dict[str, SourceRow] = {}
    for row in sorted(source.pipeline, key=lambda item: (item.ref, item.archived, item.task_id)):
        if row.meta.get(META_RECORD_TYPE) in (PRODUCT_TYPE, ISSUE_TYPE):
            continue
        board_cards.setdefault(row.ref, row)
    board_sprints = {row.ref: row for row in _one_row_per_reference(source.sprints)}

    refused = Counter(item["kind"] for item in report.records_not_imported)
    counts = [
        _check(
            "every board card is a tasks row or a named refusal",
            sum(1 for row in source.pipeline if row.meta.get(META_RECORD_TYPE) not in (PRODUCT_TYPE, ISSUE_TYPE)),
            len(rows["tasks"]) + refused["card"],
            note=f"{sum(1 for row in board_cards.values() if row.archived)} of them archived",
        ),
        _check("every Issue row is an issues row or a named refusal", len(board_issues),
               len(rows["issues"]) + refused["issue"]),
        _check("every Product row is a products row or a named refusal", len(board_products),
               len(rows["products"]) + refused["product"]),
        _check(
            "every sprint board row is a sprints row or a named refusal",
            len(source.sprints),
            len(rows["sprints"]) + refused["sprint"],
        ),
        _check(
            "every comment of an imported card or sprint is stored",
            sum(len(row.comments) for row in source.pipeline if row.ref in {task["task_ref"] for task in rows["tasks"]})
            + sum(len(row.comments) for row in _one_row_per_reference(source.sprints)
                  if _sprint_number_of(row.ref) in {sprint["sprint_number"] for sprint in rows["sprints"]}),
            len(rows["task_comments"]) + len(rows["sprint_comments"]),
        ),
    ]

    identifiers = [
        _check(
            "task refs, literally",
            set(board_cards) - refused_records,
            {row["task_ref"] for row in rows["tasks"]},
        ),
        _check(
            "issue refs, literally",
            {ref for ref in board_issues if ref not in refused_records},
            {f"issue:{row['issue_id']}" for row in rows["issues"]},
        ),
        _check(
            "product refs, literally",
            {ref for ref in board_products if ref not in refused_records},
            {f"product:{row['product_id']}" for row in rows["products"]},
        ),
        _check(
            "sprint refs, literally",
            {ref for ref in board_sprints if ref not in refused_records},
            {f"sprint:{row['sprint_number']}" for row in rows["sprints"]},
        ),
        _check(
            "task numbers agree with the reference suffix",
            {(row["task_ref"], _task_number_of(row["task_ref"])) for row in rows["tasks"]},
            {(row["task_ref"], row["task_number"]) for row in rows["tasks"]},
        ),
    ]

    stored_tasks = {row["task_ref"]: row for row in rows["tasks"]}
    stored_sprints = {row["sprint_number"]: row for row in rows["sprints"]}
    expected_card_sprint = {
        (ref, _sprint_number_of(_text(row.meta.get("sprint_ref"))))
        for ref, row in board_cards.items()
        if ref in stored_tasks and _text(row.meta.get("sprint_ref"))
        and ("tasks.sprint", ref, _text(row.meta.get("sprint_ref"))) not in refused_links
    }
    expected_sprint_issues = {
        (_sprint_number_of(ref), value.removeprefix("issue:"))
        for ref, row in board_sprints.items()
        for value in _json_list(row.meta.get("sprint_issues"))
        if _sprint_number_of(ref) in stored_sprints
        and ("sprint_issues", ref, value) not in refused_links
    }
    expected_sprint_repositories = {
        (_sprint_number_of(ref), path)
        for ref, row in board_sprints.items()
        for path in _json_list(row.meta.get("sprint_repositories"))
        if _sprint_number_of(ref) in stored_sprints
    }
    expected_dependencies = {
        (ref, _text(row.meta.get("blocked_by")))
        for ref, row in board_cards.items()
        if ref in stored_tasks and _text(row.meta.get("blocked_by"))
        and ("task_dependencies", ref, _text(row.meta.get("blocked_by"))) not in refused_links
    }
    links = [
        _check(
            "card -> sprint, from the card's side",
            expected_card_sprint,
            {(row["task_ref"], row["sprint_number"]) for row in rows["tasks"] if row["sprint_number"]},
        ),
        _check(
            "sprint -> card, from the sprint's side",
            {number for _, number in expected_card_sprint},
            {row["sprint_number"] for row in rows["tasks"] if row["sprint_number"]},
        ),
        _check(
            "sprint <-> issue",
            expected_sprint_issues,
            {(row["sprint_number"], row["issue_id"]) for row in rows["sprint_issues"]},
        ),
        _check(
            "sprint <-> repository, by path",
            expected_sprint_repositories,
            {(row["sprint_number"], row["path"]) for row in rows["sprint_repositories"]},
        ),
        _check(
            "card -> card dependency",
            expected_dependencies,
            {(row["task_ref"], row["depends_on"]) for row in rows["task_dependencies"]},
        ),
        _check(
            "the sprint cursor points inside its own sprint",
            set(),
            {
                row["sprint_number"]
                for row in rows["sprints"]
                if row["current_task_ref"]
                and stored_tasks.get(row["current_task_ref"], {}).get("sprint_number")
                != row["sprint_number"]
            },
        ),
        _check(
            "every product names its projects",
            {
                (ref.removeprefix("product:"), project)
                for ref, row in board_products.items()
                for project in _json_list(row.meta.get(META_PRODUCT_PROJECTS))
                if ref not in refused_records
                and ("product_projects", ref, project) not in refused_links
            },
            {(row["product_id"], row["project_id"]) for row in rows["product_projects"]},
        ),
    ]

    stored_comments = Counter(
        (row["task_ref"], _iso(row["created_at"]), _text(row["marker"]), row["body"])
        for row in rows["task_comments"]
    )
    expected_comments: Counter[tuple[str, str, str, str]] = Counter()
    for ref, row in board_cards.items():
        if ref not in stored_tasks:
            continue
        for comment in _comments_for(source, ref):
            body = _text(comment.get("comment"))
            marker, text = parse_marker(body)
            expected_comments[
                (
                    ref,
                    _iso(_required_when(comment.get("date_creation"), stored_tasks[ref]["updated_at"])),
                    marker or "",
                    text,
                )
            ] += 1
    content = [
        _check(
            "card title, description, state, archived flag and position",
            {
                (
                    ref,
                    _text(row.raw.get("title")),
                    _text(row.raw.get("description")),
                    _STATE_BY_COLUMN.get(
                        source.pipeline_columns.get(_positive_int(row.raw.get("column_id")) or -1, ""), ""
                    ),
                    row.archived,
                    _nonnegative_int(row.raw.get("position")),
                )
                for ref, row in board_cards.items()
                if ref in stored_tasks
            },
            {
                (
                    row["task_ref"],
                    row["title"],
                    row["description"],
                    row["state"],
                    bool(row["archived"]),
                    row["position"],
                )
                for row in rows["tasks"]
            },
        ),
        _check(
            "issue kind, priority, state and close reason, closed issues included",
            {
                (
                    ref.removeprefix("issue:"),
                    _text(row.meta.get(META_ISSUE_KIND)),
                    _text(row.meta.get(META_ISSUE_PRIORITY)),
                    "closed" if row.archived else "open",
                    _text(row.meta.get(META_ISSUE_CLOSED_REASON)),
                )
                for ref, row in board_issues.items()
                if ref not in refused_records
            },
            {
                (
                    row["issue_id"],
                    row["issue_kind"],
                    row["priority"],
                    row["state"],
                    _text(row["close_reason"]),
                )
                for row in rows["issues"]
            },
        ),
        _check(
            "sprint goal, definition of done and status",
            {
                (
                    _sprint_number_of(ref),
                    row.meta.get("sprint_goal", ""),
                    row.meta.get("sprint_definition_of_done", ""),
                    row.meta.get("sprint_status")
                    if row.meta.get("sprint_status") in SPRINT_STATUSES
                    else "open",
                )
                for ref, row in board_sprints.items()
                if _sprint_number_of(ref) in stored_sprints
            },
            {
                (row["sprint_number"], row["goal"], row["definition_of_done"], row["status"])
                for row in rows["sprints"]
            },
        ),
        _check(
            "card comment bodies and markers, as a multiset",
            expected_comments,
            stored_comments,
            note="142 comments on this board share a card, a second and a body, so the check is a "
            "multiset and not a set",
        ),
        _check(
            "the budget counters equal the rows, per sprint and per type",
            _counter_totals(source, stored_sprints),
            Counter(
                (row["sprint_number"], row["event_type"]) for row in rows["sprint_budget_events"]
            ),
        ),
    ]

    axes = {"counts": counts, "identifiers": identifiers, "links": links, "content": content}
    return {"ok": all(check["ok"] for checks in axes.values() for check in checks), **axes}


def _comments_for(source: BoardSource, ref: str) -> list[dict[str, Any]]:
    """Every comment of a reference, including those of an archived duplicate row of it."""
    found: list[dict[str, Any]] = []
    for row in source.pipeline:
        if row.ref == ref and row.meta.get(META_RECORD_TYPE) not in (PRODUCT_TYPE, ISSUE_TYPE):
            found.extend(row.comments)
    return found


def _counter_totals(source: BoardSource, stored_sprints: dict[int, Any]) -> Counter:
    totals: Counter = Counter()
    for row in _one_row_per_reference(source.sprints):
        number = _sprint_number_of(row.ref)
        if number not in stored_sprints:
            continue
        budget = _budget(row.meta.get("sprint_budget"), None, row.meta.get(BUDGET_UNCHARGED_FIELD))
        counts = {**budget["by_type"], **budget["uncharged"]}
        for event_type in BUDGET_RECORDED_EVENT_TYPES:
            wanted = int(counts.get(event_type, 0))
            if wanted:
                totals[(number, event_type)] = wanted
    return totals


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return _text(value)


# --- the run -------------------------------------------------------------------------------


def engine_for(dsn: str | None, instance: Path | None):
    """A SQLAlchemy engine for the store, from an explicit DSN or from §5.4's configuration.

    An explicit DSN is what the throwaway-container run uses; it is also the only way this card
    reaches a database at all, because it creates no ``board-store.env`` and stands no live
    PostgreSQL up.  A configured installation resolves through ``board_store`` as everything else
    does, under the ``app`` role: the import is DML from first row to last, and §5.5's ``app``
    role is exactly the one that may run it.  Nothing here needs DDL — the schema is already
    there, applied by ``migrate`` under ``secretary_owner``.
    """
    import sqlalchemy as sa

    if dsn:
        return sa.create_engine(dsn)
    if instance is None:
        raise BoardImportError("the import needs either a --dsn or an instance with a board store")
    from secretary.board import migrate as board_migrate
    from secretary.board import store as board_store

    return sa.create_engine(board_migrate.sqlalchemy_url(board_store.resolve(instance).for_role("app")))


def run(
    instance: str | Path,
    *,
    data_dir: str | Path | None = None,
    dsn: str | None = None,
    dry_run: bool = True,
    report_path: str | Path | None = None,
    thresholds: dict[str, int] | None = None,
) -> ImportReport:
    """Read the board, map it, and — unless this is a dry run — write it.  Returns the report.

    A dry run reads everything, writes nothing anywhere, and produces the same report the real
    run does: the same counts, the same discrepancies and the same parity axes, because parity is
    a comparison between the board and the rows, and the rows are the plan's in one mode and the
    database's read-back in the other.
    """
    instance_path = Path(instance).expanduser()
    source = read_source(instance_path, data_dir=data_dir)
    result = plan(source, thresholds=thresholds)
    report = result.report
    report.mode = "dry-run" if dry_run else "apply"

    if dry_run:
        report.parity = parity(source, result.rows, report)
    else:
        from secretary.board import migrate as board_migrate

        engine = engine_for(dsn, instance_path)
        try:
            with engine.connect() as connection:
                report.schema_revision = board_migrate.assert_schema_revision(connection)
                written = apply(result, connection)
                report.counts = {name: written.get(name, 0) for name in TABLE_ORDER}
                stored = fetch_rows(connection)
            report.parity = parity(source, stored, report)
        finally:
            engine.dispose()

    if report_path is not None:
        target = Path(report_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target.with_suffix(".txt").write_text(render(report), encoding="utf-8")
    return report


__all__ = [
    "IMPORT_VERSION",
    "MARKER_EXACT",
    "MARKER_PREFIXES",
    "MARKER_ROLES",
    "TABLE_ORDER",
    "TASK_KNOWN_METADATA",
    "TRANSACTION_GROUPS",
    "BoardImportError",
    "BoardSource",
    "ImportPlan",
    "ImportReport",
    "RegistryEntry",
    "SourceRow",
    "apply",
    "engine_for",
    "fetch_rows",
    "marker_token",
    "occupied_tables",
    "parity",
    "parse_marker",
    "plan",
    "read_budget_records",
    "read_registry",
    "read_source",
    "read_transaction_documents",
    "render",
    "run",
]
