from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from triggered_agents.runtime.agent_prompt_transport import prepare_agent_prompt
from triggered_agents.runtime.prompt_document import (
    NUDGE_MAX_BYTES,
    PromptDocumentError,
    nudge_for,
    write_prompt_document,
)


# What a card description can carry into a prompt and what the composer failures were made of: an
# escape, a bracketed-paste terminator, and the CRLF the board's own web form submits.
HOSTILE_PROMPT = (
    "# Review secretary-1409\r\n"
    "\x1b[201~ terminator pasted into the description\r\n"
    "\x1b]0;retitle the pane\x07 and an OSC for good measure\r\n"
    "\x1b[200~ opener too\r\n"
    "tail\r\n"
)


class NudgeTests(unittest.TestCase):
    """The line that goes into the pane, and the only thing it is allowed to be."""

    def test_the_nudge_is_one_bounded_line_naming_the_document(self) -> None:
        nudge = nudge_for("/var/lib/secretary/artifacts/prompts/secretary-1409/reviewer-0.md")

        self.assertLessEqual(len(nudge.encode("utf-8")), NUDGE_MAX_BYTES)
        self.assertEqual(nudge.splitlines(), [nudge], "a nudge is one line")
        self.assertIn("/var/lib/secretary/artifacts/prompts/secretary-1409/reviewer-0.md", nudge)

    def test_a_hostile_prompt_cannot_reach_the_pane_through_its_nudge(self) -> None:
        """The point of the whole seam: the document holds the content, the pane holds a path.

        The nudge is built from the path alone, so ESC, a bracket terminator and CRLF in the prompt
        are bytes in a file the head opens rather than framing in a terminal write.
        """
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "prompts" / "reviewer-0.md"
            write_prompt_document(document, HOSTILE_PROMPT)

            nudge = nudge_for(document)

            self.assertNotIn("\x1b", nudge)
            self.assertNotIn("\r", nudge)
            self.assertEqual(nudge.splitlines(), [nudge])
            self.assertLessEqual(len(nudge.encode("utf-8")), NUDGE_MAX_BYTES)
            # And the transport takes it as it stands: a prompt this hostile is exactly what the
            # body policy rejects, so a delivery that still carried the text would have no way out.
            self.assertEqual(prepare_agent_prompt(nudge, adapter="codex").text, nudge)
            self.assertEqual(document.read_bytes(), HOSTILE_PROMPT.encode("utf-8"))

    def test_a_relative_path_is_refused(self) -> None:
        """The sender does not know the head's working directory: a reviewer sits in the candidate
        worktree and an observer in its own."""
        with self.assertRaisesRegex(PromptDocumentError, "absolute"):
            nudge_for("prompts/reviewer-0.md")

    def test_a_path_that_would_not_fit_is_refused_rather_than_truncated(self) -> None:
        with self.assertRaisesRegex(PromptDocumentError, "ceiling"):
            nudge_for("/" + "d" * NUDGE_MAX_BYTES + "/reviewer-0.md")

    def test_control_bytes_and_non_ascii_in_a_path_are_refused(self) -> None:
        for path in ("/tmp/two\nlines.md", "/tmp/esc\x1b.md", "/tmp/ревьюер.md"):
            with self.subTest(path=path), self.assertRaises(PromptDocumentError):
                nudge_for(path)


class PromptDocumentTests(unittest.TestCase):
    """Where the document is allowed to live, and what state it is left in."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def test_the_document_is_private_and_its_directory_with_it(self) -> None:
        document = write_prompt_document(self.root / "prompts" / "card" / "reviewer-0.md", "body")

        self.assertEqual(stat.S_IMODE(document.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(document.parent.stat().st_mode), 0o700)
        self.assertEqual(document.read_text(encoding="utf-8"), "body")

    def test_a_document_inside_the_worktree_it_describes_is_refused(self) -> None:
        """Receipts hash a checkout's tracked diff and its untracked files to say which code they
        are evidence for. A prompt written in there moves that identity for no candidate reason."""
        worktree = self.root / "ws"
        worktree.mkdir()

        with self.assertRaisesRegex(PromptDocumentError, "inside the worktree"):
            write_prompt_document(worktree / "REVIEW.md", "body", outside=worktree)

        self.assertFalse((worktree / "REVIEW.md").exists(), "nothing is written before the refusal")

    def test_a_worktree_reached_through_a_symlink_is_still_the_same_worktree(self) -> None:
        worktree = self.root / "ws"
        worktree.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(worktree)

        with self.assertRaises(PromptDocumentError):
            write_prompt_document(alias / "nested" / "REVIEW.md", "body", outside=worktree)

    def test_a_document_beside_the_worktree_is_accepted(self) -> None:
        worktree = self.root / "ws"
        worktree.mkdir()

        document = write_prompt_document(
            self.root / "prompts" / "reviewer-0.md", "body", outside=worktree
        )

        self.assertTrue(document.is_file())

    def test_a_retry_with_the_same_text_leaves_the_file_alone(self) -> None:
        """The reviewer re-renders the same prompt on most retries, and the mtime is what a reader
        has to tell "asked again" from "asked once" with."""
        document = write_prompt_document(self.root / "prompts" / "reviewer-0.md", "body")
        os.utime(document, (1_000_000, 1_000_000))

        write_prompt_document(document, "body")

        self.assertEqual(int(document.stat().st_mtime), 1_000_000)

    def test_a_retry_with_changed_text_replaces_the_document_in_place(self) -> None:
        document = write_prompt_document(self.root / "prompts" / "reviewer-0.md", "first")

        write_prompt_document(document, "second")

        self.assertEqual(document.read_text(encoding="utf-8"), "second")
        self.assertEqual(stat.S_IMODE(document.stat().st_mode), 0o600)
        self.assertEqual(
            sorted(path.name for path in document.parent.iterdir()),
            ["reviewer-0.md"],
            "the replacement leaves no staging file behind",
        )

    def test_the_document_survives_the_git_status_of_the_checkout_it_describes(self) -> None:
        """The invariant stated as the gate reads it: writing the document dirties nothing."""
        worktree = self.root / "repo"
        worktree.mkdir()
        for args in (
            ["git", "-C", str(worktree), "init", "-q"],
            ["git", "-C", str(worktree), "config", "user.email", "t@example.com"],
            ["git", "-C", str(worktree), "config", "user.name", "t"],
            ["git", "-C", str(worktree), "commit", "-q", "--allow-empty", "-m", "base"],
        ):
            subprocess.run(args, check=True, capture_output=True)

        write_prompt_document(self.root / "prompts" / "reviewer-0.md", "body", outside=worktree)

        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(status.stdout, "")


if __name__ == "__main__":
    unittest.main()
