from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


@contextlib.contextmanager
def fixture_runtime_account(root: Path):
    """Resolve the installation's runtime account to a fixture-owned `operator` under `root`."""
    home = root / "operator"
    home.mkdir(parents=True, exist_ok=True)
    account = SimpleNamespace(pw_name="operator", pw_dir=str(home))
    with (
        mock.patch("secretary.host_apply.pwd.getpwuid", return_value=account),
        mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
    ):
        yield home
