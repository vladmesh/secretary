"""Role-scoped runtime env for secretary automation launch boundaries.

The source of host secrets is an instance-owned runtime env file. Launchers must not
inherit it wholesale: each role gets only the names declared here, and sensitive names outside the
role allowlist are stripped even if the parent process already had them.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

RUNTIME_ENV = Path(
    os.environ.get("TA_RUNTIME_ENV_FILE", "/home/dev/secretary-instance/runtime.env")
)
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PYTHONPATH = os.environ.get("TA_RUNTIME_PYTHONPATH", str(REPO_ROOT))

BOARD_ENV = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")
# SECRETARY_DATA_DIR names the installation's data plane, not a secret. It has to survive the
# allowlist: the production dispatcher unit imports runtime.env wholesale, so a host that moves its
# data dir through that file moves the WRITER. A role stripped of the same name would fall back to
# instance.yaml and read a production-state.json nobody writes, calling that silence healthy
# (secretary-833 review, round 3).
NONSECRET_ENV = ("SECRETARY_INSTANCE", "SECRETARY_DATA_DIR", "TA_SECRETARY_REPO")

ROLE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "pipeline": (*BOARD_ENV, *NONSECRET_ENV),
    "worker": (*BOARD_ENV, *NONSECRET_ENV),
    "reviewer": (*BOARD_ENV, *NONSECRET_ENV),
    "steward": (*BOARD_ENV, *NONSECRET_ENV),
    "retro": (*BOARD_ENV, *NONSECRET_ENV),
    "curator": NONSECRET_ENV,
}

ROLE_REQUIRED: dict[str, tuple[str, ...]] = {
    "pipeline": BOARD_ENV,
    "worker": BOARD_ENV,
    "reviewer": BOARD_ENV,
    "steward": BOARD_ENV,
    "retro": BOARD_ENV,
    "curator": (),
}

BOARD_ROLES = {"po", "dispatcher", "worker", "reviewer", "steward", "retro"}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_NAME_RE = re.compile(
    r"(^|_)(TOKEN|PASSWORD|PASSWD|SECRET|PAT|KEY|IDENTITY|CREDENTIAL)(_|$)", re.IGNORECASE
)


class RoleEnvError(RuntimeError):
    """The role runtime env cannot be built without leaking or missing required names."""


def _parse_assignment(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if not _KEY_RE.match(key):
        return None
    try:
        parts = shlex.split(f"x={raw_value}", comments=True, posix=True)
    except ValueError:
        value = raw_value.strip().strip("'\"")
    else:
        value = parts[0].split("=", 1)[1] if parts else ""
    return key, value


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Read simple KEY=value lines from the control-panel env file without logging values."""
    env_path = Path(path) if path is not None else RUNTIME_ENV
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        item = _parse_assignment(line)
        if item is not None:
            key, value = item
            out[key] = value
    return out


def allowlist(role: str) -> tuple[str, ...]:
    try:
        return ROLE_ALLOWLIST[role]
    except KeyError as e:
        known = ", ".join(sorted(ROLE_ALLOWLIST))
        raise RoleEnvError(f"unknown runtime role {role!r} (known: {known})") from e


def _is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME_RE.search(name))


def runtime_env(role: str, *, base_env: dict[str, str] | None = None,
                env_file: Path | str | None = None, require: bool = False) -> dict[str, str]:
    """Return a sanitized env for `role`, with role-allowed values overlaid from the source file."""
    allowed = set(allowlist(role))
    required = ROLE_REQUIRED.get(role, ())
    source = load_env_file(env_file)
    base = dict(os.environ if base_env is None else base_env)

    env: dict[str, str] = {}
    for key, value in base.items():
        if key in source and key not in allowed:
            continue
        if _is_sensitive_name(key) and key not in allowed:
            continue
        env[key] = value

    for key in allowed:
        if key in source:
            env[key] = source[key]
        elif key in base:
            env[key] = base[key]

    if role in BOARD_ROLES:
        env["BOARD_ROLE"] = role
    else:
        env.pop("BOARD_ROLE", None)
    if require:
        missing = [key for key in required if not env.get(key)]
        if missing:
            names = ", ".join(missing)
            raise RoleEnvError(
                f"runtime env for role {role!r} missing {names}; check provisioning/launcher"
            )
    return env


def apply_runtime_env(role: str, *, env_file: Path | str | None = None, require: bool = True) -> None:
    env = runtime_env(role, env_file=env_file, require=require)
    os.environ.clear()
    os.environ.update(env)


def wrap_shell_command(role: str, command: str, *, pythonpath: str | None = None,
                       env_file: Path | str | None = None) -> str:
    """Shell command that execs `command` under the role env without putting secret values in argv."""
    py_path = pythonpath or RUNTIME_PYTHONPATH
    parts = [
        f"PYTHONPATH={shlex.quote(py_path)}",
        "python3",
        "-m",
        "triggered_agents.runtime.role_env",
        "exec",
        "--role",
        shlex.quote(role),
    ]
    if env_file is not None:
        parts += ["--env-file", shlex.quote(str(env_file))]
    parts += ["--", "/bin/sh", "-lc", shlex.quote(command)]
    return " ".join(parts)


def _main_exec(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m triggered_agents.runtime.role_env exec")
    parser.add_argument("--role", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    command = list(ns.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")
    try:
        env = runtime_env(ns.role, env_file=ns.env_file, require=True)
    except RoleEnvError as e:
        print(f"role-env: {e}", file=sys.stderr)
        return 125
    try:
        os.execvpe(command[0], command, env)
    except OSError as e:
        print(f"role-env: exec {command[0]!r} failed: {e}", file=sys.stderr)
        return 126


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "exec":
        return _main_exec(rest)
    print(f"role-env: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
