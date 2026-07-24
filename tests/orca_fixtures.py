from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


@contextlib.contextmanager
def legacy_orca_runtime(root: Path):
    """Provide a legacy Orca executable for a fixture-owned runtime account."""
    home = root / "operator"
    executable = home / ".local" / "bin" / "orca"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    account = SimpleNamespace(pw_name="operator", pw_dir=str(home))
    with (
        mock.patch("secretary.host_apply.pwd.getpwuid", return_value=account),
        mock.patch("secretary.host_apply.pwd.getpwnam", return_value=account),
        mock.patch("secretary.host_apply.find_orca_executable", return_value=executable),
    ):
        yield executable
