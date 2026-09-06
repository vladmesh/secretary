"""The observer a sprint declares: one durable tagged value, four forms, nothing implied.

A sprint carries exactly one observer value. There is no dynamic default, no value inherited from
the head registry, no missing-field fallback and no permanent tri-state:

  {"kind": "head", "profile": "claude-observer"}   executable, one concrete head
  {"kind": "none"}                                  executable, the sprint runs without one
  {"kind": "historical", "profile": <head>,         a closed row whose head the migration
   "source": "observer_lifecycle_audit",            recovered from durable lifecycle events
   "event_id": <evt>}
  {"kind": "historical", "profile": null,           a closed row that never launched one, so
   "source": "migration_unknown"}                   there is nothing honest to recover

A historical value is never executable: it is provenance of what happened, not a declaration of
what to run. An open sprint carrying one is corrupt in exactly the way a missing value is. The
absent field is not a fifth form — every row carries a value, and a row without one is corrupt.

Beside the observer a sprint may also pin the two executor roles it cuts cards for, and those are a
different kind of value: optional. See "The optional executor pins" at the bottom of this module —
the pattern is the observer's (one durable field, parsed here, checked against the same registry),
the contract is not (there is no `none`, and an absent field is a legal, meaningful state).
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
    """An open sprint whose declared observer cannot be executed."""

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

    None means the row carries something that is not an observer value. It never means "absent": the
    caller decides that from whether the metadata key is there at all, because an absent field and an
    unreadable one are repaired differently.

    The shapes are matched exactly, keys included: reading a value with an extra key as the form it
    resembles would let a writer nobody audited put fields on the sprint's most load-bearing decision.
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
            and isinstance(profile, str)
            and profile.strip()
            and isinstance(event_id, str)
            and event_id.strip()
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

    A caller comparing what a row holds against what it would write needs a difference to mean "a
    different value", not "the same value serialized differently".
    """
    parsed = parse_observer(value)
    if parsed is None:
        raise ValueError(f"not an observer value: {value!r}")
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def is_executable(value: dict[str, Any] | None) -> bool:
    return isinstance(value, dict) and value.get("kind") in {KIND_HEAD, KIND_NONE}


def observer_choice(spelling: str) -> dict[str, Any] | None:
    """The executable value an operator spelled, or None when the word is not one."""
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


def installed_head_profiles(instance: str | Path | None) -> set[str]:
    """The head profiles this installation runs off, or `ObserverMetadataError`.

    The same snapshot the dispatcher resolves a declared head against, so "valid profile" means one
    thing at every boundary. A registry that cannot be read is a refusal, never a pass: accepting a
    declaration nobody could check is how an open sprint ends up fenced the moment it opens.
    """
    if instance is None:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE,
            "the head registry is needed to validate a head profile, and no instance directory was given",
        )
    from secretary.head_registry import HeadRegistryConfigError, installed_heads

    try:
        profiles = installed_heads(Path(instance)).get("profiles")
    except HeadRegistryConfigError as exc:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE, f"the head registry could not be read: {exc}"
        ) from None
    if not isinstance(profiles, dict):
        raise ObserverMetadataError(REASON_UNKNOWN_PROFILE, "the head registry has no profiles table")
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


# --- The optional executor pins -----------------------------------------------------------------
#
# A sprint may fix the head profile its cards run on, for the worker role and for the reviewer role,
# independently. Unlike the observer, this value is genuinely optional, and the three states it has
# are kept apart everywhere:
#
#   the field is absent   the owner pinned nothing; the observer picks the profile per card
#   the field names a     every card of this sprint runs that role on exactly that profile
#   registry profile
#   the field holds       corruption; it is reported as such and never read as "pinned nothing"
#   anything else
#
# There is deliberately no `none` here. `--observer none` means "this sprint runs without an
# observer", which is a thing a sprint can be; a sprint whose cards run without a worker is not, so
# the word is refused rather than quietly turned into the absent state.

WORKER_FIELD = "sprint_worker"
REVIEWER_FIELD = "sprint_reviewer"
# The two executor roles, in the order they are shown, with the field each one is stored in.
EXECUTOR_FIELDS: dict[str, str] = {"worker": WORKER_FIELD, "reviewer": REVIEWER_FIELD}

EXECUTOR_UNSET = "unset"
EXECUTOR_PINNED = "pinned"
EXECUTOR_MALFORMED = "malformed"


def executor_unset() -> dict[str, Any]:
    """No constraint: the owner pinned no profile for this role."""
    return {"state": EXECUTOR_UNSET}


def executor_pinned(profile: str) -> dict[str, Any]:
    return {"state": EXECUTOR_PINNED, "profile": str(profile)}


def executor_malformed() -> dict[str, Any]:
    return {"state": EXECUTOR_MALFORMED}


def parse_executor(raw: Any) -> dict[str, Any]:
    """The stored pin of one executor role, as one of its three states.

    Absence is decided by the caller from whether the metadata key is there at all, exactly as it is
    for the observer. What arrives here is a value somebody wrote, so a value that is not a profile
    name is `malformed` and never `unset`: a corrupt field is not a sprint that chose to pin nothing.
    """
    if not isinstance(raw, str):
        return executor_malformed()
    if not raw or raw != raw.strip() or raw == NONE_SPELLING:
        return executor_malformed()
    return executor_pinned(raw)


def encode_executor(profile: str) -> str:
    """The exact text one pin is stored as, so equality is byte equality."""
    value = parse_executor(profile)
    if value["state"] != EXECUTOR_PINNED:
        raise ValueError(f"not an executor profile: {profile!r}")
    return str(value["profile"])


def stored_executors(meta: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Both pins as the row holds them, with a state for each role whatever the row carries."""
    return {
        role: (parse_executor(meta[field]) if field in meta else executor_unset())
        for role, field in EXECUTOR_FIELDS.items()
    }


def pinned_executor(sprint: dict[str, Any], role: str) -> str:
    """The profile this sprint pins for `role`, or `""` when it pins none.

    A malformed value answers `""` to nobody: callers that must fail closed read the state instead.
    """
    state = (sprint.get("executors") or {}).get(role) or executor_unset()
    return str(state.get("profile") or "") if state.get("state") == EXECUTOR_PINNED else ""
