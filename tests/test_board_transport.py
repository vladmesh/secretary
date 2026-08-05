from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board_transport import BoardTransportError, ensure, ensure_from_runtime_file, resolve, transport_path
from secretary.tasks import KanboardClient, TaskError
from triggered_agents.runtime import kanboard


class BoardTransportTests(unittest.TestCase):
    def test_default_is_deterministic_and_matches_client_basic_auth(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one, _ = ensure(Path(first), allow_default=True)
            two, _ = ensure(Path(second), allow_default=True)
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
            transport, _ = ensure(instance, allow_default=True)
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
            self.assertEqual(status, "imported legacy transport; retired legacy runtime values")
            self.assertEqual(transport.token, "legacy-token")
            self.assertEqual(runtime.read_text(encoding="utf-8"), "OTHER=value\n")
            self.assertEqual(resolve(instance), transport)
            self.assertEqual((instance / "board-transport.env").stat().st_mode & 0o777, 0o600)

    def test_padded_runtime_assignment_is_refused_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                " KANBOARD_URL=http://legacy/jsonrpc.php\n"
                " KANBOARD_API_USER=jsonrpc\n KANBOARD_API_TOKEN=legacy-token\n",
                encoding="utf-8",
            )
            runtime.chmod(0o600)

            with self.assertRaisesRegex(BoardTransportError, "padded with whitespace"):
                ensure_from_runtime_file(instance, runtime)

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

            preview = ensure_from_runtime_file(instance, runtime, dry_run=True)
            applied = ensure_from_runtime_file(instance, runtime)

        self.assertEqual(
            (preview.render(dry_run=True), applied.render()),
            ("would retire legacy runtime values", "retired legacy runtime values"),
        )

    def test_conflicting_legacy_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ensure(instance, allow_default=True)
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

    def test_existing_instance_without_transport_or_complete_legacy_tuple_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with self.assertRaisesRegex(BoardTransportError, "refuse to guess or rotate"):
                ensure_from_runtime_file(instance)

    def test_fresh_bootstrap_may_explicitly_create_the_default_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = ensure_from_runtime_file(Path(tmp), allow_default=True)
        self.assertEqual(outcome.render(), "created default transport")
        self.assertEqual(outcome.transport.token, "secretary-local-kanboard-jsonrpc-v1")

    def test_instance_yaml_spelling_and_empty_selection_never_use_the_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            instance.mkdir()
            ensure(instance, allow_default=True)
            self.assertEqual(transport_path(instance / "instance.yaml"), instance / "board-transport.env")
            cwd = root / "workspace"
            cwd.mkdir()
            (cwd / "board-transport.env").write_text(
                "KANBOARD_URL=http://attacker.invalid/jsonrpc.php\nKANBOARD_API_USER=evil\n"
                "KANBOARD_API_TOKEN=evil-token\n",
                encoding="utf-8",
            )
            (cwd / "board-transport.env").chmod(0o600)
            previous = Path.cwd()
            try:
                os.chdir(cwd)
                with (
                    mock.patch("triggered_agents.runtime.board_transport.default_instance_path", return_value=instance),
                    mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": ""}, clear=True),
                ):
                    self.assertEqual(KanboardClient().url, "http://127.0.0.1:8080/jsonrpc.php")
            finally:
                os.chdir(previous)

    def test_reader_rejects_insecure_or_linked_transport_and_lifecycle_repairs_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            transport, _ = ensure(instance, allow_default=True)
            path = instance / "board-transport.env"
            path.chmod(0o644)
            with self.assertRaisesRegex(BoardTransportError, "permissions are too broad"):
                resolve(instance)
            repaired, status = ensure(instance)
            self.assertEqual((repaired, status), (transport, "secured transport mode"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            linked = instance.parent / "linked.env"
            linked.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            linked.chmod(0o600)
            path.unlink()
            path.symlink_to(linked)
            with self.assertRaisesRegex(BoardTransportError, "regular file"):
                resolve(instance)

    def test_dry_run_reports_a_planned_durable_ignore_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ensure(instance, allow_default=True)
            for args in (("init", "--quiet"), ("config", "user.name", "Test"),
                         ("config", "user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(instance), *args], check=True)
            preview = ensure(instance, dry_run=True)
            applied = ensure(instance)
        self.assertEqual(
            (preview.render(dry_run=True), applied.render()),
            ("would add transport ignore", "added transport ignore"),
        )


if __name__ == "__main__":
    unittest.main()
