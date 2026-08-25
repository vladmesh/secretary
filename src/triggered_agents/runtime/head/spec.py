"""`HeadSpec`: one head's launch shape, resolved once and then carried by value.

Every operation in this package has to know the same three things about the head it acts on:
which adapter drives it, what that adapter is pointed at, and whether its prompt arrives on the
command line or is delivered into a live session afterwards. The adapter is *required* here and a
profile without one fails to load: a `HeadSpec` in hand is proof the head is launchable, and
there is no state in which one exists with the adapter guessed.

The rules the adapter, effort, codex launch mode and backend runtime are checked against live in
`command.validate_launch_shape`, beside the renderer that has to spell them. What stays with the
registry is what only a whole registry can answer: that the resource a profile names exists, and
that its fallback chain points at profiles that do.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..head_runtimes import DEFAULT_HEAD_RUNTIME
from .command import (
    CODEX_TUI_MODE,
    PROMPT_AFTER_START_ADAPTERS,
    HeadCommandError,
    validate_launch_shape,
)

if TYPE_CHECKING:  # pragma: no cover - the registry is data this module is handed
    from ...agents.pipeline.heads import Registry

# The registry is read here and nowhere else in this package, and only when a caller declined to
# hand one over. The import is deferred to keep that direction one-way at module scope: the
# registry package imports this one for the launch shapes it validates against, so a head module
# that imported it back at import time would close the loop.


def _load_registry() -> Registry:
    from ...agents.pipeline.heads import load_registry

    return load_registry()


# The effort a profile that pins none runs at. Absent effort is legal and means "whatever the
# adapter's own default is"; an effort the adapter does not know is not.
DEFAULT_EFFORT = "default"


class HeadSpecError(HeadCommandError):
    """A profile that is not a head: no adapter, or one this product cannot launch."""


@dataclass(frozen=True)
class HeadSpec:
    """What is needed to launch, address and stop one head, with the adapter never in doubt.

    Frozen: a spec describes the head a run was started on, and handing the same object to spawn,
    then nudge, then stop is only safe if none of them can move it under the others.
    """

    profile_id: str
    adapter: str
    model: str | None = None
    effort: str = DEFAULT_EFFORT
    resource: str | None = None
    codex_mode: str | None = None
    fallback: tuple[str, ...] = ()
    #: Which backend this head's life is lived through, carried by value like everything else here.
    #: It is the profile's answer and not the caller's: the dispatcher builds the backend named
    #: here, so a head raised under one backend cannot be observed or stopped through another —
    #: including on a later tick, because the value travels with the durable run record. Orthogonal
    #: to `adapter`: this says what holds the head, `adapter` says what the head is.
    runtime: str = DEFAULT_HEAD_RUNTIME

    @property
    def prompt_after_start(self) -> bool:
        """Whether this head's prompt is delivered into the session after it comes up."""
        return self.adapter in PROMPT_AFTER_START_ADAPTERS

    @classmethod
    def from_profile(cls, profile_id: str, profile: Any) -> HeadSpec:
        """The spec for one registry profile, or `HeadSpecError` naming that profile.

        Checked by `validate_launch_shape`. Its cross-profile fields are deliberately not checked — a
        single profile is not where "this resource exists" can be answered, and a caller holding one
        profile must not be told its head is unlaunchable over a table it never had.
        """
        if not isinstance(profile, Mapping):
            raise HeadSpecError(
                f"head {profile_id!r} is not a profile table, got {type(profile).__name__}"
            )
        try:
            validate_launch_shape(profile_id, profile)
        except HeadCommandError as exc:
            raise HeadSpecError(str(exc)) from None
        adapter = str(profile["adapter"])
        model = profile.get("model")
        resource = profile.get("resource")
        fallback = profile.get("fallback") or []
        return cls(
            profile_id=profile_id,
            adapter=adapter,
            model=str(model) if isinstance(model, str) and model else None,
            effort=str(profile.get("effort", DEFAULT_EFFORT)),
            resource=resource if isinstance(resource, str) and resource else None,
            codex_mode=(
                str(profile.get("codex_mode", CODEX_TUI_MODE)) if adapter == "codex" else None
            ),
            fallback=tuple(str(fb) for fb in fallback) if isinstance(fallback, list) else (),
            runtime=str(profile.get("runtime", DEFAULT_HEAD_RUNTIME)),
        )


def load_head_specs(registry: Registry | None = None) -> dict[str, HeadSpec]:
    """Every profile of the selected registry as a `HeadSpec`, keyed by profile id.

    All of them, not the worker/reviewer subset: every role is launched, nudged and stopped by the
    same operations. A registry whose profiles do not all load is a broken registry, so the first one
    that does not stops the load by name rather than being dropped from the result.
    """
    reg = registry if registry is not None else _load_registry()
    return {pid: HeadSpec.from_profile(pid, prof) for pid, prof in reg.profiles.items()}


def head_spec(profile_id: str, registry: Registry | None = None) -> HeadSpec:
    """One head of the selected registry, resolved through its compatibility ids."""
    reg = registry if registry is not None else _load_registry()
    resolved = reg.resolve(profile_id)
    return HeadSpec.from_profile(resolved, reg.profile(resolved))
