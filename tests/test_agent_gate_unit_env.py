"""What the packaged gate units actually give a background role on a non-default installation.

The gate execs through `triggered_agents.runtime.role_env`, which forwards `SECRETARY_INSTANCE`
only when the unit already exported it: it never derives it from `TA_RUNTIME_ENV_FILE`. A unit that
sets the env file alone leaves curator and retro resolving `~/secretary-instance`, so on an
installation upgraded with `--instance` elsewhere they read the wrong head registry and the curator
writes memory into the wrong instance (secretary-849 review, round 1).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import host
from triggered_agents.agents.curator import memory_protocol
from triggered_agents.agents.pipeline import heads
from triggered_agents.runtime import role_env

GATE_UNITS = {
    "secretary-curator.service": "curator",
    "secretary-retro.service": "retro",
    "secretary-steward.service": "steward",
    "secretary-steward-deep-sweep.service": "steward",
}


class GateUnitInstanceEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        # Not ~/secretary-instance: the whole point is an installation that named its own.
        self.instance = self.root / "srv" / "other-instance"
        (self.instance / "heads").mkdir(parents=True)
        (self.instance / "heads" / "heads.toml").write_text(
            "[resources]\n[profiles]\n[role_defaults]\n", encoding="utf-8"
        )
        # The live runtime.env carries board credentials and no instance path.
        (self.instance / "runtime.env").write_text(
            "KANBOARD_URL=https://board.invalid/jsonrpc.php\n"
            "KANBOARD_API_USER=agent\n"
            "KANBOARD_API_TOKEN=secret\n",
            encoding="utf-8",
        )
        self.layout = host.SystemdLayout(
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.root / "srv" / "secretary-data",
            runtime_user="operator",
            runtime_home=self.root / "home",
        )

    def unit_env(self, name: str) -> dict[str, str]:
        rendered = host.render_systemd_unit(
            (host.default_packaging_root() / name).read_bytes(), self.layout
        ).decode()
        env = {}
        for line in rendered.splitlines():
            if line.startswith("Environment="):
                key, value = line[len("Environment="):].split("=", 1)
                env[key] = value
        return env

    def test_every_gate_unit_exports_the_installations_instance_path(self) -> None:
        for name in GATE_UNITS:
            with self.subTest(unit=name):
                env = self.unit_env(name)
                self.assertEqual(env["SECRETARY_INSTANCE"], str(self.instance))
                self.assertEqual(
                    env["TA_RUNTIME_ENV_FILE"], str(self.instance / "runtime.env")
                )

    def role_process_env(self, name: str) -> dict[str, str]:
        return role_env.runtime_env(
            GATE_UNITS[name],
            base_env=self.unit_env(name),
            env_file=self.instance / "runtime.env",
            require=True,
        )

    def test_the_role_process_reads_the_instances_head_registry(self) -> None:
        for name in GATE_UNITS:
            with self.subTest(unit=name):
                env = self.role_process_env(name)
                self.assertEqual(env["SECRETARY_INSTANCE"], str(self.instance))
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        heads.registry_path(), self.instance / "heads" / "heads.toml"
                    )

    def test_the_curator_writes_memory_into_its_own_instance(self) -> None:
        env = self.role_process_env("secretary-curator.service")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(memory_protocol.default_secretary_instance(), self.instance)

    def test_a_portable_installation_still_falls_back_to_the_product_registry(self) -> None:
        """No instance-owned canon: the same env must resolve the shipped default, not fail."""
        (self.instance / "heads" / "heads.toml").unlink()
        env = self.role_process_env("secretary-retro.service")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(heads.registry_path(), heads.HEADS_TOML)


if __name__ == "__main__":
    unittest.main()
