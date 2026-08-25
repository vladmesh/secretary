from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import head_health
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



# The PATH `packaging/systemd/secretary-dispatcher-production.service` pins for the dispatcher: the
# ordinary system directories and nothing else. The unit starts the dispatcher from its venv but
# never puts that venv on PATH, which is what this test environment reproduces.
UNIT_PATH_DIRECTORIES = (
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
)
SRC = Path(__file__).resolve().parents[1] / "src"
# A probe that reaches no provider and answers exactly the question the real one dies on: can the
# interpreter this command resolves to import the product at all.
IMPORT_PROBE = 'python3 -P -c "import triggered_agents"'


def _interpreter(directory: Path, *, pythonpath: Path | None) -> Path:
    """A `python3` on disk that stands in for one of the two interpreters a probe can resolve to.

    Both run this process's own interpreter, so the test says nothing about the developer's venv;
    what separates them is whether the product is on their import path. `-S` keeps site-packages
    out of both, and `-E` keeps the suite's own PYTHONPATH out of the system one, so "can it import
    triggered_agents" has the same answer on every machine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "python3"
    if pythonpath is None:
        body = f'exec "{sys.executable}" -S -E "$@"\n'
    else:
        body = f'PYTHONPATH="{pythonpath}"\nexport PYTHONPATH\nexec "{sys.executable}" -S "$@"\n'
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


class ImportProbeCatalog:
    def head_profile(self, head: str):
        return {"resource": head}

    def resource(self, resource: str):
        return {"probe": IMPORT_PROBE}


class ProbeInterpreterTests(unittest.TestCase):
    """secretary-1464: the probe has to run under the interpreter the dispatcher itself runs under.

    Reproduced under the unit's environment rather than under the test process's, because the test
    process is exactly the one place where this defect never showed: it inherits a PATH with a venv
    on it, so `python3` found the product and the probe worked. Production did not, and from
    `691673d` (the src/ layout, 2026-08-19) every probe there answered `No module named
    triggered_agents` — an unclassified failure, which was `unknown`, which allowed every claim.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.system = _interpreter(root / "usr" / "bin", pythonpath=None)
        self.venv = _interpreter(root / "venv" / "bin", pythonpath=SRC)
        self.health = HeadHealth(ImportProbeCatalog(), root / "data")
        self.unit_env = {
            "PATH": os.pathsep.join((str(self.system.parent), *UNIT_PATH_DIRECTORIES)),
            "HOME": str(root),
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_a_path_without_the_venv_still_finds_the_product(self) -> None:
        with mock.patch.dict(os.environ, self.unit_env, clear=True), \
                mock.patch.object(head_health.sys, "executable", str(self.venv)):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "ready", result.reason)
        self.assertTrue(result.launch_allowed)

    def test_an_interpreter_that_cannot_import_the_product_is_a_broken_probe(self) -> None:
        """The pre-fix production shape, kept as the negative control: when prepending our own
        interpreter changes nothing because that interpreter is the one without the product, the
        probe is reported as unrunnable rather than as an unknown resource."""
        with mock.patch.dict(os.environ, self.unit_env, clear=True), \
                mock.patch.object(head_health.sys, "executable", str(self.system)):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertIn("No module named", result.reason)
        self.assertFalse(result.launch_allowed)

    def test_the_environment_of_this_process_is_not_modified(self) -> None:
        before = dict(os.environ)
        with mock.patch.object(head_health.sys, "executable", str(self.venv)):
            environment = head_health.probe_env()

        self.assertEqual(dict(os.environ), before)
        self.assertTrue(environment["PATH"].startswith(f"{self.venv.parent}{os.pathsep}"))
        self.assertIn(os.environ.get("PATH", ""), environment["PATH"])


class BrokenProbeStatusTests(unittest.TestCase):
    """secretary-1464: "the probe could not be run" is its own verdict, and it blocks the claim."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.health = HeadHealth(Catalog(), Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def check(self, completed: subprocess.CompletedProcess) -> HeadReadiness:
        """One verdict per call: a fresh store, so the TTL cache never answers for the next case."""
        health = HeadHealth(Catalog(), Path(tempfile.mkdtemp(dir=self.tmpdir.name)))
        with mock.patch("secretary.head_health.subprocess.run", return_value=completed):
            return health.check("openai-sub")

    def test_a_missing_module_is_a_broken_probe(self) -> None:
        result = self.check(subprocess.CompletedProcess(
            "probe", 1, "", "/usr/bin/python3: No module named triggered_agents\n"))

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertEqual(
            result.reason,
            "probe could not be launched: /usr/bin/python3: No module named triggered_agents",
        )
        self.assertFalse(result.launch_allowed)

    def test_a_command_the_shell_cannot_find_is_a_broken_probe(self) -> None:
        result = self.check(subprocess.CompletedProcess(
            "probe", 127, "", "sh: 1: python3: not found\n"))

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertFalse(result.launch_allowed)

    def test_a_probe_that_cannot_be_started_at_all_is_a_broken_probe(self) -> None:
        """No shell to run it with is the same defect as no interpreter to run it under."""
        with mock.patch("secretary.head_health.subprocess.run", side_effect=OSError(8, "Exec format error")):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, head_health.PROBE_BROKEN)
        self.assertFalse(result.launch_allowed)

    def test_a_timeout_is_still_unknown_and_still_allows_a_launch(self) -> None:
        """A probe that started and hung says nothing about the account, and that semantics is
        deliberately unchanged: `unknown` still means "nothing is known" and still lets a claim
        through."""
        with mock.patch("secretary.head_health.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.launch_allowed)

    def test_an_unexplained_provider_refusal_is_still_unknown(self) -> None:
        result = self.check(subprocess.CompletedProcess("probe", 1, "", "the model declined\n"))

        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.launch_allowed)

    def test_a_classified_provider_failure_is_never_read_as_a_broken_probe(self) -> None:
        """The three statuses the classifier already names keep their meaning even when the body
        also carries a word this card reads as a launch failure — only a provider that was actually
        reached can say any of them."""
        for text, expected in (
            ("Please run /login: no such file or directory ~/.codex/auth.json", "unauthenticated"),
            ("You've hit your usage limit; no such file or directory", "exhausted"),
            ("503 service unavailable, no such file or directory", "unavailable"),
        ):
            with self.subTest(expected=expected):
                result = self.check(subprocess.CompletedProcess("probe", 1, "", text))

                self.assertEqual(result.status, expected)
                self.assertFalse(result.launch_allowed)


class BrokenProbeChainTests(unittest.TestCase):
    """secretary-1464: a resource nobody can probe is walked past, not launched into."""

    def test_a_broken_probe_is_not_claimable_and_the_chain_is_walked(self) -> None:
        choice = resolve_head_chain(
            "codex", _readiness({"openai-sub": head_health.PROBE_BROKEN}), _fallback)

        self.assertEqual(choice.head, "claude-opus")
        self.assertTrue(choice.substituted)
        self.assertEqual([head for head, _ in choice.rejected], ["codex"])
        self.assertIn("openai-sub is probe_broken", choice.reason)

    def test_a_broken_probe_everywhere_claims_nothing(self) -> None:
        """The whole point of the new status: with the gate itself broken on every resource the
        card stays in Ready instead of being handed to an unprobed account."""
        choice = resolve_head_chain(
            "codex",
            _readiness({"openai-sub": head_health.PROBE_BROKEN, "claude-sub": head_health.PROBE_BROKEN}),
            _fallback,
        )

        self.assertEqual(choice.head, "")
        self.assertFalse(choice.resolved)
