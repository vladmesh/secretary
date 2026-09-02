"""secretary-1458: one implementation decides whether a project's broad-check contract is usable.

The card that pays for this is the one nobody wants to spend: a registered project whose adapter
cannot name a check that attests it. Until now that was discovered by the worker, inside the
workspace, after a head had been brought up — the round was gone before the refusal could be read.
The rules therefore live in `secretary.projects.contract`, and both sides ask them: the worker's
own `secretary check broad --module` and the dispatcher's preflight before a card is given out.

The live constraint on the day this was written: not one adapter in the installation declares
`broad_check`, so every registered project resolves the legacy default. For Secretary itself that
default is a true contract — the checkout being attested holds the very sources the check imports —
and it has to keep working, because the pipeline running this card is made of Secretary cards.
"""

from __future__ import annotations

import hashlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from secretary.projects.contract import (
    ADAPTER_INVALID,
    ADAPTER_UNAVAILABLE,
    BROAD_CHECK_INCOMPLETE,
    CANNOT_ATTEST_PROJECT,
    CONTRACT_FIT,
    CONTRACT_REFUSALS,
    CONTRACT_REFUSED,
    CONTRACT_STATES,
    CONTRACT_UNDECIDABLE,
    INTERPRETER_UNAVAILABLE,
    LEGACY_IMPORT_PACKAGE,
    LEGACY_REASON_MISSING_BROAD_CHECK,
    UNDECIDABLE_QUESTIONS,
    UNDECIDABLE_RELATIVE_INTERPRETER,
    ContractStateError,
    ContractUnusable,
    ContractVerdict,
    ModuleContract,
    contract_of,
    decide,
    module_contract,
)

ADAPTER_BODY = (
    "setup:\n  commands: ['true']\n"
    "smoke:\n  command: 'true'\n"
    "validation:\n  ci: github\n"
    "artifact_policy:\n  write_project_files: false\n"
)


class ProjectContractTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        (self.instance / "adapters").mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def adapter(self, body: str = ADAPTER_BODY, name: str = "example") -> None:
        (self.instance / "adapters" / f"{name}.yaml").write_text(body, encoding="utf-8")

    def package(self, name: str, *, under: str = "src") -> None:
        directory = (self.repo / under / name) if under else (self.repo / name)
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("", encoding="utf-8")

    def resolve(self, binding: dict | None = None):
        return module_contract(
            binding if binding is not None else {"adapter": "example", "repo": str(self.repo)},
            instance=self.instance,
            project_root=self.repo,
        )

    def binding(self) -> dict:
        return {"adapter": "example", "repo": str(self.repo)}

    def ask_preflight(self, project_root: Path | None = None) -> ContractVerdict:
        """The dispatcher's side: the same rules, asked with no candidate workspace."""
        return decide(
            self.binding(),
            instance=self.instance,
            project_root=self.repo if project_root is None else project_root,
            workspace=None,
        )

    def relative_interpreter(self, tree: Path) -> Path:
        """The `.venv/bin/python` an adapter's relative contract means, inside one tree."""
        interpreter = tree / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.symlink_to(sys.executable)
        return interpreter

    def refusal(self, binding: dict | None = None) -> ContractUnusable:
        with self.assertRaises(ContractUnusable) as caught:
            self.resolve(binding)
        return caught.exception

    # --- The project whose contract can attest it goes to work exactly as before ----------------

    def test_the_legacy_default_still_attests_the_secretary_project_itself(self) -> None:
        """AC4, and the reason this card cannot be naive: a regression here stops the pipeline.

        No adapter in the live installation declares `broad_check`, so this is the contract every
        Secretary card in flight — including this one — is dispatched on.
        """
        self.adapter()
        self.package(LEGACY_IMPORT_PACKAGE)

        contract = self.resolve()

        self.assertEqual(contract.interpreter, sys.executable)
        self.assertEqual(contract.import_package, LEGACY_IMPORT_PACKAGE)
        self.assertEqual(
            contract.as_dict(),
            {"source": "legacy_default", "reason": LEGACY_REASON_MISSING_BROAD_CHECK},
        )

    def test_a_flat_layout_holds_the_package_just_as_well_as_src(self) -> None:
        self.adapter()
        self.package(LEGACY_IMPORT_PACKAGE, under="")

        self.assertEqual(self.resolve().import_package, LEGACY_IMPORT_PACKAGE)

    def test_an_adapters_own_contract_is_taken_at_its_word(self) -> None:
        interpreter = self.repo / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n"
            + "  import_package: codegen_orchestrator\n"
            + "  module: project_suite\n"
            + "  args: ['-v']\n"
        )

        contract = self.resolve()

        self.assertEqual(contract.interpreter, str(interpreter))
        self.assertEqual(contract.import_package, "codegen_orchestrator")
        self.assertEqual(contract.module, "project_suite")
        self.assertEqual(contract.args, ("-v",))
        self.assertEqual(contract.as_dict(), {"source": "adapter"})
        self.assertEqual(
            contract.reason,
            "",
            "an adapter that declares a contract is not judged by the checkout's layout: the "
            "receipt's own provenance is what catches a check that imported elsewhere",
        )

    # --- The suite the contract names (issue:8b39e60e4df361c6138e) -------------------------------

    def test_the_declared_module_and_args_are_the_exact_argument_vector(self) -> None:
        """The half the contract could not express before, and the placeholder it left behind.

        Without a place for the project to name its broad suite, the worker task packet printed
        `<this project's broad suite module>` and every document answered it with repository-wide
        discovery: all seven CI suites in one process. The vector stays a list because a rendered
        command line cannot carry one — `--only 'fast lane'` and `--only fast lane` render the same
        and run different checks.
        """
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n"
            + f"  interpreter: {sys.executable}\n"
            + "  import_package: thing\n"
            + "  module: tests.broad\n"
            + "  args: ['--only', 'fast lane']\n"
        )

        contract = self.resolve()

        self.assertEqual(contract.module, "tests.broad")
        self.assertEqual(contract.args, ("--only", "fast lane"))

    def test_a_declared_contract_with_no_interpreter_uses_the_wrappers_own(self) -> None:
        """AC of issue:8b39e60e4df361c6138e: a supported contract must not require a missing venv.

        The Secretary worktrees have no `.venv` and are not getting one. Since PR #329 the check
        subprocess prepends the candidate's own import roots to `sys.path` before importing the
        project, so a shared installation interpreter imports the CANDIDATE, not production —
        which is what makes "no interpreter named" both a legal and an honest contract, rather
        than the silent production-import it would have been before.
        """
        self.adapter(ADAPTER_BODY + "broad_check:\n  import_package: thing\n  module: tests.broad\n")

        contract = self.resolve()

        self.assertEqual(contract.interpreter, sys.executable)
        self.assertEqual(contract.module, "tests.broad")
        self.assertEqual(contract.args, ())
        self.assertEqual(contract.as_dict(), {"source": "adapter"})

    def test_no_interpreter_is_answerable_at_preflight_too(self) -> None:
        """It is `fit`, not `undecidable`: there is no relative path needing a candidate tree."""
        self.adapter(ADAPTER_BODY + "broad_check:\n  import_package: thing\n  module: tests.broad\n")

        verdict = self.ask_preflight()

        self.assertEqual(verdict.state, CONTRACT_FIT)
        self.assertIsNotNone(verdict.contract)
        self.assertEqual(verdict.contract.interpreter, sys.executable)

    def test_a_declared_contract_that_names_no_module_is_incomplete(self) -> None:
        """A declared block is a promise to say which suite; leaving it out is not a quiet fallback.

        Falling back here would put the project straight back to repository-wide discovery under a
        contract that looks declared, which is the exact confusion this key removes.
        """
        self.adapter(
            ADAPTER_BODY + f"broad_check:\n  interpreter: {sys.executable}\n  import_package: thing\n"
        )

        refused = self.refusal()

        self.assertEqual(refused.shape, BROAD_CHECK_INCOMPLETE)
        self.assertEqual(refused.code, "invalid_project_adapter")

    def test_the_legacy_default_names_no_module_at_all(self) -> None:
        """It cannot: it is a fallback for adapters that said nothing, and it says nothing either.

        Removing that fallback is issue:81a0a1e5c15225fa360e, and it has to come after a live
        adapter declares its contract — not with this change.
        """
        self.adapter()
        self.package(LEGACY_IMPORT_PACKAGE)

        contract = self.resolve()

        self.assertEqual(contract.module, "")
        self.assertEqual(contract.args, ())

    # --- And every shape of unusable contract is named, once ------------------------------------

    def test_a_binding_with_no_adapter_names_that_shape(self) -> None:
        refused = self.refusal({"repo": str(self.repo)})

        self.assertEqual(refused.shape, ADAPTER_UNAVAILABLE)
        self.assertEqual(refused.adapter, "")

    def test_an_adapter_that_is_not_there_names_that_shape(self) -> None:
        refused = self.refusal()

        self.assertEqual(refused.shape, ADAPTER_UNAVAILABLE)
        self.assertEqual(refused.adapter, "example")
        self.assertIn("unavailable", refused.message)

    def test_an_adapter_that_fails_its_schema_names_that_shape(self) -> None:
        self.adapter("setup:\n  commands: ['true']\n")

        refused = self.refusal()

        self.assertEqual(refused.shape, ADAPTER_INVALID)
        self.assertIn("example", refused.message)

    def test_a_broad_check_block_that_names_no_runtime_names_that_shape(self) -> None:
        """A contract whose runtime is blank names nothing runnable, however it got past the schema.

        The schema already refuses a missing key or an empty string, and that refusal arrives as
        `adapter_invalid` below; whitespace is what still reaches this rule, and the rule is what
        keeps the shape from depending on how thorough the schema happens to be.
        """
        self.adapter(
            ADAPTER_BODY + "broad_check:\n  interpreter: '   '\n  import_package: thing\n  module: suite\n"
        )

        refused = self.refusal()

        self.assertEqual(refused.shape, BROAD_CHECK_INCOMPLETE)
        self.assertIn("broad-check contract", refused.message)

    def test_a_broad_check_block_the_schema_refuses_is_an_invalid_adapter(self) -> None:
        cases = {
            "empty interpreter": "broad_check:\n  interpreter: ''\n  import_package: thing\n  module: suite\n",
            "not a mapping": "broad_check: [interpreter]\n",
        }
        for name, block in cases.items():
            with self.subTest(case=name):
                self.adapter(ADAPTER_BODY + block)

                self.assertEqual(self.refusal().shape, ADAPTER_INVALID)

    def test_an_interpreter_that_cannot_run_names_that_shape(self) -> None:
        missing = ADAPTER_BODY + (
            "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n  module: suite\n"
        )
        unreadable = self.repo / "not-executable"
        unreadable.write_text("", encoding="utf-8")
        unreadable.chmod(stat.S_IRUSR)
        cases = {
            "no such file": missing,
            "not executable": ADAPTER_BODY
            + (f"broad_check:\n  interpreter: {unreadable}\n  import_package: thing\n  module: suite\n"),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.adapter(body)

                refused = self.refusal()

                self.assertEqual(refused.shape, INTERPRETER_UNAVAILABLE)
                self.assertEqual(
                    refused.code,
                    "interpreter_start_failed",
                    "the worker already reported this condition under that code when it "
                    "discovered it by trying to start the process; the preflight only finds it "
                    "earlier, and never under a second name",
                )

    # --- The third state: named, carried, and the same answer whatever tree is standing there --

    def test_a_relative_interpreter_is_undecidable_without_a_candidate_workspace(self) -> None:
        """AC6's third state, and the defect this card kept re-growing.

        The adapter schema resolves a relative interpreter from the *candidate workspace*, and at
        preflight there is no candidate workspace. Round one answered from the registered checkout
        — a statement about a different directory, whose untracked `.venv` approved contracts the
        fresh worktree then refused. Round two answered with silence, which no caller could see or
        test. The answer now is a state with a name, and it is the same state whether or not the
        registered checkout happens to hold that interpreter: the verdict never came from that
        tree.
        """
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n  module: suite\n"
        )
        interpreter = self.relative_interpreter(self.repo)

        with_a_venv = self.ask_preflight()
        interpreter.unlink()
        without_one = self.ask_preflight()

        for verdict in (with_a_venv, without_one):
            self.assertEqual(verdict.state, CONTRACT_UNDECIDABLE)
            self.assertTrue(verdict.undecidable)
            self.assertFalse(verdict.fit)
            self.assertFalse(verdict.refused)
            self.assertEqual(verdict.question, UNDECIDABLE_RELATIVE_INTERPRETER)
            self.assertEqual(verdict.adapter, "example")
            self.assertIn(".venv/bin/python", verdict.detail)
            self.assertEqual(
                verdict.evidence(),
                {
                    "state": CONTRACT_UNDECIDABLE,
                    "adapter": "example",
                    "detail": verdict.detail,
                    "question": UNDECIDABLE_RELATIVE_INTERPRETER,
                },
                "the state carries what could not be decided and why, for whoever logs it",
            )
        self.assertEqual(
            with_a_venv,
            without_one,
            "the registered checkout's contents never entered the answer",
        )

    def test_the_worker_answers_the_relative_interpreter_in_the_tree_that_runs_it(self) -> None:
        """The other half of the same boundary: the side holding the tree decides, both ways, and
        it never leaves the question open."""
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n  module: suite\n"
        )
        workspace = self.root / "worktree"
        workspace.mkdir()
        self.relative_interpreter(self.repo)

        refused = decide(self.binding(), instance=self.instance, project_root=workspace, workspace=workspace)

        self.assertEqual(refused.state, CONTRACT_REFUSED)
        self.assertEqual(refused.refusal.shape, INTERPRETER_UNAVAILABLE)
        self.assertIn(
            str(workspace),
            refused.refusal.message,
            "the worker's refusal is about the worktree it was given, not the registered checkout",
        )
        with self.assertRaises(ContractUnusable):
            module_contract(self.binding(), instance=self.instance, project_root=workspace)

        in_the_worktree = self.relative_interpreter(workspace)
        resolved = module_contract(self.binding(), instance=self.instance, project_root=workspace)
        self.assertEqual(
            resolved.interpreter,
            str(in_the_worktree),
            "and the contract it resolves runs that tree's own interpreter, not the checkout's",
        )

    def test_a_side_that_holds_a_tree_is_never_left_with_an_open_question(self) -> None:
        """AC5's invariant, stated as a property rather than as one example: given a workspace,
        `decide` answers `fit` or `refused` for every shape a fixture can produce."""
        workspace = self.root / "worktree"
        workspace.mkdir()
        self.package(LEGACY_IMPORT_PACKAGE)
        bodies = {
            "no adapter file": None,
            "invalid adapter": "setup:\n  commands: ['true']\n",
            "legacy default": ADAPTER_BODY,
            "blank runtime": ADAPTER_BODY
            + ("broad_check:\n  interpreter: '   '\n  import_package: thing\n  module: suite\n"),
            "relative interpreter": ADAPTER_BODY
            + ("broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n  module: suite\n"),
            "absolute interpreter": ADAPTER_BODY
            + (f"broad_check:\n  interpreter: {sys.executable}\n  import_package: thing\n  module: suite\n"),
        }
        for name, body in bodies.items():
            with self.subTest(case=name):
                adapter_file = self.instance / "adapters" / "example.yaml"
                if body is None:
                    adapter_file.unlink(missing_ok=True)
                else:
                    self.adapter(body)

                verdict = decide(
                    self.binding(),
                    instance=self.instance,
                    project_root=self.repo,
                    workspace=workspace,
                )

                self.assertIn(verdict.state, (CONTRACT_FIT, CONTRACT_REFUSED))

    # --- What each caller does with a state, and the branch that must not exist ----------------

    def test_the_tree_holding_side_acts_on_the_state_and_never_falls_through(self) -> None:
        """The hole, closed at the shared implementation: no caller turns an unanswered question
        into permission. `contract_of` is exhaustive over the three states, and the two it cannot
        act on raise rather than returning something usable."""
        contract = ModuleContract(sys.executable, "thing")

        self.assertIs(contract_of(ContractVerdict.as_fit(contract, "example")), contract)
        with self.assertRaises(ContractUnusable):
            contract_of(ContractVerdict.as_refused(ADAPTER_INVALID, "example", "why"))
        with self.assertRaises(ContractStateError):
            contract_of(
                ContractVerdict.as_undecidable(UNDECIDABLE_RELATIVE_INTERPRETER, "example", "no workspace")
            )
        with self.assertRaises(ContractStateError):
            contract_of(ContractVerdict(state="who-knows", adapter="example"))

    def test_a_state_cannot_be_built_without_the_thing_that_makes_it_readable(self) -> None:
        """The enumerations are the whole list, on both axes, and a constructor refuses anything
        outside them rather than minting a state nobody can branch on."""
        self.assertEqual(CONTRACT_STATES, (CONTRACT_FIT, CONTRACT_REFUSED, CONTRACT_UNDECIDABLE))
        for shape in CONTRACT_REFUSALS:
            self.assertEqual(ContractVerdict.as_refused(shape, "example", "why").refusal.shape, shape)
        for question in UNDECIDABLE_QUESTIONS:
            self.assertEqual(ContractVerdict.as_undecidable(question, "example", "why").question, question)
        with self.assertRaises(ContractStateError):
            ContractVerdict.as_refused("invented", "example", "why")
        with self.assertRaises(ContractStateError):
            ContractVerdict.as_undecidable("invented", "example", "why")

    def test_the_preflight_does_refuse_an_absolute_interpreter_that_is_not_there(self) -> None:
        """AC2 as the observer narrowed it: `interpreter_unavailable` is the preflight's to report
        for an absolute path, which names the same file whatever tree the check runs in."""
        self.adapter(
            ADAPTER_BODY
            + f"broad_check:\n  interpreter: {self.root / 'nowhere' / 'python'}\n  module: suite\n"
            + "  import_package: thing\n"
        )

        verdict = self.ask_preflight()

        self.assertEqual(verdict.state, CONTRACT_REFUSED)
        self.assertEqual(verdict.refusal.shape, INTERPRETER_UNAVAILABLE)

    def test_every_other_shape_is_the_preflights_to_refuse_too(self) -> None:
        """AC1/AC3: a refusal always wins, and always before the card is issued. Every shape that
        does not need a tree is decided by the dispatcher's side of the same implementation.
        """
        cases = {
            ADAPTER_UNAVAILABLE: None,
            ADAPTER_INVALID: "setup:\n  commands: ['true']\n",
            BROAD_CHECK_INCOMPLETE: ADAPTER_BODY
            + ("broad_check:\n  interpreter: '   '\n  import_package: thing\n  module: suite\n"),
            CANNOT_ATTEST_PROJECT: ADAPTER_BODY,
        }
        self.package("codegen_orchestrator")
        for shape, body in cases.items():
            with self.subTest(shape=shape):
                adapter_file = self.instance / "adapters" / "example.yaml"
                if body is None:
                    adapter_file.unlink(missing_ok=True)
                else:
                    self.adapter(body)

                verdict = self.ask_preflight()

                self.assertEqual(verdict.state, CONTRACT_REFUSED)
                self.assertEqual(verdict.refusal.shape, shape)
                self.assertEqual(verdict.evidence()["shape"], shape)

    def test_a_workspace_independent_refusal_wins_over_an_open_question(self) -> None:
        """Order matters and is fixed here: an adapter that is broken is refused even though its
        contract would also have left the relative-interpreter question open."""
        self.adapter("setup:\n  commands: ['true']\nbroad_check:\n  interpreter: .venv/bin/x\n")

        verdict = self.ask_preflight()

        self.assertEqual(verdict.state, CONTRACT_REFUSED)
        self.assertEqual(verdict.refusal.shape, ADAPTER_INVALID)

    def test_the_legacy_default_over_somebody_elses_checkout_cannot_attest_it(self) -> None:
        """The live shape: 13 adapters, none declaring `broad_check`, one Secretary checkout.

        For every other registered project the same default imports an installed Secretary — a
        check that can neither pass nor fail for a reason about that project.
        """
        self.adapter()
        self.package("codegen_orchestrator")

        refused = self.refusal()

        self.assertEqual(refused.shape, CANNOT_ATTEST_PROJECT)
        self.assertEqual(refused.adapter, "example")
        self.assertIn(LEGACY_IMPORT_PACKAGE, refused.message)
        self.assertIn(str(self.repo), refused.message)

    def test_every_enumerated_shape_has_one_worker_error_code(self) -> None:
        """The enumeration is the whole list, and neither side may grow a sixth of its own."""
        self.assertEqual(len(set(CONTRACT_REFUSALS)), len(CONTRACT_REFUSALS))
        for shape in CONTRACT_REFUSALS:
            with self.subTest(shape=shape):
                self.assertTrue(ContractUnusable(shape, "example", "why").code)
                self.assertIn(shape, ContractUnusable(shape, "example", "why").detail())


class CatalogContractTests(unittest.TestCase):
    """The dispatcher's catalog answers the preflight through that same implementation."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "src" / "secretary").mkdir(parents=True)
        (self.repo / "src" / "secretary" / "__init__.py").write_text("", encoding="utf-8")

    def catalog(self, adapter_body: str | None = ADAPTER_BODY):
        from secretary.dispatcher import InstanceCatalog
        from secretary.head_registry import snapshot_header

        instance = self.root / "instance"
        (instance / "heads").mkdir(parents=True)
        (instance / "projects").mkdir()
        (instance / "adapters").mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: contract\ndata_dir: "
            + str(self.root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            + "host:\n  unit_prefix: secretary-\n",
            encoding="utf-8",
        )
        snapshot = (
            "resources:\n  sub:\n    account: account\n"
            "profiles:\n  head:\n    resource: sub\n    adapter: claude\n"
            "role_defaults:\n  new_card: head\n"
        )
        rendered = snapshot_header(instance / "heads" / "heads.toml") + snapshot
        (instance / "heads" / "heads.yaml").write_text(rendered, encoding="utf-8")
        (instance / "heads" / "source.yaml").write_text(
            "canonical: " + str(instance / "heads" / "heads.toml") + "\n"
            "canonical_owner: instance\nproduct_root: /fixture/product\nrevision: fixture\n"
            "snapshot_sha256: " + hashlib.sha256(rendered.encode("utf-8")).hexdigest() + "\n",
            encoding="utf-8",
        )
        (instance / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.repo}\nadapter: example\nenabled: true\ndefault_branch: main\n",
            encoding="utf-8",
        )
        if adapter_body is not None:
            (instance / "adapters" / "example.yaml").write_text(adapter_body, encoding="utf-8")
        return InstanceCatalog(instance / "instance.yaml")

    def test_a_usable_contract_reaches_the_preflight_as_fit(self) -> None:
        """The live AC4 case, through the dispatcher's own catalog: a Secretary checkout on the
        legacy default. It is a named state with the contract in it, not an absence of refusal."""
        verdict = self.catalog().broad_check_verdict("example")

        self.assertEqual(verdict.state, CONTRACT_FIT)
        self.assertEqual(verdict.contract.import_package, LEGACY_IMPORT_PACKAGE)
        self.assertEqual(verdict.contract.reason, LEGACY_REASON_MISSING_BROAD_CHECK)

    def test_an_unusable_one_reaches_the_preflight_as_the_shared_refusal(self) -> None:
        verdict = self.catalog(adapter_body=None).broad_check_verdict("example")

        self.assertEqual(verdict.state, CONTRACT_REFUSED)
        self.assertEqual(verdict.refusal.shape, ADAPTER_UNAVAILABLE)

    def test_an_open_question_reaches_the_preflight_as_undecidable(self) -> None:
        """The third state through the real catalog, with `workspace=None` supplied there and
        nowhere else: the dispatcher never has a candidate workspace to answer with."""
        verdict = self.catalog(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n  module: suite\n"
        ).broad_check_verdict("example")

        self.assertEqual(verdict.state, CONTRACT_UNDECIDABLE)
        self.assertEqual(verdict.question, UNDECIDABLE_RELATIVE_INTERPRETER)


if __name__ == "__main__":
    unittest.main()
