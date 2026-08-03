"""Board-comment scrub contract (`runtime.redact.scrub_secrets`).

It used to live in the pipeline dispatcher's worker.py with no coverage at all; the dispatcher is
gone (secretary-1135) and its two surviving callers — ops.add_comment for steward bodies and the
steward's own precheck error path — now read it from the runtime. These pin what has to keep
being masked and, just as important, what must not be: a git sha in a CI-failure comment.
"""
from __future__ import annotations

import unittest

from triggered_agents.runtime.redact import REDACTED, scrub_secrets


class ScrubSecretsTests(unittest.TestCase):
    def test_empty_text_passes_through(self):
        self.assertEqual(scrub_secrets(""), "")

    def test_a_secret_looking_assignment_loses_its_value(self):
        out = scrub_secrets("export GH_TOKEN=ghs_notarealvalue")

        self.assertEqual(out, f"export GH_TOKEN={REDACTED}")

    def test_a_known_key_shape_is_masked_by_the_pattern_layer(self):
        out = scrub_secrets("key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX rest")

        self.assertNotIn("sk-ant-api03", out)
        self.assertIn("rest", out)

    def test_a_long_token_shaped_blob_is_masked(self):
        out = scrub_secrets("payload " + "A" * 44)

        self.assertEqual(out, f"payload {REDACTED}:blob")

    def test_a_git_sha_survives(self):
        """A commit reference in a CI-failure comment is the useful part of it, not a leak."""
        sha = "4baca94f8f7605cd18cd9495b838092ba41aaaa1"

        self.assertEqual(scrub_secrets(f"failed at {sha}"), f"failed at {sha}")
        self.assertEqual(scrub_secrets("failed at 4baca94"), "failed at 4baca94")

    def test_a_filesystem_path_survives(self):
        path = "/home/dev/orca/workspaces/secretary/secretary-1135-drop-pipeline/tests"

        self.assertEqual(scrub_secrets(f"cwd {path}"), f"cwd {path}")


if __name__ == "__main__":
    unittest.main()
