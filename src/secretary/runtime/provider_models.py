"""Which model and reasoning effort a provider session actually ran, read from its own journal.

A head's configuration names a model the way its operator spells it — `opus`, or nothing at all
under `cli_default` — and the CLI resolves that name at startup. The only record of what it resolved
to is the provider's session journal, so this module reads it there and nowhere else:

* Claude writes one record per assistant message; ``message.model`` is the full model id
  (`claude-opus-5-5`) and the record's own ``effort`` is the reasoning effort it ran with. A message
  the CLI synthesized itself (an API error, an interrupt) carries the model ``<synthetic>`` and names
  no model at all.
* Codex writes one ``turn_context`` record per turn; its payload names ``model`` and ``effort``
  (``reasoning_effort`` in older rollouts).

Both answers are session-wide through the last record read: a session can switch models, so every
distinct model is kept in order of its last use, and the last one is the model the session ended on.
Nothing here raises on a record it does not recognise; such a record names nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ProviderModels",
    "claude_session_models",
    "codex_rollout_path",
    "codex_session_models",
]

# Claude Code's model id on a message it wrote itself rather than received from a model.
CLAUDE_SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class ProviderModels:
    """Every model a session ran, oldest last use first, and the effort it last ran with."""

    models: tuple[str, ...] = ()
    effort: str = ""

    @property
    def model(self) -> str:
        """The model the session ended on, or an empty string when the journal named none."""
        return self.models[-1] if self.models else ""


def claude_session_models(records: Iterable[Any]) -> ProviderModels:
    """The resolved models and effort of a Claude session journal."""
    used: dict[str, None] = {}
    effort = ""
    for record in records:
        if not isinstance(record, Mapping) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        model = _text(message.get("model")) if isinstance(message, Mapping) else ""
        if model and model != CLAUDE_SYNTHETIC_MODEL:
            used.pop(model, None)
            used[model] = None
        effort = _text(record.get("effort")) or effort
    return ProviderModels(tuple(used), effort)


def codex_session_models(records: Iterable[Any]) -> ProviderModels:
    """The resolved models and effort of a Codex rollout."""
    used: dict[str, None] = {}
    effort = ""
    for record in records:
        if not isinstance(record, Mapping) or record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            continue
        model = _text(payload.get("model"))
        if model:
            used.pop(model, None)
            used[model] = None
        effort = _text(payload.get("effort")) or _text(payload.get("reasoning_effort")) or effort
    return ProviderModels(tuple(used), effort)


def codex_rollout_path(codex_home: Path | str, thread_id: str) -> Path | None:
    """The rollout Codex keeps for one thread, ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl``.

    The thread id is the file name's suffix and the date directories are the thread's start, so the
    lookup is one glob over them; ``None`` when no file or more than one answers.
    """
    if not thread_id or "/" in thread_id or "*" in thread_id:
        return None
    found = sorted((Path(codex_home) / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return found[0] if len(found) == 1 else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
