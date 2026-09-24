"""The Orca and git workspaces roots are disjoint, or the dispatcher does not start (secretary-1710).

Which manager owns a workspace is read from its path alone, so an Orca root equal to, inside, or
around `<data_dir>/workspaces` would hand one path to both. The real host refuses at startup and
names both paths.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatch.bootstrap import runtime_from_args, validate_workspace_roots
from secretary.dispatch.git_workspace import (
    ORCA_WORKSPACES_ROOT_ENV,
    orca_workspaces_root,
    workspace_roots_overlap,
)
from secretary.dispatch.types import DispatcherError


class WorkspaceRootsOverlapTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data_dir = self.root / "secretary-data"
        self.git_root = self.data_dir / "workspaces"

    def assert_refused(self, orca_root: Path) -> None:
        with (
            mock.patch.dict(os.environ, {ORCA_WORKSPACES_ROOT_ENV: str(orca_root)}),
            self.assertRaises(DispatcherError) as refused,
        ):
            validate_workspace_roots(self.data_dir)
        self.assertEqual(refused.exception.code, "workspace_roots_overlap")
        self.assertIn(str(orca_root), refused.exception.message)
        self.assertIn(str(self.git_root), refused.exception.message)

    def test_equal_roots_are_refused(self) -> None:
        self.assert_refused(self.git_root)

    def test_a_git_root_inside_the_orca_root_is_refused(self) -> None:
        self.assert_refused(self.data_dir)
        self.assert_refused(self.root)

    def test_an_orca_root_inside_the_git_root_is_refused(self) -> None:
        self.assert_refused(self.git_root / "orca")

    def test_a_symlinked_alias_of_the_git_root_is_refused(self) -> None:
        self.git_root.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(self.git_root, target_is_directory=True)
        with (
            mock.patch.dict(os.environ, {ORCA_WORKSPACES_ROOT_ENV: str(alias)}),
            self.assertRaises(DispatcherError),
        ):
            validate_workspace_roots(self.data_dir)

    def test_the_live_shaped_layout_passes(self) -> None:
        home = self.root / "home" / "dev"
        orca = home / "orca" / "workspaces"
        self.assertIsNone(workspace_roots_overlap(orca, home / "secretary-data"))
        # A sibling whose name shares a prefix is not inside.
        self.assertIsNone(workspace_roots_overlap(self.root / "secretary-data-workspaces", self.data_dir))
        with mock.patch.dict(os.environ, {ORCA_WORKSPACES_ROOT_ENV: str(orca)}):
            validate_workspace_roots(home / "secretary-data")

    def test_the_default_orca_root_is_the_home_one(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(ORCA_WORKSPACES_ROOT_ENV, None)
            self.assertEqual(orca_workspaces_root(), Path.home() / "orca" / "workspaces")

    def test_the_real_dispatcher_refuses_before_it_touches_the_board(self) -> None:
        with (
            mock.patch.dict(os.environ, {ORCA_WORKSPACES_ROOT_ENV: str(self.data_dir)}),
            mock.patch("secretary.dispatch.bootstrap.board_client") as board,
            self.assertRaises(DispatcherError) as refused,
        ):
            runtime_from_args(
                str(self.root / "instance"),
                str(self.data_dir),
                host_mode="real",
                owner="secretary-production",
            )
        self.assertEqual(refused.exception.code, "workspace_roots_overlap")
        board.assert_not_called()


if __name__ == "__main__":
    unittest.main()
