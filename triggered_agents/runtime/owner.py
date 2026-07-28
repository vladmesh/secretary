"""Who the installation escalates to.

Blocking a card is an address, not a decoration: the comment has to name the human who owns this
installation. That name is installation configuration, so the product carries a neutral default
and reads ``SECRETARY_OWNER`` when a host wants its own.
"""
from __future__ import annotations

import os
from typing import Mapping

OWNER_ENV = "SECRETARY_OWNER"
DEFAULT_OWNER = "владельца установки"


def owner(environ: Mapping[str, str] | None = None) -> str:
    """The configured owner name, or the neutral default an unconfigured installation uses."""
    env = os.environ if environ is None else environ
    return (env.get(OWNER_ENV) or "").strip() or DEFAULT_OWNER
