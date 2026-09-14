"""Which models a PO session may be opened with, per CLI.

`instance.yaml` may say so under ``po.models``; a CLI it does not name keeps the product default, and
an empty list offers that CLI no model at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from secretary.po.store import CLIS

DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "claude": ("fable", "opus", "sonnet"),
    "codex": ("gpt-6-astra", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"),
}


def models_from_instance(instance: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    section = (instance or {}).get("po") if isinstance(instance, Mapping) else None
    configured = section.get("models") if isinstance(section, Mapping) else None
    models: dict[str, tuple[str, ...]] = {}
    for cli in CLIS:
        values = configured.get(cli) if isinstance(configured, Mapping) else None
        if values is None:
            models[cli] = DEFAULT_MODELS[cli]
        else:
            models[cli] = tuple(str(value).strip() for value in values if str(value).strip())
    return models


__all__ = ["DEFAULT_MODELS", "models_from_instance"]
