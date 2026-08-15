from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.task_commands import resolve_data_dir
from secretary.tasks import TaskError


def _args(**overrides) -> argparse.Namespace:
    values = {"data_dir": None, "instance": None}
    values.update(overrides)
    return argparse.Namespace(**values)


class ResolveDataDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance_dir = Path(self.tmp.name) / "secretary-instance"
        self.instance_dir.mkdir()

    def write_instance(self, data_dir: str) -> Path:
        path = self.instance_dir / "instance.yaml"
        path.write_text(
            "version: 1\n"
            "name: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return path

    def test_explicit_data_dir_wins(self) -> None:
        self.write_instance("/var/lib/secretary-data")
        args = _args(data_dir="/elsewhere/data", instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/elsewhere/data")

    def test_absolute_data_dir_from_instance(self) -> None:
        self.write_instance("/var/lib/secretary-data")
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/var/lib/secretary-data")

    def test_relative_instance_data_dir_pins_to_instance_not_cwd(self) -> None:
        self.write_instance("secretary-data")
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), str(self.instance_dir / "secretary-data"))

    def test_workspace_cwd_never_becomes_the_data_dir(self) -> None:
        self.write_instance("/var/lib/secretary-data")
        workspace = Path(self.tmp.name) / "workspace"
        workspace.mkdir()
        cwd = os.getcwd()
        os.chdir(workspace)
        self.addCleanup(os.chdir, cwd)
        args = _args(instance=str(self.instance_dir))
        self.assertEqual(resolve_data_dir(args), "/var/lib/secretary-data")
        self.assertFalse((workspace / "secretary-data").exists())

    def test_instance_file_path_is_accepted(self) -> None:
        instance_file = self.write_instance("/var/lib/secretary-data")
        args = _args(instance=str(instance_file))
        self.assertEqual(resolve_data_dir(args), "/var/lib/secretary-data")

    def test_missing_instance_is_a_usage_error(self) -> None:
        args = _args(instance=str(self.instance_dir / "absent"))
        with self.assertRaises(TaskError) as caught:
            resolve_data_dir(args)
        self.assertEqual(caught.exception.code, "usage")
        self.assertIn("--data-dir", caught.exception.message)

    def test_instance_without_data_dir_is_a_usage_error(self) -> None:
        (self.instance_dir / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        args = _args(instance=str(self.instance_dir))
        with self.assertRaises(TaskError) as caught:
            resolve_data_dir(args)
        self.assertIn("data_dir", caught.exception.message)

    def test_env_data_dir_is_read_at_parse_time(self) -> None:
        from secretary.cli import build_parser

        with mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": "/env/data"}):
            parser = build_parser()
            args = parser.parse_args(
                ["task", "report", "--ref", "x-1", "--role", "worker", "--kind", "done"]
            )
        self.assertEqual(resolve_data_dir(args), "/env/data")


if __name__ == "__main__":
    unittest.main()
