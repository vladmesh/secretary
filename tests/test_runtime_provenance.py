"""Regression coverage for task-environment isolation and production import fences."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from secretary.dispatch.runtime_provenance import ProductionRuntime
from triggered_agents.runtime.role_env import runtime_env


def _fixture(root: Path, marker: str) -> None:
    package = root / "secretary"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
    (root / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='secretary', version='0.1', packages=['secretary'], extras_require={'dev': []})\n",
        encoding="utf-8",
    )


def _venv(root: Path) -> Path:
    subprocess.run(
        [os.sys.executable, "-m", "venv", "--system-site-packages", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    return root / "bin" / "python3"


def _install(python: Path, checkout: Path) -> None:
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-build-isolation",
            "-e",
            ".[dev]",
        ],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )


class ProductionRuntimeTests(unittest.TestCase):
    def test_missing_interpreter_has_its_own_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observed = ProductionRuntime(
                str(root / "missing-python"),
                str(root / "secretary"),
                workspaces_root=str(root / "workspaces"),
            ).probe()
        self.assertEqual(observed.classification, "interpreter_unavailable")

    def test_representative_worker_install_cannot_retarget_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "secretary"
            task = root / "workspaces" / "secretary" / "task-1"
            _fixture(production, "production")
            _fixture(task, "candidate")
            production_python = _venv(production / ".venv")
            _install(production_python, production)
            _venv(task / ".venv")

            env = runtime_env(
                "worker",
                base_env={"PATH": os.environ["PATH"], "PYTHONPATH": str(production)},
                env_file=root / "absent.env",
                workspace=task,
            )
            resolved = subprocess.run(
                ["/bin/sh", "-c", "command -v python3; command -v python; command -v pip"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertTrue(all(str(task / ".venv" / "bin") in entry for entry in resolved))
            self.assertNotIn("PYTHONPATH", env)

            # This is the ordinary incident command shape. The launch environment, not a rewritten
            # pip command, is what makes its target the disposable task environment.
            subprocess.run(
                ["python3", "-m", "pip", "install", "-e", ".[dev]"],
                cwd=task,
                env={**env, "PIP_NO_INDEX": "1", "PIP_NO_BUILD_ISOLATION": "1"},
                check=True,
                capture_output=True,
                text=True,
            )
            shutil.rmtree(task)

            runtime = ProductionRuntime(
                str(production_python),
                str(production),
                workspaces_root=str(root / "workspaces"),
            )
            observed = runtime.probe()
            self.assertEqual(observed.classification, "valid", observed.as_dict())
            tick = subprocess.run(
                [str(production_python), "-I", "-c", "import secretary; print(secretary.MARKER)"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tick.stdout.strip(), "production")

    def test_workspace_metadata_is_refused_before_and_after_checkout_vanishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "secretary"
            task = root / "workspaces" / "secretary" / "task-2"
            _fixture(production, "production")
            _fixture(task, "candidate")
            python = _venv(production / ".venv")
            _install(python, task)
            site_packages = next((production / ".venv" / "lib").glob("python*/site-packages"))
            dist_info = site_packages / "secretary-0.1.dist-info"
            dist_info.mkdir(exist_ok=True)
            (dist_info / "direct_url.json").write_text(
                json.dumps({"url": task.as_uri(), "dir_info": {"editable": True}}),
                encoding="utf-8",
            )
            runtime = ProductionRuntime(
                str(python), str(production), workspaces_root=str(root / "workspaces")
            )

            present = runtime.probe()
            self.assertEqual(present.classification, "workspace_targeted_editable")
            self.assertEqual({entry[0] for entry in present.metadata_targets}, {"pth", "direct_url"})
            shutil.rmtree(task)
            self.assertEqual(runtime.probe().classification, "workspace_targeted_editable")

    def test_symlinks_are_normalized_without_lexical_prefix_confusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            production = root / "secretary"
            linked_production = root / "registered-secretary"
            similarly_named = root / "secretary-old"
            workspaces = root / "real-workspaces"
            linked_workspaces = root / "workspaces"
            _fixture(production, "production")
            linked_production.symlink_to(production, target_is_directory=True)
            _fixture(similarly_named, "old")
            workspaces.mkdir()
            linked_workspaces.symlink_to(workspaces, target_is_directory=True)
            python = _venv(production / ".venv")
            _install(python, production)
            valid = ProductionRuntime(
                str(python), str(linked_production), workspaces_root=str(linked_workspaces)
            ).probe()
            self.assertEqual(valid.classification, "valid", valid.as_dict())

            wrong = ProductionRuntime(
                str(python), str(similarly_named), workspaces_root=str(linked_workspaces)
            ).probe()
            self.assertEqual(wrong.classification, "wrong_root")


if __name__ == "__main__":
    unittest.main()
