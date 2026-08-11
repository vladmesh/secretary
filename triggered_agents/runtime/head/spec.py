"""`HeadSpec`: one head's launch shape, resolved once and then carried by value.

Every operation that will live in this package — spawn, nudge, stop — has to know the same three
things about the head it acts on: which adapter drives it, what that adapter is pointed at, and
whether its prompt arrives on the command line or is delivered into a live session afterwards.
Today each caller re-derives that from a registry profile dict, and a dict has no answer for the
one question that matters: what happens when the field is not there. The dispatcher's own answer
was `"codex"` — a silent default that turns an unreadable or unfinished profile into a Codex
framing sent at whatever is actually running in the pane.

So the adapter is *required* here and a profile without one fails to load. That is the whole point
of the type: a `HeadSpec` in hand is proof the head is launchable, and there is no state in which
one exists with the adapter guessed.

The rules the adapter, effort and codex launch mode are checked against are not restated here.
`validate_registry` already owns them — it is what the product canon and the installation snapshot
are both loaded through — so this module runs the profile through that same validator rather than
keeping a second opinion that could drift from it. What stays with the registry is what only a
whole registry can answer: that the resource a profile names exists, and that its fallback chain
points at profiles that do. A `HeadSpec` is one head's own launch shape, so those cross-profile
references are deliberately not re-checked from a single profile.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...agents.pipeline.heads import (
    CODEX_TUI_MODE,
    HeadRegistryError,
    PROMPT_AFTER_START_ADAPTERS,
    Registry,
    load_registry,
    validate_registry,
)

# The resource stood in for a profile that names none. A spec is loadable without one — resource
# binding is a registry-level fact, checked where the whole table is in view — but the shared
# validator asks for a name before it will look at the launch shape, so validation is handed one.
_UNBOUND_RESOURCE = "(unbound)"

# The effort a profile that pins none runs at. Absent effort is legal and means "whatever the
# adapter's own default is"; an effort the adapter does not know is not.
DEFAULT_EFFORT = "default"


class HeadSpecError(HeadRegistryError):
    """A profile that is not a head: no adapter, or one this product cannot launch.

    A subclass of `HeadRegistryError` because that is what every caller reading a registry already
    handles, and this is the same class of fact arriving one profile at a time.
    """


@dataclass(frozen=True)
class HeadSpec:
    """What is needed to launch, address and stop one head, with the adapter never in doubt.

    Frozen: a spec describes the head a run was started on. Handing the same object to spawn, then
    to nudge, then to stop is only safe if none of them can move it under the others.
    """

    profile_id: str
    adapter: str
    model: str | None = None
    effort: str = DEFAULT_EFFORT
    resource: str | None = None
    codex_mode: str | None = None
    fallback: tuple[str, ...] = ()

    @property
    def prompt_after_start(self) -> bool:
        """Whether this head's prompt is delivered into the session after it comes up.

        The same fact `render_command` reports for a rendered command, read from the adapter alone
        so an operation that never renders a command still knows which delivery shape it is in.
        """
        return self.adapter in PROMPT_AFTER_START_ADAPTERS

    @classmethod
    def from_profile(cls, profile_id: str, profile: Any) -> HeadSpec:
        """The spec for one registry profile, or `HeadSpecError` naming that profile.

        The profile is checked by `validate_registry`, one profile at a time: adapter present and
        known, effort known for the adapters that have one, Codex launch mode known. Its
        cross-profile fields are neutralised for that check rather than re-validated — the fallback
        chain is emptied and an absent resource is stood in for — because a single profile is not
        where "this resource exists" or "this fallback exists" can be answered, and a caller
        holding one profile must not be told its head is unlaunchable over a table it never had.
        """
        if not isinstance(profile, Mapping):
            raise HeadSpecError(
                f"head {profile_id!r} is not a profile table, got {type(profile).__name__}"
            )
        resource = profile.get("resource")
        bound = resource if isinstance(resource, str) and resource else _UNBOUND_RESOURCE
        probe = {**dict(profile), "resource": bound, "fallback": []}
        try:
            validate_registry({bound: {}}, {profile_id: probe})
        except HeadRegistryError as exc:
            raise HeadSpecError(str(exc)) from None
        adapter = str(profile["adapter"])
        model = profile.get("model")
        fallback = profile.get("fallback") or []
        return cls(
            profile_id=profile_id,
            adapter=adapter,
            model=str(model) if isinstance(model, str) and model else None,
            effort=str(profile.get("effort", DEFAULT_EFFORT)),
            resource=bound if bound != _UNBOUND_RESOURCE else None,
            codex_mode=(
                str(profile.get("codex_mode", CODEX_TUI_MODE)) if adapter == "codex" else None
            ),
            fallback=tuple(str(fb) for fb in fallback) if isinstance(fallback, list) else (),
        )


def load_head_specs(registry: Registry | None = None) -> dict[str, HeadSpec]:
    """Every profile of the selected registry as a `HeadSpec`, keyed by profile id.

    All of them, not the worker/reviewer subset: an observer head, a curator, a steward and a retro
    are launched, nudged and stopped by the same operations, and a loader that quietly skipped the
    mechanical roles would leave those callers back on the dict they came from. A registry whose
    profiles do not all load is a broken registry, so the first one that does not stops the load by
    name rather than being dropped from the result.
    """
    reg = registry if registry is not None else load_registry()
    return {pid: HeadSpec.from_profile(pid, prof) for pid, prof in reg.profiles.items()}


def head_spec(profile_id: str, registry: Registry | None = None) -> HeadSpec:
    """One head of the selected registry, resolved through its compatibility ids."""
    reg = registry if registry is not None else load_registry()
    resolved = reg.resolve(profile_id)
    return HeadSpec.from_profile(resolved, reg.profile(resolved))
