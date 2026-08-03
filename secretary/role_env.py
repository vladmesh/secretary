"""Role-scoped runtime env for dispatcher-launched heads."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

from triggered_agents.runtime.paths import default_instance_path

RUNTIME_ENV_FILE_ENV = "SECRETARY_RUNTIME_ENV_FILE"
RUNTIME_ENV_DEFAULT = str(default_instance_path() / "runtime.env")
RUNTIME_ENV = Path(os.environ.get(RUNTIME_ENV_FILE_ENV, RUNTIME_ENV_DEFAULT))


def runtime_env_path() -> Path:
    """Where the runtime env file lives, resolved per call.

    The file itself is read on every `runtime_env()`, so the override that names it is read the
    same way rather than frozen at import: an in-process caller that has to model what a launched
    head will receive sees the same file the launched process would open.
    """
    return Path(os.environ.get(RUNTIME_ENV_FILE_ENV, RUNTIME_ENV_DEFAULT))
BOARD_ENV = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")
# SECRETARY_DATA_DIR is carried for the same reason as in triggered_agents.runtime.role_env: it
# binds a process to the installation's data plane, and a head that reports through
# `secretary task` must land on the data dir the dispatcher itself uses.
NONSECRET_ENV = ("SECRETARY_INSTANCE", "SECRETARY_DATA_DIR", "TA_SECRETARY_REPO")
# Names the launcher binds and the runtime env file may not take back. Which installation a role
# belongs to is decided by whoever started it (the rendered unit, or a dispatcher launching a
# head), and runtime.env is a file inside an installation. Letting its copy of the name win lets a
# stale or copied line under one instance route a role to another one: a unit rendered for a
# non-default instance launched heads that came up on ~/secretary-instance (secretary-859 review).
#
# The checkout is bound the same way and for the same reason. An upgrade from an alternate product
# root renders the dispatcher unit against that root; a head it launches has to import the product
# the installation was moved onto, not whatever ~/secretary still is or what a runtime.env written
# before the move says.
#
# Which sprint an observer head belongs to is bound the same way. The launcher reads it off the
# record it is bringing the head up for, and the head proves its identity with it on every write.
# A runtime.env that could name another sprint would be a head able to name itself the observer of
# somebody else's sprint, which is the whole thing the binding exists to refuse.
OBSERVER_SPRINT_ENV = "SECRETARY_OBSERVER_SPRINT"
OBSERVER_GENERATION_ENV = "SECRETARY_OBSERVER_GENERATION"
UNIT_BOUND_ENV = (
    "SECRETARY_INSTANCE",
    "TA_SECRETARY_REPO",
    OBSERVER_SPRINT_ENV,
    OBSERVER_GENERATION_ENV,
)
# Of the bound names, the ones only a launcher may supply: they name the caller rather than the
# installation, so `runtime.env` cannot fill them in when the launcher passed none.
LAUNCHER_ONLY_ENV = (OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV)
ROLE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "worker": (*BOARD_ENV, *NONSECRET_ENV),
    "reviewer": (*BOARD_ENV, *NONSECRET_ENV),
    # The sprint observer is a dispatcher-launched head like the other two: it reads the sprint
    # entity and its cards off the board and gets nothing else out of runtime.env. The two names
    # beyond that are its identity, bound by the launcher rather than read from the file.
    "observer": (*BOARD_ENV, *NONSECRET_ENV, OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV),
}
ROLE_REQUIRED: dict[str, tuple[str, ...]] = {
    "worker": BOARD_ENV,
    "reviewer": BOARD_ENV,
    "observer": BOARD_ENV,
}
BOARD_ROLES = {"worker", "reviewer", "observer"}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_NAME_RE = re.compile(
    r"(^|_)(TOKEN|PASSWORD|PASSWD|SECRET|PAT|KEY|IDENTITY|CREDENTIAL)(_|$)",
    re.IGNORECASE,
)


class RoleEnvError(RuntimeError):
    pass


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
    env_path = Path(path) if path is not None else runtime_env_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed is not None:
            key, value = parsed
            out[key] = value
    return out


def _allowlist(role: str) -> tuple[str, ...]:
    try:
        return ROLE_ALLOWLIST[role]
    except KeyError as exc:
        known = ", ".join(sorted(ROLE_ALLOWLIST))
        raise RoleEnvError(f"unknown runtime role {role!r} (known: {known})") from exc


def _is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME_RE.search(name))


def runtime_env(
    role: str,
    *,
    base_env: dict[str, str] | None = None,
    env_file: Path | str | None = None,
    require: bool = False,
) -> dict[str, str]:
    allowed = set(_allowlist(role))
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
        if key in base and key in UNIT_BOUND_ENV:
            env[key] = base[key]
        elif key in LAUNCHER_ONLY_ENV:
            # The installation binding may fall back to the file, because a head that was not
            # told which installation it belongs to still belongs to the one whose file it reads.
            # An identity has no such fallback: a head the launcher named no sprint for is a head
            # that cannot prove one, and a line in the file must not be able to supply it.
            env.pop(key, None)
        elif key in source:
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
            raise RoleEnvError(
                f"runtime env for role {role!r} missing {', '.join(missing)}"
            )
    return env


def observer_binding(sprint: str, generation: str) -> dict[str, str]:
    """The identity a launcher renders into one observer head's command line.

    Both names or neither. The generation is the record's own token, carried so the head's
    environment says which lifecycle of that reference launched it; the write path reads it as a
    presence flag only, and does not compare it to the record a sprint currently has.
    """
    sprint = str(sprint or "").strip()
    generation = str(generation or "").strip()
    if not sprint or not generation:
        return {}
    return {OBSERVER_SPRINT_ENV: sprint, OBSERVER_GENERATION_ENV: generation}


def declared_observer_sprint(env: dict[str, str] | None = None) -> str:
    """The sprint this process was launched to observe, or an empty string.

    Empty is not a privilege. A caller that cannot name its sprint cannot prove which one it is,
    and the writes of role `observer` refuse on that. Half a binding is no binding: the launcher
    renders both names together, so a sprint arriving without its generation did not come from
    one and is not read as an identity.
    """
    source = os.environ if env is None else env
    sprint = str(source.get(OBSERVER_SPRINT_ENV, "") or "").strip()
    generation = str(source.get(OBSERVER_GENERATION_ENV, "") or "").strip()
    return sprint if generation else ""


def _main_exec(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m secretary.role_env exec")
    parser.add_argument("--role", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")
    try:
        env = runtime_env(args.role, env_file=args.env_file, require=True)
    except RoleEnvError as exc:
        print(f"role-env: {exc}", file=sys.stderr)
        return 125
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(f"role-env: exec {command[0]!r} failed: {exc}", file=sys.stderr)
        return 126


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "exec":
        return _main_exec(rest)
    print(f"role-env: unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
