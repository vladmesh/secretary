"""secretary-1411: a head is a `HeadSpec`, and its adapter is never guessed.

Two things are pinned here. First, that the loader really does read *every* profile a registry has
— not the worker/reviewer pair the pipeline thinks about most, but the observer heads, the
mechanical curator/steward/retro roles and the third-party adapter a live installation actually
runs. The fixture next door is a copy of one, so a profile shape that only exists on a real host
cannot pass the product default and fail in production.

Second, that the ways a profile can fail to be a head all stop the load by name: no adapter, an
adapter nothing renders, an effort the adapter does not know. An absent effort is not one of them —
most of the claude family pins none, and "default" is what that means.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from secretary.dispatcher import CommandHostRuntime, HostError
from triggered_agents.agents.pipeline.heads import HEADS_TOML, Registry, load_registry
from triggered_agents.runtime.head import HeadSpec, HeadSpecError, head_spec, load_head_specs


INSTALLED_SNAPSHOT = Path(__file__).parent / "fixtures" / "heads" / "installed-heads.yaml"


class ProductRegistryTests(unittest.TestCase):
    """The registry a checkout with no installation behind it falls back to."""

    def test_every_shipped_profile_becomes_a_spec(self) -> None:
        registry = load_registry(HEADS_TOML)
        specs = load_head_specs(registry)
        self.assertEqual(sorted(specs), sorted(registry.profiles))
        for pid, spec in specs.items():
            self.assertTrue(spec.adapter, f"{pid} loaded with no adapter")

    def test_shipped_role_defaults_all_resolve_to_specs(self) -> None:
        registry = load_registry(HEADS_TOML)
        for role in registry.role_defaults:
            head = registry.role_default(role)
            self.assertIsInstance(head_spec(str(head), registry), HeadSpec)


class InstalledSnapshotTests(unittest.TestCase):
    """A real installation's snapshot, profile shapes and all."""

    def setUp(self) -> None:
        self.registry = load_registry(INSTALLED_SNAPSHOT)
        self.specs = load_head_specs(self.registry)

    def test_every_installed_profile_becomes_a_spec(self) -> None:
        self.assertEqual(sorted(self.specs), sorted(self.registry.profiles))
        for pid, spec in self.specs.items():
            self.assertEqual(spec.profile_id, pid)
            self.assertTrue(spec.adapter, f"{pid} loaded with no adapter")

    def test_the_whole_registry_is_covered_not_just_worker_heads(self) -> None:
        """The roles a loader could plausibly have been written for only the pipeline's sake."""
        for pid in (
            "codex-observer",
            "claude-observer",
            "claude-observer-medium",
            "codex-curator",
            "codex-steward",
            "codex-retro",
            "codex-tui",
            "codex-high-tui",
            "codex-extra-tui",
            "codex-reviewer-tui",
            "claude-default",
            "hermes",
        ):
            self.assertIn(pid, self.specs, f"{pid} is missing from the loaded registry")

    def test_adapters_models_and_efforts_come_through(self) -> None:
        codex = self.specs["codex-extra-tui"]
        self.assertEqual(codex.adapter, "codex")
        self.assertEqual(codex.effort, "extra")
        self.assertEqual(codex.codex_mode, "tui")
        self.assertTrue(codex.prompt_after_start)
        hermes = self.specs["hermes"]
        self.assertEqual(hermes.adapter, "hermes")
        self.assertEqual(hermes.model, "openai/gpt-5.5")
        self.assertFalse(hermes.prompt_after_start)
        opus = self.specs["claude-opus-high"]
        self.assertEqual((opus.adapter, opus.model, opus.effort), ("claude", "opus", "high"))
        self.assertFalse(opus.prompt_after_start)

    def test_a_profile_pinning_no_effort_is_default_not_an_error(self) -> None:
        bare = self.specs["claude-default"]
        self.assertEqual(bare.effort, "default")
        self.assertIsNone(bare.model)

    def test_a_spec_cannot_be_edited_under_the_operation_holding_it(self) -> None:
        with self.assertRaises(Exception):
            self.specs["codex"].adapter = "claude"  # type: ignore[misc]

    def test_a_legacy_codex_id_resolves_to_the_profile_serving_it(self) -> None:
        self.assertEqual(head_spec("codex-mini", self.registry).profile_id, "codex-mini")


def _registry(profile: dict) -> Registry:
    return Registry(
        resources={"acct": {"account": "acct"}},
        profiles={"broken": profile},
        role_defaults={},
    )


class RejectedProfileTests(unittest.TestCase):
    """Every shape that is not a head, and the message that says which profile it was."""

    def _refused(self, profile: dict) -> str:
        with self.assertRaises(HeadSpecError) as caught:
            load_head_specs(_registry(profile))
        message = str(caught.exception)
        self.assertIn("broken", message)
        return message

    def test_a_profile_without_an_adapter_is_not_a_head(self) -> None:
        self._refused({"resource": "acct", "model": "opus"})

    def test_an_unknown_adapter_is_refused(self) -> None:
        self.assertIn("unknown adapter", self._refused({"resource": "acct", "adapter": "gemini"}))

    def test_an_unknown_claude_effort_is_refused(self) -> None:
        self.assertIn(
            "effort",
            self._refused({"resource": "acct", "adapter": "claude", "effort": "ultra"}),
        )

    def test_an_unknown_codex_effort_is_refused(self) -> None:
        self.assertIn(
            "effort",
            self._refused({"resource": "acct", "adapter": "codex", "effort": "turbo"}),
        )

    def test_a_retired_codex_launch_mode_is_refused(self) -> None:
        self._refused({"resource": "acct", "adapter": "codex", "codex_mode": "exec"})

    def test_a_profile_that_is_not_a_table_is_refused(self) -> None:
        with self.assertRaises(HeadSpecError) as caught:
            HeadSpec.from_profile("broken", ["codex"])
        self.assertIn("broken", str(caught.exception))

    def test_a_single_profile_is_not_judged_by_a_table_it_does_not_have(self) -> None:
        """Resource binding and fallback chains are registry-scope, not one head's launch shape."""
        spec = HeadSpec.from_profile("lone", {"adapter": "claude", "fallback": ["nowhere"]})
        self.assertEqual(spec.adapter, "claude")
        self.assertIsNone(spec.resource)
        self.assertEqual(spec.fallback, ("nowhere",))


class _Catalog:
    def __init__(self, profiles: dict) -> None:
        self.profiles = profiles

    def head_profile(self, head: str) -> dict:
        try:
            return self.profiles[head]
        except KeyError:
            raise HostError(f"unknown head {head!r}") from None


class PromptAdapterTests(unittest.TestCase):
    """`_prompt_adapter` resolves or fails; it no longer answers `codex` on a guess."""

    def _host(self, profiles: dict) -> CommandHostRuntime:
        return CommandHostRuntime(_Catalog(profiles), Path("/nonexistent"), mode="noop")  # type: ignore[arg-type]

    def test_the_run_snapshot_wins(self) -> None:
        host = self._host({"claude-opus": {"adapter": "claude", "resource": "claude-sub"}})
        self.assertEqual(host._prompt_adapter({"adapter": "Codex"}, "claude-opus"), "codex")

    def test_a_record_without_a_snapshot_resolves_the_profile(self) -> None:
        host = self._host({"claude-opus": {"adapter": "claude", "resource": "claude-sub"}})
        self.assertEqual(host._prompt_adapter({}, "claude-opus"), "claude")
        self.assertEqual(host._prompt_adapter(None, "claude-opus"), "claude")

    def test_an_unknown_head_fails_closed_by_name(self) -> None:
        host = self._host({})
        with self.assertRaises(HostError) as caught:
            host._prompt_adapter({}, "retired-head")
        self.assertIn("retired-head", str(caught.exception))

    def test_a_profile_without_an_adapter_fails_closed_by_name(self) -> None:
        host = self._host({"half-written": {"resource": "claude-sub"}})
        with self.assertRaises(HostError) as caught:
            host._prompt_adapter({}, "half-written")
        self.assertIn("half-written", str(caught.exception))

    def test_a_catalog_that_cannot_answer_at_all_fails_closed(self) -> None:
        host = CommandHostRuntime(object(), Path("/nonexistent"), mode="noop")  # type: ignore[arg-type]
        with self.assertRaises(HostError) as caught:
            host._prompt_adapter({}, "some-head")
        self.assertIn("some-head", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
