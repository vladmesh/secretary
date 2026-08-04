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

The absent field is not a fifth form. Every row carries a value, and a row without one is corrupt.
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

    A caller comparing what a row already holds against what it would write needs a difference to
    mean "a different value", not "the same value serialized differently".
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


def installed_observer_profiles(instance: str | Path | None) -> set[str]:
    """The head profiles this installation runs off, or `ObserverMetadataError`.

    The same snapshot the dispatcher resolves a declared head against (`InstanceCatalog` reads
    `installed_heads` too), so "valid profile" means one thing at every boundary: the transition
    that declares it, the recovery that republishes it, and the fence that judges it at a tick.

    A registry that cannot be read is a refusal, never a pass.  Accepting a declaration nobody
    could check is how an open sprint ends up fenced the moment it opens.
    """
    if instance is None:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE,
            "the head registry is needed to validate an observer profile, and no instance "
            "directory was given",
        )
    from secretary.head_registry import HeadRegistryConfigError, installed_heads

    try:
        profiles = installed_heads(Path(instance)).get("profiles")
    except HeadRegistryConfigError as exc:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE, f"the head registry could not be read: {exc}"
        ) from None
    if not isinstance(profiles, dict):
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE, "the head registry has no profiles table"
        )
    return {str(name) for name in profiles}


def check_observer_profile(value: dict[str, Any], profiles: set[str], *, subject: str) -> None:
    """Refuse a declared head the registry does not have. `none` and provenance have no profile."""
    if not isinstance(value, dict) or value.get("kind") != KIND_HEAD:
        return
    profile = str(value.get("profile") or "")
    if profile not in profiles:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE,
            f"{subject} declares observer head {profile!r}, which is not a profile of this "
            "installation's head registry",
        )
