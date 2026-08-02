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

# The audit kind written after the post-migration checkpoint has been taken and pushed, naming the
# inventory it finished. It is what closes the cutover's open interval on the host that ran it.
#
# It is an audit event rather than a second local file for the same reason the completion event is:
# a local file can be lost or damaged, and if the retained inventory then read as an open interval,
# a finished installation would fall back to the tolerant reader and the role default. The
# append-only log cannot take strictness away from an installation that earned it.
MIGRATION_ACTIVATED_KIND = "observer_migration_activated"

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

    Three states, and the signals that tell them apart:

      not migrated          no completion event                      -> tolerant
      migrating, this host  completion event, an inventory on this   -> tolerant
                            host, and no activation naming it
      migrated              the latch, or a completion event with    -> strict
                            no cutover in flight

    The completion event is the *durable* signal: `state/board/events.ndjson` is checkpoint canon
    and recovery materializes it back, so a replacement host rebuilt from a post-migration
    checkpoint comes back strict instead of silently returning to the role default. A local marker
    file could not carry that.

    But the event alone is written before the post-migration checkpoint is taken and pushed, so on
    the host running the cutover there is a live interval in which the event exists and the ordered
    sequence has not finished. Strict there would be strict before the recovery point the order
    requires. `cutover_in_flight` is what marks that interval, from the inventory this host holds
    and the activation event that closes it. Both halves of that answer are durable: a recovered
    host has no inventory and is never mistaken for a host mid-cutover, and a host whose latch was
    damaged still has the activation event and stays strict.

    The latch settles the finished case without any of it: the cutover writes it last, so the
    ordinary read is one stat.
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
    """Whether this host is inside a cutover that has not reached its post-migration checkpoint.

    The inventory is written before the first backend write and is deliberately never removed: it
    is what a retry reads instead of recomputing provenance. So its presence alone cannot mean
    "in flight" — after a successful cutover it is retained, and reading that as an open interval
    would make the latch the single thing holding strictness up, which a damaged local file could
    then take away from a migrated installation.

    The activation event closes the interval instead. It is written after the post-migration
    checkpoint has been taken and pushed, it names the inventory it finished, and it is in the
    append-only audit log rather than in a file that can be edited or lost. An inventory with no
    activation naming it is a run that has not got that far; an inventory with one is the retained
    working set of a finished cutover.
    """
    digest = _inventory_digest(data_dir)
    if digest is None:
        return False
    return digest not in cutover_activations(data_dir)


def _inventory_digest(data_dir: str | Path) -> str | None:
    """The digest of the cutover inventory on this host, or None when there is none."""
    try:
        raw = json.loads(
            (Path(data_dir) / CUTOVER_DIR / CUTOVER_INVENTORY).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    digest = raw.get("digest")
    return str(digest) if digest else None


# One pass over the log answers both questions, and the answer is memoized on the file's size and
# mtime: the log grows on a live installation, so a tick that appended re-reads and one that did
# not pays nothing. The latch short-circuits before any of this in the ordinary case.
_MIGRATION_SCAN: dict[str, tuple[tuple[int, int], bool, frozenset[str]]] = {}


def _cutover_audit(data_dir: str | Path) -> tuple[bool, frozenset[str]]:
    """`(migration completed, inventory digests whose activation is recorded)`."""
    key = str(Path(data_dir).expanduser())
    events_path = Path(key) / "board" / "events.ndjson"
    try:
        stat = events_path.stat()
    except OSError:
        return False, frozenset()
    fingerprint = (stat.st_size, stat.st_mtime_ns)
    cached = _MIGRATION_SCAN.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1], cached[2]
    completed = False
    activated: set[str] = set()
    try:
        with events_path.open(encoding="utf-8") as events:
            for line in events:
                # The kinds are matched on the raw line before the record is parsed: the log holds
                # every event this installation ever wrote, and decoding all of them to find two
                # kinds would put a full JSON parse of the whole history on the tick.
                if MIGRATION_COMPLETED_KIND not in line and MIGRATION_ACTIVATED_KIND not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("outcome") != "success":
                    continue
                kind = event.get("kind")
                if kind == MIGRATION_COMPLETED_KIND:
                    completed = True
                elif kind == MIGRATION_ACTIVATED_KIND:
                    payload = event.get("payload")
                    digest = (payload or {}).get("inventory_digest") if isinstance(payload, dict) else None
                    if digest:
                        activated.add(str(digest))
    except OSError:
        return False, frozenset()
    result = (completed, frozenset(activated))
    _MIGRATION_SCAN[key] = (fingerprint, *result)
    return result


def migration_recorded(data_dir: str | Path) -> bool:
    """Whether the committed audit log carries this installation's completed observer migration."""
    return _cutover_audit(data_dir)[0]


def cutover_activations(data_dir: str | Path) -> frozenset[str]:
    """Inventory digests whose cutover reached its post-migration checkpoint, from the audit log."""
    return _cutover_audit(data_dir)[1]


def forget_migration_state(data_dir: str | Path | None = None) -> None:
    """Drop the process-level memo. For tests, and for a caller that rebuilt the data directory."""
    if data_dir is None:
        _MIGRATION_SCAN.clear()
        return
    _MIGRATION_SCAN.pop(str(Path(data_dir).expanduser()), None)


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
