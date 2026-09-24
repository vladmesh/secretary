"""Which models and reasoning efforts a PO session may be opened with, per CLI.

`instance.yaml` may say so under ``po.models`` and ``po.efforts``; a CLI it does not name keeps the
product default, and an empty list offers that CLI no model (or no effort besides ``default``) at all.

``default`` is the effort that passes the CLI no effort flag, so it runs with its own configured one.
It is always accepted, listed or not: a form that sends no effort must still open a session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from secretary.po.store import CLIS, DEFAULT_EFFORT

DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("fable", "opus", "sonnet"),
    "codex": ("gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"),
}

# What each CLI's flag takes (checked on Claude Code 2.1.280, `--effort`, and Codex 0.155.1,
# `-c model_reasoning_effort=`), with `default` first so a form preselects it.
DEFAULT_EFFORTS: dict[str, tuple[str, ...]] = {
    "claude": (DEFAULT_EFFORT, "low", "medium", "high", "xhigh", "max"),
    "codex": (DEFAULT_EFFORT, "low", "medium", "high", "xhigh"),
}


def models_from_instance(instance: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    return _lists(instance, "models", DEFAULT_MODELS)


def efforts_from_instance(instance: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    return _lists(instance, "efforts", DEFAULT_EFFORTS)


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


__all__ = ["DEFAULT_EFFORTS", "DEFAULT_MODELS", "efforts_from_instance", "models_from_instance"]
