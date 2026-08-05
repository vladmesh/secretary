from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board_transport import BoardTransportError, ensure, ensure_from_runtime_file, resolve
from secretary.tasks import KanboardClient, TaskError
from triggered_agents.runtime import kanboard


class BoardTransportTests(unittest.TestCase):
    def test_default_is_deterministic_and_matches_client_basic_auth(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one, _ = ensure(Path(first))
            two, _ = ensure(Path(second))
            self.assertEqual(one, two)
            client = KanboardClient(transport=one)
        self.assertEqual(client.url, one.url)
        self.assertEqual(
            one.authorization_header(),
            "Basic " + base64.b64encode(f"{client.user}:{client.token}".encode()).decode(),
        )

    def test_both_clients_send_the_resolved_basic_auth_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            transport, _ = ensure(instance)
            observed: list[str] = []

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self) -> bytes:
                    return b'{"result": "1.2.3"}'

            def open_request(request, **_kwargs):
                observed.append(request.get_header("Authorization"))
                return Response()

            with (
                mock.patch("secretary.tasks.urllib.request.urlopen", side_effect=open_request),
                mock.patch("triggered_agents.runtime.kanboard.urllib.request.urlopen", side_effect=open_request),
                mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(instance)}, clear=True),
            ):
                KanboardClient().call("getVersion")
                kanboard.call("getVersion")

        self.assertEqual(observed, [transport.authorization_header(), transport.authorization_header()])

    def test_legacy_runtime_is_imported_once_then_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "OTHER=value\nKANBOARD_URL=http://legacy/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8"
            )
            runtime.chmod(0o600)
            transport, status = ensure_from_runtime_file(instance, runtime)
            self.assertEqual(status, "imported-legacy")
            self.assertEqual(transport.token, "legacy-token")
            self.assertEqual(runtime.read_text(encoding="utf-8"), "OTHER=value\n")
            self.assertEqual(resolve(instance), transport)
            self.assertEqual((instance / "board-transport.env").stat().st_mode & 0o777, 0o600)

    def test_whitespace_in_a_valid_runtime_assignment_is_migrated_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                " KANBOARD_URL=http://legacy/jsonrpc.php\n"
                " KANBOARD_API_USER=jsonrpc\n KANBOARD_API_TOKEN=legacy-token\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)

            transport, status = ensure_from_runtime_file(instance, runtime)

        self.assertEqual((transport.token, status), ("legacy-token", "imported-legacy"))

    def test_dry_run_truthfully_reports_retiring_a_matching_legacy_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8",
            )
            runtime.chmod(0o600)
            ensure_from_runtime_file(instance, runtime)
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8",
            )
            runtime.chmod(0o600)

            _, preview = ensure_from_runtime_file(instance, runtime, dry_run=True)
            _, applied = ensure_from_runtime_file(instance, runtime)

        self.assertEqual((preview, applied), ("retired-legacy", "retired-legacy"))

    def test_conflicting_legacy_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ensure(instance)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://other/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=other-token\n", encoding="utf-8"
            )
            runtime.chmod(0o600)
            with self.assertRaisesRegex(BoardTransportError, "board transport mismatch"):
                ensure_from_runtime_file(instance, runtime)

    def test_duplicate_legacy_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_URL=http://other/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8"
            )
            runtime.chmod(0o600)
            with self.assertRaisesRegex(BoardTransportError, "ambiguous"):
                ensure_from_runtime_file(Path(tmp), runtime)

    def test_normal_client_does_not_use_ambient_legacy_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {
                "SECRETARY_INSTANCE": str(Path(tmp) / "missing-instance"),
                "KANBOARD_URL": "http://legacy/jsonrpc.php",
                "KANBOARD_API_USER": "jsonrpc",
                "KANBOARD_API_TOKEN": "legacy-token",
            }, clear=True):
                with self.assertRaisesRegex(TaskError, "configuration is unavailable"):
                    KanboardClient()


if __name__ == "__main__":
    unittest.main()
