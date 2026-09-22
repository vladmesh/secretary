"""The retired board transport's leftovers, as a stale installation may still carry them.

Nothing in the product names them any more, so what a test proves about a leftover -- that it is
neither read nor reported, or that it is an ordinary entry like any other -- holds for any name.
`RETIRED_STORE` is therefore a neutral stand-in for the word an earlier store used, not that word:
the reader of historical identities accepts any lowercase store word (`board/backend.py`), and the
real historical form is checked on the live store, not here.
"""

from __future__ import annotations

from pathlib import Path

RETIRED_STORE = "legacy"
STALE_FILE = "-".join(("board", "transport")) + ".env"
LEGACY_ENV = tuple(f"{RETIRED_STORE.upper()}_{suffix}" for suffix in ("URL", "API_USER", "API_TOKEN"))
LEGACY_SECRET_IDS = tuple(name.lower() for name in LEGACY_ENV)
LEGACY_VALUES = ("http://127.0.0.1:8080/legacy-rpc", "legacy-user", "legacy-board-token-value")


def legacy_runtime_lines() -> str:
    """The three legacy runtime.env lines an older build left behind."""
    return "".join(f"{name}={value}\n" for name, value in zip(LEGACY_ENV, LEGACY_VALUES))


def write_stale_leftovers(instance: Path) -> Path:
    """Leave the stale transport file an older build materialized, mode 0600."""
    path = instance / STALE_FILE
    path.write_text(legacy_runtime_lines(), encoding="utf-8")
    path.chmod(0o600)
    return path
# The status/doctor section and the finding code the retired transport used to carry.
STATUS_SECTION = "_".join(("board", "transport"))
