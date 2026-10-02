"""Which models and reasoning efforts a PO session may be opened with, per CLI.

`instance.yaml` may say so under ``po.models`` and ``po.efforts``; a CLI it does not name keeps the
product default, and an empty list offers that CLI no model (or no effort) at all.

A new session's effort is always explicit: one the installation offers for its CLI
(:func:`require_explicit_effort`). ``default`` — no effort flag, the CLI's own configured one — is
never offered and never accepted for a new session, even when an installation lists it; it survives
only as the stored value of sessions opened before that rule, which still resume with no flag and
read "not set". The first offered effort of a CLI is what a form preselects and what a session
opened without a choice (a sprint's) takes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ummanu.po.store import CLIS, DEFAULT_EFFORT

DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("fable", "claude-opus-5-5"),
    "codex": ("gpt-6-astra", "gpt-6-sol", "gpt-5.6-terra"),
}

# What each CLI's flag takes (checked on Claude Code 2.1.280, `--effort`, and Codex 0.155.1,
# `-c model_reasoning_effort=`), with `high` first so a form preselects it.
DEFAULT_EFFORTS: dict[str, tuple[str, ...]] = {
    "claude": ("high", "low", "medium", "xhigh", "max"),
    "codex": ("high", "low", "medium", "xhigh"),
}

# Stored values that name no effort: the legacy `default`, and what a form or row may say instead.
UNSET_EFFORTS = frozenset({"", DEFAULT_EFFORT, "none"})


class EffortRefused(ValueError):
    """A new session's effort that is missing, `default`, or not offered for its CLI."""


def models_from_instance(instance: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    return _lists(instance, "models", DEFAULT_MODELS)


def efforts_from_instance(instance: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    return {
        cli: tuple(value for value in values if value.lower() not in UNSET_EFFORTS)
        for cli, values in _lists(instance, "efforts", DEFAULT_EFFORTS).items()
    }


def first_effort(cli: str, efforts: Mapping[str, Any]) -> str | None:
    """The effort a session of `cli` opens at when nobody chose one: the first offered, or None."""
    for value in efforts.get(cli) or ():
        value = str(value).strip()
        if value.lower() not in UNSET_EFFORTS:
            return value
    return None


def require_explicit_effort(cli: str, effort: Any, efforts: Mapping[str, Any]) -> str:
    """The one gate for a new session's effort: `effort` stripped, or `EffortRefused` naming the offered.

    Empty, `default`/`none`, or a value `efforts` does not offer for `cli` is refused.
    """
    offered = [
        value
        for value in (str(item).strip() for item in efforts.get(cli) or ())
        if value.lower() not in UNSET_EFFORTS
    ]
    listing = ", ".join(offered) if offered else "none"
    value = str(effort or "").strip()
    if value.lower() in UNSET_EFFORTS:
        raise EffortRefused(
            f"a new PO session needs an explicit effort; this installation offers for {cli}: {listing}"
        )
    if value not in offered:
        raise EffortRefused(f"{value!r} is not an effort this installation offers for {cli}: {listing}")
    return value


def _lists(
    instance: Mapping[str, Any] | None, key: str, defaults: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    section = (instance or {}).get("po") if isinstance(instance, Mapping) else None
    configured = section.get(key) if isinstance(section, Mapping) else None
    found: dict[str, tuple[str, ...]] = {}
    for cli in CLIS:
        values = configured.get(cli) if isinstance(configured, Mapping) else None
        if values is None:
            found[cli] = defaults[cli]
        else:
            found[cli] = tuple(str(value).strip() for value in values if str(value).strip())
    return found


def default_session_choice(models: Mapping[str, Any]) -> tuple[str, str] | None:
    """The CLI and model a new session is opened with when nobody chose: the web form's preselection.

    The first CLI that offers any model, and the first model it lists; None when no CLI offers one.
    The effort of such a session is the first one offered for that CLI (`first_effort`). The web's
    new-session form (`web/pages.py`, `_po_new_session_form`) preselects by the same rule over the
    same `po_models` document; the web transport imports nothing outside
    `ummanu.web`/`ummanu.webproto`, so it spells it itself.
    """
    for cli, values in models.items():
        listed = [str(value) for value in values or () if str(value).strip()]
        if listed:
            return str(cli), listed[0]
    return None


def successor_choice(
    previous: tuple[str, str, str] | None,
    models: Mapping[str, Any],
    efforts: Mapping[str, Any],
) -> tuple[str, str, str] | None:
    """The CLI, model and effort of a session opened to succeed a closed or missing one.

    The previous session's `(cli, model, effort)`, or the new-session form's defaults when there is
    no row (`default_session_choice`); an effort that is `default` or no longer offered gives way to
    the first one offered for that CLI. None when no CLI offers a model. The one rule for a sprint's
    successor (`PoService.sprint_session`) and a wait card's (`ummanu.dispatch.wait_cards`).
    """
    if previous is not None:
        cli, model, effort = previous
    else:
        choice = default_session_choice(models)
        if choice is None:
            return None
        (cli, model), effort = choice, ""
    if effort not in (efforts.get(cli) or ()):
        effort = first_effort(cli, efforts) or ""
    return cli, model, effort


__all__ = [
    "DEFAULT_EFFORTS",
    "DEFAULT_MODELS",
    "UNSET_EFFORTS",
    "EffortRefused",
    "default_session_choice",
    "efforts_from_instance",
    "first_effort",
    "models_from_instance",
    "require_explicit_effort",
    "successor_choice",
]
