"""What a card of each kind must leave behind before it may be Done.

A `code` card's evidence is its merged candidate and is produced by the merge itself. A `research`
or `infra` card has no candidate: no branch is published, no pull request is opened and no CI runs
for it, so its completion is proved by a marked dispatcher comment on the card instead.

- `infra`: the worker's `report:done` body carries `## What was done` and `## How to verify`; the
  dispatcher copies both into one `[completion:infra]` comment when it accepts the report.
- `research`: the worker leaves its report in `.secretary-report/` of its workspace, with a non-empty
  `report.md`. After the report is accepted and any review is done, and before the card parks in
  Assessment or is released, the dispatcher copies that directory to
  `state/knowledge/reports/<card ref>/` through the knowledge directory writer and writes one
  `[completion:research]` comment naming it. A refused or failed transfer Blocks the card.

Both markers are read only from comments the dispatcher wrote, so a worker or reviewer comment that
happens to contain the marker line proves nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from secretary.board.task_routing import TaskReview, TaskType

NO_CANDIDATE_KINDS = frozenset({TaskType.RESEARCH.value, TaskType.INFRA.value})

INFRA_COMPLETION_MARKER = "completion:infra"
RESEARCH_COMPLETION_MARKER = "completion:research"
INFRA_REPORT_SECTIONS = ("What was done", "How to verify")
# The research report directory, relative to the worker's workspace, and the file it must hold.
RESEARCH_REPORT_DIR = ".secretary-report"
RESEARCH_REPORT_FILE = "report.md"

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
_DISPATCHER_LINE = "[dispatcher]"


def has_candidate(task: Mapping[str, Any]) -> bool:
    """Whether this card delivers a candidate branch. A card of unknown kind is treated as code."""
    return str(task.get("type") or "") not in NO_CANDIDATE_KINDS


def review_required(task: Mapping[str, Any]) -> bool:
    """The stored review choice. A card written before the choice was stored reads as required."""
    return str(task.get("review") or "") != TaskReview.SKIPPED.value


def research_report_path(reference: str) -> str:
    return f"state/knowledge/reports/{reference}/"


def research_report_refusal(workspace: Path) -> str:
    """Why this workspace holds no research report (`""` when `.secretary-report/report.md` is non-empty)."""
    report = Path(workspace) / RESEARCH_REPORT_DIR / RESEARCH_REPORT_FILE
    try:
        empty = report.is_symlink() or not report.is_file() or not report.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        empty = True
    if not empty:
        return ""
    return (
        f"a research done report needs a non-empty `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}` in the "
        f"workspace ({workspace}); write the report there, with any artifacts beside it in "
        f"`{RESEARCH_REPORT_DIR}/`, and report again"
    )


def _sections(body: str) -> dict[str, str]:
    """Level-2 sections of a Markdown body, by heading text; deeper headings stay in their section."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        match = None if fence else _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) <= 2:
            current = found.setdefault(match.group(2).strip(), []) if len(match.group(1)) == 2 else None
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def infra_report_fields(body: str) -> tuple[dict[str, str], str]:
    """The two infra report fields, and why the body lacks them (`""` when it has both)."""
    sections = _sections(body)
    fields = {name: sections.get(name, "") for name in INFRA_REPORT_SECTIONS}
    missing = [f"`## {name}`" for name, text in fields.items() if not text]
    if not missing:
        return fields, ""
    return fields, (
        "an infra done report must carry two non-empty sections, `## What was done` and "
        "`## How to verify` (a command or an observation); missing or empty: " + ", ".join(missing)
    )


def render_infra_completion_record(fields: Mapping[str, str]) -> str:
    """The comment body the dispatcher writes; `infra_completion_record` reads it back."""
    parts = [f"[{INFRA_COMPLETION_MARKER}]", ""]
    for name in INFRA_REPORT_SECTIONS:
        parts += [f"## {name}", "", str(fields.get(name) or "").strip(), ""]
    return "\n".join(parts).rstrip() + "\n"


def render_research_completion_link(reference: str) -> str:
    return f"[{RESEARCH_COMPLETION_MARKER}]\n\n{research_report_path(reference)}\n"


def _dispatcher_marked(comments: Iterable[Mapping[str, Any]], marker: str) -> list[str]:
    """Bodies of dispatcher comments whose first content line is `[marker]`, oldest first."""
    bodies: list[str] = []
    for comment in comments:
        if comment.get("marker") != "dispatcher":
            continue
        lines = str(comment.get("body") or "").splitlines()
        if lines and lines[0].strip() == _DISPATCHER_LINE:
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
        if lines and lines[0].strip() == f"[{marker}]":
            bodies.append("\n".join(lines[1:]))
    return bodies


def infra_completion_record(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The latest well-formed infra completion record on the card, or None."""
    for body in reversed(_dispatcher_marked(task.get("comments") or [], INFRA_COMPLETION_MARKER)):
        fields, refusal = infra_report_fields(body)
        if not refusal:
            return fields
    return None


def research_completion_link(task: Mapping[str, Any]) -> str | None:
    """The report directory the latest research completion link names, or None."""
    expected = research_report_path(str(task.get("ref") or ""))
    for body in reversed(_dispatcher_marked(task.get("comments") or [], RESEARCH_COMPLETION_MARKER)):
        if any(line.strip().strip("`") == expected for line in body.splitlines()):
            return expected
    return None


def missing_completion_evidence(task: Mapping[str, Any]) -> str:
    """Name the completion evidence a research/infra card lacks, or `""` when it has it.

    A `code` card answers `""`: its evidence is the merge, which the release performs itself.
    """
    kind = str(task.get("type") or "")
    if kind == TaskType.INFRA.value:
        return "" if infra_completion_record(task) is not None else INFRA_COMPLETION_MARKER
    if kind == TaskType.RESEARCH.value:
        return "" if research_completion_link(task) is not None else RESEARCH_COMPLETION_MARKER
    return ""


def no_candidate_report_contract(kind: str) -> list[str]:
    """The report contract a research/infra worker is handed in its task document."""
    if kind == TaskType.INFRA.value:
        return [
            "## Report contract for an infra card",
            "",
            "This card has no candidate: no branch is published, no pull request is opened and no",
            "CI runs for it, and nothing needs to be committed. Your done report body must carry two",
            "non-empty sections, which the dispatcher copies into the card's completion record:",
            "",
            "    ## What was done",
            "    ## How to verify",
            "",
            "`How to verify` is a command someone can run or an observation someone can repeat. A",
            "done report without both is refused.",
            "",
        ]
    if kind == TaskType.RESEARCH.value:
        return [
            "## Report contract for a research card",
            "",
            "This card has no candidate: no branch is published, no pull request is opened and no",
            "CI runs for it, and nothing needs to be committed. Put the report and every artifact",
            f"(markdown, scripts, data) in `{RESEARCH_REPORT_DIR}/` at the root of this workspace, with",
            f"the report itself in `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}` (non-empty); subdirectories",
            "are fine. Do not commit that directory. A done report without the file is refused.",
            "",
            "After the report is accepted and any review is done, the dispatcher copies the whole",
            f"directory to `{research_report_path('<card ref>')}` in the instance repository and links",
            "it on the card; that link is the completion evidence. The copy is refused, and the card",
            "Blocked, for a symlink or special file, a secret in any text file, or more than 20 MiB in",
            "total. A rework round's next report replaces the directory.",
            "",
        ]
    return []
