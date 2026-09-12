"""The long-lived web process, and the upgrade step that makes it coherent with the checkout.

Four things are pinned here, and the first is the defect the rest exist for.

**The split is reproduced, not asserted from timestamps.** A process holds the callables it imported
when it started; `importlib.resources` reads bundled data files from the checkout as the checkout is
*now*. On 2026-09-11 `secretary-web.service` had been running since 06:33 UTC when
`f9cabc3` landed at 09:42 UTC and made the onboarding contract's drafted adapter a relative `$ref`
to `adapter.schema.json`, resolved through a registry the process's validator did not have. Every
route then answered an empty reply, because `jsonschema.exceptions._WrappedReferencingError:
Unresolvable: adapter.schema.json` escaped the socket handler. Two tests reproduce that: one against
the product's own bundled schemas with the pre-`f9cabc3` validator reconstructed, and one with a real
long-lived process over a real loopback socket whose checkout moves underneath it. Neither compares
a timestamp to anything; both fail on the resolution and pass after a restart.

**The supported path is what repairs it.** `upgrade.step_web` is the restart, and the drift test
drives the repair through that step rather than through a bare `systemctl restart`, so the test would
fail if the step stopped restarting or stopped probing.

**The step is honest about an optional unit.** Uninstalled and inactive are two different skips and
neither starts anything; a current process is not restarted; `--dry-run` says what it would do.

**Ordering is structural.** The step list is asserted, and a failed prerequisite is shown to stop the
run before the restart rather than after it.
"""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from jsonschema import Draft202012Validator

from secretary import upgrade
from secretary.config import load_schema, validate
from secretary.host_apply import HostCommandError
from secretary.web.health import (
    WebProbeError,
    WebTarget,
    probe_web,
    target_from_unit,
)
from secretary.web.server import LoopbackOnly
from tests.fakes.upgrade import FakeRegistrar, FakeUnitInstaller

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "onboarding" / "happy-path.json"

UNIT_PREFIX = "secretary-"
WEB_UNIT = f"{UNIT_PREFIX}web.service"


def _unit_text(port: int, host: str = "127.0.0.1") -> bytes:
    """The shape of the installed unit the probe reads its target out of."""
    return (
        "[Service]\n"
        f"ExecStart=/opt/secretary/.venv/bin/secretary web-serve --instance /srv/i "
        f"--host {host} --port {port}\n"
    ).encode()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Report:
    """The slice of an InstanceReport the host and process steps read."""

    host: ClassVar = {"unit_prefix": UNIT_PREFIX}
    instance: ClassVar = {"host": {"unit_prefix": UNIT_PREFIX}, "data_dir": "/tmp/does-not-matter"}
    data_dir = Path("/tmp/does-not-matter")
    bindings: ClassVar[list] = []


def _context(units: Any, **overrides) -> upgrade.UpgradeContext:
    base = upgrade.UpgradeContext(
        instance_path=Path("/tmp/instance"),
        product_root=upgrade.running_product_root(),
        base_branch="main",
        dry_run=False,
        units=units,
        orca=FakeRegistrar(),
        automations=None,
        report=_Report(),
    )
    return replace(base, **overrides)


# -- the split, against the product's own bundled schemas ----------------------------------------


class RetainedValidatorTests(unittest.TestCase):
    """The live failure, with the pre-`f9cabc3` validation callable and today's bundled schemas.

    This is the in-process half of the reproduction and it is deliberately built out of the product's
    real schemas rather than a fixture pair: it fails for as long as *any* bundled schema `$ref`s
    another by file name, which is exactly the condition that makes an unrestarted process a defect.
    """

    def document(self) -> dict[str, Any]:
        return json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))

    def test_a_retained_registry_less_validator_cannot_resolve_the_bundled_cross_file_ref(self) -> None:
        document = self.document()
        # Exactly what `secretary.config.validate` was before f9cabc3: no registry. A process that
        # imported that module keeps this callable no matter what the checkout does afterwards.
        stale = Draft202012Validator(load_schema("onboarding-contract"))

        with self.assertRaises(Exception) as escaped:
            list(stale.iter_errors(document))

        self.assertIn("Unresolvable", str(escaped.exception))
        self.assertIn("adapter.schema.json", str(escaped.exception))

    def test_the_same_document_and_schemas_validate_through_the_current_callable(self) -> None:
        """The other half: nothing is wrong with the document or the files, only with the process."""
        self.assertEqual(validate(self.document(), "onboarding-contract", "happy-path.json"), [])

    def test_the_bundled_schemas_do_carry_a_cross_file_reference(self) -> None:
        """Guards the two tests above from passing vacuously if the `$ref` were ever inlined again."""
        text = (REPO_ROOT / "src" / "secretary" / "schemas" / "onboarding-contract.schema.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$ref": "adapter.schema.json"', text)


# -- the split, with a real long-lived process over a real loopback socket ------------------------

#: The checkout before the move: the validator has no registry, and the schema needs none.
OLD_VALIDATOR = """
import json
from importlib import resources
from jsonschema import Draft202012Validator


def _read(name):
    return json.loads(resources.files("driftpkg.schemas").joinpath(name).read_text("utf-8"))


# Resources are read inside the call, every time; this callable is loaded once, at import.
def validate(document):
    validator = Draft202012Validator(_read("contract.schema.json"))
    return [error.validator for error in validator.iter_errors(document)]
"""

#: The checkout after the move: the schema `$ref`s a second file, so the validator needs a registry.
NEW_VALIDATOR = """
import json
from importlib import resources
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

NAMES = ("contract.schema.json", "adapter.schema.json")


def _read(name):
    return json.loads(resources.files("driftpkg.schemas").joinpath(name).read_text("utf-8"))


def _registry():
    return Registry().with_resources(
        (_read(name)["$id"], DRAFT202012.create_resource(_read(name))) for name in NAMES
    )


def validate(document):
    validator = Draft202012Validator(_read("contract.schema.json"), registry=_registry())
    return [error.validator for error in validator.iter_errors(document)]
"""

OLD_CONTRACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "contract.schema.json",
    "type": "object",
    "required": ["adapter"],
    "properties": {"adapter": {"type": "object", "required": ["id"]}},
}

NEW_CONTRACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "contract.schema.json",
    "type": "object",
    "required": ["adapter"],
    "properties": {"adapter": {"$ref": "adapter.schema.json"}},
}

ADAPTER_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "adapter.schema.json",
    "type": "object",
    "required": ["id"],
    "properties": {"id": {"type": "string"}},
}

#: A stand-in for the web transport: it imports the checkout once and reads bundled data per request.
#: `/ready` is answered without touching the checkout, so a test can wait for the socket without
#: deciding anything about validation; `/api/system` is the read the real probe performs.
SERVER_SCRIPT = """
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, sys.argv[1])
# Imported once, at start. This is the whole of what a restart replaces.
from driftpkg import validator

DOCUMENT = {"adapter": {"id": "secretary"}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/ready":
            self._write(200, b"ready")
            return
        try:
            errors = validator.validate(DOCUMENT)
        except Exception as exc:
            self._write(500, f"{type(exc).__name__}: {exc}".encode("utf-8"))
            return
        self._write(200 if not errors else 500, json.dumps({"errors": errors}).encode("utf-8"))

    def _write(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
"""


class DriftingWebUnit(FakeUnitInstaller):
    """A unit whose `restart` really replaces a process: the seam `step_web` is meant to drive.

    Everything else about it is the recorded double the other upgrade tests use, so a restart this
    step does not ask for is visible as an absent call rather than as a passing test.
    """

    def __init__(self, checkout: Path, port: int, script: Path) -> None:
        super().__init__(present={WEB_UNIT: _unit_text(port)}, active={WEB_UNIT})
        self.checkout = checkout
        self.port = port
        self.script = script
        self.process: subprocess.Popen[bytes] | None = None
        self.generations = 0

    def start(self) -> subprocess.Popen[bytes]:
        self.generations += 1
        self.process = subprocess.Popen(
            [sys.executable, "-P", str(self.script), str(self.checkout), str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self.process

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - a terminate that was ignored
                self.process.kill()
                self.process.wait(timeout=10)
            self.process = None

    def restart(self, name: str) -> None:
        super().restart(name)
        self.stop()
        self.start()


class LongLivedProcessDriftTests(unittest.TestCase):
    """One process, one moving checkout, and the supported step that replaces the process."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.tmp / "checkout"
        self.schemas = self.checkout / "driftpkg" / "schemas"
        self.schemas.mkdir(parents=True)
        (self.checkout / "driftpkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.schemas / "__init__.py").write_text("", encoding="utf-8")
        self.script = self.tmp / "serve.py"
        self.script.write_text(SERVER_SCRIPT, encoding="utf-8")
        self._write_old_checkout()
        self.port = _free_port()
        self.target = WebTarget("127.0.0.1", self.port)
        self.ready = WebTarget("127.0.0.1", self.port, "/ready")
        self.units = DriftingWebUnit(self.checkout, self.port, self.script)
        self.addCleanup(self.units.stop)

    # -- the two checkouts ---------------------------------------------------------------------

    def _write_old_checkout(self) -> None:
        (self.checkout / "driftpkg" / "validator.py").write_text(OLD_VALIDATOR, encoding="utf-8")
        self._schema("contract.schema.json", OLD_CONTRACT_SCHEMA)
        (self.schemas / "adapter.schema.json").unlink(missing_ok=True)

    def _move_checkout(self) -> None:
        """What `step_pull` does: both files move, and only the files. Nothing restarts."""
        (self.checkout / "driftpkg" / "validator.py").write_text(NEW_VALIDATOR, encoding="utf-8")
        self._schema("contract.schema.json", NEW_CONTRACT_SCHEMA)
        self._schema("adapter.schema.json", ADAPTER_SCHEMA)

    def _schema(self, name: str, document: dict[str, Any]) -> None:
        (self.schemas / name).write_text(json.dumps(document, indent=2), encoding="utf-8")

    # -- the process ---------------------------------------------------------------------------

    def _serve(self) -> None:
        self.units.start()
        probe_web(self.ready, timeout_seconds=20.0, retry_seconds=0.05)

    def _read(self) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", "/api/system")
            response = connection.getresponse()
            return int(response.status), response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

    # -- the test ------------------------------------------------------------------------------

    def test_a_moved_checkout_breaks_the_running_process_and_the_upgrade_step_replaces_it(self) -> None:
        self._serve()
        self.assertEqual(self._read()[0], 200, "the process is coherent with the checkout it started on")
        first_generation = self.units.generations

        self._move_checkout()

        # The defect, with nothing restarted and no clock consulted: old callables in memory, the
        # new bundled schema on disk, and the cross-file `$ref` that needs the registry the retained
        # validator does not have.
        broken_status, broken_body = self._read()
        self.assertEqual(broken_status, 500)
        self.assertIn("Unresolvable", broken_body)
        self.assertIn("adapter.schema.json", broken_body)
        self.assertEqual(self.units.generations, first_generation, "nothing has restarted it yet")

        # The supported repair: the upgrade step, not a bare systemctl call.
        result = upgrade.step_web(_context(self.units, code_changed=True, schemas_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(f"restarted {WEB_UNIT}", result.detail)
        self.assertIn(self.target.url, result.detail)
        self.assertIn("bundled schemas changed", result.detail)
        self.assertIn(("restart", WEB_UNIT), self.units.calls)
        self.assertEqual(self.units.generations, first_generation + 1)
        self.assertEqual(self._read()[0], 200, "the replaced process reads the schemas it shipped with")

    def test_a_second_upgrade_over_the_coherent_process_restarts_nothing(self) -> None:
        """The no-op: a current checkout, schemas, unit and process leave the process alone."""
        self._move_checkout()
        self._serve()
        self.assertEqual(self._read()[0], 200)
        generation = self.units.generations

        result = upgrade.step_web(_context(self.units))

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(self.units.calls, [])
        self.assertEqual(self.units.generations, generation)
        self.assertEqual(self._read()[0], 200)

    def test_a_restart_whose_process_cannot_answer_fails_the_step(self) -> None:
        """The probe is the evidence: a unit that restarts into a broken process is a failure."""
        self._serve()
        self._move_checkout()
        # Make the checkout itself incoherent, so a freshly started process is no better than the
        # stale one: the contract still references the adapter schema and the file is gone.
        (self.schemas / "adapter.schema.json").unlink()

        with mock.patch.object(upgrade, "WEB_PROBE_TIMEOUT_SECONDS", 1.0):
            result = upgrade.step_web(_context(self.units, schemas_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("loopback probe failed", result.detail)
        self.assertIn(self.target.url, result.detail)
        self.assertIn("HTTP 500", result.detail)
        self.assertIn(("restart", WEB_UNIT), self.units.calls)


# -- the step's own decisions, against fake units -------------------------------------------------


class WebStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = self.enterContext(mock.patch.object(upgrade, "probe_web", return_value=200))

    def units(self, *, installed: bool = True, active: bool = True, port: int = 8787) -> FakeUnitInstaller:
        return FakeUnitInstaller(
            present={WEB_UNIT: _unit_text(port)} if installed else {},
            active={WEB_UNIT} if active else set(),
        )

    def test_an_uninstalled_web_unit_is_reported_and_never_started(self) -> None:
        units = self.units(installed=False, active=False)

        result = upgrade.step_web(_context(units, code_changed=True))

        self.assertEqual(result.status, "skipped")
        self.assertIn("not installed", result.detail)
        self.assertEqual(units.calls, [])
        self.probe.assert_not_called()

    def test_an_inactive_web_unit_is_reported_and_never_started(self) -> None:
        units = self.units(active=False)

        result = upgrade.step_web(_context(units, code_changed=True))

        self.assertEqual(result.status, "skipped")
        self.assertIn("not active", result.detail)
        self.assertIn("does not start it", result.detail)
        self.assertEqual(units.calls, [])
        self.probe.assert_not_called()

    def test_every_materialized_change_is_its_own_named_restart_reason(self) -> None:
        for field, reason in (
            ("code_changed", "product code or dependencies changed"),
            ("schemas_changed", "bundled schemas changed"),
            ("web_unit_changed", "a web unit file changed"),
        ):
            with self.subTest(reason=field):
                units = self.units()

                result = upgrade.step_web(_context(units, **{field: True}))

                self.assertEqual(result.status, "changed")
                self.assertIn(reason, result.detail)
                self.assertIn(("restart", WEB_UNIT), units.calls)

    def test_a_current_process_is_left_alone_and_not_probed(self) -> None:
        units = self.units()

        result = upgrade.step_web(_context(units))

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(units.calls, [])
        self.probe.assert_not_called()

    def test_dry_run_names_the_pending_restart_and_the_probe_and_touches_nothing(self) -> None:
        units = self.units(port=8899)

        result = upgrade.step_web(_context(units, code_changed=True, dry_run=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(f"would restart {WEB_UNIT}", result.detail)
        self.assertIn("http://127.0.0.1:8899/api/system", result.detail)
        self.assertEqual(units.calls, [])
        self.probe.assert_not_called()

    def test_a_successful_reconciliation_names_the_unit_and_the_path_it_read(self) -> None:
        units = self.units(port=8901)

        result = upgrade.step_web(_context(units, code_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(WEB_UNIT, result.detail)
        self.assertIn("http://127.0.0.1:8901/api/system -> 200", result.detail)
        self.assertEqual(self.probe.call_args.args[0], WebTarget("127.0.0.1", 8901, "/api/system"))

    def test_a_failed_restart_fails_the_step_without_claiming_a_current_process(self) -> None:
        units = self.units()
        units.restart = lambda name: (_ for _ in ()).throw(HostCommandError(f"restart {name}: exited 1"))

        result = upgrade.step_web(_context(units, code_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("restarting", result.detail)
        self.probe.assert_not_called()

    def test_a_failed_probe_fails_the_step(self) -> None:
        self.probe.side_effect = WebProbeError("http://127.0.0.1:8787/api/system did not answer 200")

        result = upgrade.step_web(_context(self.units(), code_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("loopback probe failed", result.detail)

    def test_a_unit_edited_to_serve_off_loopback_refuses_the_probe_rather_than_reaching_out(self) -> None:
        units = FakeUnitInstaller(present={WEB_UNIT: _unit_text(8787, host="192.168.7.7")}, active={WEB_UNIT})

        result = upgrade.step_web(_context(units, code_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("loopback", result.detail)
        self.assertEqual(units.calls, [])
        self.probe.assert_not_called()

    def test_a_failed_result_is_a_failed_upgrade(self) -> None:
        """Criterion 2: a failure here cannot be reported as an upgrade that served current code."""
        self.probe.side_effect = WebProbeError("no answer")

        result = upgrade.run_steps(_context(self.units(), code_changed=True), steps=(upgrade.step_web,))

        self.assertFalse(result.ok)
        self.assertIn("failed", result.render())


class WebStepOrderingTests(unittest.TestCase):
    """Criterion 4: the order is a property of the step list, and it is asserted rather than assumed."""

    def index(self, step) -> int:
        return upgrade.STEPS.index(step)

    def test_the_restart_follows_the_checkout_the_dependencies_the_schemas_and_the_unit(self) -> None:
        web = self.index(upgrade.step_web)
        for earlier in (upgrade.step_pull, upgrade.step_dependencies, upgrade.step_host):
            with self.subTest(step=earlier.__name__):
                self.assertLess(self.index(earlier), web)

    def test_verification_runs_after_the_restart_so_it_cannot_call_an_old_process_current(self) -> None:
        self.assertLess(self.index(upgrade.step_web), self.index(upgrade.step_verify))
        self.assertIs(upgrade.STEPS[-1], upgrade.step_verify)

    def test_a_failed_prerequisite_stops_the_run_before_the_restart(self) -> None:
        units = FakeUnitInstaller(present={WEB_UNIT: _unit_text(8787)}, active={WEB_UNIT})

        def failing(context):
            return upgrade.StepResult("dependencies", "failed", "pip install exited 1")

        result = upgrade.run_steps(_context(units, code_changed=True), steps=(failing, upgrade.step_web))

        self.assertFalse(result.ok)
        self.assertEqual([step.name for step in result.steps], ["dependencies"])
        self.assertEqual(units.calls, [])


# -- the probe -----------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, _limit: int | None = None) -> bytes:
        return b"{}"


class _FakeConnection:
    def __init__(self, statuses: list[int | Exception]) -> None:
        self.statuses = statuses
        self.requests: list[str] = []

    def request(self, _method: str, path: str) -> None:
        self.requests.append(path)

    def getresponse(self) -> _FakeResponse:
        answer = self.statuses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)

    def close(self) -> None:
        pass


class ProbeTests(unittest.TestCase):
    def connector(self, statuses: list[int | Exception]):
        self.connection = _FakeConnection(statuses)
        return lambda *_args, **_kwargs: self.connection

    def test_a_200_is_the_only_answer_that_ends_the_probe(self) -> None:
        target = WebTarget("127.0.0.1", 8787)
        clock = iter([0.0, 0.0, 0.0, 0.0])

        status = probe_web(
            target,
            connect=self.connector([ConnectionRefusedError(), 503, 200]),
            clock=lambda: next(clock),
            sleep=lambda _seconds: None,
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.connection.requests, ["/api/system"] * 3)

    def test_the_probe_is_bounded_and_names_the_target_and_the_last_answer(self) -> None:
        clock = iter([0.0, 100.0])

        with self.assertRaises(WebProbeError) as failed:
            probe_web(
                WebTarget("127.0.0.1", 8787),
                timeout_seconds=1.0,
                connect=self.connector([500]),
                clock=lambda: next(clock),
                sleep=lambda _seconds: None,
            )

        self.assertIn("http://127.0.0.1:8787/api/system", str(failed.exception))
        self.assertIn("HTTP 500", str(failed.exception))

    def test_a_socket_failure_is_reported_by_class_and_not_by_the_address_it_dialled(self) -> None:
        clock = iter([0.0, 100.0])

        with self.assertRaises(WebProbeError) as failed:
            probe_web(
                WebTarget("127.0.0.1", 8787),
                timeout_seconds=1.0,
                connect=self.connector([ConnectionRefusedError("[Errno 111] to 127.0.0.1:8787")]),
                clock=lambda: next(clock),
                sleep=lambda _seconds: None,
            )

        self.assertIn("ConnectionRefusedError", str(failed.exception))
        self.assertNotIn("Errno", str(failed.exception))

    def test_the_target_comes_out_of_the_installed_unit(self) -> None:
        self.assertEqual(target_from_unit(_unit_text(9001)), WebTarget("127.0.0.1", 9001, "/api/system"))

    def test_a_unit_naming_no_address_falls_back_to_the_shipped_default(self) -> None:
        self.assertEqual(target_from_unit(None), WebTarget("127.0.0.1", 8787, "/api/system"))

    def test_the_shipped_unit_serves_the_address_the_probe_would_read(self) -> None:
        """The packaged unit and the probe cannot disagree, because one is read from the other."""
        packaged = (REPO_ROOT / "packaging" / "systemd" / "secretary-web.service").read_bytes()
        self.assertEqual(target_from_unit(packaged), WebTarget("127.0.0.1", 8787, "/api/system"))

    def test_a_unit_serving_off_loopback_is_refused_before_a_request_is_made(self) -> None:
        with self.assertRaises(LoopbackOnly):
            target_from_unit(_unit_text(8787, host="10.1.2.3"))


if __name__ == "__main__":
    unittest.main()
