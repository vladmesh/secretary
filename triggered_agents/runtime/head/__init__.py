"""The head: the thing a pipeline run is actually carried out by, as a type rather than a dict.

A head is one agent session — a Codex TUI, a Claude terminal — that the product brings up, points
at a task document, and eventually stops. Three operations describe its whole life (spawn, nudge,
stop) and they will live here, next to the type they all take. What is here now is that type,
`HeadSpec`, and the one loader that turns whichever registry this installation runs off into specs.

The neighbours are the other two halves of the same boundary: `prompt_document` owns what a head is
given, `pane_host` owns the pane it runs in. This package owns which head that is.
"""
from __future__ import annotations

from .spec import DEFAULT_EFFORT, HeadSpec, HeadSpecError, head_spec, load_head_specs

__all__ = [
    "DEFAULT_EFFORT",
    "HeadSpec",
    "HeadSpecError",
    "head_spec",
    "load_head_specs",
]
