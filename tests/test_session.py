from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secretary import session
from triggered_agents.agents.pipeline import heads as head_registry


def _write_env(dir_path: Path, body: str) -> Path:
    path = dir_path / "runtime.env"
    path.write_text(body, encoding="utf-8")
    return path


class OperatorEnvTest(unittest.TestCase):
    def test_full_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = _write_env(
                Path(tmp),
                "KANBOARD_URL=http://board/jsonrpc.php\n"
                "KANBOARD_API_USER=svc\n"
                "KANBOARD_API_TOKEN=tok\n"
                "KANBOARD_ADMIN_PASSWORD=hunter2\n"
                "GITHUB_TOKEN=gh-test-token\n",
            )
            env = session.operator_env(env_file, base_env={"PATH": "/bin"})
        # Board creds plus every other secret from the file — operator is unscoped on purpose.
        self.assertEqual(env["KANBOARD_URL"], "http://board/jsonrpc.php")
        self.assertEqual(env["KANBOARD_ADMIN_PASSWORD"], "hunter2")
        self.assertEqual(env["GITHUB_TOKEN"], "gh-test-token")
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["SECRETARY_ROLE"], "operator")

    def test_fails_closed_without_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = _write_env(Path(tmp), "SOMETHING=else\n")
            with self.assertRaises(session.SessionError) as ctx:
                session.operator_env(env_file, base_env={})
        self.assertIn("KANBOARD_URL", str(ctx.exception))


# The product ships a small neutral registry; an OpenRouter-backed hermes head is one
# installation's account policy, so the adapter is exercised against a fixture registry rather
# than whichever profiles the shipped default happens to carry.
HERMES_REGISTRY = head_registry.Registry(
    {"openrouter": {"account": "pooled"}},
    {"hermes": {"resource": "openrouter", "adapter": "hermes",
                "model": "openai/gpt-5.5", "provider": "openrouter"}},
)


class ResolveHeadTest(unittest.TestCase):
    def test_adapter_aliases(self):
        self.assertEqual(session.resolve_profile_id("claude"), "claude-default")
        self.assertEqual(session.resolve_profile_id("codex"), "codex")
        self.assertEqual(
            session.resolve_profile_id("hermes", registry=HERMES_REGISTRY), "hermes"
        )
        self.assertEqual(session.resolve_profile_id(None), session.DEFAULT_HEAD)

    def test_profile_passthrough_and_unknown(self):
        self.assertEqual(session.resolve_profile_id("claude-opus"), "claude-opus")
        with self.assertRaises(head_registry.HeadRegistryError):
            session.resolve_profile_id("bogus")


class RenderInteractiveTest(unittest.TestCase):
    def test_no_seeded_prompt_per_adapter(self):
        cases = {
            "claude": "claude --dangerously-skip-permissions",
            "claude-opus": "--model opus",
            "claude-opus-medium": "--model opus --effort medium",
            "codex": "codex --dangerously-bypass-approvals-and-sandbox",
        }
        for head, needle in cases.items():
            profile_id = session.resolve_profile_id(head)
            command = session.render_interactive(profile_id, workspace="/tmp/ws")
            self.assertIn(needle, command, head)
            self.assertNotIn("codex exec", command, head)

    def test_hermes_is_repl_not_seeded(self):
        command = session.render_interactive(
            "hermes", workspace="/tmp/ws", registry=HERMES_REGISTRY
        )
        self.assertIn("--cli", command)
        self.assertIn("--yolo", command)
        self.assertNotIn(" -z ", command)


if __name__ == "__main__":
    unittest.main()
