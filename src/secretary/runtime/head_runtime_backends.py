"""The one place a head-runtime name becomes a backend object.

`head_runtimes` holds the closed vocabulary — one name, what an absent one means, and the legacy
record marker. This module holds the other half: which class that name is, and how to read the name
off the thing a caller is acting on. They are separate files because the vocabulary is imported by
`head.command.validate_launch_shape`, which is the check every reader of a registry goes through,
and that check must stay free of the backends it is validating names against.

Every caller that raises, observes or stops a head reads it: `secretary.dispatch.host.CommandHostRuntime`
for the pipeline's heads, the background agents' dispatch for curator, steward and retro,
`head-status` and the web's product runs. A second copy of either half is a way for one of them to
raise a head another cannot reach, so there is deliberately one build site and one name reader, and
both are here.

A durable record written while heads were Orca panes names `orca-legacy`, or nothing. It still
loads and its name is still read here, but no backend is built for it: `build_head_runtime` refuses
it with `LegacyHeadRecordError`, by name, and never falls back to `local-pty`.

The dependencies a backend needs are passed in rather than reached for — a run root and the
product's launch-identity reader — as callables, so naming a backend resolves nothing until it is
built.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.runtime.head_runtimes import (
    HEAD_RUNTIMES,
    LOCAL_PTY_RUNTIME,
    ORCA_LEGACY_RUNTIME,
    RECORD_RUNTIME_WHEN_ABSENT,
    is_legacy_runtime,
)

from .local_pty_head import LocalPtyHeadRuntime


class UnknownHeadRuntimeError(ValueError):
    """A name no validated registry could have produced reached the one place backends are built."""


class LegacyHeadRecordError(UnknownHeadRuntimeError):
    """A legacy Orca record's runtime reached the build site: such a head is never launched."""


def head_runtime_name(subject: Any) -> str:
    """Which backend the thing a lifecycle call was given is held by, as a name.

    The one reader of `HeadSpec.runtime` outside the spec itself. Every lifecycle site already
    holds one of three things — the run it is acting on, the spec that run was launched from, or
    nothing at all — so this takes all three rather than making each caller reach for the same
    attribute. `None`, and anything that carries no runtime of its own, is read by the record
    rule: every subject handed here is a head or its record, never a profile — profiles are read
    through `HeadSpec`, which applies the profile default — and absence in a record has meant
    `orca-legacy` since before the key existed and goes on meaning it here.
    """
    if subject is None:
        return RECORD_RUNTIME_WHEN_ABSENT
    if isinstance(subject, str):
        return subject or RECORD_RUNTIME_WHEN_ABSENT
    spec = getattr(subject, "spec", subject)
    return str(getattr(spec, "runtime", "") or RECORD_RUNTIME_WHEN_ABSENT)


def is_legacy_record(subject: Any) -> bool:
    """Whether a run, spec or name is a legacy Orca record: it names `orca-legacy`, or nothing.

    The one predicate every reader asks. A legacy record loads and is shown as legacy; it is never
    launched, delivered to or given a backend.
    """
    return is_legacy_runtime(head_runtime_name(subject))


def build_head_runtime(
    name: str,
    *,
    local_pty_root: Callable[[], Path],
    head_process_status: Callable[..., Any],
) -> Any:
    """Build the backend called `name`.

    An unknown name cannot arrive from a validated registry (`validate_launch_shape` refuses it
    when the table loads), so reaching this refusal means a record or a caller invented one, and it
    fails closed by name rather than falling back to a backend the head is not on. A legacy
    record's name is refused the same way, with its own error: the record is readable, the head it
    describes was an Orca pane, and nothing here holds one. Callers that keep one instance per name
    do their own caching around this: a rebuilt runtime would forget the turns it handed out.
    """
    if name == LOCAL_PTY_RUNTIME:
        return LocalPtyHeadRuntime(local_pty_root(), head_process_status=head_process_status)
    if is_legacy_runtime(name):
        raise LegacyHeadRecordError(
            f"head runtime {ORCA_LEGACY_RUNTIME!r} is a legacy Orca record: it is never launched, "
            f"delivered to or given a backend (known: {', '.join(HEAD_RUNTIMES)})"
        )
    raise UnknownHeadRuntimeError(f"unknown head runtime {name!r} (known: {', '.join(HEAD_RUNTIMES)})")
