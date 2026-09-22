"""Where an installation and its product checkout live when nothing names them.

The product ships no absolute path of its own. An installation is named by ``--instance`` or
``SECRETARY_INSTANCE``, a checkout by ``--product-root`` or ``TA_SECRETARY_REPO``; without either,
both resolve under the running user's home. That is what makes one checkout installable for any
user instead of only for the host it grew up on, and it keeps a single spelling of each fallback so
the CLI, the units, the pipeline tick and the curator cannot disagree about which installation or
which checkout they are talking to.

Only the fallback lives here. Every caller reads its own override first, so an operator who
configured a path keeps it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def configured_product_root(environ: Mapping[str, str] | None = None) -> Path:
    """The product checkout this process was pointed at, or the home default.

    Deliberately not the checkout containing the running module. An upgrade run out of a candidate
    checkout materializes the installation the operator configured, and a repair run out of a
    rescue copy must not silently install that copy; both are named by ``TA_SECRETARY_REPO`` or by
    ``--product-root``, which the callers read first.
    """
    env = os.environ if environ is None else environ
    configured = env.get(PRODUCT_ENV)
    return Path(configured).expanduser() if configured else default_product_root()


def instance_dir(path: Path | str) -> Path:
    """The instance directory for a path that may name either the directory or its config file.

    Callers take ``--instance`` from a human, who reasonably writes either spelling.
    """
    resolved = Path(path).expanduser()
    return resolved.parent if resolved.name == INSTANCE_CONFIG_NAME else resolved


def component_enabled(host: dict[str, Any], component: str) -> bool:
    """Whether an installation wants a packaged component. An omitted entry means yes."""
    components = host.get("components") if isinstance(host, dict) else None
    if not isinstance(components, dict):
        return True
    entry = components.get(component)
    if not isinstance(entry, dict):
        return True
    return entry.get("enabled") is not False
