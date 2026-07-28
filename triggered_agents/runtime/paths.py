"""Where an installation lives when nothing names it.

The product ships no absolute path of its own. An installation is named by ``--instance`` or
``SECRETARY_INSTANCE``; without either, everything resolves relative to the running user's home.
That is what makes one checkout installable for any user instead of only for the host it grew up
on, and it keeps a single spelling of the fallback so the CLI, the units, the pipeline tick and
the curator cannot disagree about which installation they are talking to.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

INSTANCE_ENV = "SECRETARY_INSTANCE"
PRODUCT_ENV = "TA_SECRETARY_REPO"
INSTANCE_DIRNAME = "secretary-instance"
PRODUCT_DIRNAME = "secretary"
INSTANCE_CONFIG_NAME = "instance.yaml"


def default_instance_path() -> Path:
    """The instance directory of a host that never configured one."""
    return Path.home() / INSTANCE_DIRNAME


def default_product_root() -> Path:
    """The product checkout of a host that never configured one."""
    return Path.home() / PRODUCT_DIRNAME


def configured_instance_path(environ: Mapping[str, str] | None = None) -> Path:
    """The instance this process was pointed at, or the home default."""
    env = os.environ if environ is None else environ
    configured = env.get(INSTANCE_ENV)
    return Path(configured).expanduser() if configured else default_instance_path()


def instance_dir(path: Path | str) -> Path:
    """The instance directory for a path that may name either the directory or its config file.

    Callers take ``--instance`` from a human, who reasonably writes either spelling.
    """
    resolved = Path(path).expanduser()
    return resolved.parent if resolved.name == INSTANCE_CONFIG_NAME else resolved
