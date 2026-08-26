"""Persistent pipeline-wide pause flag: `state/pipeline/pause.json`.

Absent (no file) = running. Present = paused, with internal `mode` stored as `"soft"` or `"hard"`.

This is the flag the background roles shed on, not the pause the task dispatcher itself obeys.
`secretary pause` writes the production flag and mirrors it here
(`secretary/dispatcher_pause.write_legacy_mirror`); `secretary resume` removes the mirror it
wrote. Nothing here writes the file: the mirror is the only writer, and the only live reader is
runtime/dispatch.py (steward/curator/retro dispatch), through is_paused(). Kept in its own tiny
module (state.py primitives only, no ops import) so runtime/dispatch.py can read it without
pulling in the board machinery — the same reason it lazy-imports agents.pipeline.health rather
than importing more of this package.
"""

from __future__ import annotations

import json

from .state import STATE

PAUSE_FILE = STATE.dir / "pause.json"
MODES = ("soft", "hard")
PUBLIC_MODES = ("drain", "freeze")


def load() -> dict:
    """{} (not paused) when the file is absent or unreadable. A corrupt file fails toward "not
    paused" rather than wedging every caller (dispatch.run reads it on every single tick) — but
    that fail-open is exactly backwards from the pause flag's own purpose, so it's not silent:
    logged as a warn every time it's hit, same discipline as any other recurring-until-fixed
    condition here."""
    if not PAUSE_FILE.is_file():
        return {}
    try:
        return json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        STATE.log_run("pause-flag", result="corrupt", level="warn", error=str(e))
        return {}


def is_paused() -> bool:
    return bool(load())
