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

# The audit kind the migration writes once per backfilled row: the record of the writes.
BACKFILL_EVENT_KIND = "observer_backfilled"

# The audit kind written once, after the strict rescan proved every row migrated. Its presence in
# the committed log is what makes this installation a migrated one.
#
# It is the completion, never a single row's write: a log carrying half the backfill must not read
# as migrated, or the strict reader would judge the rows the cutover has not reached yet.
#
# It lives in the audit log because that is what survives the product's declared recovery boundary.
# `state/board/events.ndjson` is checkpoint canon (docs/RECOVERY.md, "What the checkpoint contains")
# and recovery materializes it back into the data directory. A local marker file would not survive
# it, and a recovered host would go back to interpreting an absent field as a role default.
MIGRATION_COMPLETED_KIND = "observer_migration_completed"

# Written by the last step of the cutover, after a full rescan proved every row migrated. It is a
# latch over the same fact the log already carries, not a second source of truth: it makes the
# common answer a stat instead of a scan of the whole audit log, and it is never the reason strict
# mode is off. Nothing removes it: an installation does not go back to interpreting an absent field.
STRICT_MARKER = "sprints/observer-strict.json"

# The cutover's own working set: the immutable inventory and journal it persists before its first
# backend write. Local by construction — neither is checkpoint canon — which is what lets the
# reader tell "this host is mid-cutover" from "this host was rebuilt from a finished one".
CUTOVER_DIR = "sprints/observer-migration"
CUTOVER_INVENTORY = "inventory.json"
CUTOVER_JOURNAL = "journal.ndjson"

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


def installed_observer_profiles(instance: str | Path | None) -> set[str]:
    """The head profiles this installation runs off, or `ObserverMetadataError`.

    The same snapshot the dispatcher resolves a declared head against (`InstanceCatalog` reads
    `installed_heads` too), so "valid profile" means one thing at every boundary: the transition
    that declares it, the recovery that republishes it, the migration that activates the strict
    reader, and the fence that judges it at a tick.

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


def strict_marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / STRICT_MARKER


def strict_reader_active(data_dir: str | Path | None) -> bool:
    """Whether this installation reads observer metadata strictly.

    Three states, and the two signals that tell them apart:

      not migrated          no completion event                    -> tolerant
      migrating, this host  completion event + the cutover's own   -> tolerant
                            local working set, no latch
      migrated              the latch, or a completion event with  -> strict
                            no cutover in flight

    The completion event is the *durable* signal: `state/board/events.ndjson` is checkpoint canon
    and recovery materializes it back, so a replacement host rebuilt from a post-migration
    checkpoint comes back strict instead of silently returning to the role default. A local marker
    file could not carry that.

    But the event alone is written before the post-migration checkpoint is taken and pushed, so on
    the host running the cutover there is a live interval in which the event exists and the ordered
    sequence has not finished. Strict there would be strict before the recovery point the order
    requires. The cutover's own working set — the inventory it persists under `sprints/` — is what
    marks that interval, and it is local by construction: it is not checkpoint canon, so a recovered
    host never has it and is never mistaken for a host mid-cutover.

    The latch settles the finished case without either read: the cutover writes it last.
    """
    if data_dir is None:
        return False
    if strict_marker_present(data_dir):
        return True
    if not migration_recorded(data_dir):
        return False
    return not cutover_in_flight(data_dir)


def strict_marker_present(data_dir: str | Path) -> bool:
    """The local latch alone. The cutover writes it last, so a retry keys its resume on it."""
    try:
        raw = json.loads(strict_marker_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(raw, dict) and raw.get("version") == 1 and raw.get("strict") is True


def cutover_in_flight(data_dir: str | Path) -> bool:
    """Whether this host is between the start of a cutover and its latch.

    The inventory is written before the first backend write and the latch after the last step, so
    an inventory without a latch is exactly the open interval. Both live under the data directory
    and neither is checkpoint canon, so this answers only for the host that ran the cutover.
    """
    if strict_marker_present(data_dir):
        return False
    return (Path(data_dir) / CUTOVER_DIR / CUTOVER_INVENTORY).is_file()


# Migration is a one-way transition, so a positive answer is remembered for the life of the
# process. A negative one is re-derived whenever the log has grown, which is what makes the very
# tick that finishes the cutover see the new state without a restart.
_MIGRATION_SCAN: dict[str, tuple[int, int]] = {}
_MIGRATED: set[str] = set()


def migration_recorded(data_dir: str | Path) -> bool:
    """Whether the committed audit log carries this installation's completed observer migration."""
    key = str(Path(data_dir).expanduser())
    if key in _MIGRATED:
        return True
    events_path = Path(key) / "board" / "events.ndjson"
    try:
        stat = events_path.stat()
    except OSError:
        return False
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    if _MIGRATION_SCAN.get(key) == fingerprint:
        return False
    _MIGRATION_SCAN[key] = fingerprint
    if _scan_for_completion(events_path):
        _MIGRATED.add(key)
        return True
    return False


def _scan_for_completion(events_path: Path) -> bool:
    try:
        with events_path.open(encoding="utf-8") as events:
            for line in events:
                # The kind is matched on the raw line before the record is parsed: the log holds
                # every event this installation ever wrote, and parsing all of them to find one
                # kind would put a full JSON decode of the whole history on the tick.
                if MIGRATION_COMPLETED_KIND not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("kind") == MIGRATION_COMPLETED_KIND
                    and event.get("outcome") == "success"
                ):
                    return True
    except OSError:
        return False
    return False


def forget_migration_state(data_dir: str | Path | None = None) -> None:
    """Drop the process-level memo. For tests, and for a caller that rebuilt the data directory."""
    if data_dir is None:
        _MIGRATED.clear()
        _MIGRATION_SCAN.clear()
        return
    key = str(Path(data_dir).expanduser())
    _MIGRATED.discard(key)
    _MIGRATION_SCAN.pop(key, None)


def activate_strict_reader(
    data_dir: str | Path, *, inventory_digest: str, rows: int, activated_at: str,
) -> dict[str, Any]:
    """Latch the strict reader on, after the scan that justified it.

    This is not what makes the installation migrated — the backfill's own audit events already did
    that, durably and inside the checkpoint. The file records which scan the activation followed,
    so an operator can tell, and it keeps the ordinary read a stat rather than a scan.

    A second activation of the same cutover rewrites the same content; it is not an error, because
    the step it ends is retried as a whole.
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
