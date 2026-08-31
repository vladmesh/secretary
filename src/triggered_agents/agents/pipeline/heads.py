"""Head registry — which heads an installation has, and which one each role runs on.

A worker/reviewer head is data (`[resources.*]`, `[profiles.*]`, `[role_defaults]`), not a
hardcoded `claude` invocation. This module owns that data and nothing else: a profile is looked
up here and handed to `triggered_agents.runtime.head.command`, which is the one place a profile
becomes a shell command. The dependency runs one way — this module imports the renderer, never
the reverse — which keeps a head operation runnable with no registry.

Which heads exist is installation configuration, not product code, so an upgraded installation
reads its own `<instance>/heads/heads.yaml` snapshot and the shipped `heads.toml` is the portable
default. Both go through the same validator.

Pure and I/O-light (`load_registry` caches its read per process): no Kanboard, no orca, no
subprocess.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ...runtime.codex_preflight import codex_home
from ...runtime.head.command import (
    CODEX_TUI_MODE,
    HeadCommandError,
    validate_launch_shape,
)

HEADS_TOML = Path(__file__).with_name("heads.toml")
# The installation whose registry this process runs off, and where that registry sits inside it.
# Only an explicitly configured instance counts: a checkout on a host that happens to have an
# installation must keep reading the product default, or every test about the shipped registry
# would silently assert against the developer's own heads.
INSTANCE_ENV = "SECRETARY_INSTANCE"
INSTANCE_SNAPSHOT_RELATIVE = Path("heads") / "heads.yaml"
# Point one process at another registry without moving its installation. Tests use it; so does an
# operator diffing a candidate registry against the live one.
REGISTRY_ENV = "TA_HEADS_REGISTRY"


def installed_registry_path() -> Path | None:
    """The configured installation's own snapshot, or None when there is no installation here.

    Whether that snapshot exists is deliberately not asked: a missing, unreadable or dangling
    snapshot is a broken installation and the load below fails by that path. Answering "no
    installation" instead would route a selected non-default instance off a mutable product checkout.
    """
    configured = os.environ.get(INSTANCE_ENV)
    if not configured:
        return None
    base = Path(configured).expanduser()
    if base.name == "instance.yaml":
        base = base.parent
    return base / INSTANCE_SNAPSHOT_RELATIVE


def registry_path() -> Path:
    """The registry this process reads: the installation's own snapshot, else the product default.

    The product default is for a checkout with no installation selected at all, not for a selected
    installation whose snapshot is unusable. Resolved per call rather than at import.
    """
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return installed_registry_path() or HEADS_TOML


# The CODEX_HOME shared by Orca-managed Codex sessions and pipeline heads. One physical home keeps
# auth refresh, MCP, skills, hooks, and quota probes on the same state. Pinned explicitly because
# the health probe is a plain subprocess rather than an Orca terminal. Env-overridable so tests can
# use a throwaway home.
CODEX_HOME = codex_home({})

# Last-resort profile ids, used only when the selected registry routes a role nowhere. Which head a
# role actually runs on is `[role_defaults]` in that registry; these keep a tick launching something
# rather than nothing when it has been stripped out.
DEFAULT_PROFILE = "codex"
REVIEWER_PROFILE = "codex-reviewer"

# Codex profile ids from before that rule. They are written down in places the registry does not
# own — a card's `head_override` or `review_head_override`, a dispatcher record, an agent's
# automation.toml — so an installation that republishes its Codex heads under interactive ids must
# not orphan them. Each maps to the ids an equivalent profile may be published under, closest
# first.
#
# The names below are a compatibility namespace reserved for Codex, not ordinary ids: every one of
# them, the id itself included, is only usable when the profile behind it is an interactive Codex
# head. A registry is free to define `codex-terra` as a Claude profile — the validator does not
# reserve ids by adapter — and an override written in the Codex generation must not follow that id
# onto another model family. So the id itself wins only on that condition, the stand-ins are held
# to the same one, and a name here with no interactive Codex profile left behind it fails closed.
LEGACY_CODEX_HEADS: dict[str, tuple[str, ...]] = {
    "codex": ("codex-tui", "codex-terra"),
    "codex-sol": ("codex", "codex-tui"),
    "codex-terra": ("codex", "codex-tui"),
    "codex-luna": ("codex", "codex-tui"),
    "codex-5-4": ("codex", "codex-tui"),
    "codex-mini": ("codex", "codex-tui"),
    "codex-spark": ("codex", "codex-tui"),
    "codex-high": ("codex-high-tui", "codex-extra", "codex", "codex-tui"),
    "codex-extra": ("codex-extra-tui", "codex-high", "codex-high-tui", "codex", "codex-tui"),
    "codex-reviewer": ("codex-reviewer-tui", "codex-extra", "codex-extra-tui", "codex-high", "codex"),
    "codex-curator": ("codex-extra", "codex-reviewer", "codex-high", "codex"),
    "codex-steward": ("codex-high", "codex-extra", "codex-reviewer", "codex"),
    "codex-retro": ("codex-high", "codex", "codex-tui"),
}


class HeadRegistryError(HeadCommandError):
    """heads.toml is missing/malformed, or a profile/resource/adapter/runtime/fallback it names is
    unknown.
    """


def is_interactive_codex(profile: Any) -> bool:
    """Whether a registry entry is a Codex head this product can still launch.

    An absent `codex_mode` is the interactive one; a profile pinning anything else names a launch
    shape no renderer produces and is not a head at all here.
    """
    return (
        isinstance(profile, Mapping)
        and profile.get("adapter") == "codex"
        and str(profile.get("codex_mode", CODEX_TUI_MODE)) == CODEX_TUI_MODE
    )


def resolve_head_id(profile_id: str, profiles: Mapping[str, Any]) -> str:
    """The profile id that actually serves `profile_id` in a registry's `profiles` table.

    An ordinary id is returned untouched, whatever it names: the lookup that follows either finds it
    or fails closed by name. A name in `LEGACY_CODEX_HEADS` is resolved instead of looked up — the id
    itself only while it still holds an interactive Codex profile, then the declared stand-ins in
    order — and a name with none of them left raises rather than handing back an id that would launch
    another model family under a Codex head's name.
    """
    if not isinstance(profiles, Mapping):
        return profile_id
    candidates = LEGACY_CODEX_HEADS.get(profile_id)
    if candidates is None:
        return profile_id
    for candidate in (profile_id, *candidates):
        if is_interactive_codex(profiles.get(candidate)):
            return candidate
    known = ", ".join(sorted(profiles)) or "(none)"
    raise HeadRegistryError(
        f"codex head {profile_id!r} has no interactive Codex profile left to run on (known: {known})"
    )


class Registry:
    def __init__(self, resources: dict, profiles: dict, role_defaults: dict | None = None):
        self.resources = resources
        self.profiles = profiles
        self.role_defaults = role_defaults or {}

    def role_default(self, role: str) -> str | None:
        """The head this registry routes `role` to, or None when it routes it nowhere."""
        head = self.role_defaults.get(role)
        return str(head) if head else None

    def resolve(self, profile_id: str) -> str:
        """The profile id that actually serves `profile_id` here. See `resolve_head_id`."""
        return resolve_head_id(profile_id, self.profiles)

    def profile(self, profile_id: str) -> dict:
        """The profile dict for `profile_id`, or HeadRegistryError with the known ids — the text
        a claim guard or a create/update validation surfaces verbatim to whoever reads it."""
        prof = self.profiles.get(profile_id)
        if prof is None:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise HeadRegistryError(f"unknown head {profile_id!r} (known: {known})")
        return prof

    def known(self) -> list[str]:
        return sorted(self.profiles)


def role_head(role: str, fallback: str, registry: Registry | None = None) -> str:
    """The head the selected registry routes `role` to, or `fallback` when it routes it nowhere.

    Best-effort by design: an unreadable registry must not leave a role with no head to launch. The
    caller that then tries to render `fallback` raises the same registry error anyway.
    """
    try:
        reg = registry or load_registry()
    except HeadRegistryError:
        return fallback
    return reg.role_default(role) or fallback


def default_head(registry: Registry | None = None) -> str:
    """The head a card that names none of its own runs on."""
    return role_head("new_card", DEFAULT_PROFILE, registry)


def reviewer_head(registry: Registry | None = None) -> str:
    """The head a card that names no reviewer of its own is reviewed by.

    ``TA_REVIEWER_HEAD`` still wins: it is the one-tick override an operator sets to try a reviewer
    without editing the installation's registry.
    """
    override = os.environ.get("TA_REVIEWER_HEAD")
    if override:
        return override
    return role_head("reviewer", REVIEWER_PROFILE, registry)


def profile_info(profile_id: str, registry: Registry | None = None) -> dict:
    """Display-facing profile facts. Unknown profiles return a marked record instead of raising."""
    reg = registry or load_registry()
    try:
        prof = reg.profile(profile_id)
    except HeadRegistryError:
        return {
            "profile": profile_id,
            "known": False,
            "adapter": "unknown",
            "model": "unknown",
            "effort": "unknown",
        }
    adapter = prof.get("adapter") or "unknown"
    effort = prof.get("effort", "default") if adapter in {"claude", "codex"} else "n/a"
    return {
        "profile": profile_id,
        "known": True,
        "adapter": adapter,
        "model": prof.get("model") or "default",
        "effort": effort,
    }


def _named(value: object, what: str) -> str:
    """A registry field that has to be a plain name before anything can be looked up by it.

    Checked before the membership tests below rather than left to them: a list where a name belongs
    is unhashable, so `value not in table` would raise TypeError past every caller.
    """
    if not isinstance(value, str):
        raise HeadRegistryError(f"{what} must be a name, got {type(value).__name__}")
    return value


def validate_registry(resources: dict, profiles: dict) -> None:
    """Structural check every consumer of the registry shares: the product canon at load time and the
    installation snapshot the dispatcher runs off.

    Shapes are checked alongside names, because a registry is hand-written TOML: every malformed
    entry has to come back as a HeadRegistryError here rather than as an AttributeError down in a
    consumer that assumed a mapping.
    """
    if not isinstance(resources, dict):
        raise HeadRegistryError(f"[resources] must be a table, got {type(resources).__name__}")
    if not isinstance(profiles, dict):
        raise HeadRegistryError(f"[profiles] must be a table, got {type(profiles).__name__}")
    for rid, res in resources.items():
        if not isinstance(res, dict):
            raise HeadRegistryError(f"resource {rid!r} must be a table, got {type(res).__name__}")
    for pid, prof in profiles.items():
        if not isinstance(prof, dict):
            raise HeadRegistryError(f"profile {pid!r} must be a table, got {type(prof).__name__}")
        resource = _named(prof.get("resource"), f"profile {pid!r} resource")
        if resource not in resources:
            raise HeadRegistryError(f"profile {pid!r} references unknown resource {resource!r}")
        # Adapter, effort, Codex launch mode and the backend runtime are the renderer's rules,
        # checked by the renderer:
        # what a registry may name is exactly what something can be launched from, and a table
        # validated against a second copy of that list is a table that can pass here and fail at
        # bring-up. An absent Codex mode is the interactive one, and a registry that still pins the
        # retired `exec` is refused there rather than launched as a shape nothing produces.
        try:
            validate_launch_shape(pid, prof)
        except HeadCommandError as exc:
            raise HeadRegistryError(str(exc)) from None
        fallback = prof.get("fallback") or []
        if not isinstance(fallback, list):
            raise HeadRegistryError(f"profile {pid!r} fallback must be a list, got {type(fallback).__name__}")
        for fb in fallback:
            fb = _named(fb, f"profile {pid!r} fallback entry")
            if fb not in profiles:
                raise HeadRegistryError(f"profile {pid!r} fallback references unknown profile {fb!r}")


def validate_role_defaults(role_defaults: dict, profiles: dict) -> None:
    """A role routed to a head nobody defined is a routing hole, not a stale line to ignore."""
    if not isinstance(role_defaults, dict):
        raise HeadRegistryError(f"[role_defaults] must be a table, got {type(role_defaults).__name__}")
    for role, head in role_defaults.items():
        head = _named(head, f"role {role!r} head")
        if head not in profiles:
            raise HeadRegistryError(
                f"role {role!r} routes to unknown head {head!r} "
                f"(known: {', '.join(sorted(profiles)) or '(none)'})"
            )


def _parse_registry(path: Path) -> dict:
    """The registry document, whichever of its two shapes is on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise HeadRegistryError(f"head registry missing: {path}") from e
    except (OSError, UnicodeError) as e:
        raise HeadRegistryError(f"cannot read head registry {path}: {e}") from e
    if path.suffix in {".yaml", ".yml"}:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise HeadRegistryError(f"head registry {path} is not valid YAML: {e}") from e
    else:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise HeadRegistryError(f"head registry {path} is not valid TOML: {e}") from e
    if not isinstance(data, dict):
        raise HeadRegistryError(f"head registry {path} has an unsupported shape")
    return data


def load_registry(path: Path | None = None) -> Registry:
    """The registry this installation runs off, resolved then parsed. See ``_load_registry``."""
    return _load_registry(path if path is not None else registry_path())


@cache
def _load_registry(path: Path) -> Registry:
    """The registry file, parsed and validated. Cached per (process, path) — every dispatcher tick
    is a fresh production-dispatcher process, so this only dedupes the 2+
    reads a single tick already does (claim's `_check_head`, then the bring-up's own lookup),
    never a long-lived process going stale against an edited file on disk. A raised
    HeadRegistryError is not cached — the next call re-reads, so a fixed-then-retried registry
    recovers without a process restart."""
    data = _parse_registry(path)
    resources = data.get("resources") or {}
    profiles = data.get("profiles") or {}
    role_defaults = data.get("role_defaults") or {}
    validate_registry(resources, profiles)
    validate_role_defaults(role_defaults, profiles)
    return Registry(resources=resources, profiles=profiles, role_defaults=role_defaults)
