from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from secretary.board_transport import BoardTransportError, ensure, ensure_from_runtime_file, resolve
from secretary.tasks import KanboardClient


class BoardTransportTests(unittest.TestCase):
    def test_default_is_deterministic_and_matches_client_basic_auth(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one, _ = ensure(Path(first))
            two, _ = ensure(Path(second))
            self.assertEqual(one, two)
            client = KanboardClient(one.as_environ())
        self.assertEqual(client.url, one.url)
        self.assertEqual(
            one.authorization_header(),
            "Basic " + base64.b64encode(f"{client.user}:{client.token}".encode()).decode(),
        )

    def test_legacy_runtime_is_imported_once_then_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "OTHER=value\nKANBOARD_URL=http://legacy/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8"
            )
            transport, status = ensure_from_runtime_file(instance, runtime)
            self.assertEqual(status, "imported-legacy")
            self.assertEqual(transport.token, "legacy-token")
            self.assertEqual(runtime.read_text(encoding="utf-8"), "OTHER=value\n")
            self.assertEqual(resolve(instance), transport)

    def test_conflicting_legacy_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ensure(instance)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://other/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=other-token\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(BoardTransportError, "board transport mismatch"):
                ensure_from_runtime_file(instance, runtime)


if __name__ == "__main__":
    unittest.main()
