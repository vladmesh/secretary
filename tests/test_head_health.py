from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.head_health import HeadHealth, HeadReadiness, resolve_head_chain


class Catalog:
    def head_profile(self, head: str):
        return {"resource": head}

    def resource(self, resource: str):
        return {"probe": f"probe {resource}"}


class HeadHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.health = HeadHealth(Catalog(), Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_auth_failure_is_cached_and_blocks_launch(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "Login expired. Please run /login")
        with mock.patch("secretary.head_health.subprocess.run", return_value=failed) as run:
            first = self.health.check("openai-sub")
            second = self.health.check("openai-sub")

        self.assertEqual(first.status, "unauthenticated")
        self.assertFalse(first.launch_allowed)
        self.assertTrue(second.cached)
        run.assert_called_once()

    def test_provider_failure_is_unavailable(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "503 biscuit_baker_service_me_circuit_open")
        with mock.patch("secretary.head_health.subprocess.run", return_value=failed):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.launch_allowed)

    def test_a_spent_subscription_is_exhausted_and_blocks_launch(self) -> None:
        """The live wording, from the 2026-08-06 canary: nothing in it says "rate limit", so this
        used to fall through to `unknown`, which allows a launch. The dispatcher then claimed a
        card and put two heads into a resource that was out until the quota reset."""
        spent = subprocess.CompletedProcess(
            "probe", 1, "",
            "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Aug 8th, 2026 9:13 PM.",
        )
        with mock.patch("secretary.head_health.subprocess.run", return_value=spent):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "exhausted")
        self.assertEqual(result.reason, "resource quota is spent")
        self.assertFalse(result.launch_allowed)

    def test_a_spent_quota_is_not_read_as_a_flaky_provider(self) -> None:
        """`unavailable` and `exhausted` mean different things to an operator, and a body can carry
        both vocabularies: a 429 quota refusal is spent credit, not a provider having a bad minute."""
        both = subprocess.CompletedProcess(
            "probe", 1, "", "429 insufficient_quota: you exceeded your current quota",
        )
        with mock.patch("secretary.head_health.subprocess.run", return_value=both):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "exhausted")
        self.assertFalse(result.launch_allowed)

    def test_probe_failure_is_unknown_and_allows_launch(self) -> None:
        with mock.patch("secretary.head_health.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.launch_allowed)


# The registry the walk below reads: two families, each head naming the other family's counterpart,
# which is the cyclic shape a real canon has once both directions are written down.
CHAINS = {
    "codex": ["claude-opus"],
    "codex-reviewer": ["claude-default"],
    "claude-opus": ["codex"],
    "claude-default": ["codex-reviewer"],
    "lonely": [],
}
RESOURCES = {
    "codex": "openai-sub", "codex-reviewer": "openai-sub",
    "claude-opus": "claude-sub", "claude-default": "claude-sub", "lonely": "claude-sub",
}


def _readiness(dead: dict[str, str]):
    def readiness(head: str) -> HeadReadiness:
        resource = RESOURCES.get(head, "")
        status = dead.get(resource, "ready")
        return HeadReadiness(resource, status, f"{resource} is {status}", 1.0)

    return readiness


def _fallback(head: str):
    return CHAINS.get(head)


class ResolveHeadChainTests(unittest.TestCase):
    """secretary-1165: which head a role is launched on when its resource is not launchable."""

    def resolve(self, preferred: str, dead: dict[str, str]):
        return resolve_head_chain(preferred, _readiness(dead), _fallback)

    def test_a_green_resource_keeps_the_preferred_head(self) -> None:
        choice = self.resolve("codex", {})

        self.assertEqual(choice.head, "codex")
        self.assertFalse(choice.substituted)
        self.assertEqual(choice.rejected, ())

    def test_a_dead_resource_walks_the_chain_to_the_other_family(self) -> None:
        for status in ("unavailable", "exhausted", "unauthenticated"):
            with self.subTest(status=status):
                choice = self.resolve("codex", {"openai-sub": status})

                self.assertEqual(choice.head, "claude-opus")
                self.assertTrue(choice.substituted)
                self.assertIn("falling back to claude-opus", choice.reason)

    def test_two_dead_resources_leave_no_head_and_name_both(self) -> None:
        choice = self.resolve("codex", {"openai-sub": "exhausted", "claude-sub": "unavailable"})

        self.assertEqual(choice.head, "")
        self.assertFalse(choice.resolved)
        self.assertEqual([head for head, _ in choice.rejected], ["codex", "claude-opus"])
        self.assertIn("openai-sub is exhausted", choice.reason)
        self.assertIn("claude-sub is unavailable", choice.reason)

    def test_a_head_with_no_chain_and_a_dead_resource_is_a_skip(self) -> None:
        """No chain is the canon saying "this head or nothing", and the reason stays the resource's
        own: there is no walk to report."""
        choice = self.resolve("lonely", {"claude-sub": "exhausted"})

        self.assertEqual(choice.head, "")
        self.assertEqual(choice.reason, "claude-sub is exhausted")

    def test_a_cyclic_chain_terminates(self) -> None:
        choice = self.resolve("codex", {"openai-sub": "unavailable", "claude-sub": "unavailable"})

        self.assertEqual([head for head, _ in choice.rejected], ["codex", "claude-opus"])

    def test_an_unknown_chain_entry_is_never_launched(self) -> None:
        """`unknown` readiness is launch-allowed, and a head the registry cannot describe answers
        exactly that. Reaching one through a chain must not pin the claim to it."""
        choice = resolve_head_chain(
            "codex",
            _readiness({"openai-sub": "exhausted"}),
            lambda head: ["retired", "claude-opus"] if head == "codex" else _fallback(head),
        )

        self.assertEqual(choice.head, "claude-opus")
        self.assertEqual(
            [(head, readiness.status) for head, readiness in choice.rejected],
            [("codex", "exhausted"), ("retired", "missing")],
        )

    def test_an_unknown_preferred_head_is_left_to_its_own_readiness(self) -> None:
        """The card override and the role default are validated where they are read. A second check
        here would answer that question differently and turn a known failure into a silent skip."""
        choice = resolve_head_chain("mystery", _readiness({}), _fallback)

        self.assertEqual(choice.head, "mystery")

