from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "pull-backups-offsite.sh"


class PullBackupsOffsiteTests(unittest.TestCase):
    def test_pull_copies_archive_and_updates_last_fetch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fakebin = root / "bin"
            remote_data = root / "remote-data"
            local_backups = root / "local-backups"
            remote_backups = remote_data / "backups"
            fakebin.mkdir()
            remote_backups.mkdir(parents=True)
            (remote_backups / "backup-20260710.tar.age").write_text(
                "encrypted\n", encoding="utf-8"
            )
            _write_executable(
                fakebin / "ssh",
                """#!/usr/bin/env bash
set -euo pipefail
target=$1
shift
bash -c "$1"
""",
            )
            _write_executable(
                fakebin / "rsync",
                """#!/usr/bin/env bash
set -euo pipefail
source=${@: -2:1}
dest=${@: -1}
source_path=${source#*:}
mkdir -p "$dest"
cp "$source_path"/*.tar.age "$dest"/
""",
            )

            env = os.environ.copy()
            env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"
            env["SECRETARY_SSH_COMMAND"] = str(fakebin / "ssh")

            result = subprocess.run(
                [
                    str(SCRIPT),
                    "localhost",
                    str(remote_data),
                    str(local_backups),
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            copied_archive = local_backups / "backup-20260710.tar.age"
            marker = remote_backups / "last_fetch"
            copied_archive_exists = copied_archive.is_file()
            if marker.exists():
                marker_text = marker.read_text(encoding="utf-8").strip()
            else:
                marker_text = ""

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(copied_archive_exists)
        self.assertRegex(marker_text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_empty_remote_backups_does_not_update_last_fetch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fakebin = root / "bin"
            remote_data = root / "remote-data"
            local_backups = root / "local-backups"
            remote_backups = remote_data / "backups"
            fakebin.mkdir()
            remote_backups.mkdir(parents=True)
            _write_executable(
                fakebin / "ssh",
                """#!/usr/bin/env bash
set -euo pipefail
target=$1
shift
bash -c "$1"
""",
            )

            env = os.environ.copy()
            env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"
            env["SECRETARY_SSH_COMMAND"] = str(fakebin / "ssh")
            env["SECRETARY_SCP_COMMAND"] = str(fakebin / "scp")

            result = subprocess.run(
                [
                    str(SCRIPT),
                    "localhost",
                    str(remote_data),
                    str(local_backups),
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            marker_exists = (remote_backups / "last_fetch").exists()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("no encrypted backup archives found", result.stderr)
        self.assertFalse(marker_exists)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


if __name__ == "__main__":
    unittest.main()
