"""Launch-bound memory-read policy, independent of the optional MCP dependencies."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.memory import access as memory_access
from triggered_agents.runtime.head import HeadRun, HeadSpec, TaskRef


class MemoryAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def head_run(self, role: str, task: TaskRef) -> HeadRun:
        return HeadRun(
            run_id=f"{role}-{task.ref}",
            spec=HeadSpec(profile_id="test", adapter="codex"),
            workspace=str(self.data_dir / "workspace"),
            task_ref=task,
            role=role,
            pid_file=str(self.data_dir / f"{role}.pid"),
        )

    def resolve(self, grant: memory_access.MemoryAccessGrant, *, now: int = 101):
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "live-match"}):
            return memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=now)

    def test_execution_heads_receive_only_their_card_project_and_product_secretary(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-1")),
            memory_access.card_subject("card-1", "other-product"),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        self.assertIsInstance(resolved, memory_access.MemoryReadIdentity)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:other-product", "product:secretary"}))
        self.assertIsInstance(memory_access.narrow(resolved, "project:foreign"), memory_access.MemoryAccessDenial)
        self.assertEqual(memory_access.narrow(resolved, "project:other-product").scopes, frozenset({"project:other-product"}))

    def test_secretary_execution_gets_its_project_scope_but_not_installation_wide_read(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("reviewer", TaskRef.card("card-2")),
            memory_access.card_subject("card-2", "secretary"),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:secretary", "product:secretary"}))
        self.assertIsInstance(memory_access.narrow(resolved, "global"), memory_access.MemoryAccessDenial)

    def test_observer_receives_sprint_reservations_and_product_secretary(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("observer", TaskRef.sprint("sprint:7")),
            memory_access.sprint_subject("sprint:7", ["alpha", "beta"]),
            data_dir=self.data_dir,
            now=100,
        )
        resolved = self.resolve(grant)

        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(resolved.scopes, frozenset({"project:alpha", "project:beta", "product:secretary"}))

    def test_interactive_po_is_installation_wide_but_that_capability_does_not_flow_to_worker(self) -> None:
        po = memory_access.issue_grant(
            self.head_run("po", TaskRef.standing("interactive")),
            memory_access.interactive_po_subject(),
            data_dir=self.data_dir,
            now=100,
        )
        worker = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-3")),
            memory_access.card_subject("card-3", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )

        po_identity = self.resolve(po)
        worker_identity = self.resolve(worker)
        assert isinstance(po_identity, memory_access.MemoryReadIdentity)
        assert isinstance(worker_identity, memory_access.MemoryReadIdentity)
        self.assertIsNone(po_identity.scopes)
        self.assertNotIn("global", worker_identity.scopes)

    def test_spoofed_token_hand_supplied_scope_and_stale_run_fail_closed(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-4")),
            memory_access.card_subject("card-4", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )
        spoofed = f"{grant.grant_id}.not-the-launch-token"
        self.assertEqual(
            memory_access.resolve_token(spoofed, data_dir=self.data_dir, now=101).code,
            "runtime_identity_mismatch",
        )
        resolved = self.resolve(grant)
        assert isinstance(resolved, memory_access.MemoryReadIdentity)
        self.assertEqual(memory_access.narrow(resolved, "project:foreign").code, "scope_not_permitted")
        with mock.patch.object(memory_access, "head_process_status", return_value={"state": "dead"}):
            self.assertEqual(
                memory_access.resolve_token(grant.token, data_dir=self.data_dir, now=101).code,
                "runtime_identity_stale",
            )

    def test_grant_never_persists_capability_material_and_unknown_roles_are_refused(self) -> None:
        grant = memory_access.issue_grant(
            self.head_run("worker", TaskRef.card("card-5")),
            memory_access.card_subject("card-5", "alpha"),
            data_dir=self.data_dir,
            now=100,
        )
        payload = json.loads((memory_access.bindings_dir(self.data_dir) / f"{grant.grant_id}.json").read_text())
        self.assertNotIn(grant.token, json.dumps(payload))
        with self.assertRaisesRegex(memory_access.MemoryAccessError, "not permitted"):
            memory_access.issue_grant(
                self.head_run("steward", TaskRef.standing("steward")),
                {"kind": "standing", "ref": "steward"},
                data_dir=self.data_dir,
            )


if __name__ == "__main__":
    unittest.main()
