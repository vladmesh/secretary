from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.memory import access
from secretary.memory import health


class _Response:
    def __init__(self, status: int, body: str, *, session: str | None = None) -> None:
        self.status = status
        self._body = body.encode()
        self._session = session

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default=None):
        if name.lower() == "mcp-session-id":
            return self._session
        if name.lower() == "content-type":
            return "application/json"
        return default


class _Connection:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, str, dict]] = []
        self.closed = False

    def request(self, method: str, path: str, *, body: str, headers: dict) -> None:
        self.calls.append((method, path, body, headers))

    def getresponse(self) -> _Response:
        return next(self.responses)

    def close(self) -> None:
        self.closed = True


class MemoryHealthWireTests(unittest.TestCase):
    def test_mcp_probe_authenticates_then_calls_memory_list_without_caller_or_scope(self) -> None:
        connection = _Connection(
            [
                _Response(200, '{"jsonrpc":"2.0","id":1,"result":{}}', session="session-1"),
                _Response(202, ""),
                _Response(
                    200,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "content": [
                                    {"type": "text", "text": '[{"id": 7, "scope": "product:secretary"}]'}
                                ]
                            },
                        }
                    ),
                ),
            ]
        )
        with mock.patch("secretary.memory.health.http.client.HTTPConnection", return_value=connection):
            rows = health._authenticated_list("launch-bound-token", port=8077, timeout_seconds=5)

        self.assertEqual(rows, [{"id": 7, "scope": "product:secretary"}])
        self.assertTrue(connection.closed)
        self.assertEqual([json.loads(call[2])["method"] for call in connection.calls], [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ])
        call = json.loads(connection.calls[-1][2])
        self.assertEqual(call["params"], {"name": "memory_list", "arguments": {"limit": 1}})
        self.assertEqual(connection.calls[-1][3]["Authorization"], "Bearer launch-bound-token")


class MemoryHealthIdentityTests(unittest.TestCase):
    def test_probe_uses_a_live_steward_grant_and_removes_its_temporary_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            def authenticated(token: str, **_kwargs):
                resolved = access.resolve_token(token, data_dir=data_dir)
                self.assertIsInstance(resolved, access.MemoryReadIdentity)
                assert isinstance(resolved, access.MemoryReadIdentity)
                self.assertEqual(resolved.role, "steward")
                self.assertEqual(resolved.scopes, frozenset({"project:secretary", "product:secretary"}))
                return [{"id": 1, "scope": "product:secretary"}]

            with mock.patch("secretary.memory.health._authenticated_list", side_effect=authenticated):
                health.probe_memory(data_dir, timeout_seconds=1, retry_seconds=0)

            self.assertFalse(list(access.bindings_dir(data_dir).glob("*.json")))
            self.assertFalse(list((access.bindings_dir(data_dir) / "health-probes").glob("*.pid")))

    def test_missing_expected_steward_entry_is_a_visible_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "secretary.memory.health._authenticated_list", return_value=[{"id": 1, "scope": "project:foreign"}]
            ):
                with self.assertRaisesRegex(health.MemoryProbeError, "expected steward-scoped"):
                    health.probe_memory(Path(tmp), timeout_seconds=0.01, retry_seconds=0)


if __name__ == "__main__":
    unittest.main()
