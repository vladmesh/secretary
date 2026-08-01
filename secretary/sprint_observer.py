"""The observer a sprint declares: one durable tagged value, four forms, nothing implied.

A sprint carries exactly one observer value. There is no dynamic default, no value inherited
from the head registry, no missing-field fallback and no permanent tri-state:

  {"kind": "head", "profile": "claude-observer"}   executable, one concrete head
  {"kind": "none"}                                  executable, the sprint runs without one
  {"kind": "historical", "profile": <head>,         a closed row whose head the migration
   "source": "observer_lifecycle_audit",            recovered from durable lifecycle events
   "event_id": <evt>}
  {"kind": "historical", "profile": null,           a closed row that never launched one, so
   "source": "migration_unknown"}                   there is nothing honest to recover

A historical value is never executable: it is provenance of what happened, not a declaration of
what to run. An open sprint carrying one is corrupt in exactly the way a missing value is.

The absent field is not a fifth form. It is what a row of an installation that has not run
`secretary sprint migrate-observer` looks like, and the strict reader is activated only once no
such row is left; see `strict_reader_active`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OBSERVER_FIELD = "sprint_observer"

KIND_HEAD = "head"
KIND_NONE = "none"
KIND_HISTORICAL = "historical"

SOURCE_LIFECYCLE_AUDIT = "observer_lifecycle_audit"
SOURCE_MIGRATION_UNKNOWN = "migration_unknown"

# The spelling `--observer none` is given as a profile name would be, so the one word that cannot
# be a head profile is reserved here rather than guessed at the CLI boundary.
NONE_SPELLING = "none"

# Written by the last step of the cutover, after a full rescan proved every row migrated. Its
# presence is what switches the reader from tolerant to strict, and nothing removes it: an
# installation does not go back to interpreting an absent field.
STRICT_MARKER = "sprints/observer-strict.json"

# Why an open sprint's declared observer cannot be executed. Each is corruption that fails closed,
# and they are named apart because the repair differs.
REASON_MISSING = "observer_missing"
REASON_MALFORMED = "observer_malformed"
REASON_HISTORICAL = "observer_historical"
REASON_UNKNOWN_PROFILE = "observer_unknown_profile"


class ObserverMetadataError(Exception):
    """An open sprint whose declared observer cannot be executed.

    Carries the reason apart from the message: the fence keys its durable outcome on it, and an
    operator repairing the row needs to know whether the field is gone or merely unreadable.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def head_choice(profile: str) -> dict[str, Any]:
    return {"kind": KIND_HEAD, "profile": str(profile)}


def none_choice() -> dict[str, Any]:
    return {"kind": KIND_NONE}


def historical_recovered(profile: str, event_id: str) -> dict[str, Any]:
    return {
        "kind": KIND_HISTORICAL,
        "profile": str(profile),
        "source": SOURCE_LIFECYCLE_AUDIT,
        "event_id": str(event_id),
    }


def historical_unknown() -> dict[str, Any]:
    return {"kind": KIND_HISTORICAL, "profile": None, "source": SOURCE_MIGRATION_UNKNOWN}


def parse_observer(raw: Any) -> dict[str, Any] | None:
    """One of the four tagged forms, or None for anything else.

    None means the row carries something that is not an observer value. It never means "absent":
    the caller decides that from whether the metadata key is there at all, because an absent field
    and an unreadable one are repaired differently.

    The shapes are matched exactly, keys included. A value with an extra key is not a form this
    module knows, and reading it as the form it resembles would let a writer nobody audited put
    fields on the sprint's most load-bearing decision.
    """
    source = raw
    if isinstance(raw, str):
        try:
            source = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(source, dict):
        return None
    kind = source.get("kind")
    keys = set(source)
    if kind == KIND_NONE and keys == {"kind"}:
        return none_choice()
    if kind == KIND_HEAD and keys == {"kind", "profile"}:
        profile = source.get("profile")
        if isinstance(profile, str) and profile.strip():
            return head_choice(profile.strip())
        return None
    if kind == KIND_HISTORICAL and keys == {"kind", "profile", "source", "event_id"}:
        profile = source.get("profile")
        event_id = source.get("event_id")
        if (
            source.get("source") == SOURCE_LIFECYCLE_AUDIT
            and isinstance(profile, str) and profile.strip()
            and isinstance(event_id, str) and event_id.strip()
        ):
            return historical_recovered(profile.strip(), event_id.strip())
        return None
    if kind == KIND_HISTORICAL and keys == {"kind", "profile", "source"}:
        if source.get("source") == SOURCE_MIGRATION_UNKNOWN and source.get("profile") is None:
            return historical_unknown()
        return None
    return None


def encode_observer(value: dict[str, Any]) -> str:
    """The exact text one observer value is stored as, so equality is byte equality.

    The migration compares what a row already holds against what it would write, and a refusal to
    overwrite has to mean "a different value", not "the same value serialized differently".
    """
    parsed = parse_observer(value)
    if parsed is None:
        raise ValueError(f"not an observer value: {value!r}")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def is_executable(value: dict[str, Any] | None) -> bool:
    return isinstance(value, dict) and value.get("kind") in {KIND_HEAD, KIND_NONE}


def observer_choice(spelling: str) -> dict[str, Any] | None:
    """The executable value an operator spelled, or None when the word is not one.

    `none` is the sprint that runs without an observer; anything else is read as the profile of
    the one concrete head it declares. There is no spelling for `default` or `inherited`.
    """
    text = (spelling or "").strip()
    if not text:
        return None
    if text == NONE_SPELLING:
        return none_choice()
    return head_choice(text)


def declared_observer(sprint: dict[str, Any]) -> dict[str, Any] | None:
    """The value on a sprint record, or None when it declares none this module can read."""
    if "observer" not in sprint:
        return None
    return parse_observer(sprint.get("observer"))


def executable_observer(sprint: dict[str, Any]) -> dict[str, Any]:
    """The choice an open sprint declares, or `ObserverMetadataError` naming why there is none."""
    if "observer" not in sprint:
        raise ObserverMetadataError(
            REASON_MISSING,
            f"sprint {sprint.get('ref') or '?'} declares no observer",
        )
    value = parse_observer(sprint.get("observer"))
    if value is None:
        raise ObserverMetadataError(
            REASON_MALFORMED,
            f"sprint {sprint.get('ref') or '?'} carries an observer value that is not one of the "
            "four tagged forms",
        )
    if not is_executable(value):
        raise ObserverMetadataError(
            REASON_HISTORICAL,
            f"sprint {sprint.get('ref') or '?'} carries migration provenance "
            f"({value.get('source')}), which is a record of what ran and never a head to run",
        )
    return value


def strict_marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / STRICT_MARKER


def strict_reader_active(data_dir: str | Path | None) -> bool:
    """Whether every sprint row of this installation has been backfilled.

    False for an installation that has not run the migration, and for a caller that has no data
    directory to ask. Both read the tolerant way, which is the only correct answer while a row
    that predates the field may still exist: the strict reader must never judge an unmigrated row.
    """
    if data_dir is None:
        return False
    try:
        raw = json.loads(strict_marker_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(raw, dict) and raw.get("version") == 1 and raw.get("strict") is True


def activate_strict_reader(
    data_dir: str | Path, *, inventory_digest: str, rows: int, activated_at: str,
) -> dict[str, Any]:
    """Switch this installation to the strict reader, once and durably.

    The digest and the row count are what the activation was justified by, so an operator reading
    the marker later can tell which scan it followed. A second activation of the same cutover
    rewrites the same content; it is not an error, because the step it ends is retried as a whole.
    """
    payload = {
        "version": 1,
        "strict": True,
        "activated_at": str(activated_at),
        "inventory_digest": str(inventory_digest),
        "rows": int(rows),
    }
    path = strict_marker_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)
    return payload
