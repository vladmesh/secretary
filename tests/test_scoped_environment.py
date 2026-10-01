"""The shared privileged crossing and runtime reader, with harmless sentinels."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from secretary.po.runner import turn_environment
from secretary.po import PO_REQUEST_ENV, PO_SESSION_ENV
from secretary.runtime.head.command import with_pid_heartbeat
from secretary.runtime.head.local_pty import scope_bootstrap, scope_environment, scope_launcher
from secretary.runtime.head.local_pty.client import LocalPtySpawnError, _supervisor_environment, spawn_head
from secretary.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from secretary.runtime.head.memory import OOM_STREAM_ENV, scope_argv, scope_unit
from tests.scoped_environment_fixtures import deployed_scope_argv


class ScopedEnvironmentTests(unittest.TestCase):
    def test_crossing_restores_prepared_snapshot_after_drop_for_both_producers(self) -> None:
        for emitter in (scope_argv, deployed_scope_argv()):
            with self.subTest(emitter=emitter.__code__.co_filename), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                bin_dir = root / "cli tools"
                bin_dir.mkdir()
                result = root / "result.json"
                executable = bin_dir / "only-prepared-cli"
                executable.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json,os,sys,pathlib,secretary\n"
                    "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
                    "'environment':dict(os.environ), 'argv':sys.argv[2:],"
                    "'python':sys.executable, 'source':secretary.__file__,"
                    "'stdin':sys.stdin.read()}))\n"
                )
                executable.chmod(0o700)
                sentinel = "safe spaces ' ; $(touch NEVER) `touch NEVER`\nconfig"
                argument = "argument spaces ' ; $(touch NEVER) `touch NEVER`"
                supplied = {
                    "PATH": str(bin_dir) + ":/usr/bin:/bin",
                    "HOME": str(root / "runtime home"), "CODEX_HOME": str(root / "codex home"),
                    "SCOPED_CONFIG_SENTINEL": sentinel, "SUPPLIED_EMPTY": "",
                    PO_SESSION_ENV: "disposable-session",
                    PO_REQUEST_ENV: "disposable-request",
                    OOM_STREAM_ENV: "caller-cannot-bind-the-kernel-stream",
                }
                with mock.patch.dict(os.environ, {}, clear=True):
                    environment = _supervisor_environment(turn_environment(supplied))
                command = with_pid_heartbeat(
                    shlex.join([executable.name, str(result), argument, ""]),
                    str(root / "head.pid"), identity={"run_id": "crossing"}, in_process=True,
                )
                arguments = emitter("crossing", 8192, ["/bin/sh", "-c", command],
                                    pythonpath=environment["PYTHONPATH"])
                bootstrap_calls = []
                # Instrument only system scope registration and the native drop. The
                # sealed-stdin handoff, isolated Python runtime reader, heartbeat,
                # executable lookup and imports execute as real processes.
                real_execve = os.execve
                stdin_copy = os.dup(0)
                self.addCleanup(os.close, stdin_copy)
                def privileged_exec(path, argv, env):
                    bootstrap_calls.append((path, argv, env))
                    self.assertEqual(env, scope_environment.BOOTSTRAP_ENVIRONMENT)
                    self.assertNotIn(sentinel, " ".join(argv))
                    divider = argv.index("--")
                    bootstrap_args = argv[divider + 4:]
                    def drop(path, argv, env):
                        self.assertEqual(path, "/usr/bin/setpriv")
                        self.assertEqual(env, scope_environment.BOOTSTRAP_ENVIRONMENT)
                        index = argv.index("-I") - 1
                        with mock.patch("os.execve", real_execve):
                            child = subprocess.run(argv[index:], stdin=0, env=env,
                                                   cwd=root, capture_output=True, timeout=10)
                        self.assertEqual(child.returncode, 0, child.stderr.decode())
                    with (mock.patch.dict(os.environ, env, clear=True),
                          mock.patch.object(scope_bootstrap, "own_cgroup", return_value=root / scope_unit("crossing")),
                          mock.patch.object(scope_bootstrap, "install_oom_contract"),
                          mock.patch.object(scope_bootstrap, "open_oom_stream", return_value=998),
                          mock.patch("os.execve", side_effect=drop)):
                        scope_bootstrap.main(bootstrap_args)
                try:
                    with (mock.patch.dict(os.environ, environment, clear=True),
                          mock.patch("os.execve", side_effect=privileged_exec)):
                        scope_environment.exec_scope(arguments)
                finally:
                    os.dup2(stdin_copy, 0)
                actual = json.loads(result.read_text())
                expected = {**environment, OOM_STREAM_ENV: "998"}
                # /bin/sh can add PWD and LC_CTYPE can be coerced by Python startup.
                for key, value in expected.items():
                    self.assertEqual(actual["environment"][key], value, key)
                self.assertNotIn("FOREIGN_INSTALLATION", actual["environment"])
                self.assertEqual(actual["argv"], [argument, ""])
                self.assertEqual(actual["stdin"], "")
                self.assertEqual(Path(actual["python"]).parent, Path(sys.executable).parent)
                self.assertTrue(actual["source"].startswith(environment["PYTHONPATH"].split(":")[0]))
                self.assertEqual(len(bootstrap_calls), 1)
                self.assertFalse((root / "NEVER").exists())

    def test_exact_absence_empty_surrogates_and_private_immutable_descriptor(self) -> None:
        environment = {"EMPTY": "", "CONFIG": "spaces ; $()", "BYTES": "\udcff"}
        fd = scope_environment.environment_descriptor(environment)
        self.addCleanup(os.close, fd)
        self.assertEqual(scope_environment.read_environment(fd), environment)
        self.assertEqual(os.fstat(fd).st_nlink, 0)
        self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o600)
        self.assertFalse(os.get_inheritable(fd))
        with self.assertRaises(OSError):
            os.write(fd, b"replace")
        with self.assertRaises(scope_environment.EnvironmentTransferError):
            scope_environment.read_environment(fd)  # one consumption

    def test_invalid_missing_unsealed_oversized_or_wrong_owner_transport_refuses(self) -> None:
        with tempfile.TemporaryFile() as ordinary:
            ordinary.write(b'{"safe":"sentinel"}')
            ordinary.seek(0)
            with self.assertRaises(scope_environment.EnvironmentTransferError):
                scope_environment.read_environment(ordinary.fileno())
        for value in ({"BAD=KEY": "safe"}, {"KEY": "safe\0"}, {"KEY": "x" * scope_environment.MAX_ENVIRONMENT_BYTES}):
            with self.assertRaises(scope_environment.EnvironmentTransferError):
                scope_environment.environment_descriptor(value)
        with self.assertRaises(scope_environment.EnvironmentTransferError):
            scope_environment.read_environment(-1)
        fd = scope_environment.environment_descriptor({"SAFE": "sentinel"})
        self.addCleanup(os.close, fd)
        with mock.patch.object(scope_environment.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(scope_environment.EnvironmentTransferError):
                scope_environment.read_environment(fd)
        self.assertEqual(scope_environment.read_environment(fd), {"SAFE": "sentinel"})

    def test_runtime_reader_does_not_copy_missing_home_or_foreign_settings(self) -> None:
        supplied = {"PATH": "", "SAFE": "sentinel", "EMPTY": ""}
        fd = scope_environment.environment_descriptor(supplied)
        self.addCleanup(os.close, fd)
        child = subprocess.run([
            sys.executable, "-I", str(scope_environment.BOOTSTRAP), "--runtime", "998",
            sys.executable, "-I", "-c", "import os,json; print(json.dumps(dict(os.environ)))",
        ], stdin=fd, capture_output=True, timeout=10, env={
            "HOME": "/foreign/home", "CODEX_HOME": "/foreign/codex",
            "FOREIGN_INSTALLATION": "must-be-absent", "PATH": "/usr/bin:/bin",
        })
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        actual = json.loads(child.stdout)
        for key, value in supplied.items():
            self.assertEqual(actual[key], value)
        for key in ("HOME", "CODEX_HOME", "PYTHONPATH", "FOREIGN_INSTALLATION"):
            self.assertNotIn(key, actual)
        self.assertEqual(actual[OOM_STREAM_ENV], "998")

    def test_privileged_code_tools_and_identity_ignore_caller_environment(self) -> None:
        argv = deployed_scope_argv()("safe", 12288, ["/bin/true"], pythonpath="untrusted ; $()")
        with mock.patch.dict(os.environ, {"PATH": "/foreign", "PYTHONHOME": "/foreign", "LD_PRELOAD": "/foreign"}):
            actual = scope_environment.privileged_argv(argv)
        self.assertEqual(actual[:3], ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run"])
        self.assertIn("--property=MemoryMax=12884901888", actual)
        self.assertIn("--property=MemorySwapMax=0", actual)
        self.assertNotIn("untrusted ; $()", " ".join(actual))
        self.assertEqual(actual[actual.index("--") + 1:][:3], [sys.executable, "-I", str(scope_environment.BOOTSTRAP)])
        for replacement in ("--reuid=999999", "--regid=999999", "--groups=999999"):
            invalid = list(argv)
            prefix = replacement.split("=")[0] + "="
            index = next(i for i, arg in enumerate(invalid) if arg.startswith(prefix) or arg == "--clear-groups")
            invalid[index] = replacement
            with self.assertRaises(scope_environment.EnvironmentTransferError):
                scope_environment.privileged_argv(invalid)

    def test_gate_eof_never_produces_payload_or_launches_and_refusal_does_not_echo_values(self) -> None:
        read, write = os.pipe()
        os.close(write)
        with mock.patch.object(scope_launcher, "exec_scope") as launch:
            self.assertEqual(scope_launcher.main(["--exec-gated", str(read), "invalid"]), 1)
            launch.assert_not_called()
        read, write = os.pipe()
        os.write(write, b"1")
        os.close(write)
        with (mock.patch.object(scope_launcher, "exec_scope", side_effect=OSError("safe sentinel never logged")),
              mock.patch("sys.stderr") as log):
            self.assertEqual(scope_launcher.main(["--exec-gated", str(read), "invalid"]), 1)
        self.assertNotIn("sentinel", str(log.mock_calls))

    def test_producer_crash_leaves_no_payload_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            child = subprocess.run([
                sys.executable, "-P", "-c",
                "import os; from secretary.runtime.head.local_pty.scope_environment import environment_descriptor; "
                "environment_descriptor({'SAFE':'sentinel'}); os._exit(7)",
            ], cwd=temp, env=_supervisor_environment(None), capture_output=True, timeout=10)
            self.assertEqual(child.returncode, 7, child.stderr.decode())
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_environment_refusal_settles_through_scope_owner_for_every_role(self) -> None:
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                def refused(argv, **kwargs):
                    self.assertIn("secretary.runtime.head.local_pty.scope_launcher", argv)
                    self.assertEqual(kwargs["env"]["SAFE"], "explicit override")
                    kwargs["stderr"].write(b"scoped environment launch refused\n")
                    return SimpleNamespace(wait=lambda: 1)
                with (mock.patch("secretary.runtime.head.local_pty.client.subprocess.Popen", side_effect=refused) as launch,
                      mock.patch("secretary.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root / "cgroups"),
                      mock.patch.dict(os.environ, {"SAFE": "inherited"})):
                    with self.assertRaises(LocalPtySpawnError) as failure:
                        spawn_head(root=root, run_id="refused", role=role, task="disposable",
                                   command="/bin/true", env={"SAFE": "explicit override"}, memory_limit_mib=8192)
                self.assertEqual(failure.exception.reason, "scope_failed")
                self.assertTrue(failure.exception.cleanup_complete)
                self.assertEqual(launch.call_count, 1)
                record = ScopedHeadLifecycle.read_owner(root / "refused")
                self.assertFalse(record["launch_allowed"])
                self.assertTrue(record["cleanup_complete"])
                self.assertNotIn("explicit override", json.dumps(record))


if __name__ == "__main__":
    unittest.main()
