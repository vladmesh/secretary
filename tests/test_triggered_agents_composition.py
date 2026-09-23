"""Focused contract tests for the one entry of the background agents, `-m triggered_agents`."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board.steward_reports import StewardReportBoard
from secretary.config import DataDirError
from secretary.runtime.state import PRECHECK_BOARD_UNREACHABLE, AgentState, BoardUnavailable
from secretary.tasks import TaskError
from triggered_agents import __main__ as triggered_main
from triggered_agents import composition
from triggered_agents.agents.retro import cli as retro_cli
from triggered_agents.agents.steward import cli as steward_cli


class StewardCliReaderTests(unittest.TestCase):
    class Reader:
        def active_cards(self, *, states=None, project=None):
            return []

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = AgentState("steward", state_dir=Path(self.tmp.name) / "state")
        self.reader = self.Reader()

    def test_scan_precheck_and_advance_pass_an_explicit_reader(self) -> None:
        batch = {"pending": {"notified_blocked": []}, "signals": {"new_blocked": []}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(steward_cli, "STATE", self.state))
            scan = stack.enter_context(mock.patch.object(steward_cli.signals, "scan", return_value=batch))
            stack.enter_context(mock.patch.object(steward_cli.signals, "ensure_pipeline_baseline"))
            stack.enter_context(mock.patch.object(steward_cli.signals, "has_signal", return_value=False))
            self.assertEqual(steward_cli.cmd_scan(True, self.reader), 0)
            scan.assert_called_once_with(self.reader)
            scan.reset_mock()
            self.assertEqual(steward_cli.cmd_precheck(self.reader), 100)
            scan.assert_called_once_with(self.reader)

            self.state.ensure_dir()
            self.state.pending_file.write_text(json.dumps({"notified_blocked": []}), encoding="utf-8")
            self.assertEqual(steward_cli.cmd_advance(self.reader), 0)


class StandingAgentEntrypointTests(unittest.TestCase):
    def test_retired_board_cli_modules_are_not_importable(self) -> None:
        for name in (
            "triggered_agents.agents.pipeline.cli",
            "triggered_agents.agents.pipeline.model",
            "triggered_agents.agents.pipeline.ops",
        ):
            with self.subTest(name=name):
                self.assertIsNone(importlib.util.find_spec(name))

    def test_pipeline_is_not_registered_as_a_triggered_agent_command(self) -> None:
        self.assertEqual(triggered_main.main(["pipeline", "list"]), 2)

    def test_signal_commands_get_the_canonical_reader(self) -> None:
        reader = object()
        with (
            mock.patch.object(composition, "_signal_board", return_value=reader) as board,
            mock.patch.object(steward_cli, "main", return_value=17) as main,
        ):
            self.assertEqual(composition.main(["steward", "scan", "--json"]), 17)

        board.assert_called_once_with()
        main.assert_called_once_with(["scan", "--json"], reader=reader)

    def test_normal_dispatch_uses_one_client_for_canonical_report_adapter(self) -> None:
        client = object()
        writer_calls: list[tuple[object, Path, object]] = []

        class Writer:
            def __init__(self, actual_client, *, data_dir):
                writer_calls.append((actual_client, Path(data_dir), self))

        with (
            mock.patch.dict(
                os.environ,
                {"SECRETARY_INSTANCE": "/instance", "SECRETARY_DATA_DIR": "/audit"},
                clear=False,
            ),
            mock.patch.object(composition, "card_client", return_value=client) as factory,
            mock.patch.object(composition, "TaskWriter", Writer),
            mock.patch.object(composition.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(composition.main(["steward", "dispatch", "deep-sweep"]), 0)
            _, kwargs = run.call_args
            board = kwargs["report_board"]
            self.assertIsInstance(board, StewardReportBoard)
            self.assertIs(board.reader.client, client)
            self.assertIs(board.writer, writer_calls[0][2])

        factory.assert_called_once_with(Path("/instance"))
        self.assertEqual(
            [(actual_client, data_dir) for actual_client, data_dir, _ in writer_calls],
            [(client, Path("/audit"))],
        )

    def test_report_board_is_lazy_until_runtime_needs_a_report(self) -> None:
        with mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
        ):
            board = composition._report_board()
        self.assertIsInstance(board, StewardReportBoard)

    def test_cleanup_only_never_constructs_board_or_reads_instance(self) -> None:
        with (
            mock.patch.object(composition, "_instance_path", side_effect=AssertionError("instance")),
            mock.patch.object(composition, "_report_board", side_effect=AssertionError("report board")),
            mock.patch.object(composition.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(composition.main(["steward", "dispatch", "--cleanup-only"]), 0)

        run.assert_called_once_with("steward", None, cleanup_only=True)

    def test_finalizer_paths_delegate_without_canonical_construction(self) -> None:
        with (
            mock.patch.object(composition, "_instance_path", side_effect=AssertionError("instance")),
            mock.patch.object(composition, "_report_board", side_effect=AssertionError("report board")),
            mock.patch.object(triggered_main, "main", return_value=9) as main,
        ):
            self.assertEqual(composition.main(["steward", "dispatch", "--finalize"]), 9)
            self.assertEqual(composition.main(["steward", "dispatch", "--spawn-finalizer"]), 9)

        self.assertEqual(
            main.call_args_list,
            [
                mock.call(["steward", "dispatch", "--finalize"]),
                mock.call(["steward", "dispatch", "--spawn-finalizer"]),
            ],
        )

    def test_non_steward_is_delegated_without_reinterpretation(self) -> None:
        argv = ["retro", "dispatch", "--generation", "bad"]
        with mock.patch.object(triggered_main, "main", return_value=4) as main:
            self.assertEqual(composition.main(argv), 4)
        main.assert_called_once_with(argv)

    def test_retro_cleanup_port_is_lazy_and_only_passed_to_cleanup_commands(self) -> None:
        with (
            mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
            ),
            mock.patch.object(retro_cli, "main", return_value=8) as main,
        ):
            self.assertEqual(composition.main(["retro", "harvest", "--json"]), 8)
        board = main.call_args.kwargs["retention"]
        self.assertIsNotNone(board)

        with mock.patch.object(triggered_main, "main", return_value=9) as legacy:
            self.assertEqual(composition.main(["retro", "status"]), 9)
        legacy.assert_called_once_with(["retro", "status"])

    def test_retro_precheck_maps_canonical_backend_unavailable_to_101(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("retro", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(retro_cli, "STATE", state),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_unavailable", "transport unavailable", 1),
                ),
            ):
                self.assertEqual(composition.main(["retro", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

    def test_retro_late_canonical_error_is_mapped_by_the_lazy_port(self) -> None:
        class Reader:
            def done_retention_candidates(self):
                raise TaskError("backend_unavailable", "late RPC", 1)

        class Writer:
            pass

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": tmp}, clear=False),
            mock.patch.object(composition, "card_client", return_value=object()),
            mock.patch.object(composition, "TaskReader", return_value=Reader()),
            mock.patch.object(composition, "TaskWriter", return_value=Writer()),
            self.assertRaises(BoardUnavailable),
        ):
            port = composition._done_retention_board()
            port.close_old_done()

    def test_report_config_errors_remain_failures_when_a_report_is_needed(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"SECRETARY_INSTANCE": "/instance", "SECRETARY_DATA_DIR": ""}, clear=False
            ),
            mock.patch.object(composition, "card_client", return_value=object()),
            mock.patch.object(composition, "instance_data_dir", side_effect=DataDirError("bad instance")),
            self.assertRaisesRegex(DataDirError, "bad instance"),
        ):
            _ = composition._report_board().reader

    def test_precheck_classifies_canonical_backend_unavailable_as_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_unavailable", "transport unavailable", 1),
                ) as factory,
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

            factory.assert_called_once_with(mock.ANY)
            self.assertIn(
                '"result": "board-unreachable"', (state.dir / "runs.jsonl").read_text(encoding="utf-8")
            )

    def test_advance_without_pending_does_not_construct_a_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(composition, "card_client", side_effect=AssertionError("client")
                ),
            ):
                self.assertEqual(composition.main(["steward", "advance"]), 1)

    def test_precheck_keeps_other_canonical_task_errors_as_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client",
                    side_effect=TaskError("backend_error", "bad board response", 1),
                ),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), 2)

    def test_precheck_maps_reader_backend_unavailable_to_deferred(self) -> None:
        class Reader:
            def steward_signal_cards(self, *, states, project):
                raise TaskError("backend_unavailable", "board unavailable", 1)

        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client", return_value=object()),
                mock.patch.object(composition, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

    def test_precheck_keeps_reader_backend_error_as_failure(self) -> None:
        class Reader:
            def steward_signal_cards(self, *, states, project):
                raise TaskError("backend_error", "bad response", 1)

        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    steward_cli.signals,
                    "scan",
                    side_effect=lambda reader: reader.active_cards(),
                ),
                mock.patch.object(composition, "card_client", return_value=object()),
                mock.patch.object(composition, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(composition.main(["steward", "precheck"]), 2)


class ModuleEntryTests(unittest.TestCase):
    """`python3 -P -m triggered_agents` is the one entry, and it always goes through the wiring."""

    def test_module_entry_injects_board_ports_for_every_board_role(self) -> None:
        # An Orca automation's precheck string names the module, not a composition root. Before the
        # entry was the root, that path reached the helpers without a port and failed as a wiring
        # error; now it classifies an absent board exactly as the systemd gate path does.
        for agent in ("steward", "retro"):
            with self.subTest(agent), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(root / "home"),
                    "TA_STATE": str(root / "state"),
                    "SECRETARY_INSTANCE": str(root / "instance"),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                }
                result = subprocess.run(
                    [sys.executable, "-P", "-m", "triggered_agents", agent, "precheck"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                self.assertNotIn("must be supplied by the composition root", result.stderr)
                self.assertEqual(result.returncode, PRECHECK_BOARD_UNREACHABLE, result.stderr)


class DispatchArgumentParityTests(unittest.TestCase):
    def test_legacy_parser_keeps_variant_and_generation_quirks(self) -> None:
        parsed = triggered_main.parse_dispatch_arguments(["--generation", "not-a-number", "deep-sweep"])
        self.assertTrue(parsed.cleanup_only is False)
        self.assertIsNone(parsed.generation)
        # The legacy selector picks the first non-flag, including this malformed value.
        self.assertEqual(parsed.variant, "not-a-number")

        parsed = triggered_main.parse_dispatch_arguments(["--generation", "2"])
        self.assertEqual(parsed.generation, 2)
        self.assertEqual(parsed.variant, "2")


if __name__ == "__main__":
    unittest.main()
