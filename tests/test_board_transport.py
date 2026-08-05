from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board_transport import BoardTransportError, ensure, ensure_from_runtime_values, resolve, transport_path
from secretary.runtime_env import RuntimeEnvError, read_runtime_env
from secretary.tasks import KanboardClient, TaskError
from triggered_agents.runtime import kanboard


class BoardTransportTests(unittest.TestCase):
    @staticmethod
    def migrate(instance: Path, runtime: Path | None = None, **kwargs):
        runtime = runtime or instance / "runtime.env"
        values = read_runtime_env(instance, str(runtime), require_ignored=False) if runtime.exists() else {}
        return ensure_from_runtime_values(instance, legacy_values=values, runtime_env=runtime, **kwargs)

    def test_default_is_deterministic_and_matches_client_basic_auth(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = ensure(Path(first), allow_default=True).transport
            two = ensure(Path(second), allow_default=True).transport
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
            transport = ensure(instance, allow_default=True).transport
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
            outcome = self.migrate(instance, runtime)
            transport = outcome.transport
            self.assertEqual(outcome.render(), "imported legacy transport; retired legacy runtime values")
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

            with self.assertRaisesRegex(RuntimeEnvError, "whitespace-padded legacy"):
                self.migrate(instance, runtime)

    def test_external_runtime_override_is_the_file_that_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            instance.mkdir()
            external = Path(tmp) / "operator.env"
            external.write_text(
                "OTHER=value\nKANBOARD_URL=http://legacy/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8",
            )
            external.chmod(0o600)
            values = read_runtime_env(instance, str(external), require_ignored=False)
            outcome = ensure_from_runtime_values(
                instance, legacy_values=values, runtime_env=external,
            )
            self.assertEqual(outcome.render(), "imported legacy transport; retired legacy runtime values")
            self.assertEqual(external.read_text(encoding="utf-8"), "OTHER=value\n")
            self.assertFalse((instance / "runtime.env").exists())

    def test_dry_run_truthfully_reports_retiring_a_matching_legacy_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            runtime = instance / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8",
            )
            runtime.chmod(0o600)
            self.migrate(instance, runtime)
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8",
            )
            runtime.chmod(0o600)

            preview = self.migrate(instance, runtime, dry_run=True)
            applied = self.migrate(instance, runtime)

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
                self.migrate(instance, runtime)

    def test_duplicate_legacy_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime.env"
            runtime.write_text(
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_URL=http://other/jsonrpc.php\n"
                "KANBOARD_API_USER=jsonrpc\nKANBOARD_API_TOKEN=legacy-token\n", encoding="utf-8"
            )
            runtime.chmod(0o600)
            with self.assertRaisesRegex(RuntimeEnvError, "ambiguous"):
                self.migrate(Path(tmp), runtime)

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
                self.migrate(instance)

    def test_fresh_bootstrap_may_explicitly_create_the_default_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outcome = self.migrate(Path(tmp), allow_default=True)
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
                    self.assertEqual(KanboardClient(instance_dir=instance).url, "http://127.0.0.1:8080/jsonrpc.php")
            finally:
                os.chdir(previous)

    def test_reader_rejects_insecure_or_linked_transport_and_lifecycle_repairs_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            transport = ensure(instance, allow_default=True).transport
            path = instance / "board-transport.env"
            path.chmod(0o644)
            with self.assertRaisesRegex(BoardTransportError, "permissions are too broad"):
                resolve(instance)
            repaired = ensure(instance, legacy_values=transport.as_environ())
            self.assertEqual((repaired.transport, repaired.render()), (transport, "secured transport mode"))
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

    def test_fresh_transport_reports_the_ignore_change_in_preview_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            for args in (("init", "--quiet"), ("config", "user.name", "Test"),
                         ("config", "user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(instance), *args], check=True)
            preview = ensure(instance, allow_default=True, dry_run=True)
            applied = ensure(instance, allow_default=True)
        self.assertEqual(
            (preview.render(dry_run=True), applied.render()),
            ("would create default transport; would add transport ignore",
             "created default transport; added transport ignore"),
        )

    def test_insecure_transport_without_a_matching_legacy_tuple_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            path = instance / "board-transport.env"
            path.write_text(
                "KANBOARD_URL=http://attacker.invalid/jsonrpc.php\nKANBOARD_API_USER=attacker\n"
                "KANBOARD_API_TOKEN=attacker-token\n", encoding="utf-8",
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(BoardTransportError, "contents are unconfirmed"):
                ensure(instance)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_confirmed_repair_reports_every_simultaneous_lifecycle_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            for args in (("init", "--quiet"), ("config", "user.name", "Test"),
                         ("config", "user.email", "test@example.invalid")):
                subprocess.run(["git", "-C", str(instance), *args], check=True)
            runtime = instance / "runtime.env"
            body = (
                "KANBOARD_URL=http://legacy/jsonrpc.php\nKANBOARD_API_USER=jsonrpc\n"
                "KANBOARD_API_TOKEN=legacy-token\n"
            )
            runtime.write_text(body, encoding="utf-8")
            runtime.chmod(0o600)
            transport = instance / "board-transport.env"
            transport.write_text(body, encoding="utf-8")
            transport.chmod(0o644)
            outcome = self.migrate(instance, runtime)
        self.assertEqual(
            outcome.render(),
            "secured transport mode; added transport ignore; retired legacy runtime values",
        )


if __name__ == "__main__":
    unittest.main()
