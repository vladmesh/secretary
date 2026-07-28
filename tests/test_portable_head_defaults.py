"""The product default registry has to bring up a host that pays for one subscription only.

Two installation shapes are checked against the shipped `heads.toml` rather than a synthetic
fixture: Claude-only (openai-sub red) and Codex-only (claude-sub red). Every role in
`[role_defaults]`, and every triggered agent dispatched through it, must resolve to a launchable
profile in both, and no shipped spec may name a profile id the product registry does not know.
"""
from __future__ import annotations

import os
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from triggered_agents.agents.pipeline import health, heads
from triggered_agents.runtime import dispatch

PRODUCT_HEADS = Path(heads.__file__).with_name("heads.toml")
AGENTS_ROOT = Path(dispatch.__file__).resolve().parents[1] / "agents"

CLAUDE_ONLY = {"claude-sub": health.GREEN, "openai-sub": health.RED, "openrouter": health.RED}
CODEX_ONLY = {"claude-sub": health.RED, "openai-sub": health.GREEN, "openrouter": health.RED}


def product_registry() -> heads.Registry:
    return heads.load_registry(PRODUCT_HEADS)


class ProductRegistryRolesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = product_registry()

    def test_every_role_default_is_a_known_profile(self) -> None:
        for role, head in self.registry.role_defaults.items():
            with self.subTest(role=role):
                self.assertIn(head, self.registry.profiles)

    def test_claude_only_installation_resolves_every_role(self) -> None:
        for role, head in self.registry.role_defaults.items():
            with self.subTest(role=role):
                resolved = health.resolve_head(head, CLAUDE_ONLY, self.registry)
                self.assertIsNotNone(resolved, f"role {role} cannot launch on Claude alone")
                self.assertEqual(self.registry.profile(resolved)["resource"], "claude-sub")

    def test_codex_only_installation_resolves_every_role(self) -> None:
        for role, head in self.registry.role_defaults.items():
            with self.subTest(role=role):
                resolved = health.resolve_head(head, CODEX_ONLY, self.registry)
                self.assertIsNotNone(resolved, f"role {role} cannot launch on Codex alone")
                self.assertEqual(self.registry.profile(resolved)["resource"], "openai-sub")

    def test_default_profile_of_a_headless_card_resolves_on_both(self) -> None:
        for statuses in (CLAUDE_ONLY, CODEX_ONLY):
            with self.subTest(statuses=sorted(statuses.items())):
                self.assertIsNotNone(
                    health.resolve_head(heads.DEFAULT_PROFILE, statuses, self.registry)
                )

    def test_paid_per_token_head_is_never_reached_from_a_default(self) -> None:
        """hermes spends real money: an installation opts into it, no product default walks there."""
        for pid, prof in self.registry.profiles.items():
            with self.subTest(profile=pid):
                self.assertNotIn("hermes", prof.get("fallback") or [])


class ShippedAutomationHeadsTests(unittest.TestCase):
    """The shipped specs may not carry a profile id the product registry does not have."""

    def _specs(self) -> list[tuple[str, dict]]:
        out = []
        for path in sorted(AGENTS_ROOT.glob("*/automation.toml")):
            out.append((path.parent.name, tomllib.loads(path.read_text(encoding="utf-8"))))
        return out

    def test_no_shipped_spec_pins_an_unknown_profile(self) -> None:
        known = product_registry().profiles
        for agent, spec in self._specs():
            with self.subTest(agent=agent):
                head = spec.get("head")
                if head is not None:
                    self.assertIn(head, known)

    def test_llm_agents_are_routed_by_the_registry(self) -> None:
        registry = product_registry()
        for agent, spec in self._specs():
            if spec.get("dispatcher") or not spec.get("skill"):
                continue
            with self.subTest(agent=agent):
                head = dispatch._preferred_head_from(spec, agent, registry)
                self.assertIsNotNone(head, f"{agent} has no head route")
                for statuses in (CLAUDE_ONLY, CODEX_ONLY):
                    self.assertIsNotNone(health.resolve_head(head, statuses, registry))


class PreferredHeadTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {heads.HEADS_TOML_ENV: str(PRODUCT_HEADS)})
        env.start()
        self.addCleanup(env.stop)

    def test_role_default_is_used_when_the_spec_pins_nothing(self) -> None:
        expected = product_registry().role_default("curator")
        with mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/curate"}):
            self.assertEqual(dispatch._preferred_head("curator"), expected)

    def test_a_known_pin_wins_over_the_role_default(self) -> None:
        spec = {"skill": "/curate", "head": "claude-opus"}
        with mock.patch.object(dispatch, "_load_spec", return_value=spec):
            self.assertEqual(dispatch._preferred_head("curator"), "claude-opus")

    def test_a_pin_this_installation_does_not_have_falls_back_to_the_role_route(self) -> None:
        """A stale id from another installation must not silently degrade to a bare `claude`."""
        spec = {"skill": "/curate", "head": "codex-curator"}
        expected = product_registry().role_default("curator")
        with mock.patch.object(dispatch, "_load_spec", return_value=spec):
            self.assertEqual(dispatch._preferred_head("curator"), expected)

    def test_an_unrouted_agent_has_no_head(self) -> None:
        with mock.patch.object(dispatch, "_load_spec", return_value={"skill": "/nothing"}):
            self.assertIsNone(dispatch._preferred_head("no-such-role"))


class LaunchCommandHeadTests(unittest.TestCase):
    """`_launch_cmd` must render the role's head, resolved against this tick's resource health."""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {heads.HEADS_TOML_ENV: str(PRODUCT_HEADS)})
        env.start()
        self.addCleanup(env.stop)

    def _launch(self, agent: str, statuses: dict[str, str]) -> tuple[str, str | None]:
        with mock.patch.object(health, "refresh", return_value=statuses):
            _, command, profile = dispatch._launch_cmd(agent)
        return command, profile

    def test_claude_only_host_launches_the_curator_on_claude(self) -> None:
        command, profile = self._launch("curator", CLAUDE_ONLY)
        self.assertEqual(product_registry().profile(profile)["resource"], "claude-sub")
        self.assertIn("claude --dangerously-skip-permissions", command)
        self.assertIn("/curate", command)

    def test_codex_only_host_launches_the_curator_on_codex(self) -> None:
        command, profile = self._launch("curator", CODEX_ONLY)
        self.assertEqual(product_registry().profile(profile)["resource"], "openai-sub")
        self.assertIn("codex exec", command)
        self.assertIn("/curate", command)

    def test_codex_only_host_launches_the_steward_on_codex(self) -> None:
        command, profile = self._launch("steward", CODEX_ONLY)
        self.assertEqual(product_registry().profile(profile)["resource"], "openai-sub")
        self.assertIn("codex exec", command)

    def test_a_broken_registry_still_dispatches_the_agent(self) -> None:
        with mock.patch.object(dispatch, "_preferred_head", side_effect=heads.HeadRegistryError("boom")):
            _, command, profile = dispatch._launch_cmd("retro")
        self.assertIsNone(profile)
        self.assertIn("claude --dangerously-skip-permissions", command)
        self.assertIn("/retro", command)


if __name__ == "__main__":
    unittest.main()
