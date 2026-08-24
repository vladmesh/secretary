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
    CONTRACT_REFUSALS,
    INTERPRETER_UNAVAILABLE,
    LEGACY_IMPORT_PACKAGE,
    LEGACY_REASON_MISSING_BROAD_CHECK,
    ContractUnusable,
    module_contract,
    preflight,
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

    def ask_preflight(self, project_root: Path | None = None) -> None:
        """The dispatcher's question, asked of the shared rules directly."""
        preflight(
            self.binding(),
            instance=self.instance,
            project_root=self.repo if project_root is None else project_root,
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
        )

        contract = self.resolve()

        self.assertEqual(contract.interpreter, str(interpreter))
        self.assertEqual(contract.import_package, "codegen_orchestrator")
        self.assertEqual(contract.as_dict(), {"source": "adapter"})
        self.assertEqual(
            contract.reason, "",
            "an adapter that declares a contract is not judged by the checkout's layout: the "
            "receipt's own provenance is what catches a check that imported elsewhere",
        )

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
        self.adapter(ADAPTER_BODY + "broad_check:\n  interpreter: '   '\n  import_package: thing\n")

        refused = self.refusal()

        self.assertEqual(refused.shape, BROAD_CHECK_INCOMPLETE)
        self.assertIn("broad-check contract", refused.message)

    def test_a_broad_check_block_the_schema_refuses_is_an_invalid_adapter(self) -> None:
        cases = {
            "empty interpreter": "broad_check:\n  interpreter: ''\n  import_package: thing\n",
            "not a mapping": "broad_check: [interpreter]\n",
        }
        for name, block in cases.items():
            with self.subTest(case=name):
                self.adapter(ADAPTER_BODY + block)

                self.assertEqual(self.refusal().shape, ADAPTER_INVALID)

    def test_an_interpreter_that_cannot_run_names_that_shape(self) -> None:
        missing = ADAPTER_BODY + (
            "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n"
        )
        unreadable = self.repo / "not-executable"
        unreadable.write_text("", encoding="utf-8")
        unreadable.chmod(stat.S_IRUSR)
        cases = {
            "no such file": missing,
            "not executable": ADAPTER_BODY + (
                f"broad_check:\n  interpreter: {unreadable}\n  import_package: thing\n"
            ),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.adapter(body)

                refused = self.refusal()

                self.assertEqual(refused.shape, INTERPRETER_UNAVAILABLE)
                self.assertEqual(
                    refused.code, "interpreter_start_failed",
                    "the worker already reported this condition under that code when it "
                    "discovered it by trying to start the process; the preflight only finds it "
                    "earlier, and never under a second name",
                )

    def test_the_preflight_never_answers_for_a_relative_interpreter_it_cannot_see(self) -> None:
        """The boundary this card had to draw: a workspace-dependent question is not the
        dispatcher's to answer.

        The adapter schema resolves a relative interpreter from the *candidate workspace*, and at
        preflight there is no candidate workspace. Answering from the registered checkout would be
        a statement about a different directory — and it is the false approval that statement
        buys (the checkout has an untracked `.venv`, the fresh worktree does not) that let a card
        through to a worker who then refused it. So the preflight gives the same answer either
        way: nothing.
        """
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n"
        )
        interpreter = self.relative_interpreter(self.repo)

        self.ask_preflight()  # no false refusal: the contract is usable as far as anyone can tell

        interpreter.unlink()
        self.ask_preflight()  # and no false approval either: the answer never came from this tree

    def test_the_worker_answers_the_relative_interpreter_in_the_tree_that_runs_it(self) -> None:
        """The other half of the same boundary: the side holding the tree decides, both ways."""
        self.adapter(
            ADAPTER_BODY
            + "broad_check:\n  interpreter: .venv/bin/python\n  import_package: thing\n"
        )
        workspace = self.root / "worktree"
        workspace.mkdir()
        self.relative_interpreter(self.repo)

        with self.assertRaises(ContractUnusable) as caught:
            module_contract(self.binding(), instance=self.instance, project_root=workspace)

        self.assertEqual(caught.exception.shape, INTERPRETER_UNAVAILABLE)
        self.assertIn(
            str(workspace), caught.exception.message,
            "the worker's refusal is about the worktree it was given, not the registered checkout",
        )
        in_the_worktree = self.relative_interpreter(workspace)
        resolved = module_contract(
            self.binding(), instance=self.instance, project_root=workspace
        )
        self.assertEqual(
            resolved.interpreter, str(in_the_worktree),
            "and the contract it resolves runs that tree's own interpreter, not the checkout's",
        )

    def test_the_preflight_does_refuse_an_absolute_interpreter_that_is_not_there(self) -> None:
        """AC2 as the observer narrowed it: `interpreter_unavailable` is the preflight's to report
        for an absolute path, which names the same file whatever tree the check runs in."""
        self.adapter(
            ADAPTER_BODY
            + f"broad_check:\n  interpreter: {self.root / 'nowhere' / 'python'}\n"
            + "  import_package: thing\n"
        )

        with self.assertRaises(ContractUnusable) as caught:
            self.ask_preflight()

        self.assertEqual(caught.exception.shape, INTERPRETER_UNAVAILABLE)

    def test_every_other_shape_is_the_preflights_to_refuse_too(self) -> None:
        """AC5: one implementation of the rules. What differs is only the workspace-dependent
        question above, so every shape that does not need a tree stops the card at the dispatcher.
        """
        cases = {
            ADAPTER_UNAVAILABLE: None,
            ADAPTER_INVALID: "setup:\n  commands: ['true']\n",
            BROAD_CHECK_INCOMPLETE: ADAPTER_BODY + (
                "broad_check:\n  interpreter: '   '\n  import_package: thing\n"
            ),
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

                with self.assertRaises(ContractUnusable) as caught:
                    self.ask_preflight()

                self.assertEqual(caught.exception.shape, shape)

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
            "version: 1\nname: contract\ndata_dir: " + str(self.root / "data")
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
            f"id: example\nrepo: {self.repo}\nadapter: example\nenabled: true\n"
            "default_branch: main\n",
            encoding="utf-8",
        )
        if adapter_body is not None:
            (instance / "adapters" / "example.yaml").write_text(adapter_body, encoding="utf-8")
        return InstanceCatalog(instance / "instance.yaml")

    def test_a_usable_contract_passes_the_preflight_in_silence(self) -> None:
        """The live AC4 case, through the dispatcher's own catalog: a Secretary checkout on the
        legacy default. The preflight answers only whether the card may go out."""
        self.assertIsNone(self.catalog().broad_check_preflight("example"))

    def test_an_unusable_one_reaches_the_preflight_as_the_shared_refusal(self) -> None:
        with self.assertRaises(ContractUnusable) as caught:
            self.catalog(adapter_body=None).broad_check_preflight("example")

        self.assertEqual(caught.exception.shape, ADAPTER_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
