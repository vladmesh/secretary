"""The installation's data dir, for the CODEX_HOME resolver that cannot read it itself.

`codex_preflight.resolve_codex_home` takes `<data_dir>/codex-home` once it holds a login, but that
module imports nothing else of `secretary`, so it knows the data dir only when it is named or in
`SECRETARY_DATA_DIR`. This module is the other half: it reads the data dir of the selected
installation, and binds it into `SECRETARY_DATA_DIR` for the processes that launch Codex heads, so
the command a head is launched with and the preflight that writes its trust resolve one home.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from secretary.runtime.codex_preflight import CodexHome, resolve_codex_home

DATA_DIR_ENV = "SECRETARY_DATA_DIR"
INSTANCE_ENV = "SECRETARY_INSTANCE"


def selected_data_dir() -> Path | None:
    """`SECRETARY_DATA_DIR`, else the `data_dir` of an explicitly selected `SECRETARY_INSTANCE`.

    None with neither, or with an instance whose data dir cannot be resolved. The default instance
    path is never read: a checkout on a host with an installation must not pick up its login.
    """
    configured = os.environ.get(DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    instance = os.environ.get(INSTANCE_ENV)
    if not instance:
        return None
    from secretary.config import DataDirError, instance_data_dir

    try:
        return instance_data_dir(Path(instance))
    except (DataDirError, OSError):
        return None


def installation_codex_home(profile: Mapping[str, Any] | None = None) -> CodexHome:
    """The CODEX_HOME this process's installation launches Codex heads with."""
    return resolve_codex_home(profile or {}, data_dir=selected_data_dir())


@contextlib.contextmanager
def bound_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Iterator[None]:
    """Name the installation's data dir in `SECRETARY_DATA_DIR` for the duration of the block.

    `data_dir` is the one the caller already serves; unnamed, it is `selected_data_dir()`. A value
    already in the environment is the operator's and is left as it is. The previous environment is
    restored on exit, so an in-process caller (a test, a CLI invoked twice) keeps no trace.
    """
    if os.environ.get(DATA_DIR_ENV):
        yield
        return
    target = Path(data_dir).expanduser() if data_dir is not None else selected_data_dir()
    if target is None:
        yield
        return
    os.environ[DATA_DIR_ENV] = str(target)
    try:
        yield
    finally:
        os.environ.pop(DATA_DIR_ENV, None)
