from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import session
from secretary.runtime import heads as head_registry


def _write_env(dir_path: Path, body: str) -> Path:
    path = dir_path / "runtime.env"
    path.write_text(body, encoding="utf-8")
    return path


class OperatorEnvTest(unittest.TestCase):
    def test_full_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = _write_env(
                Path(tmp),
                "EXAMPLE_ADMIN_PASSWORD=hunter2\nGITHUB_TOKEN=gh-test-token\n",
            )
            env = session.operator_env(env_file, base_env={"PATH": "/bin", "SECRETARY_INSTANCE": tmp})
        self.assertEqual(env["EXAMPLE_ADMIN_PASSWORD"], "hunter2")
        self.assertEqual(env["GITHUB_TOKEN"], "gh-test-token")
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["SECRETARY_ROLE"], "operator")

    def test_launches_with_no_transport_file(self):
        """The operator session needs no transport file: the board is reached through its store."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = _write_env(Path(tmp), "SOMETHING=else\n")
            env = session.operator_env(env_file, base_env={"SECRETARY_INSTANCE": tmp})
        self.assertEqual(env["SOMETHING"], "else")
        self.assertEqual(env["SECRETARY_ROLE"], "operator")


# The product ships a small neutral registry; an OpenRouter-backed hermes head is one
# installation's account policy, so the adapter is exercised against a fixture registry rather
# than whichever profiles the shipped default happens to carry.
HERMES_REGISTRY = head_registry.Registry(
    {"openrouter": {"account": "pooled"}},
    {
        "hermes": {
            "resource": "openrouter",
            "adapter": "hermes",
            "model": "openai/gpt-5.5",
            "provider": "openrouter",
        }
    },
)


class ShippedRegistryTestCase(unittest.TestCase):
    """Reads the shipped registry, whatever installation the ambient environment selects."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {head_registry.REGISTRY_ENV: str(head_registry.HEADS_TOML)})
        patcher.start()
        self.addCleanup(patcher.stop)


class ResolveHeadTest(ShippedRegistryTestCase):
    def test_a_bare_adapter_is_an_adapter_choice(self):
        """`claude`/`codex`/`hermes` pick that adapter's head from the registry, not a profile id."""
        shipped = head_registry.load_registry()
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter):
                resolved = session.resolve_profile_id(adapter)
                self.assertNotEqual(resolved, adapter)
                self.assertEqual(shipped.profile(resolved)["adapter"], adapter)
        self.assertEqual(session.resolve_profile_id("hermes", registry=HERMES_REGISTRY), "hermes")

    def test_a_bare_adapter_prefers_a_role_default_on_it(self):
        registry = head_registry.Registry(
            {"acct": {"account": "acct"}},
            {
                "claude-aaa-low": {"resource": "acct", "adapter": "claude"},
                "claude-opus-high": {"resource": "acct", "adapter": "claude"},
                "claude-opus-local-pty": {"resource": "acct", "adapter": "claude", "runtime": "local-pty"},
                "codex-sol-medium": {"resource": "acct", "adapter": "codex"},
            },
            {"new_card": "codex-sol-medium", "observer": "claude-opus-high"},
        )

        self.assertEqual(session.resolve_profile_id("claude", registry=registry), "claude-opus-high")
        self.assertEqual(session.resolve_profile_id("codex", registry=registry), "codex-sol-medium")
        with self.assertRaisesRegex(head_registry.HeadRegistryError, "no hermes head"):
            session.resolve_profile_id("hermes", registry=registry)

    def test_a_keyless_profile_is_a_pty_held_variant(self):
        """secretary-1718: naming no runtime is `local-pty`, so it yields to a pane profile."""
        registry = head_registry.Registry(
            {"acct": {"account": "acct"}},
            {
                "claude-keyless": {"resource": "acct", "adapter": "claude"},
                "claude-pane": {"resource": "acct", "adapter": "claude", "runtime": "orca-legacy"},
            },
            {"new_card": "claude-keyless", "observer": "claude-pane"},
        )

        self.assertEqual(session.resolve_profile_id("claude", registry=registry), "claude-pane")

    def test_the_default_is_the_registry_new_card_head(self):
        shipped = head_registry.load_registry()
        self.assertEqual(session.resolve_profile_id(None), shipped.role_defaults["new_card"])

    def test_no_new_card_default_is_refused_by_that_key(self):
        registry = head_registry.Registry(
            {"acct": {"account": "acct"}}, {"claude-opus-high": {"resource": "acct", "adapter": "claude"}}
        )
        with self.assertRaisesRegex(head_registry.HeadRegistryError, r"role_defaults\.new_card"):
            session.resolve_profile_id(None, registry=registry)

    def test_profile_passthrough_and_unknown(self):
        self.assertEqual(session.resolve_profile_id("claude-opus-high"), "claude-opus-high")
        for unknown in ("bogus", "claude-default", "codex-high"):
            with (
                self.subTest(head=unknown),
                self.assertRaisesRegex(head_registry.HeadRegistryError, repr(unknown)),
            ):
                session.resolve_profile_id(unknown)


class RenderInteractiveTest(ShippedRegistryTestCase):
    def test_no_seeded_prompt_per_adapter(self):
        cases = {
            "claude": "claude --dangerously-skip-permissions",
            "claude-opus-high": "--model opus --effort high",
            "claude-opus-medium": "--model opus --effort medium",
            "codex": "codex --dangerously-bypass-approvals-and-sandbox",
        }
        for head, needle in cases.items():
            profile_id = session.resolve_profile_id(head)
            command = session.render_interactive(profile_id, workspace="/tmp/ws")
            self.assertIn(needle, command, head)
            self.assertNotIn("codex exec", command, head)

    def test_hermes_is_repl_not_seeded(self):
        command = session.render_interactive("hermes", workspace="/tmp/ws", registry=HERMES_REGISTRY)
        self.assertIn("--cli", command)
        self.assertIn("--yolo", command)
        self.assertNotIn(" -z ", command)


if __name__ == "__main__":
    unittest.main()
