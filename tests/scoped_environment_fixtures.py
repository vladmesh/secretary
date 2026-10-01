"""The actual deployed argv producer, loaded without running its old launcher."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from secretary.runtime.head.memory import scope_unit

DEPLOYED_PO_SHA = "64c42d735bd99740bba94c65788d34f7f354bee0"


def deployed_scope_argv():
    # Both local Git and exact-SHA CI retain this delivered main ancestor. Execute
    # only the old emitter, with native identity and the same public scope hash.
    root = Path(__file__).resolve().parents[1]
    source = subprocess.run(
        ["git", "show", f"{DEPLOYED_PO_SHA}:src/secretary/runtime/head/memory.py"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "scope_argv")
    namespace = {"os": os, "sys": sys, "scope_unit": scope_unit}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "deployed-scope-argv", "exec"), namespace)
    return namespace["scope_argv"]
