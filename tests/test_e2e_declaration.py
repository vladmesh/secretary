"""The e2e check an adapter declares (`validation.e2e`), read once and refused typed (secretary-1795).

Unit-level: `parse_e2e` is the one reading, `InstanceCatalog.adapter` fails a read that carries a
malformed declaration, and the adapter schema accepts exactly what the reading accepts. The last class
is the one new create right the stage needs: the dispatcher cuts a wait card (and, since secretary-1796,
the decision a spent e2e budget needs), nothing else, and
only it names a `card:<ref>` return address. The stage that acts on a declaration is in
`tests/test_e2e_stage.py`.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from secretary.board.wait_card import WaitSpecError, parse_returns
from secretary.config import validate
from secretary.dispatch.e2e import (
    DEFAULT_DEADLINE,
    AdapterE2eDeclarationError,
    E2eDeclaration,
    parse_e2e,
)
from secretary.dispatch.host import InstanceCatalog
from secretary.dispatch.types import HostError
from secretary.tasks import TaskError, TaskWriter

#: A schema-valid adapter around the `validation` block under test.
VALID_ADAPTER = {
    "setup": {"commands": ["npm ci"]},
    "smoke": {"command": "node --check index.js"},
    "validation": {"ci": "github"},
    "artifact_policy": {"write_project_files": False},
}


def github(e2e: object) -> dict[str, object]:
    return {"ci": "github", "required_checks": ["test"], "e2e": e2e}


class DeclarationTests(unittest.TestCase):
    def test_a_full_declaration(self) -> None:
        declaration = parse_e2e(
            github(
                {
                    "workflow": "e2e.yml",
                    "inputs": {"suite": "mega", "stands": 2, "fast": False},
                    "deadline": "90m",
                    "candidate_input": "sha",
                    "dispatch_id_input": "secretary_dispatch_id",
                }
            ),
            adapter="codegen",
        )
        self.assertEqual(
            declaration,
            E2eDeclaration(
                "e2e.yml",
                (("suite", "mega"), ("stands", "2"), ("fast", "false")),
                "90m",
                "sha",
                "secretary_dispatch_id",
            ),
        )
        assert declaration is not None
        self.assertEqual(
            declaration.dispatch_inputs("d-1", "a" * 40),
            {
                "suite": "mega",
                "stands": "2",
                "fast": "false",
                "sha": "a" * 40,
                "secretary_dispatch_id": "d-1",
            },
        )

    def test_without_a_dispatch_id_input_only_the_declared_inputs_are_sent(self) -> None:
        """The Codegen mega's shape: its workflow takes its own inputs and nothing the dispatcher adds."""
        declaration = parse_e2e(
            github({"workflow": "stand-e2e.yml", "inputs": {"suite": "mega", "qa": True}})
        )
        assert declaration is not None
        self.assertEqual(declaration.dispatch_inputs("d-1", "a" * 40), {"suite": "mega", "qa": "true"})
        # A name the dispatcher reserved before is an ordinary input when no dispatch id input is declared.
        plain = parse_e2e(github({"workflow": "e2e.yml", "inputs": {"secretary_dispatch_id": "x"}}))
        assert plain is not None
        self.assertEqual(plain.dispatch_inputs("d-1", "a" * 40), {"secretary_dispatch_id": "x"})

    def test_the_minimal_declaration_takes_the_defaults(self) -> None:
        declaration = parse_e2e(github({"workflow": 4242}))
        self.assertEqual(declaration, E2eDeclaration("4242", (), DEFAULT_DEADLINE, "", ""))
        assert declaration is not None
        self.assertEqual(DEFAULT_DEADLINE, "6h")
        self.assertEqual(declaration.dispatch_inputs("d-1", "a" * 40), {})
        self.assertEqual(parse_e2e(github({"workflow": "e2e.yaml", "inputs": None})).workflow, "e2e.yaml")

    def test_placement_defaults_to_before_merge_and_takes_after_merge(self) -> None:
        """secretary-1807: `after_merge` is the one other placement; anything else is refused typed."""
        default = parse_e2e(github({"workflow": "e2e.yml"}))
        assert default is not None
        self.assertEqual((default.placement, default.after_merge), ("before_merge", False))
        self.assertEqual(parse_e2e(github({"workflow": "e2e.yml", "placement": None})), default)
        self.assertEqual(parse_e2e(github({"workflow": "e2e.yml", "placement": "before_merge"})), default)
        after = parse_e2e(
            github(
                {"workflow": "stand-e2e.yml", "inputs": {"suite": "mega-noop"}, "placement": "after_merge"}
            )
        )
        assert after is not None
        self.assertEqual((after.placement, after.after_merge), ("after_merge", True))
        self.assertEqual(after.dispatch_inputs("d-1", "a" * 40), {"suite": "mega-noop"})
        for bad in ("after-merge", "AFTER_MERGE", "", 1, True, ["after_merge"]):
            with self.subTest(placement=bad), self.assertRaisesRegex(AdapterE2eDeclarationError, "placement"):
                parse_e2e(github({"workflow": "e2e.yml", "placement": bad}), adapter="codegen")

    def test_no_e2e_key_is_no_declaration(self) -> None:
        self.assertIsNone(parse_e2e({"ci": "github", "required_checks": ["test"]}))
        self.assertIsNone(parse_e2e({"ci": "local", "command": "make test"}))
        self.assertIsNone(parse_e2e(None))
        self.assertIsNone(parse_e2e("github"))

    def test_each_malformed_shape_is_a_typed_adapter_error(self) -> None:
        cases = (
            ("e2e.yml", "not a mapping"),
            ({}, "neither a workflow file name"),
            ({"workflow": "e2e.yml", "retries": 2}, "unknown key"),
            ({"workflow": ""}, "neither a workflow file name"),
            ({"workflow": "../e2e.yml"}, "neither a workflow file name"),
            ({"workflow": "e2e"}, "neither a workflow file name"),
            ({"workflow": 0}, "neither a workflow file name"),
            ({"workflow": True}, "neither a workflow file name"),
            ({"workflow": "e2e.yml", "inputs": ["suite"]}, "inputs is not a mapping"),
            ({"workflow": "e2e.yml", "inputs": {"suite": ["a"]}}, "is a list"),
            ({"workflow": "e2e.yml", "inputs": {"suite": None}}, "is a NoneType"),
            ({"workflow": "e2e.yml", "inputs": {"bad name": "x"}}, "not a workflow input name"),
            (
                {"workflow": "e2e.yml", "inputs": {"sid": "x"}, "dispatch_id_input": "sid"},
                "sets itself",
            ),
            ({"workflow": "e2e.yml", "dispatch_id_input": "a b"}, "not a workflow input name"),
            ({"workflow": "e2e.yml", "dispatch_id_input": 7}, "not a workflow input name"),
            ({"workflow": "e2e.yml", "deadline": "soon"}, "not a duration"),
            ({"workflow": "e2e.yml", "deadline": "0m"}, "not a positive duration"),
            ({"workflow": "e2e.yml", "deadline": 3600}, "not a duration"),
            ({"workflow": "e2e.yml", "candidate_input": "a b"}, "not a workflow input name"),
            ({"workflow": "e2e.yml", "candidate_input": "sid", "dispatch_id_input": "sid"}, "sets itself"),
            (
                {"workflow": "e2e.yml", "inputs": {"sha": "main"}, "candidate_input": "sha"},
                "also a static input",
            ),
        )
        for e2e, message in cases:
            with self.subTest(e2e=e2e), self.assertRaisesRegex(AdapterE2eDeclarationError, message) as raised:
                parse_e2e(github(e2e), adapter="codegen")
            self.assertIsInstance(raised.exception, HostError)
            self.assertEqual(raised.exception.adapter, "codegen")
            self.assertIn("adapter codegen declares a malformed validation.e2e", str(raised.exception))

    def test_e2e_needs_the_github_gate(self) -> None:
        for validation in (
            {"ci": "local", "command": "make test", "e2e": {"workflow": "e2e.yml"}},
            {"ci": "none", "missing": ["tests"], "e2e": {"workflow": "e2e.yml"}},
            {"e2e": {"workflow": "e2e.yml"}},
        ):
            with (
                self.subTest(validation=validation),
                self.assertRaisesRegex(AdapterE2eDeclarationError, "needs ci: github"),
            ):
                parse_e2e(validation)


class AdapterReadTests(unittest.TestCase):
    """The dispatcher's one read of an adapter fails on a malformed declaration, typed."""

    def catalog(self, validation: dict[str, object]) -> InstanceCatalog:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "adapters").mkdir()
        adapter = {**copy.deepcopy(VALID_ADAPTER), "validation": validation}
        (root / "adapters" / "codegen.yaml").write_text(yaml.safe_dump(adapter), encoding="utf-8")
        catalog = InstanceCatalog.__new__(InstanceCatalog)
        catalog.instance_dir = root
        catalog.bindings = {"codegen": {"repo": str(root / "repo"), "adapter": "codegen"}}
        return catalog

    def test_a_well_formed_declaration_reads(self) -> None:
        validation = github({"workflow": "e2e.yml"})
        self.assertEqual(self.catalog(validation).adapter("codegen")["validation"], validation)

    def test_no_declaration_reads_as_before(self) -> None:
        validation = {"ci": "github", "required_checks": ["test"]}
        self.assertEqual(self.catalog(validation).adapter("codegen")["validation"], validation)

    def test_a_malformed_declaration_fails_the_read(self) -> None:
        catalog = self.catalog(github({"workflow": "e2e.yml", "deadline": "soon"}))
        with self.assertRaisesRegex(AdapterE2eDeclarationError, "adapter codegen declares a malformed"):
            catalog.adapter("codegen")


class AdapterSchemaTests(unittest.TestCase):
    def adapter(self, validation: dict[str, object]) -> dict[str, object]:
        return {**copy.deepcopy(VALID_ADAPTER), "validation": validation}

    def test_the_schema_accepts_a_declaration(self) -> None:
        for e2e in (
            {"workflow": "e2e.yml"},
            {"workflow": 4242, "deadline": "6h"},
            {
                "workflow": "e2e.yml",
                "inputs": {"suite": "mega", "n": 2, "fast": True},
                "candidate_input": "sha",
            },
            {"workflow": "e2e.yml", "dispatch_id_input": "secretary_dispatch_id"},
            {"workflow": "stand-e2e.yml", "inputs": {"suite": "mega-noop"}, "placement": "after_merge"},
            {"workflow": "e2e.yml", "placement": "before_merge"},
        ):
            with self.subTest(e2e=e2e):
                self.assertEqual(validate(self.adapter(github(e2e)), "adapter", "a.yaml"), [])
                self.assertIsNotNone(parse_e2e(github(e2e)))

    def test_the_schema_refuses_what_the_reading_refuses(self) -> None:
        for validation in (
            github({"workflow": "e2e.yml", "retries": 2}),
            github({"inputs": {"suite": "mega"}}),
            github({"workflow": "e2e"}),
            github({"workflow": "e2e.yml", "dispatch_id_input": "a b"}),
            github({"workflow": "e2e.yml", "inputs": {"suite": ["a"]}}),
            github({"workflow": "e2e.yml", "deadline": "soon"}),
            github({"workflow": "e2e.yml", "candidate_input": "a b"}),
            github({"workflow": "e2e.yml", "placement": "after-merge"}),
            {"ci": "local", "command": "make test", "e2e": {"workflow": "e2e.yml"}},
        ):
            with self.subTest(validation=validation):
                self.assertNotEqual(validate(self.adapter(validation), "adapter", "a.yaml"), [])
                with self.assertRaises(AdapterE2eDeclarationError):
                    parse_e2e(validation)


class DispatcherWaitRightsTests(unittest.TestCase):
    """What the stage may create: a wait card, returning to one named card. Nothing is written here."""

    WRITES = ("createTask", "updateTask", "moveTaskPosition", "saveTaskMetadata", "createComment")
    RUN_URL = "https://github.com/vladmesh/secretary/actions/runs/4242"

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = mock.Mock(instance_dir=tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)

    def assertNothingWritten(self) -> None:
        written = [call for call in self.client.mock_calls if call.args and call.args[0] in self.WRITES]
        self.assertEqual(written, [])

    def test_the_card_address_names_one_card(self) -> None:
        self.assertEqual(parse_returns(["card: secretary-1795"]), ("card:secretary-1795",))
        for address in ("card:", "card:Secretary 1795", "card:sprint:1469"):
            with (
                self.subTest(address=address),
                self.assertRaisesRegex(WaitSpecError, "not observer, dependents or po-session"),
            ):
                parse_returns([address])

    def test_the_dispatcher_cuts_no_other_kind(self) -> None:
        # A decision it does cut: the one a spent e2e budget needs (secretary-1796, tests/test_e2e_budget.py);
        # and a code card only under a red after-merge run's hotfix request id (secretary-1807,
        # tests/test_e2e_after_merge.py), which none of these carries.
        for kind in ("code", "research", "infra", "operation"):
            with self.subTest(kind=kind), self.assertRaises(TaskError) as raised:
                self.writer.create(
                    role="dispatcher",
                    actor="secretary-dispatcher",
                    project="secretary",
                    task_type=kind,
                    title="T",
                )
            self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertNothingWritten()

    def test_only_the_dispatcher_names_a_card_address(self) -> None:
        for role in ("po", "observer"):
            with (
                self.subTest(role=role),
                self.assertRaisesRegex(TaskError, "is the dispatcher's own") as raised,
            ):
                self.writer.create(
                    role=role,
                    actor=role,
                    project="secretary",
                    task_type="wait",
                    title="T",
                    sprint="sprint:1469",
                    wait={"run": self.RUN_URL, "deadline": "2h", "returns": ["card:secretary-1795"]},
                )
            self.assertEqual(raised.exception.code, "validation")
        self.assertNothingWritten()


if __name__ == "__main__":
    unittest.main()
