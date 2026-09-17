from __future__ import annotations

import base64
import gzip
import unittest
from pathlib import Path


class A12SourceBridgeTests(unittest.TestCase):
    def test_emit_large_sources_for_connector_patch(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for label, relative in (
            ("TASKS", "src/secretary/tasks.py"),
            ("DISPATCHER", "src/secretary/dispatcher.py"),
        ):
            encoded = base64.b64encode(gzip.compress((root / relative).read_bytes(), mtime=0)).decode()
            print(f"A12_SOURCE_{label}={encoded}")


if __name__ == "__main__":
    unittest.main()
