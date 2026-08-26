"""Shell prefixes that make the provisioned secretary source importable to a head."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .paths import PRODUCT_DIRNAME, configured_product_root

SECRETARY_REPO_ENV = "TA_SECRETARY_REPO"
# The same fallback written as a shell expression: the prefixes below are rendered into card text
# and run by a head in its own shell, so the home has to be the one that head runs as rather than
# the one this process resolved.
SECRETARY_SOURCE_SHELL = (
    f'"${{{SECRETARY_REPO_ENV}:-$HOME/{PRODUCT_DIRNAME}}}/src${{PYTHONPATH:+:$PYTHONPATH}}"'
)


def secretary_repo(environ: dict[str, str] | None = None) -> Path:
    """The checkout this process imports the product from. Resolved per call, never at import:
    the fallback hangs off a home, and a caller passing an environment is asking about that one."""
    return configured_product_root(os.environ if environ is None else environ)


def pythonpath_prefix(environ: dict[str, str] | None = None) -> str:
    """The PYTHONPATH assignment that makes the provisioned secretary source importable.

    Left as a shell expression by default: these prefixes are rendered into card text a head runs
    later in its own shell, where its own ``TA_SECRETARY_REPO`` and home are the right answer.

    A caller that is building the launch command itself passes its environment and gets the
    checkout written out. That command carries ``TA_SECRETARY_REPO=<root>`` as its own assignment
    prefix, and whether an assignment in a simple command is visible to a later word of the same
    command is unspecified — so the launcher resolves the checkout instead of hoping the head's
    shell resolves it in the order it was written.
    """
    if environ is None:
        return f"PYTHONPATH={SECRETARY_SOURCE_SHELL}"
    return f'PYTHONPATH={shlex.quote(str(secretary_repo(environ) / "src"))}"${{PYTHONPATH:+:$PYTHONPATH}}"'
