"""Execute the shipped guard and shared launch wrapper with a transcript-only native Docker.

No Docker daemon, socket, container or production target is used by this component suite.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.runtime import docker_guard, role_env
from secretary.runtime.container_labels import PRODUCTION_BOARD_LABEL, TEST_BOARD_LABEL
from secretary.runtime.head.command import wrap_role_command
from tests.support.managed_venv import guarded_product_env, managed_product_root

SAFE_ID = "a" * 64
OTHER_ID = "b" * 64
PROTECTED_ID = "c" * 64

# This is a subprocess fixture, not a mocked guard policy. It logs every native call, implements
# inspection responses, and returns a distinct native status/output for forwarded commands.
NATIVE = r"""
import json
import os
import sys
from pathlib import Path

case_path = Path(os.environ["GUARD_CASE"])
case = json.loads(case_path.read_text())
args = sys.argv[1:]
rest = list(args)
flags = {}
values = {"--host", "-H", "--context", "-c", "--config", "--log-level", "-l",
          "--tlscacert", "--tlscert", "--tlskey"}
while rest and rest[0].startswith("-"):
    flag = rest.pop(0)
    if "=" in flag:
        name, value = flag.split("=", 1)
    elif flag in values:
        name, value = flag, rest.pop(0)
    else:
        name, value = flag, True
    flags[name] = value
context = flags.get("--context", flags.get("-c"))
host = flags.get("--host", flags.get("-H"))
if context and host:
    sys.exit(9)
if context:
    selected = context
elif host or os.environ.get("DOCKER_HOST"):
    selected = "default"
else:
    selected = os.environ.get("DOCKER_CONTEXT") or case.get("current", "default")
if selected == "default":
    host = host or os.environ.get("DOCKER_HOST") or "unix:///fake/default.sock"
else:
    host = case.get("contexts", {}).get(selected, "unix:///fake/" + selected + ".sock")
with open(os.environ["GUARD_TRANSCRIPT"], "a") as output:
    output.write(json.dumps({"args": args, "host": host, "context": selected,
        "docker_env": {k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}}) + "\n")
if rest == ["context", "inspect"]:
    if case.get("context_failure"):
        sys.exit(7)
    record = {"Name": selected, "Endpoints": {"docker": {"Host": host,
        "SkipTLSVerify": case.get("skip_tls", False)}}, "TLSMaterial": case.get("tls", {})}
    print(json.dumps(case.get("context_response", [record])))
elif rest[:2] == ["container", "inspect"]:
    target = rest[-1]
    if target == "failed":
        sys.exit(8)
    if target == "invalid-json":
        print("bad-json")
    elif target not in case["containers"]:
        sys.exit(1)
    else:
        print(json.dumps(case["containers"][target]))
        if case.get("reuse"):
            case["containers"][target] = case["containers"]["protected"]
            case["current"] = "replaced"
            case_path.write_text(json.dumps(case))
elif rest[:2] in (["container", "rm"], ["container", "remove"], ["container", "stop"], ["container", "kill"]):
    print("native mutation")
    sys.exit(case.get("mutation_status", 0))
elif rest and rest[0] == "ps" and "ancestor=postgres:16" in rest:
    print("a" * 64 + "\n" + "c" * 64)
else:
    print("native stdout")
    print("native stderr", file=sys.stderr)
    sys.exit(case.get("read_status", 0))
"""


def container(identifier: str, labels: object) -> list[dict[str, object]]:
    return [{"Id": identifier, "Config": {"Labels": labels}}]


class DockerGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.product = managed_product_root(self.root)
        self.workspace = self.root / "candidate"
        self.workspace.mkdir()
        venv_bin = self.workspace / role_env.WORKSPACE_ENV_DIR / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").symlink_to(sys.executable)
        (venv_bin / "ruff").write_text("#!/bin/sh\nprintf '%s\\n' 'workspace ruff'\n")
        (venv_bin / "ruff").chmod(0o755)
        native_bin = self.root / "native"
        native_bin.mkdir()
        self.native = native_bin / "docker"
        self.native.write_text(f"#!{sys.executable}\n" + NATIVE)
        self.native.chmod(0o755)
        self.case_file = self.root / "case.json"
        self.transcript = self.root / "calls.jsonl"
        self.case: dict[str, object] = {
            "containers": {
                "safe": container(SAFE_ID, {TEST_BOARD_LABEL: "123"}),
                SAFE_ID: container(SAFE_ID, {TEST_BOARD_LABEL: "123"}),
                "other": container(OTHER_ID, {TEST_BOARD_LABEL: "456"}),
                "protected": container(PROTECTED_ID, {}),
                PROTECTED_ID: container(PROTECTED_ID, {}),
            }
        }
        self.base = {
            "PATH": str(native_bin) + os.pathsep + str(self.product / ".venv/bin"),
            "TA_SECRETARY_REPO": str(self.product),
            "SECRETARY_RUNTIME_ENV_FILE": str(self.root / "absent.env"),
            "GUARD_CASE": str(self.case_file),
            "GUARD_TRANSCRIPT": str(self.transcript),
        }

    def save_case(self) -> None:
        self.case_file.write_text(json.dumps(self.case))

    def environment(self, role: str = "worker") -> dict[str, str]:
        with mock.patch.dict(os.environ, self.base, clear=True):
            return role_env.runtime_env(role, base_env=self.base, workspace=self.workspace)

    def calls(self) -> list[dict[str, object]]:
        if not self.transcript.exists():
            return []
        return [json.loads(line) for line in self.transcript.read_text().splitlines()]

    def destructive_calls(self) -> list[dict[str, object]]:
        return [
            call
            for call in self.calls()
            if any(arg in {"rm", "remove", "stop", "kill", "down", "prune"} for arg in call["args"])
            and "inspect" not in call["args"]
        ]

    def run_guard(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.save_case()
        return subprocess.run(
            ["docker", *args],
            env=self.environment() if env is None else env,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def run_shell(
        self, command: str, role: str = "worker", *, binding: str = "head"
    ) -> subprocess.CompletedProcess[str]:
        self.save_case()
        with mock.patch.dict(os.environ, self.base, clear=True):
            wrapped = wrap_role_command(role, command, workspace=str(self.workspace), binding=binding)
        return subprocess.run(
            ["/bin/sh", "-c", wrapped],
            env=self.base,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("docker-guard:", result.stderr)
        self.assertEqual(self.destructive_calls(), [], self.calls())

    def test_safe_batch_uses_full_ids_and_preserves_native_status(self) -> None:
        self.case["mutation_status"] = 19
        result = self.run_guard("rm", "safe", "--force=false", "--volumes", "other")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(result.stdout, "native mutation\n")
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(
            self.calls()[-1]["args"],
            [
                "--host",
                "unix:///fake/default.sock",
                "container",
                "rm",
                "--force=false",
                "--volumes",
                "--",
                SAFE_ID,
                OTHER_ID,
            ],
        )

    def test_protected_labels_refuse_the_entire_mixed_batch(self) -> None:
        for labels in (
            None,
            {},
            [],
            {TEST_BOARD_LABEL: ""},
            {TEST_BOARD_LABEL: "0"},
            {TEST_BOARD_LABEL: "-1"},
            {TEST_BOARD_LABEL: "1.0"},
            {TEST_BOARD_LABEL: " 1"},
            {TEST_BOARD_LABEL: "01"},
            {TEST_BOARD_LABEL: "١"},
            {TEST_BOARD_LABEL: 123},
            {"com.docker.compose.project": "test", "image": "postgres:16"},
            {PRODUCTION_BOARD_LABEL: "true"},
            {TEST_BOARD_LABEL: "123", PRODUCTION_BOARD_LABEL: "true"},
            {TEST_BOARD_LABEL: "123", PRODUCTION_BOARD_LABEL: "false"},
        ):
            for operation in ("rm", "stop", "kill"):
                with self.subTest(labels=labels, operation=operation):
                    self.transcript.write_text("")
                    self.case["containers"]["protected"] = container(PROTECTED_ID, labels)
                    self.assert_refused(self.run_guard(operation, "safe", "protected"))
                    self.assertEqual(len(self.calls()), 3)

    def test_container_remove_alias_refuses_protected_batches_through_both_roles(self) -> None:
        for role in ("worker", "reviewer"):
            for labels in (
                {},
                {TEST_BOARD_LABEL: "0"},
                {PRODUCTION_BOARD_LABEL: "true"},
                {TEST_BOARD_LABEL: "123", PRODUCTION_BOARD_LABEL: "true"},
            ):
                for targets in ("protected", "safe protected"):
                    with self.subTest(role=role, labels=labels, targets=targets):
                        self.transcript.write_text("")
                        self.case["containers"]["protected"] = container(PROTECTED_ID, labels)
                        result = self.run_shell(
                            "docker --context=remote container remove -f " + targets, role
                        )
                        self.assert_refused(result)
                        calls = self.calls()
                        self.assertEqual(len(calls), 1 + len(targets.split()))
                        self.assertEqual(calls[0]["args"], ["--context", "remote", "context", "inspect"])
                        self.assertEqual([call["args"][-1] for call in calls[1:]], targets.split())
                        self.assertEqual(
                            [call["host"] for call in calls], ["unix:///fake/remote.sock"] * len(calls)
                        )

    def test_container_remove_alias_pins_endpoint_and_substitutes_full_ids(self) -> None:
        self.case.update({"reuse": True, "mutation_status": 19})
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                self.transcript.write_text("")
                result = self.run_shell(
                    "docker --context=initial container --log-level=debug remove -f --volumes -- safe other",
                    role,
                )
                self.assertEqual(result.returncode, 19, result.stderr)
                self.assertEqual(result.stdout, "native mutation\n")
                calls = self.calls()
                self.assertEqual(len(calls), 4)
                self.assertEqual([call["host"] for call in calls], ["unix:///fake/initial.sock"] * 4)
                self.assertEqual([call["args"][-1] for call in calls[1:3]], ["safe", "other"])
                self.assertEqual(len(self.destructive_calls()), 1)
                self.assertEqual(
                    calls[-1]["args"],
                    [
                        "--log-level",
                        "debug",
                        "--host",
                        "unix:///fake/initial.sock",
                        "container",
                        "rm",
                        "-f",
                        "--volumes",
                        "--",
                        SAFE_ID,
                        OTHER_ID,
                    ],
                )
                # The fake reuses both names after inspection; execution still names their old IDs.
                changed = json.loads(self.case_file.read_text())
                self.assertEqual(changed["containers"]["safe"], changed["containers"]["protected"])
                self.assertEqual(changed["containers"]["other"], changed["containers"]["protected"])

    def test_rm_local_link_spellings_never_become_global_log_levels(self) -> None:
        for role in ("worker", "reviewer"):
            for command in ("rm", "container rm", "container remove"):
                for arguments in (
                    "-l debug safe",
                    "-l=debug safe",
                    "-ldebug safe",
                    "-l safe",
                    "-l=true safe",
                    "-lfalse safe",
                    "--link safe",
                    "--link=false safe",
                    "safe -ldebug",
                ):
                    with self.subTest(role=role, command=command, arguments=arguments):
                        self.transcript.write_text("")
                        result = self.run_shell("docker " + command + " " + arguments, role)
                        self.assert_refused(result)
                        self.assertIn("link removal is unsupported", result.stderr)
                        self.assertEqual(
                            self.calls(), [], "reject local link syntax before native inspection"
                        )

    def test_unknown_failed_ambiguous_and_malformed_inspections(self) -> None:
        cases = {
            "unknown": None,
            "failed": None,
            "invalid-json": None,
            "empty": [],
            "ambiguous": container(SAFE_ID, {TEST_BOARD_LABEL: "1"}) * 2,
            "wrong-type": {},
            "null": [None],
            "no-config": [{"Id": SAFE_ID}],
            "short-id": container("a" * 12, {TEST_BOARD_LABEL: "1"}),
            "no-id": [{"Config": {"Labels": {TEST_BOARD_LABEL: "1"}}}],
        }
        for target, response in cases.items():
            with self.subTest(target=target):
                self.transcript.write_text("")
                if response is not None:
                    self.case["containers"][target] = response
                self.assert_refused(self.run_guard("rm", "safe", target))

    def test_name_reuse_and_current_context_change_cannot_redirect_execution(self) -> None:
        self.case.update({"reuse": True, "current": "initial"})
        result = self.run_guard("rm", "safe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["args"][-1], SAFE_ID)
        self.assertEqual([call["host"] for call in self.calls()], ["unix:///fake/initial.sock"] * 3)

    def test_aliases_options_boundaries_and_global_placements(self) -> None:
        vectors = (
            (["container", "rm", "-f", "--", "safe"], ["-f"]),
            (["-l", "debug", "rm", "-f", "-v", "--", "safe"], ["-f", "-v"]),
            (["-ldebug", "container", "remove", "--force=false", "safe"], ["--force=false"]),
            (["container", "-l=debug", "remove", "--", "safe"], []),
            (["rm", "safe", "--log-level=debug"], []),
            (["container", "remove", "--log-level", "debug", "safe"], []),
            (["--debug", "container", "--context=remote", "stop", "-t", "-1", "safe"], ["-t", "-1"]),
            (["kill", "safe", "--signal=HUP", "-Htcp://example:2375"], ["--signal", "HUP"]),
            (["container", "kill", "-sTERM", "safe", "--context", "remote"], ["-s", "TERM"]),
            (["stop", "--time", "8", "--signal", "SIGTERM", "safe"], ["--time", "8", "--signal", "SIGTERM"]),
        )
        for args, options in vectors:
            with self.subTest(args=args):
                self.transcript.write_text("")
                result = self.run_guard(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                mutation = self.calls()[-1]["args"]
                self.assertEqual(mutation[-len(options) - 2 :], [*options, "--", SAFE_ID])
                self.assertEqual(self.calls()[-2]["host"], self.calls()[-1]["host"])

    def test_endpoint_flags_environment_and_config_share_one_pinned_host(self) -> None:
        cases = (
            ({}, [], "unix:///fake/default.sock"),
            ({"DOCKER_HOST": "tcp://inherited:2375"}, [], "tcp://inherited:2375"),
            ({"DOCKER_CONTEXT": "inherited"}, [], "unix:///fake/inherited.sock"),
            (
                {"DOCKER_HOST": "tcp://inherited:2375", "DOCKER_CONTEXT": "inherited"},
                ["--context", "explicit"],
                "unix:///fake/explicit.sock",
            ),
            ({"DOCKER_CONTEXT": "inherited"}, ["--host=tcp://explicit:2375"], "tcp://explicit:2375"),
            ({}, ["--config", "/fake/config", "--context", "remote"], "unix:///fake/remote.sock"),
            (
                {"DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/fake/certs"},
                ["--host", "tcp://tls:2376", "--tlsverify", "--tlscacert", "/fake/ca.pem"],
                "tcp://tls:2376",
            ),
        )
        for extra, options, host in cases:
            with self.subTest(extra=extra, options=options):
                self.transcript.write_text("")
                env = {**self.environment(), **extra}
                result = self.run_guard(*options, "rm", "safe", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call["host"] for call in self.calls()], [host] * 3)
                for call in self.calls()[1:]:
                    self.assertNotIn("DOCKER_CONTEXT", call["docker_env"])
                    self.assertNotIn("DOCKER_HOST", call["docker_env"])
                self.assertEqual(self.calls()[1]["args"][:-4], self.calls()[2]["args"][:-4])

    def test_unresolved_endpoint_and_unsupported_tls_context_fail_closed(self) -> None:
        for changes in (
            {"context_failure": True},
            {"context_response": []},
            {"context_response": [None]},
            {"context_response": [{"Name": "default", "Endpoints": {"docker": {"Host": ""}}}]},
            {"current": "tls", "tls": {"docker": ["ca.pem"]}},
            {"current": "tls", "skip_tls": True},
        ):
            with self.subTest(changes=changes):
                self.transcript.write_text("")
                old = dict(self.case)
                self.case.update(changes)
                self.assert_refused(self.run_guard("rm", "safe"))
                self.assertEqual(len(self.calls()), 1)
                self.case = old
        self.transcript.write_text("")
        self.assert_refused(self.run_guard("--host", "tcp://one:2375", "--context", "two", "rm", "safe"))

    def test_compose_and_prune_variants_refuse_before_native_calls(self) -> None:
        vectors = []
        for operation in ("down", "rm", "stop", "kill"):
            vectors += [
                ["compose", operation, "--help"],
                [
                    "--context",
                    "remote",
                    "compose",
                    "-f",
                    "compose.yml",
                    "--project-name=tests",
                    operation,
                    "-f",
                ],
                ["compose", "--profile", "test", "--dry-run", operation],
            ]
        for group in ("container", "system", "volume", "image", "builder", "buildx", "network"):
            vectors += [
                [group, "prune", "--force", "--all", "--filter", "label=" + TEST_BOARD_LABEL],
                ["--host", "tcp://remote:2375", group, "prune", "-af"],
            ]
        vectors += [
            ["buildx", "--builder", "tests", "prune", "--filter=until=24h"],
            ["system", "--context", "remote", "prune", "-f"],
            ["--unknown", "system", "prune", "--force"],
            ["compose", "--unknown", "down"],
        ]
        for args in vectors:
            with self.subTest(args=args):
                self.transcript.write_text("")
                self.assert_refused(self.run_guard(*args))
                self.assertEqual(self.calls(), [])

    def test_uncertain_direct_syntax_fails_before_inspection(self) -> None:
        for args in (
            ["rm"],
            ["rm", "--all"],
            ["rm", "--link", "safe"],
            ["rm", "-fv", "safe"],
            ["rm", "--force=maybe", "safe"],
            ["stop", "--timeout"],
            ["kill", "--signal=", "safe"],
            ["rm", "--", "--context", "safe"],
            ["--unknown", "rm", "safe"],
            ["--context"],
            ["rm", "safe", "--unknown"],
        ):
            with self.subTest(args=args):
                self.transcript.write_text("")
                self.assert_refused(self.run_guard(*args))
                self.assertEqual(self.calls(), [])

    def test_read_only_commands_keep_arguments_output_and_exit_status(self) -> None:
        self.case["read_status"] = 23
        for args in (
            [],
            ["version"],
            ["--version"],
            ["--context=remote", "ps", "--all"],
            ["inspect", "prune"],
            ["container", "inspect", "safe"],
            ["compose", "--file", "down", "config"],
            ["compose", "config", "--services"],
        ):
            with self.subTest(args=args):
                self.transcript.write_text("")
                # The native fixture's container inspect branch is deliberately a JSON response.
                expected_status = 0 if args[:2] == ["container", "inspect"] else 23
                result = self.run_guard(*args)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(len(self.calls()), 1)
                self.assertEqual(self.calls()[0]["args"], args)
                if expected_status:
                    self.assertEqual(result.stdout, "native stdout\n")
                    self.assertEqual(result.stderr, "native stderr\n")

    def test_image_selector_expansion_is_refused_as_a_batch(self) -> None:
        result = self.run_shell("docker rm -f $(docker ps -aq --filter ancestor=postgres:16)")
        self.assert_refused(result)
        self.assertEqual(len(self.calls()), 4)

    def test_both_roles_and_both_shared_bindings_execute_guarded_bare_docker(self) -> None:
        for role in ("worker", "reviewer"):
            for binding in ("head", "standing"):
                with self.subTest(role=role, binding=binding):
                    self.transcript.write_text("")
                    result = self.run_shell("docker kill protected", role, binding=binding)
                    self.assert_refused(result)
                    self.assertEqual(len(self.calls()), 2)
                    env = self.environment(role)
                    self.assertEqual(env["PATH"].split(os.pathsep)[0], str(role_env_path(self.product)))

    def test_ci_checkout_without_a_managed_venv_uses_the_named_product_fixture(self) -> None:
        # CI installs into its selected interpreter, not <checkout>/.venv. These probes use the
        # same fixture as the dispatcher integration tests and exercise the actual launch boundary.
        parent = self.root / "ci-fixture"
        parent.mkdir()
        product_env = guarded_product_env(parent)
        runtime = parent / "runtime.env"
        runtime.write_text("ANTHROPIC_MODEL=opus\nSECRETARY_INSTANCE=/decoy\n")
        home = parent / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"model": "sonnet"}))
        self.base.update(
            {
                **product_env,
                "HOME": str(home),
                "CLAUDE_MANAGED_SETTINGS": str(parent / "no-managed.json"),
                "SECRETARY_RUNTIME_ENV_FILE": str(runtime),
                "SECRETARY_INSTANCE": str(parent / "selected-instance"),
                "ANTHROPIC_MODEL": "opus",
            }
        )
        probe = (
            "id -u >/dev/null && printenv TA_SECRETARY_REPO SECRETARY_INSTANCE BOARD_ROLE"
            ' && command -v python3 && test -z "${ANTHROPIC_MODEL:-}"'
        )
        snapshot = (
            "import json; from secretary.dispatch.launcher import claude_launch_model; "
            "print(json.dumps(claude_launch_model({'adapter': 'claude'})))"
        )
        probe += (
            f" && PYTHONPATH={shlex.quote(str(Path(product_env['TA_SECRETARY_REPO']) / 'src'))} "
            f"python3 -P -c {shlex.quote(snapshot)}"
        )
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                result = self.run_shell(probe, role)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        product_env["TA_SECRETARY_REPO"],
                        str(parent / "selected-instance"),
                        role,
                        str(self.workspace / role_env.WORKSPACE_ENV_DIR / "bin/python3"),
                        '["sonnet", "user_settings"]',
                    ],
                )
                self.assertFalse((parent / "docker-calls").exists())
                self.assertEqual(self.calls(), [])

        # Exposing the two launch utilities must not reopen host Docker resolution on failure.
        (parent / "native-bin/docker").chmod(0o644)
        for role in ("worker", "reviewer"):
            with self.subTest(missing_backend=role):
                result = self.run_shell("docker rm safe", role)
                self.assertEqual(result.returncode, 125, result.stderr)
                self.assertIn("native Docker backend is unavailable", result.stderr)
                self.assertFalse((parent / "docker-calls").exists())
                self.assertEqual(self.calls(), [])

    def test_login_profile_reset_preserves_guard_workspace_tools_and_product_binding(self) -> None:
        # bash reads this profile during -lc. It actively removes every launcher PATH prefix.
        home = self.root / "home"
        home.mkdir()
        marker = self.root / "profile-ran"
        (home / ".bash_profile").write_text(
            f"PATH=/usr/bin:/bin\nexport PATH\n: > {shlex.quote(str(marker))}\n"
        )
        self.base["HOME"] = str(home)
        shadow = self.workspace / "secretary" / "runtime"
        shadow.mkdir(parents=True)
        (shadow.parent / "__init__.py").write_text("")
        (shadow / "__init__.py").write_text("")
        (shadow / "docker_guard.py").write_text("raise RuntimeError('candidate shadow imported')\n")
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                self.transcript.write_text("")
                with mock.patch.dict(os.environ, self.base, clear=True):
                    env = self.environment(role)
                    # Use the actual shared role-env executable boundary and an actual login shell.
                    restored = role_env.role_shell_command(
                        role,
                        "command -v docker; command -v python3; ruff --version; docker stop protected",
                        workspace=self.workspace,
                    )
                self.save_case()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-P",
                        "-m",
                        role_env.ENTRY_POINT,
                        "exec",
                        "--role",
                        role,
                        "--workspace",
                        str(self.workspace),
                        "--",
                        "/bin/bash",
                        "-lc",
                        restored,
                    ],
                    env={**self.base, "PYTHONPATH": str(self.product / "src")},
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                self.assert_refused(result)
                self.assertTrue(marker.exists(), "login profile must actually run")
                self.assertEqual(
                    result.stdout.splitlines(),
                    [
                        str(role_env_path(self.product) / "docker"),
                        str(self.workspace / role_env.WORKSPACE_ENV_DIR / "bin" / "python3"),
                        "workspace ruff",
                    ],
                )
                self.assertEqual(env[docker_guard.PYTHON_ENV], str(self.product / ".venv/bin/python3"))

    def test_bindings_are_recomputed_and_backend_resolution_does_not_recurse(self) -> None:
        self.base.update({name: "/forged" for name in docker_guard.BINDINGS})
        self.base["PATH"] = str(role_env_path(self.product)) + os.pathsep + self.base["PATH"]
        env = self.environment()
        self.assertEqual(env[docker_guard.BACKEND_ENV], str(self.native))
        self.assertEqual(env[docker_guard.SOURCE_ENV], str(self.product / "src"))
        result = self.run_guard("rm", "safe", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 3)

    def test_missing_backend_runtime_and_recursive_backend_never_fall_through(self) -> None:
        for name, value in (
            (docker_guard.BACKEND_ENV, ""),
            (docker_guard.BACKEND_ENV, "/does-not-exist"),
            (docker_guard.BACKEND_ENV, str(role_env_path(self.product) / "docker")),
            (docker_guard.PYTHON_ENV, "/does-not-exist"),
            (docker_guard.SOURCE_ENV, str(self.workspace)),
        ):
            with self.subTest(name=name, value=value):
                self.assert_refused(self.run_guard("rm", "safe", env={**self.environment(), name: value}))
                self.assertEqual(self.calls(), [])
        self.native.chmod(0o644)
        # Do not permit resolution to fall back to a host Docker executable in this fault test.
        self.base["PATH"] = str(self.native.parent) + os.pathsep + str(self.product / ".venv/bin")
        result = self.run_shell("docker rm safe")
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("native Docker backend is unavailable", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_or_unexecutable_guard_refuses_the_actual_role_launch(self) -> None:
        broken = self.root / "broken"
        guard = role_env_path(broken) / "docker"
        guard.parent.mkdir(parents=True)
        (broken / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
        self.base["TA_SECRETARY_REPO"] = str(broken)
        for state in ("missing", "unexecutable"):
            if state == "unexecutable":
                guard.write_text("#!/bin/sh\nexit 0\n")
                guard.chmod(0o644)
            for role in ("worker", "reviewer"):
                with self.subTest(state=state, role=role):
                    with mock.patch.dict(os.environ, self.base, clear=True):
                        restored = role_env.role_shell_command(
                            role, "docker rm safe", workspace=self.workspace
                        )
                    # Import the trusted test product while selecting a broken installed product.
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-P",
                            "-m",
                            role_env.ENTRY_POINT,
                            "exec",
                            "--role",
                            role,
                            "--workspace",
                            str(self.workspace),
                            "--",
                            "/bin/sh",
                            "-lc",
                            restored,
                        ],
                        env={**self.base, "PYTHONPATH": str(self.product / "src")},
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 125, result.stderr)
                    self.assertIn("Docker guard is unavailable", result.stderr)
                    self.assertEqual(self.calls(), [])

    def test_unrelated_roles_keep_native_docker_and_strip_guard_bindings(self) -> None:
        for role in ("pipeline", "observer", "steward", "retro", "curator"):
            with self.subTest(role=role):
                self.base.update({name: "/forged" for name in docker_guard.BINDINGS})
                env = self.environment(role)
                self.assertNotIn(str(role_env_path(self.product)), env["PATH"])
                self.assertTrue(all(name not in env for name in docker_guard.BINDINGS))
                self.save_case()
                result = subprocess.run(
                    ["docker", "version"], env=env, capture_output=True, text=True, check=False, timeout=15
                )
                self.assertEqual(result.stdout, "native stdout\n")
                self.assertEqual(result.returncode, 0, result.stderr)


def role_env_path(product: Path) -> Path:
    return product / "src/secretary/runtime/docker-bin"


if __name__ == "__main__":
    unittest.main()
