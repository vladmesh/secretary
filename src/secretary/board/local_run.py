"""Creation-only sprint authority for exact commands on the control host.

The public field is a list of {project, argv, rationale} objects. argv includes the
executable, preserves every argument (including empty arguments), and grants only
that vector. Missing fields on released sprints and create intents mean [].
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

LOCAL_RUN_EXCEPTIONS_FIELD = "sprint_local_run_exceptions"


def _text(value: Any) -> bool:
    return isinstance(value, str) and not any(ord(char) < 32 or ord(char) == 127 for char in value)


@dataclass(frozen=True, slots=True)
class LocalRunException:
    project: str
    argv: tuple[str, ...]
    rationale: str

    def to_document(self) -> dict[str, Any]:
        return {"project": self.project, "argv": list(self.argv), "rationale": self.rationale}


def parse_local_run_exceptions(value: Any, *, projects: Sequence[str]) -> tuple[LocalRunException, ...]:
    """Validate the whole declaration; a malformed member never leaves partial authority."""
    if not isinstance(value, list):
        raise ValueError("local_run_exceptions must be a list")  # noqa: TRY004 - uniform JSON value validation
    entries = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"project", "argv", "rationale"}:
            raise ValueError("local_run_exceptions entries require exactly project, argv and rationale")
        project, argv, rationale = item["project"], item["argv"], item["rationale"]
        if not _text(project) or not project or project not in projects:
            raise ValueError("local_run_exceptions project must be reserved by this sprint")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(_text(arg) for arg in argv)
            or not argv[0].strip()
        ):
            raise ValueError("local_run_exceptions argv must name an executable and exact string arguments")
        if not _text(rationale) or not rationale.strip():
            raise ValueError(
                "local_run_exceptions rationale must be nonempty text without control characters"
            )
        entries.append(LocalRunException(project, tuple(argv), rationale))
    return tuple(entries)


def stored_local_run_exceptions(value: str | None, *, projects: Sequence[str]) -> list[dict[str, Any]]:
    """Decode metadata strictly; only absence, not malformed JSON, means the empty default."""
    raw = [] if value is None else json.loads(value)
    return [entry.to_document() for entry in parse_local_run_exceptions(raw, projects=projects)]


def parse_local_run_policy(value: Any) -> tuple[LocalRunException, ...]:
    """Validate a launch snapshot, already scoped by the dispatcher to one card's project.

    Only an explicit launch argument supplies this document. Environment values and files are
    not authority. Validate the entire snapshot again at the role boundary and in the guard.
    """
    if not isinstance(value, Mapping) or set(value) != {"card", "sprint", "project", "exceptions"}:
        raise ValueError("malformed local-run launch snapshot")
    patterns = {
        "card": r"[a-z0-9][a-z0-9-]*-[0-9]+",
        "sprint": r"sprint:[0-9]+",
        "project": r"[a-z0-9][a-z0-9-]*",
    }
    for field, pattern in patterns.items():
        identity = value[field]
        if not isinstance(identity, str) or not re.fullmatch(pattern, identity):
            raise ValueError(f"malformed local-run {field} identity")
    return parse_local_run_exceptions(value["exceptions"], projects=[value["project"]])
