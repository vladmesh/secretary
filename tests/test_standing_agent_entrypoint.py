"""Focused contract tests for Secretary's not-yet-wired standing-agent root."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board.steward_reports import StewardReportBoard
from secretary.config import DataDirError
from secretary.dispatch import standing_agent
from secretary.tasks import TaskError
from triggered_agents import __main__ as triggered_main
from triggered_agents.agents.steward import cli as steward_cli
from triggered_agents.runtime.state import PRECHECK_BOARD_UNREACHABLE, AgentState


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
    def test_signal_commands_get_the_canonical_reader(self) -> None:
        reader = object()
        with (
            mock.patch.object(standing_agent, "_signal_board", return_value=reader) as board,
            mock.patch.object(steward_cli, "main", return_value=17) as main,
        ):
            self.assertEqual(standing_agent.main(["steward", "scan", "--json"]), 17)

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
            mock.patch.object(standing_agent.KanboardClient, "for_instance", return_value=client) as factory,
            mock.patch.object(standing_agent, "TaskWriter", Writer),
            mock.patch.object(standing_agent.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(standing_agent.main(["steward", "dispatch", "deep-sweep"]), 0)
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
        with mock.patch.object(
            standing_agent.KanboardClient, "for_instance", side_effect=AssertionError("client")
        ):
            board = standing_agent._report_board()
        self.assertIsInstance(board, StewardReportBoard)

    def test_cleanup_only_never_constructs_board_or_reads_instance(self) -> None:
        with (
            mock.patch.object(standing_agent, "_instance_path", side_effect=AssertionError("instance")),
            mock.patch.object(standing_agent, "_report_board", side_effect=AssertionError("report board")),
            mock.patch.object(standing_agent.dispatch, "run", return_value=0) as run,
        ):
            self.assertEqual(standing_agent.main(["steward", "dispatch", "--cleanup-only"]), 0)

        run.assert_called_once_with("steward", None, cleanup_only=True)

    def test_finalizer_paths_delegate_without_canonical_construction(self) -> None:
        with (
            mock.patch.object(standing_agent, "_instance_path", side_effect=AssertionError("instance")),
            mock.patch.object(standing_agent, "_report_board", side_effect=AssertionError("report board")),
            mock.patch.object(triggered_main, "main", return_value=9) as main,
        ):
            self.assertEqual(standing_agent.main(["steward", "dispatch", "--finalize"]), 9)
            self.assertEqual(standing_agent.main(["steward", "dispatch", "--spawn-finalizer"]), 9)

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
            self.assertEqual(standing_agent.main(argv), 4)
        main.assert_called_once_with(argv)

    def test_report_config_errors_remain_failures_when_a_report_is_needed(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"SECRETARY_INSTANCE": "/instance", "SECRETARY_DATA_DIR": ""}, clear=False
            ),
            mock.patch.object(standing_agent.KanboardClient, "for_instance", return_value=object()),
            mock.patch.object(standing_agent, "instance_data_dir", side_effect=DataDirError("bad instance")),
            self.assertRaisesRegex(DataDirError, "bad instance"),
        ):
            _ = standing_agent._report_board().reader

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
                mock.patch.object(
                    standing_agent.KanboardClient,
                    "for_instance",
                    side_effect=TaskError("backend_unavailable", "transport unavailable", 1),
                ) as factory,
            ):
                self.assertEqual(standing_agent.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

            factory.assert_called_once_with(mock.ANY)
            self.assertIn(
                '"result": "board-unreachable"', (state.dir / "runs.jsonl").read_text(encoding="utf-8")
            )

    def test_advance_without_pending_does_not_construct_a_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            with (
                mock.patch.object(steward_cli, "STATE", state),
                mock.patch.object(
                    standing_agent.KanboardClient, "for_instance", side_effect=AssertionError("client")
                ),
            ):
                self.assertEqual(standing_agent.main(["steward", "advance"]), 1)

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
                mock.patch.object(
                    standing_agent.KanboardClient,
                    "for_instance",
                    side_effect=TaskError("backend_error", "bad board response", 1),
                ),
            ):
                self.assertEqual(standing_agent.main(["steward", "precheck"]), 2)

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
                mock.patch.object(standing_agent.KanboardClient, "for_instance", return_value=object()),
                mock.patch.object(standing_agent, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(standing_agent.main(["steward", "precheck"]), PRECHECK_BOARD_UNREACHABLE)

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
                mock.patch.object(standing_agent.KanboardClient, "for_instance", return_value=object()),
                mock.patch.object(standing_agent, "TaskReader", return_value=Reader()),
            ):
                self.assertEqual(standing_agent.main(["steward", "precheck"]), 2)


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
