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

**A green parity means the whole board is in the store, and nothing else** (2026-09-07).  The
first revision of :func:`parity` subtracted the report's own refusals from what each axis expected
to find, so a run that had just declined to write 479 comments still returned PASS: the check
agreed with the report instead of with the board, which is a check that cannot fail.  Nothing is
subtracted now.  A record the board holds and the store does not fails its axis, is named by its
own identifier in ``records_missing`` — one line each, never an aggregate — and makes the result
red.  A fifth axis, ``accounting``, then compares the refusals with the records actually missing in
both directions, so "a record that is neither imported nor named is a defect" is a check rather
than a promise.  The content axis compares all four comment tables, because comparing only the
first of them let a reviewer change a stored sprint comment's body and still be told ``True``.

**What revision ``0002_board_gaps`` moved here.**  The sprint's identity is its reference, so every
sprint-scoped row this module builds carries ``sprint_ref``; ``issue_comments`` is the third table
of §3.7 and the 479 comments land in it; ``issues.extensions`` takes an Issue's leftover metadata
keys; ``tasks.project_id`` may be NULL; and ``task_dependencies`` keeps the reference and its
resolution apart.  §9's ``sprint:1037`` is on the board twice and the owner's answer is
outstanding, so this module runs §9's **option 1**: the live row keeps the spelling, the archived
row is stored under a distinguishing reference, its original spelling goes to
``sprints.source_audit``, and the report names the row on a line of its own so a different answer
is cheap to apply (:func:`sprint_references`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any

import yaml

from secretary.board.backend import record_key
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
#: The open families: the board already carries more than one token under each, so the suffix is
#: not enumerated.  `validate:*`, `claim:*` and `watchdog:*` joined the list on 2026-09-07, when a
#: re-read of both boards counted 743 comments under them (§8.1).
MARKER_PREFIXES = ("report:", "review:", "decision:", "issue:", "validate:", "claim:", "watchdog:")
#: The closed ones: exactly one token each, so the token is a name rather than a family.
#: `steward:blocked-done` is deliberately not read as the role `steward` — the role marker is bare.
MARKER_EXACT = frozenset(
    {"sprint:resume", "archive", "rejected", "steward:blocked-done", "provision:request"}
)

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
    "issue_comments",
    "product_comments",
    "sprint_decisions",
)

#: The four comment tables, each with the column that names its entity.  They are one
#: list because every rule about a comment — §8.1's marker, §8.2's counting, both parity axes —
#: applies to all three, and the reviewer's finding of 2026-09-07 was a rule that reached only
#: the first of them.
#: Parity's axes, in the order the report prints them.  ``accounting`` joined the other four on
#: 2026-09-07: it is the axis that compares the *refusals* with the records actually missing, so
#: "a record that is neither imported nor named is a defect" is a check and not a promise.
PARITY_AXES = ("counts", "identifiers", "links", "content", "accounting")

COMMENT_TABLES = (
    ("task_comments", "task_ref"),
    ("sprint_comments", "sprint_ref"),
    ("issue_comments", "issue_id"),
    ("product_comments", "product_id"),
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
    """One read of everything, so a dry run and a real run describe the same board.

    The client is Kanboard-only **by statement, not by default**: the importer's whole subject is
    the Kanboard board it copies *into* the store, so consulting `SECRETARY_CARD_BACKEND`
    (`board/backend.py`) here would ask the destination to be the source.  An import run under a
    `postgres` switch still reads Kanboard, and that is correct rather than an oversight.
    """
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


def comment_identifier(owner_ref: str, comment: dict[str, Any]) -> str:
    """One comment's own name: the record it sits on, and Kanboard's own comment id.

    §3.7 gives a stored comment a generated identity and the board carries no column to match it
    back on, so this identifier exists for the *report* and not for the schema.  That is enough
    for what it is for: naming, one line each, the comments a run did not store.
    """
    identifier = _positive_int(comment.get("id"))
    if identifier is None:
        # No id: a digest of what the comment says, which is stable across runs of one board.
        digest = sha1(_text(comment.get("comment")).encode("utf-8")).hexdigest()[:12]
        return f"{owner_ref}#comment-body-{digest}"
    return f"{owner_ref}#comment-{identifier}"


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
    #: Two Kanboard rows carrying one reference.  Not a loss and deliberately not a refusal: the
    #: record is the reference, the surviving row keeps it and the other row's comments merge into
    #: it, so parity has nothing missing to find.  It is listed because a reader who counts board
    #: rows and store rows will otherwise see a gap and have to guess what closed it.
    duplicate_board_rows: list[dict[str, Any]] = field(default_factory=list)
    #: §9 option 1: an archived sprint row whose reference a live row already holds, stored under a
    #: distinguishing reference with its original spelling in `sprints.source_audit`.  One line per
    #: row, so the owner's other choice can be applied to exactly these rows and no others.
    disambiguated_references: list[dict[str, Any]] = field(default_factory=list)
    fields_without_a_column: list[dict[str, Any]] = field(default_factory=list)
    extensions_keys: list[dict[str, Any]] = field(default_factory=list)
    project_repository_mismatch: dict[str, Any] = field(default_factory=dict)
    sprints_without_recoverable_decisions: list[str] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    unrecognized_comment_markers: list[dict[str, Any]] = field(default_factory=list)
    approximate_values: list[dict[str, Any]] = field(default_factory=list)
    #: A card the board carries with no value at all for a column the schema has.  The column is
    #: NULL and `tasks.extensions` says which field the board never named, so a reader of the row
    #: can tell "the board did not say" from "the import lost it".  One line per record, because
    #: the whole point is that these are named rather than counted: a NULL nobody named is
    #: indistinguishable from a value that went missing.
    fields_the_board_never_named: list[dict[str, Any]] = field(default_factory=list)
    parity: dict[str, Any] = field(default_factory=dict)

    def record_not_imported(self, *, kind: str, ref: str, reason: str) -> None:
        """Name one record, by an identifier that is its own and nobody else's.

        ``ref`` is the identifier :func:`parity` compares against, so it has to survive being put
        in a set: a decorated string like ``"479 comments on Issue rows"`` names a count and not a
        record, and the reviewer's finding of 2026-09-07 is exactly what that costs.  Where a
        board reference is not unique by itself — a second row claiming it, a comment, which has
        no reference at all — the identifier carries the Kanboard row or comment id.
        """
        self.records_not_imported.append({"kind": kind, "ref": ref, "reason": reason})

    def board_rows_merged(self, *, kind: str, ref: str, kept: int, dropped: int) -> None:
        self.duplicate_board_rows.append(
            {"kind": kind, "ref": ref, "kept_kanboard_task": kept, "merged_kanboard_task": dropped}
        )

    def board_never_named(self, *, field_name: str, ref: str, reason: str) -> None:
        """One record whose column is NULL because the board carries no value for it.

        Not a loss and not an approximation: the record lands whole, and the NULL is the board's
        own silence written down.  It is reported by name because a NULL in a column that usually
        carries a value is exactly what a reader would otherwise have to guess about.
        """
        self.fields_the_board_never_named.append(
            {"field": field_name, "ref": ref, "reason": reason}
        )

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
                "duplicate_board_rows": self.duplicate_board_rows,
                "disambiguated_references": self.disambiguated_references,
                "fields_without_a_column": self.fields_without_a_column,
                "extensions_keys": self.extensions_keys,
                "project_repository_mismatch": self.project_repository_mismatch,
                "sprints_without_recoverable_decisions": self.sprints_without_recoverable_decisions,
                "budget": self.budget,
                "unrecognized_comment_markers": self.unrecognized_comment_markers,
                "approximate_values": self.approximate_values,
                "fields_the_board_never_named": self.fields_the_board_never_named,
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
    if report.disambiguated_references:
        lines += [
            "",
            (
                "references disambiguated at import (§9, option 1): "
                f"{len(report.disambiguated_references)}"
            ),
        ]
        lines += [
            f"  {item['kind']} {item['original_ref']} (kanboard task {item['kanboard_task']}) "
            f"stored as {item['stored_ref']}; original spelling kept in {item['provenance']}"
            for item in report.disambiguated_references
        ]
    if report.duplicate_board_rows:
        lines += [
            "",
            (
                "two board rows, one reference, one record (comments merged, nothing lost): "
                f"{len(report.duplicate_board_rows)}"
            ),
        ]
        lines += [
            f"  {item['kind']} {item['ref']}: kept kanboard task {item['kept_kanboard_task']}, "
            f"merged kanboard task {item['merged_kanboard_task']}"
            for item in report.duplicate_board_rows
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
        # §8.2 asks for the key, the number of rows and *where it landed*: two tables have an
        # extensions bag since 0002, and a key named without its table reads as a duplicate.
        lines += ["", "metadata keys carried into an extensions bag (§8.2), per key and table:"]
        lines += [
            f"  {item['where']}.{item['key']}: {item['rows']} row(s)"
            for item in report.extensions_keys
        ]
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
    if report.fields_the_board_never_named:
        lines += [
            "",
            (
                "cards the board never gave a value for (column NULL, said so in extensions): "
                f"{len(report.fields_the_board_never_named)}"
            ),
        ]
        lines += [
            f"  {item['field']} {item['ref']}: {item['reason']}"
            for item in report.fields_the_board_never_named
        ]
    if report.parity:
        missing = report.parity.get("records_missing", [])
        lines += ["", f"parity: {'PASS' if report.parity.get('ok') else 'FAIL'}"]
        for axis in PARITY_AXES:
            checks = report.parity.get(axis, [])
            failed = [check for check in checks if not check.get("ok")]
            lines.append(f"  {axis}: {len(checks)} check(s), {len(failed)} failed")
            lines += [f"    FAIL {check['name']}: {check.get('detail')}" for check in failed]
        lines.append(f"  records the board holds and the store does not: {len(missing)}")
        # One line per record, with its own identifier.  An aggregate cannot answer "which ones",
        # and "which ones" is the whole question a failing parity asks.
        lines += [f"    MISSING {item['kind']} {item['id']}: {item['reason']}" for item in missing]
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
    _plan_comments(source, tasks, sprints, products, issues, rows, report)
    _plan_decisions(source, sprints, tasks, rows, report)

    report.expected_zero = [
        {
            "table": "task_issues",
            "reason": "no source field exists: no metadata key holds a card's issue refs and "
            "board/kanboard.py:_card builds every Card with an empty issue_refs (§8.4, gap 1). "
            "A card's issue association is reachable through its sprint "
            "(tasks.sprint_ref -> sprint_issues).",
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


def one_row_per_ref(
    board_rows: list[SourceRow], *, kind: str, report: ImportReport
) -> list[SourceRow]:
    """The rows of one board, one per reference, and a report line for every row that merged.

    Kanboard lets two rows carry one reference.  For a card, a Product and an Issue that is one
    *record* seen twice, not two records: the live row wins, the other row's comments are still
    imported against the same reference (:func:`_comments_for`), and nothing is lost.  It is
    therefore not a refusal — a refusal is a record the store does not hold — and calling it one
    is what made the old report claim 479 comments were missing when they were not the same kind
    of thing at all.  It is still listed, so a reader who counts board rows finds the arithmetic.
    """
    chosen = chosen_by_identifier(board_rows, kind=kind)
    for row in sorted(board_rows, key=lambda item: (item.ref, item.archived, item.task_id)):
        key = board_identifier(row, kind)
        if chosen[key] is not row:
            report.board_rows_merged(
                kind=kind, ref=row.ref, kept=chosen[key].task_id, dropped=row.task_id
            )
    return [chosen[key] for key in sorted(chosen)]


def board_identifier(row: SourceRow, kind: str) -> str:
    """The name a board row answers to in the report and in parity, whether or not it has a ref.

    One scheme in one place, because the accounting axis of :func:`parity` compares the report's
    refusals with the records actually missing, and two spellings of one record would make that
    comparison lie in both directions.
    """
    return row.ref or f"{kind}@kanboard-{row.task_id}"


def chosen_by_identifier(board_rows: list[SourceRow], *, kind: str) -> dict[str, SourceRow]:
    """One row per identifier, the live one winning over an archived duplicate.  Pure."""
    chosen: dict[str, SourceRow] = {}
    for row in sorted(board_rows, key=lambda item: (item.ref, item.archived, item.task_id)):
        chosen.setdefault(board_identifier(row, kind), row)
    return chosen


def _plan_products(
    product_rows: list[SourceRow], rows: dict[str, list[dict[str, Any]]], report: ImportReport
) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    stray_keys: Counter[str] = Counter()
    known_projects = {row["project_id"] for row in rows["projects"]}
    for row in one_row_per_ref(product_rows, kind="product", report=report):
        product_id = _text(row.meta.get(META_PRODUCT_ID))
        if not product_id:
            report.record_not_imported(
                kind="product",
                ref=row.ref or f"product@kanboard-{row.task_id}",
                reason="the row carries no product_id, and products.product_id is the primary key",
            )
            continue
        if product_id in products:
            report.record_not_imported(
                kind="product",
                ref=row.ref,
                reason=f"a second board reference claims product_id {product_id!r}, which is a "
                "primary key; the first row was kept",
            )
            continue
        created = _required_when(row.raw.get("date_creation"), datetime.fromtimestamp(0, tz=UTC))
        extensions = {
            key: value
            for key, value in row.meta.items()
            if key not in {META_RECORD_TYPE, META_PRODUCT_ID, META_PRODUCT_PROJECTS}
        }
        for key in extensions:
            stray_keys[key] += 1
        products[product_id] = {
            "product_id": product_id,
            "board_key": record_key("product", product_id),
            "title": _text(row.raw.get("title")) or product_id,
            "description": _text(row.raw.get("description")),
            "state": "archived" if row.archived else "active",
            "extensions": _extensions_of(extensions),
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
    report.extensions_keys += [
        {"key": key, "rows": count, "where": "products.extensions.kanboard"}
        for key, count in sorted(stray_keys.items(), key=lambda item: (-item[1], item[0]))
    ]
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
    for row in one_row_per_ref(issue_rows, kind="issue", report=report):
        issue_id = row.ref.removeprefix("issue:")
        if not issue_id or issue_id == row.ref:
            report.record_not_imported(
                kind="issue",
                ref=row.ref or f"issue@kanboard-{row.task_id}",
                reason="the reference is not of the form issue:<id>",
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
        # §8.2 since 0002: an Issue has an `extensions` bag of its own, so a metadata key the
        # schema does not name is provenance a query can reach rather than a record loss.
        extensions = {
            key: value
            for key, value in row.meta.items()
            if key
            not in {
                META_RECORD_TYPE,
                META_ISSUE_PRODUCT,
                META_ISSUE_KIND,
                META_ISSUE_PRIORITY,
                META_ISSUE_CLOSED_REASON,
            }
        }
        lane = source.pipeline_swimlanes.get(_positive_int(row.raw.get("swimlane_id")) or -1, "")
        if lane and lane != product_lane_name(product_id):
            # The same rule §8.6 gives a card: where the observed lane disagrees with the
            # product-derived one, the observed one is kept instead of being normalized away.
            extensions["swimlane"] = lane
            misplaced_lanes += 1
        for key in extensions:
            stray_keys[key] += 1
        issues[issue_id] = {
            "issue_id": issue_id,
            "board_key": record_key("issue", issue_id),
            "product_id": product_id,
            "title": _text(row.raw.get("title")),
            "description": _text(row.raw.get("description")),
            "issue_kind": kind,
            "priority": priority,
            "state": "closed" if closed else "open",
            "close_reason": reason,
            "extensions": _extensions_of(extensions),
            "created_at": created,
            "updated_at": _required_when(row.raw.get("date_modification"), created),
        }
    rows["issues"] = [issues[key] for key in sorted(issues)]
    report.extensions_keys += [
        {"key": key, "rows": count, "where": "issues.extensions.kanboard"}
        for key, count in sorted(stray_keys.items(), key=lambda item: (-item[1], item[0]))
    ]
    if misplaced_lanes:
        report.approximate_values.append(
            {
                "field": "issues.extensions.kanboard.swimlane",
                "rows": misplaced_lanes,
                "reason": "the row sits in a lane that is not its product's; the observed lane is "
                "kept as provenance rather than replaced by the product-derived one (§8.2)",
            }
        )
    return issues


#: How a disambiguated sprint reference is spelled, and where the original one is kept.
DISAMBIGUATION_SUFFIX = "-archived-{kanboard_task}"
SOURCE_AUDIT_ORIGINAL_REF = "imported_original_ref"


def sprint_references(rows: tuple[SourceRow, ...]) -> dict[int, str]:
    """The reference each sprint row is stored under, keyed by Kanboard task id (§9, option 1).

    A sprint reference is a primary key and this board carries one of them twice: `sprint:1037` is
    a live row (Kanboard task 748) and an archived row (task 1037).  The owner has been asked and
    has not answered, so the card runs §9's option 1: the live row keeps the spelling, the
    archived row is stored under a distinguishing reference, and its original spelling is kept in
    ``sprints.source_audit``.  Both *records* survive whole — every field, every comment, every
    card link — and the only thing one archived row loses is its spelling.

    The distinguishing reference deliberately does not match ``^sprint:[0-9]+$``, which is what
    lets ``sprint_number_agrees_with_ref`` hold with a NULL number instead of colliding with the
    live row on ``UNIQUE (sprint_number)``.  It needs no schema change, and reversing it — if the
    owner picks option 2 or 3 — is a rewrite of exactly the rows the report names.
    """
    assigned: dict[int, str] = {}
    taken: set[str] = set()
    for row in sorted(rows, key=lambda item: (item.ref, item.archived, item.task_id)):
        reference = row.ref
        if reference and reference in taken:
            candidate = reference + DISAMBIGUATION_SUFFIX.format(kanboard_task=row.task_id)
            ordinal = 2
            while candidate in taken:
                candidate = (
                    reference
                    + DISAMBIGUATION_SUFFIX.format(kanboard_task=row.task_id)
                    + f"-{ordinal}"
                )
                ordinal += 1
            reference = candidate
        if reference:
            taken.add(reference)
        assigned[row.task_id] = reference
    return assigned


def _plan_sprints(
    source: BoardSource,
    products: dict[str, dict[str, Any]],
    issues: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> dict[str, dict[str, Any]]:
    known_projects = {row["project_id"] for row in rows["projects"]}
    repository_paths = {row["path"] for row in rows["repositories"]}
    sprints: dict[str, dict[str, Any]] = {}
    approximate_closed_at = 0
    live_reservation: dict[str, str] = {}
    assigned = sprint_references(source.sprints)
    for row in sorted(source.sprints, key=lambda item: (item.ref, item.archived, item.task_id)):
        reference = assigned[row.task_id]
        if not reference.startswith(SPRINT_REFERENCE_PREFIX):
            report.record_not_imported(
                kind="sprint",
                ref=reference or f"sprint@kanboard-{row.task_id}",
                reason="sprint_ref_is_a_sprint_reference requires a reference beginning "
                f"{SPRINT_REFERENCE_PREFIX!r}, and this row carries {row.ref!r}",
            )
            continue
        number = _sprint_number_of(reference)
        if reference != row.ref:
            report.disambiguated_references.append(
                {
                    "kind": "sprint",
                    "original_ref": row.ref,
                    "stored_ref": reference,
                    "kanboard_task": row.task_id,
                    "provenance": f"sprints.source_audit.{SOURCE_AUDIT_ORIGINAL_REF}",
                    "reason": "a live row already holds this reference and §3.3 makes the "
                    "reference the primary key; §9 option 1 stores the archived row under a "
                    "distinguishing reference rather than dropping the record",
                }
            )
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
                subject=reference,
                target=product_id,
                reason="no products row exists for this id; sprints.product_id stays NULL",
            )
            product_id = ""
        pins = stored_executors(row.meta)
        observer = parse_observer(row.meta[OBSERVER_FIELD]) if OBSERVER_FIELD in row.meta else None
        if OBSERVER_FIELD in row.meta and observer is None:
            report.link_not_imported(
                kind="sprint.observer",
                subject=reference,
                target=row.meta[OBSERVER_FIELD][:60],
                reason="the value is not one of the four tagged observer forms; the column stays NULL",
            )
        source_audit = _source_audit(row.meta.get("sprint_source_audit"))
        if reference != row.ref:
            source_audit = {**(source_audit or {}), SOURCE_AUDIT_ORIGINAL_REF: row.ref}
        sprints[reference] = {
            "ref": reference,
            "board_key": record_key("sprint", reference),
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
            "source_audit": source_audit,
            "created_at": created,
            "updated_at": updated,
            "closed_at": closed_at,
        }
        for path in _json_list(row.meta.get("sprint_repositories")):
            if path not in repository_paths:  # pragma: no cover - _plan_registry created them all
                report.link_not_imported(
                    kind="sprint_repositories",
                    subject=reference,
                    target=path,
                    reason="no repositories row exists for this path",
                )
                continue
            rows["sprint_repositories"].append({"sprint_ref": reference, "path": path})
        for value in _json_list(row.meta.get("sprint_issues")):
            issue_id = value.removeprefix("issue:")
            if issue_id not in issues:
                report.link_not_imported(
                    kind="sprint_issues",
                    subject=reference,
                    target=value,
                    reason="the issue was not imported, and sprint_issues.issue_id is a foreign key",
                )
                continue
            rows["sprint_issues"].append({"sprint_ref": reference, "issue_id": issue_id})
        reserved = status == "open"
        for project_id in _json_list(row.meta.get("sprint_reservations")):
            if project_id not in known_projects:  # pragma: no cover - _plan_registry created them
                report.link_not_imported(
                    kind="sprint_projects",
                    subject=reference,
                    target=project_id,
                    reason="no projects row exists for this id",
                )
                continue
            if reserved and project_id in live_reservation:
                report.link_not_imported(
                    kind="sprint_projects",
                    subject=reference,
                    target=project_id,
                    reason="sprint_projects_one_live_reservation already holds this project for "
                    f"{live_reservation[project_id]}; §4 allows exactly one live reservation",
                )
                continue
            if reserved:
                live_reservation[project_id] = reference
            rows["sprint_projects"].append(
                {
                    "sprint_ref": reference,
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
                    "sprint_ref": reference,
                    **{field_name: resume[field_name] for field_name in RESUME_FIELDS},
                    "recorded_at": recorded,
                }
            )
        elif row.meta.get("sprint_resume"):
            report.record_not_imported(
                kind="sprint_resume",
                ref=reference,
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
            "task_comments.actor_id / sprint_comments.actor_id / issue_comments.actor_id",
            (
                "every Kanboard comment on this board carries user_id 0 and a null username, so "
                "the actor's identity is not recoverable; actor_role comes from a role marker only"
            ),
        ),
        (
            "task_comments.request_id / sprint_comments.request_id / issue_comments.request_id",
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


def _extensions_of(
    kanboard: dict[str, Any], *, silent: tuple[str, ...] = ()
) -> dict[str, Any]:
    """`tasks.extensions` (J3), in its two namespaced halves.

    ``kanboard`` is §8.2's provenance bag — the metadata keys the model does not name.  ``silent``
    is the other half and is not provenance at all: it names the columns the board carries no
    value for, so a NULL in the row can be read as the board's silence rather than as a loss.
    """
    bag: dict[str, Any] = {}
    if kanboard:
        bag["kanboard"] = kanboard
    if silent:
        bag["board_never_named"] = list(silent)
    return bag


def _plan_tasks(
    source: BoardSource,
    card_rows: list[SourceRow],
    sprints: dict[str, dict[str, Any]],
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
    for row in one_row_per_ref(card_rows, kind="card", report=report):
        ref = row.ref
        project_id = _null_if_empty(row.meta.get("project"))
        task_number = _task_number_of(ref)
        # Nullable since 0003 (§8.6): a card the board never gave a type is stored with NULL,
        # not with a type this importer chose for it.
        task_type = _null_if_empty(row.meta.get("task_type"))
        column = source.pipeline_columns.get(_positive_int(row.raw.get("column_id")) or -1, "")
        refusal = None
        if not ref:
            refusal = "the row carries no reference, and tasks.task_ref is the primary key"
        elif task_number is None:
            refusal = "the reference does not end in -<number>, so UNIQUE (project_id, task_number) has no value"
        elif project_id is not None and project_id not in known_projects:  # pragma: no cover
            refusal = f"project {project_id!r} has no projects row"
        elif task_type is not None and task_type not in _TASK_TYPES:
            refusal = f"task_type {task_type!r} is outside the CHECK vocabulary {sorted(_TASK_TYPES)}"
        elif column not in _STATE_BY_COLUMN:
            refusal = f"the row sits in column {column!r}, which _STATE_BY_COLUMN does not map to a state"
        elif not _text(row.raw.get("title")):
            refusal = "tasks.title carries CHECK (title <> '')"
        if refusal is not None:
            report.record_not_imported(
                kind="card", ref=ref or f"card@kanboard-{row.task_id}", reason=refusal
            )
            continue
        if project_id is None:
            # Nullable since 0002 (§8.6): the board does not say which project this card belongs
            # to, and a NULL says exactly that.  UNIQUE (project_id, task_number) does not
            # constrain a row with no project, which is correct — there is no numbering to collide.
            report.approximate_values.append(
                {
                    "field": "tasks.project_id",
                    "rows": 1,
                    "reason": f"{ref} carries no project metadata; the column is NULL rather than "
                    "derived from the reference prefix, which would invent a fact",
                }
            )
        if task_type is None:
            report.board_never_named(
                field_name="tasks.task_type",
                ref=ref,
                reason="the card carries no task_type metadata; the column is NULL and "
                "extensions.board_never_named records that the board did not name a type, so "
                "the NULL reads as 'not said' rather than as a value that went missing",
            )

        sprint_ref = _null_if_empty(row.meta.get("sprint_ref"))
        if sprint_ref is not None and sprint_ref not in sprints:
            report.link_not_imported(
                kind="tasks.sprint",
                subject=ref,
                target=sprint_ref,
                reason="the sprint was not imported; tasks.sprint_ref stays NULL",
            )
            sprint_ref = None

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
            "sprint_ref": sprint_ref,
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
            "extensions": _extensions_of(
                extensions, silent=() if task_type else ("task_type",)
            ),
            "created_at": created,
            "updated_at": _required_when(row.raw.get("date_modification"), created),
            "date_moved": _when(row.raw.get("date_moved")),
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
    unresolved_dependencies = 0
    for table, subject, target in pending_links:
        if subject == target:
            report.link_not_imported(
                kind=table,
                subject=subject,
                target=target,
                reason="a card may not reference itself (no_self_dependency / no_self_supersession)",
            )
            continue
        if table == "task_dependencies":
            # Two columns since 0002 (§3.5, §8.6): the reference as the card writes it is always
            # kept, and the foreign key is set exactly when the board holds that card.  A
            # dependency on a card nobody has is a row with `depends_on_task IS NULL`, which is
            # the query for "not on this board" — it is no longer a lost link.
            resolved = target in tasks
            if not resolved:
                unresolved_dependencies += 1
            rows["task_dependencies"].append(
                {
                    "task_ref": subject,
                    "depends_on": target,
                    "depends_on_task": target if resolved else None,
                }
            )
            continue
        if target not in tasks:
            report.link_not_imported(
                kind=table,
                subject=subject,
                target=target,
                reason="the referenced card is not on the board, and task_supersessions.supersedes "
                "is a foreign key into tasks(task_ref)",
            )
            continue
        rows["task_supersessions"].append(
            {
                "task_ref": subject,
                "supersedes": target,
                "recorded_at": tasks[subject]["updated_at"],
            }
        )
    if unresolved_dependencies:
        report.approximate_values.append(
            {
                "field": "task_dependencies.depends_on_task",
                "rows": unresolved_dependencies,
                "reason": "the reference names a card that is not on this board; the reference is "
                "stored and the foreign key stays NULL (§8.6), so the dependency is a row rather "
                "than a loss",
            }
        )
    report.extensions_keys += [
        {"key": key, "rows": count, "where": "tasks.extensions.kanboard"}
        for key, count in sorted(extension_keys.items(), key=lambda item: (-item[1], item[0]))
    ]
    return tasks


def _link_sprint_cursors(
    source: BoardSource,
    sprints: dict[str, dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    report: ImportReport,
) -> None:
    """§3.3's scoped cursor: a sprint's current task must be a card of that sprint."""
    assigned = sprint_references(source.sprints)
    for row in source.sprints:
        reference = assigned[row.task_id]
        if reference not in sprints:
            continue
        current = _null_if_empty(row.meta.get("sprint_current_task"))
        if not current:
            continue
        card = tasks.get(current)
        if card is None or card["sprint_ref"] != reference:
            report.link_not_imported(
                kind="sprints.current_task_ref",
                subject=reference,
                target=current,
                reason="sprint_current_task_is_in_this_sprint scopes the cursor to a card this "
                "sprint holds, and this card "
                + ("was not imported" if card is None else "belongs to another sprint")
                + "; the cursor stays NULL",
            )
            continue
        sprints[reference]["current_task_ref"] = current


def _plan_budget(
    source: BoardSource,
    sprints: dict[str, dict[str, Any]],
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
    assigned = sprint_references(source.sprints)
    # Two sprint rows can share the board spelling the journal keys on, so the journal records of
    # one spelling are handed out once and never twice: the second row starts where the first
    # stopped rather than importing the same charge again.
    consumed: Counter[tuple[str, str]] = Counter()
    for row in sorted(source.sprints, key=lambda item: (item.ref, item.archived, item.task_id)):
        reference = assigned[row.task_id]
        if reference not in sprints:
            continue
        budget = _budget(
            row.meta.get("sprint_budget"), thresholds, row.meta.get(BUDGET_UNCHARGED_FIELD)
        )
        counts = {**budget["by_type"], **budget["uncharged"]}
        for event_type in BUDGET_RECORDED_EVENT_TYPES:
            wanted = int(counts.get(event_type, 0))
            counter_total += wanted
            per_type[event_type] += wanted
            already = consumed[(row.ref, event_type)]
            available = journal.get((row.ref, event_type), [])[already:]
            consumed[(row.ref, event_type)] += min(wanted, len(available))
            if len(available) > wanted:
                mismatches.append(
                    {
                        "sprint": reference,
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
                        f"import:budget:{reference}:{event_type}:{index}"
                    )
                    occurred = (
                        _timestamp(record.get("occurred_at")) or sprints[reference]["updated_at"]
                    )
                    reason = f"imported from audit journal record {_text(record.get('event_id'))}"
                else:
                    request_id = f"import:budget:{reference}:{event_type}:{index}"
                    occurred = sprints[reference]["updated_at"]
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
                            "sprint": reference,
                            "event_type": event_type,
                            "imported_by": f"board-import:{IMPORT_VERSION}",
                        },
                        "status": "committed",
                        "protocol": True,
                        "entity_kind": "sprint",
                        "ref": reference,
                        "created_at": occurred,
                        "settled_at": occurred,
                    }
                )
                rows["sprint_budget_events"].append(
                    {
                        "sprint_ref": reference,
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
    sprints: dict[str, dict[str, Any]],
    products: dict[str, dict[str, Any]],
    issues: dict[str, dict[str, Any]],
    rows: dict[str, list[dict[str, Any]]],
    report: ImportReport,
) -> None:
    """§8.1's marker rule, applied once, to every comment on both boards and all four tables.

    The fourth is ``product_comments``, added by 0004. A comment that belongs to a record the
    store does not hold is named one
    line per comment, with the Kanboard comment id, because "479 comments on Issue rows" names a
    number and the question a failing parity asks is *which ones*.
    """
    unrecognized: Counter[str] = Counter()

    def stored(comment: dict[str, Any], fallback: datetime) -> dict[str, Any]:
        body = _text(comment.get("comment"))
        marker, text = parse_marker(body)
        if marker is None and (token := marker_token(body)) is not None:
            unrecognized[token] += 1
        return {
            "marker": marker,
            "body": text,
            "actor_role": marker if marker in MARKER_ROLES else None,
            "actor_id": None,
            "request_id": None,
            "created_at": _required_when(comment.get("date_creation"), fallback),
        }

    def refuse(kind: str, owner_ref: str, comment: dict[str, Any], reason: str) -> None:
        report.record_not_imported(
            kind=kind, ref=comment_identifier(owner_ref, comment), reason=reason
        )

    for row in source.pipeline:
        record_type = row.meta.get(META_RECORD_TYPE)
        if record_type == PRODUCT_TYPE:
            product_id = _text(row.meta.get(META_PRODUCT_ID))
            for comment in row.comments:
                if product_id not in products:
                    refuse(
                        "product comment", row.ref, comment,
                        "the Product itself was not imported, and product_comments.product_id "
                        "is a foreign key into products",
                    )
                    continue
                rows["product_comments"].append(
                    {
                        "product_id": product_id,
                        **stored(comment, products[product_id]["updated_at"]),
                    }
                )
            continue
        if record_type == ISSUE_TYPE:
            issue_id = row.ref.removeprefix("issue:")
            if issue_id not in issues:
                for comment in row.comments:
                    refuse(
                        "issue comment",
                        row.ref,
                        comment,
                        "the Issue itself was not imported, and issue_comments.issue_id is a "
                        "foreign key into issues; the refusal of the Issue names the reason",
                    )
                continue
            for comment in row.comments:
                rows["issue_comments"].append(
                    {
                        "issue_id": issue_id,
                        **stored(comment, issues[issue_id]["updated_at"]),
                    }
                )
            continue
        if row.ref not in tasks:
            for comment in row.comments:
                refuse(
                    "card comment",
                    row.ref or f"card@kanboard-{row.task_id}",
                    comment,
                    "the card itself was not imported, and task_comments.task_ref is a foreign "
                    "key into tasks; the refusal of the card names the reason",
                )
            continue
        # A duplicated reference keeps one card; a comment of the row that lost is still a comment
        # of that reference, so it is imported rather than dropped.
        for comment in row.comments:
            rows["task_comments"].append(
                {"task_ref": row.ref, **stored(comment, tasks[row.ref]["updated_at"])}
            )

    assigned = sprint_references(source.sprints)
    for row in source.sprints:
        reference = assigned[row.task_id]
        if reference not in sprints:
            for comment in row.comments:
                refuse(
                    "sprint comment",
                    row.ref or f"sprint@kanboard-{row.task_id}",
                    comment,
                    "the sprint itself was not imported, and sprint_comments.sprint_ref is a "
                    "foreign key into sprints; the refusal of the sprint names the reason",
                )
            continue
        # Since §9's option 1 gives the archived duplicate a reference of its own, its journal is
        # its own too: nothing merges one sprint's record into another's, and nothing is refused.
        for comment in row.comments:
            rows["sprint_comments"].append(
                {"sprint_ref": reference, **stored(comment, sprints[reference]["updated_at"])}
            )

    report.unrecognized_comment_markers = [
        {"token": token, "comments": count}
        for token, count in sorted(unrecognized.items(), key=lambda item: (-item[1], item[0]))
    ]


def _plan_decisions(
    source: BoardSource,
    sprints: dict[str, dict[str, Any]],
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
    declared_issues = {(link["sprint_ref"], link["issue_id"]) for link in rows["sprint_issues"]}
    recovered: set[str] = set()
    claimed: set[str] = set()
    for document in source.transaction_documents:
        event = document.get("event")
        if not isinstance(event, dict):
            continue
        reference = _text(event.get("ref"))
        if reference not in sprints:
            continue
        payload = event.get("payload")
        plan_of = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(plan_of, dict):
            continue
        request_id = _text(document.get("request_id"))
        decided_at = _timestamp(event.get("occurred_at")) or sprints[reference]["updated_at"]
        entries: list[dict[str, Any]] = []
        for entry in plan_of.get("issues") or []:
            if not isinstance(entry, dict):
                continue
            issue_id = _text(entry.get("ref")).removeprefix("issue:")
            if (reference, issue_id) not in declared_issues:
                report.link_not_imported(
                    kind="sprint_decisions.issue",
                    subject=reference,
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
            if card is None or card["sprint_ref"] != reference:
                report.link_not_imported(
                    kind="sprint_decisions.card",
                    subject=reference,
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
                    "ref": reference,
                    "created_at": decided_at,
                    "settled_at": decided_at,
                }
            )
        recovered.add(reference)
        for entry in entries:
            rows["sprint_decisions"].append(
                {"sprint_ref": reference, "request_id": request_id, "decided_at": decided_at, **entry}
            )
    report.sprints_without_recoverable_decisions = [
        reference
        for reference, row in sorted(sprints.items())
        if row["status"] != "open" and reference not in recovered
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
        (
            "requests",
            "board_events",
            "sprint_budget_events",
            "task_comments",
            "sprint_comments",
            "issue_comments",
            "product_comments",
            "sprint_decisions",
        ),
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
                {"sprint_ref": link["sprint_ref"], "repository_id": repository_id[link["path"]]}
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
            reference: identifier
            for identifier, reference in connection.execute(
                sa.select(_table("sprint_resumes").c.resume_id, _table("sprint_resumes").c.sprint_ref)
            )
        }
        sprints = _table("sprints")
        for row in rows["sprints"]:
            reference = row["ref"]
            resume_id = resume_of.get(reference)
            if row["current_task_ref"] is None and resume_id is None:
                continue
            connection.execute(
                sprints.update()
                .where(sprints.c.ref == reference)
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
            "issue_comments",
            "product_comments",
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
            statement = sa.select(table.c.sprint_ref, repositories.c.path).join_from(
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


@dataclass(frozen=True)
class BoardInventory:
    """What the board holds, by the identifiers parity compares on.  One read, one interpretation.

    Both halves of parity are built from this: the checks, which ask whether the store agrees with
    it, and the per-record listing, which asks *which rows* it holds that the store does not.
    Building it once is what keeps those two from disagreeing.
    """

    products: dict[str, SourceRow]
    issues: dict[str, SourceRow]
    cards: dict[str, SourceRow]
    sprints: dict[str, SourceRow]
    #: Every comment on either board, in board order, as (owner identifier, row, comment).
    comments: tuple[tuple[str, SourceRow, dict[str, Any]], ...]


def board_inventory(source: BoardSource) -> BoardInventory:
    product_rows = [row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == PRODUCT_TYPE]
    issue_rows = [row for row in source.pipeline if row.meta.get(META_RECORD_TYPE) == ISSUE_TYPE]
    card_rows = [
        row
        for row in source.pipeline
        if row.meta.get(META_RECORD_TYPE) not in (PRODUCT_TYPE, ISSUE_TYPE)
    ]
    assigned = sprint_references(source.sprints)
    comments: list[tuple[str, SourceRow, dict[str, Any]]] = []
    for row in source.pipeline:
        kind = {PRODUCT_TYPE: "product", ISSUE_TYPE: "issue"}.get(
            row.meta.get(META_RECORD_TYPE, ""), "card"
        )
        # A duplicated card reference is one card seen twice and its comments merge into the
        # survivor, so a comment's owner is the reference, never the Kanboard row that carries it.
        owner = board_identifier(row, kind)
        for comment in row.comments:
            comments.append((owner, row, comment))
    for row in source.sprints:
        owner = assigned[row.task_id] or board_identifier(row, "sprint")
        for comment in row.comments:
            comments.append((owner, row, comment))
    return BoardInventory(
        products=chosen_by_identifier(product_rows, kind="product"),
        issues=chosen_by_identifier(issue_rows, kind="issue"),
        cards=chosen_by_identifier(card_rows, kind="card"),
        sprints={
            assigned[row.task_id] or board_identifier(row, "sprint"): row for row in source.sprints
        },
        comments=tuple(comments),
    )


#: Where a stored comment's owner reference is read from, per §3.7 table, and how it is spelled so
#: it matches the board identifier of the record it belongs to.
_COMMENT_OWNER = {
    "task_comments": lambda row: row["task_ref"],
    "sprint_comments": lambda row: row["sprint_ref"],
    "issue_comments": lambda row: "issue:" + row["issue_id"],
    "product_comments": lambda row: "product:" + row["product_id"],
}


def _comment_key(owner: str, marker: Any, body: Any, created: Any) -> tuple[str, str, str, str]:
    return (owner, _iso(created), _text(marker), _text(body))


def _stored_comments(rows: dict[str, list[dict[str, Any]]], table: str) -> Counter:
    owner_of = _COMMENT_OWNER[table]
    return Counter(
        _comment_key(owner_of(row), row["marker"], row["body"], row["created_at"])
        for row in rows[table]
    )


def _expected_comments(
    inventory: BoardInventory, owners: set[str], table: str, fallback: dict[str, Any]
) -> Counter:
    """The comments the board holds for the owners the store actually has, as a multiset."""
    expected: Counter = Counter()
    for owner, _row, comment in _comments_of(inventory, table):
        if owner not in owners:
            continue
        marker, body = parse_marker(_text(comment.get("comment")))
        expected[
            _comment_key(
                owner, marker or "", body, _required_when(comment.get("date_creation"), fallback[owner])
            )
        ] += 1
    return expected


def _comments_of(inventory: BoardInventory, table: str):
    """Every board comment that belongs in one §3.7 table."""
    for owner, row, comment in inventory.comments:
        kind = (
            "sprint"
            if owner in inventory.sprints
            else {PRODUCT_TYPE: "product", ISSUE_TYPE: "issue"}.get(
                row.meta.get(META_RECORD_TYPE, ""), "card"
            )
        )
        if _COMMENT_TABLE_OF_KIND.get(kind) == table:
            yield owner, row, comment


#: Which comment table a comment belongs in, by the kind of record it sits on.
_COMMENT_TABLE_OF_KIND = {
    "card": "task_comments",
    "sprint": "sprint_comments",
    "issue": "issue_comments",
    "product": "product_comments",
}


def parity(source: BoardSource, rows: dict[str, list[dict[str, Any]]], report: ImportReport) -> dict[str, Any]:
    """Compare the board against what was stored, on counts, identifiers, links, content and
    accounting — and refuse to call a run green while the store is missing a record.

    **A refusal is not an excuse.**  The previous revision subtracted every record the report had
    refused from what it expected to find, so a run that declined to write 479 comments still
    returned PASS: the report explained the loss and parity agreed with the report instead of with
    the board.  That is a check that cannot fail, and DoD 4 rests on it.  Nothing is subtracted
    here.  A record the board holds and the store does not fails its axis, is listed by its own
    identifier in ``records_missing``, and makes ``ok`` false — so a green parity means *"the whole
    board is in the store"* and nothing else.

    The accounting axis then closes the circle in both directions: every missing record must be
    named in the report, and every record the report names must really be missing.  A refusal that
    is not a loss and a loss that is not named are both defects, and both are now checks.
    """
    inventory = board_inventory(source)

    stored_tasks = {row["task_ref"]: row for row in rows["tasks"]}
    stored_sprints = {row["ref"]: row for row in rows["sprints"]}
    stored_issues = {"issue:" + row["issue_id"]: row for row in rows["issues"]}
    stored_products = {"product:" + row["product_id"]: row for row in rows["products"]}

    board_comment_total = len(inventory.comments)
    stored_comment_total = sum(len(rows[table]) for table, _ in COMMENT_TABLES)
    counts = [
        _check(
            "every board card is a tasks row",
            len(inventory.cards),
            len(rows["tasks"]),
            note=f"{sum(1 for row in inventory.cards.values() if row.archived)} of them archived",
        ),
        _check("every Issue row is an issues row", len(inventory.issues), len(rows["issues"])),
        _check("every Product row is a products row", len(inventory.products), len(rows["products"])),
        _check(
            "every sprint board row is a sprints row",
            len(inventory.sprints),
            len(rows["sprints"]),
            note="one row per Kanboard row, not per reference: §9's option 1 stores the archived "
            "duplicate of a reference under a distinguishing one rather than dropping it",
        ),
        _check(
            "every comment on either board is stored, in one of the four comment tables",
            board_comment_total,
            stored_comment_total,
            note=", ".join(f"{table}: {len(rows[table])}" for table, _ in COMMENT_TABLES),
        ),
    ]

    identifiers = [
        _check("task refs, literally", set(inventory.cards), set(stored_tasks)),
        _check("issue refs, literally", set(inventory.issues), set(stored_issues)),
        _check("product refs, literally", set(inventory.products), set(stored_products)),
        _check(
            "sprint refs, literally",
            set(inventory.sprints),
            set(stored_sprints),
            note="the reference each row is stored under; a disambiguated one is listed in the "
            "report and keeps its original spelling in sprints.source_audit",
        ),
        _check(
            "task numbers agree with the reference suffix",
            {(row["task_ref"], _task_number_of(row["task_ref"])) for row in rows["tasks"]},
            {(row["task_ref"], row["task_number"]) for row in rows["tasks"]},
        ),
        _check(
            "a numbered sprint reference keeps its number, and only a numbered one has one",
            {(row["ref"], _sprint_number_of(row["ref"])) for row in rows["sprints"]},
            {(row["ref"], row["sprint_number"]) for row in rows["sprints"]},
        ),
        _check(
            "sprint transport keys agree with their references",
            {(row["ref"], record_key("sprint", row["ref"])) for row in rows["sprints"]},
            {(row["ref"], row["board_key"]) for row in rows["sprints"]},
        ),
    ]

    expected_card_sprint = {
        (ref, _text(row.meta.get("sprint_ref")))
        for ref, row in inventory.cards.items()
        if ref in stored_tasks and _text(row.meta.get("sprint_ref")) in stored_sprints
    }
    expected_sprint_issues = {
        (ref, value.removeprefix("issue:"))
        for ref, row in inventory.sprints.items()
        for value in _json_list(row.meta.get("sprint_issues"))
        if ref in stored_sprints and value in stored_issues
    }
    expected_sprint_repositories = {
        (ref, path)
        for ref, row in inventory.sprints.items()
        for path in _json_list(row.meta.get("sprint_repositories"))
        if ref in stored_sprints
    }
    expected_dependencies = {
        (ref, _text(row.meta.get("blocked_by")))
        for ref, row in inventory.cards.items()
        if ref in stored_tasks
        and _text(row.meta.get("blocked_by"))
        and _text(row.meta.get("blocked_by")) != ref
    }
    links = [
        _check(
            "card -> sprint, from the card's side",
            expected_card_sprint,
            {(row["task_ref"], row["sprint_ref"]) for row in rows["tasks"] if row["sprint_ref"]},
        ),
        _check(
            "sprint -> card, from the sprint's side",
            {reference for _, reference in expected_card_sprint},
            {row["sprint_ref"] for row in rows["tasks"] if row["sprint_ref"]},
        ),
        _check(
            "sprint <-> issue",
            expected_sprint_issues,
            {(row["sprint_ref"], row["issue_id"]) for row in rows["sprint_issues"]},
        ),
        _check(
            "sprint <-> repository, by path",
            expected_sprint_repositories,
            {(row["sprint_ref"], row["path"]) for row in rows["sprint_repositories"]},
        ),
        _check(
            "card -> card dependency, resolved or not",
            expected_dependencies,
            {(row["task_ref"], row["depends_on"]) for row in rows["task_dependencies"]},
            note="a dependency naming a card the board does not hold is a row with "
            "depends_on_task NULL (§8.6), so it is compared here like any other",
        ),
        _check(
            "a resolved dependency names a card the store holds",
            set(),
            {
                (row["task_ref"], row["depends_on_task"])
                for row in rows["task_dependencies"]
                if row["depends_on_task"] and row["depends_on_task"] not in stored_tasks
            },
        ),
        _check(
            "the sprint cursor points inside its own sprint",
            set(),
            {
                row["ref"]
                for row in rows["sprints"]
                if row["current_task_ref"]
                and stored_tasks.get(row["current_task_ref"], {}).get("sprint_ref") != row["ref"]
            },
        ),
        _check(
            "every product names its projects",
            {
                (ref.removeprefix("product:"), project)
                for ref, row in inventory.products.items()
                for project in _json_list(row.meta.get(META_PRODUCT_PROJECTS))
                if ref in stored_products
                and ("product_projects", ref, project)
                not in {
                    (item["kind"], item["subject"], item["target"])
                    for item in report.links_not_imported
                }
            },
            {(row["product_id"], row["project_id"]) for row in rows["product_projects"]},
        ),
    ]

    fallback = {
        **{ref: row["updated_at"] for ref, row in stored_tasks.items()},
        **{ref: row["updated_at"] for ref, row in stored_sprints.items()},
        **{ref: row["updated_at"] for ref, row in stored_issues.items()},
        **{ref: row["updated_at"] for ref, row in stored_products.items()},
    }
    owners = set(stored_tasks) | set(stored_sprints) | set(stored_issues) | set(stored_products)
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
                for ref, row in inventory.cards.items()
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
                for ref, row in inventory.issues.items()
                if ref in stored_issues
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
                    ref,
                    row.meta.get("sprint_goal", ""),
                    row.meta.get("sprint_definition_of_done", ""),
                    row.meta.get("sprint_status")
                    if row.meta.get("sprint_status") in SPRINT_STATUSES
                    else "open",
                )
                for ref, row in inventory.sprints.items()
                if ref in stored_sprints
            },
            {
                (row["ref"], row["goal"], row["definition_of_done"], row["status"])
                for row in rows["sprints"]
            },
        ),
        _check(
            "the budget counters equal the rows, per sprint and per type",
            _counter_totals(source, stored_sprints),
            Counter((row["sprint_ref"], row["event_type"]) for row in rows["sprint_budget_events"]),
        ),
    ]
    # One content check per comment table.  The axis used to compare card comments
    # alone, so a reviewer could change the body of a stored *sprint* comment from `original` to
    # `altered` and parity still returned True.  Each table is now its own multiset of
    # (owner, second, marker, body), which is what makes an altered body a failure wherever it is.
    for table, _column in COMMENT_TABLES:
        content.append(
            _check(
                f"{table}: owner, second, marker and body, as a multiset",
                _expected_comments(inventory, owners, table, fallback),
                _stored_comments(rows, table),
                note="a multiset and not a set, because comments sharing a record, a second and a "
                "body are ordinary on this board",
            )
        )

    missing = _records_missing(inventory, rows, stored_tasks, stored_sprints, stored_issues,
                              stored_products, owners, fallback, report)
    named = {item["ref"] for item in report.records_not_imported}
    found = {item["id"] for item in missing}
    accounting = [
        _check(
            "every record the store is missing is named in the report, by its own identifier",
            set(),
            found - named,
            note="§8's rule: a record that is neither imported nor named is a defect. This is the "
            "check that makes it one.",
        ),
        _check(
            "every record the report refuses is really absent from the store",
            set(),
            named - found,
            note="a refusal that names a record the store does hold is a false alarm, and it "
            "would teach a reader to discount the list that matters",
        ),
    ]

    axes = {
        "counts": counts,
        "identifiers": identifiers,
        "links": links,
        "content": content,
        "accounting": accounting,
    }
    return {
        "ok": all(check["ok"] for checks in axes.values() for check in checks) and not missing,
        "records_missing": missing,
        **axes,
    }


def _records_missing(
    inventory: BoardInventory,
    rows: dict[str, list[dict[str, Any]]],
    stored_tasks: dict[str, Any],
    stored_sprints: dict[str, Any],
    stored_issues: dict[str, Any],
    stored_products: dict[str, Any],
    owners: set[str],
    fallback: dict[str, Any],
    report: ImportReport,
) -> list[dict[str, Any]]:
    """Every board record the store does not hold, one entry each, with its own identifier.

    This is the half of parity that answers "which ones".  An aggregate — "479 comments on Issue
    rows" — states a number nobody can act on: it does not say which comments, so it cannot be
    checked, repaired or even looked up.  Every entry here carries an identifier that names one
    board record and no other, and the reason the report gave for refusing it where it gave one.
    """
    reasons = {item["ref"]: item["reason"] for item in report.records_not_imported}
    missing: list[dict[str, Any]] = []

    def note(kind: str, identifier: str) -> None:
        missing.append(
            {
                "kind": kind,
                "id": identifier,
                "reason": reasons.get(identifier, "the store holds no row for it and the report "
                "does not say why: this is the defect §8 calls a record neither imported nor named"),
            }
        )

    for kind, board, stored in (
        ("product", inventory.products, stored_products),
        ("issue", inventory.issues, stored_issues),
        ("card", inventory.cards, stored_tasks),
        ("sprint", inventory.sprints, stored_sprints),
    ):
        for identifier in sorted(set(board) - set(stored)):
            note(kind, identifier)

    # Comments have no stored identity the board shares, so they are matched on what a reader can
    # see — the record, the second, the marker and the body — and the ones the store has no slot
    # for are named individually, in board order.
    stored_counts: Counter = Counter()
    for table, _column in COMMENT_TABLES:
        stored_counts += _stored_comments(rows, table)
    seen: Counter = Counter()
    for owner, row, comment in inventory.comments:
        kind = {PRODUCT_TYPE: "product", ISSUE_TYPE: "issue"}.get(
            row.meta.get(META_RECORD_TYPE, ""), "card"
        )
        if owner in inventory.sprints:
            kind = "sprint"
        identifier = comment_identifier(owner, comment)
        if owner not in owners:
            note(f"{kind} comment", identifier)
            continue
        marker, body = parse_marker(_text(comment.get("comment")))
        key = _comment_key(
            owner, marker or "", body, _required_when(comment.get("date_creation"), fallback[owner])
        )
        seen[key] += 1
        if seen[key] > stored_counts[key]:
            note(f"{kind} comment", identifier)
    return missing


def _counter_totals(source: BoardSource, stored_sprints: dict[str, Any]) -> Counter:
    totals: Counter = Counter()
    assigned = sprint_references(source.sprints)
    for row in source.sprints:
        reference = assigned[row.task_id]
        if reference not in stored_sprints:
            continue
        budget = _budget(row.meta.get("sprint_budget"), None, row.meta.get(BUDGET_UNCHARGED_FIELD))
        counts = {**budget["by_type"], **budget["uncharged"]}
        for event_type in BUDGET_RECORDED_EVENT_TYPES:
            wanted = int(counts.get(event_type, 0))
            if wanted:
                totals[(reference, event_type)] = wanted
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
    "COMMENT_TABLES",
    "DISAMBIGUATION_SUFFIX",
    "IMPORT_VERSION",
    "MARKER_EXACT",
    "MARKER_PREFIXES",
    "MARKER_ROLES",
    "PARITY_AXES",
    "SOURCE_AUDIT_ORIGINAL_REF",
    "TABLE_ORDER",
    "TASK_KNOWN_METADATA",
    "TRANSACTION_GROUPS",
    "BoardImportError",
    "BoardInventory",
    "BoardSource",
    "ImportPlan",
    "ImportReport",
    "RegistryEntry",
    "SourceRow",
    "apply",
    "board_identifier",
    "board_inventory",
    "chosen_by_identifier",
    "comment_identifier",
    "engine_for",
    "fetch_rows",
    "marker_token",
    "occupied_tables",
    "one_row_per_ref",
    "parity",
    "parse_marker",
    "plan",
    "read_budget_records",
    "read_registry",
    "read_source",
    "read_transaction_documents",
    "render",
    "run",
    "sprint_references",
]
