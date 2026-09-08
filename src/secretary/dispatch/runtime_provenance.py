"""Production interpreter provenance at dispatcher-owned lifecycle boundaries.

The dispatcher runs this probe with its registered production interpreter, in isolated Python
mode, before it trusts or destroys a candidate workspace.  Candidate environments are deliberately
not repaired here: a mismatch is evidence an operator must inspect, not permission to rewrite an
installation while a task checkout is still involved.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROBE = r"""
import importlib
import importlib.metadata
import json
import pathlib
import site
import sys

package = sys.argv[1]
payload = {"python": sys.executable, "import_origin": "", "metadata_targets": []}
try:
    imported = importlib.import_module(package)
    payload["import_origin"] = getattr(imported, "__file__", "") or ""
except Exception as exc:
    payload["import_error"] = type(exc).__name__

roots = []
try:
    roots.extend(site.getsitepackages())
except Exception:
    pass
try:
    user = site.getusersitepackages()
    if isinstance(user, str):
        roots.append(user)
except Exception:
    pass
seen = set()
for raw_root in roots:
    root = pathlib.Path(raw_root)
    if not root.is_dir():
        continue
    for pth in root.glob("*.pth"):
        try:
            lines = pth.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            value = line.strip()
            if not value or value.startswith("#") or value.startswith("import "):
                continue
            target = pathlib.Path(value)
            if not target.is_absolute():
                target = pth.parent / target
            item = ("pth", str(pth), str(target))
            if item not in seen:
                seen.add(item)
                payload["metadata_targets"].append(item)
    for direct in root.glob("*.dist-info/direct_url.json"):
        try:
            body = json.loads(direct.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        url = body.get("url") if isinstance(body, dict) else None
        if isinstance(url, str) and url.startswith("file://"):
            from urllib.parse import unquote, urlsplit
            target = unquote(urlsplit(url).path)
            item = ("direct_url", str(direct), target)
            if item not in seen:
                seen.add(item)
                payload["metadata_targets"].append(item)
print(json.dumps(payload, sort_keys=True))
"""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _inside(path: str | Path, root: str | Path) -> bool:
    try:
        return _resolved(path).is_relative_to(_resolved(root))
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class ProductionRuntime:
    """The single value identifying the runtime whose integrity the dispatcher protects."""

    interpreter: str
    product_root: str
    package: str = "secretary"
    workspaces_root: str = ""

    @classmethod
    def current(cls, product_root: Path | str, *, workspaces_root: Path | str = "") -> ProductionRuntime:
        root = workspaces_root or os.environ.get(
            "SECRETARY_DISPATCHER_WORKSPACES_ROOT", str(Path.home() / "orca" / "workspaces")
        )
        registered = Path(product_root).expanduser()
        # Capture the interpreter that is actually running the dispatcher. Production's systemd
        # unit starts the entry point from the registered checkout's venv; recording sys.executable
        # preserves that fact without guessing a layout that differs for an installed CI runner.
        return cls(sys.executable, str(registered), workspaces_root=str(root))

    def probe(self) -> RuntimeProvenance:
        """Observe this exact interpreter without ambient source-path assistance."""
        # Preserve a venv's interpreter path. Resolving its ordinary ``bin/python3`` symlink to the
        # base executable changes Python's prefix discovery and silently bypasses that venv.
        expected_python = Path(self.interpreter).expanduser().absolute()
        if not expected_python.is_file() or not os.access(expected_python, os.X_OK):
            return RuntimeProvenance(
                classification="interpreter_unavailable",
                interpreter=str(expected_python),
                product_root=str(_resolved(self.product_root)),
                import_origin="",
                metadata_targets=(),
            )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        try:
            completed = subprocess.run(
                [str(expected_python), "-I", "-c", _PROBE, self.package],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return RuntimeProvenance(
                classification="interpreter_unavailable",
                interpreter=str(expected_python),
                product_root=str(_resolved(self.product_root)),
                import_origin="",
                metadata_targets=(),
            )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        observed_python = str(payload.get("python") or expected_python)
        origin = str(payload.get("import_origin") or "")
        raw_targets = payload.get("metadata_targets")
        targets: list[tuple[str, str, str]] = []
        if isinstance(raw_targets, list):
            for entry in raw_targets:
                if (
                    isinstance(entry, list)
                    and len(entry) == 3
                    and all(isinstance(value, str) for value in entry)
                ):
                    targets.append((entry[0], entry[1], entry[2]))
        classification = "valid"
        offending = ""
        for _kind, _source, target in targets:
            if self.workspaces_root and _inside(target, self.workspaces_root):
                classification = "workspace_targeted_editable"
                offending = target
                break
        if classification == "valid" and (completed.returncode != 0 or not origin):
            classification = "missing_import"
        elif classification == "valid" and not _inside(origin, self.product_root):
            classification = "wrong_root"
            offending = origin
        return RuntimeProvenance(
            classification=classification,
            interpreter=observed_python,
            product_root=str(_resolved(self.product_root)),
            import_origin=origin,
            metadata_targets=tuple(targets),
            offending_target=offending,
        )


@dataclass(frozen=True)
class RuntimeProvenance:
    classification: str
    interpreter: str
    product_root: str
    import_origin: str
    metadata_targets: tuple[tuple[str, str, str], ...]
    offending_target: str = ""

    @property
    def valid(self) -> bool:
        return self.classification == "valid"

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "interpreter": self.interpreter,
            "product_root": self.product_root,
            "import_origin": self.import_origin,
            "metadata_targets": [
                {"kind": kind, "source": source, "target": target}
                for kind, source, target in self.metadata_targets
            ],
            "offending_target": self.offending_target,
        }

    def refusal(self, boundary: str) -> str:
        detail = self.offending_target or self.import_origin or self.interpreter
        return (
            f"production runtime provenance refused at {boundary}: {self.classification}; "
            f"interpreter={self.interpreter}; product_root={self.product_root}; observed={detail}"
        )
