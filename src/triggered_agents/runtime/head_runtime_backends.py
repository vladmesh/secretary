"""The one place a head-runtime name becomes a backend object.

`head_runtimes` holds the closed vocabulary — two names and what an absent one means. This module
holds the other half: which class each of those names is, and how to read the name off the thing a
caller is acting on. They are separate files because the vocabulary is imported by
`head.command.validate_launch_shape`, which is the check every reader of a registry goes through,
and that check must stay free of the backends it is validating names against.

Two callers read it, and it is the same mapping for both. `secretary.dispatcher.DispatcherHost`
raises, observes and stops the pipeline's heads through it; `runtime.dispatch` — the mechanical-role
driver for curator, steward and retro — chooses through it which of the two ways it holds a head of
its own. A second copy of either half is a way for one of them to raise a head the other cannot
reach, so there is deliberately one build site and one name reader, and both are here.

The dependencies a backend needs are passed in rather than reached for. The Orca backend needs a
session manager and the local-pty one needs a run root and the product's launch-identity reader,
and neither caller has the other's: this module knows which name needs which, and nothing about
where either comes from. Both are callables so that naming one backend never constructs the other's
dependency — an Orca session is not opened to raise a supervised head, and no run root is resolved
to reach a pane.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .head_runtimes import (
    DEFAULT_HEAD_RUNTIME,
    HEAD_RUNTIMES,
    LOCAL_PTY_RUNTIME,
    ORCA_LEGACY_RUNTIME,
)
from .local_pty_head import LocalPtyHeadRuntime
from .orca_legacy_head import OrcaLegacyHeadRuntime


class UnknownHeadRuntimeError(ValueError):
    """A name no validated registry could have produced reached the one place backends are built."""


def head_runtime_name(subject: Any) -> str:
    """Which backend the thing a lifecycle call was given is held by, as a name.

    The one reader of `HeadSpec.runtime` outside the spec itself. Every lifecycle site already
    holds one of three things — the run it is acting on, the spec that run was launched from, or
    nothing at all — so this takes all three rather than making each caller reach for the same
    attribute. `None` (an operation that names no head, such as an Orca workspace teardown) is the
    product default, and so is anything that carries no runtime of its own: absence has meant
    `orca-legacy` since before the key existed and goes on meaning it here.
    """
    if subject is None:
        return DEFAULT_HEAD_RUNTIME
    if isinstance(subject, str):
        return subject or DEFAULT_HEAD_RUNTIME
    spec = getattr(subject, "spec", subject)
    return str(getattr(spec, "runtime", "") or DEFAULT_HEAD_RUNTIME)


def build_head_runtime(
    name: str,
    *,
    session: Callable[[], Any],
    local_pty_root: Callable[[], Path],
    head_process_status: Callable[..., Any],
) -> Any:
    """Build the backend called `name`.

    An unknown name cannot arrive from a validated registry (`validate_launch_shape` refuses it
    when the table loads), so reaching this refusal means a record or a caller invented one, and it
    fails closed by name rather than falling back to a backend the head is not on. Callers that
    keep one instance per name do their own caching around this: a rebuilt runtime would forget the
    turns it handed out.
    """
    if name == ORCA_LEGACY_RUNTIME:
        return OrcaLegacyHeadRuntime(session)
    if name == LOCAL_PTY_RUNTIME:
        return LocalPtyHeadRuntime(local_pty_root(), head_process_status=head_process_status)
    raise UnknownHeadRuntimeError(f"unknown head runtime {name!r} (known: {', '.join(HEAD_RUNTIMES)})")
