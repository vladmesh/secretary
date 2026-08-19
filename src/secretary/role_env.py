"""Secretary-facing entry point for the shared role-scoped runtime environment."""

from __future__ import annotations

from triggered_agents.runtime.role_env import (
    BOARD_ROLES,
    BOARD_TRANSPORT_ROLES,
    LAUNCH_BOUND_ENV,
    LAUNCHER_ONLY_ENV,
    NONSECRET_ENV,
    OBSERVER_GENERATION_ENV,
    OBSERVER_SPRINT_ENV,
    ROLE_ALLOWLIST,
    RUNTIME_ENV,
    RUNTIME_ENV_DEFAULT,
    RUNTIME_ENV_FILE_ENVS,
    SECRETARY_RUNTIME_ENV_FILE_ENV,
    SENSITIVE_ENV_NAME_RE,
    UNIT_BOUND_ENV,
    RoleEnvError,
    allowlist,
    declared_observer_sprint,
    is_sensitive_env_name,
    load_env_file,
    observer_binding,
    runtime_env,
    runtime_env_path,
)
from triggered_agents.runtime.role_env import (
    main as _shared_main,
)

__all__ = [
    "BOARD_ROLES",
    "BOARD_TRANSPORT_ROLES",
    "LAUNCHER_ONLY_ENV",
    "LAUNCH_BOUND_ENV",
    "NONSECRET_ENV",
    "OBSERVER_GENERATION_ENV",
    "OBSERVER_SPRINT_ENV",
    "ROLE_ALLOWLIST",
    "RUNTIME_ENV",
    "RUNTIME_ENV_DEFAULT",
    "RUNTIME_ENV_FILE_ENVS",
    "SECRETARY_RUNTIME_ENV_FILE_ENV",
    "SENSITIVE_ENV_NAME_RE",
    "UNIT_BOUND_ENV",
    "RoleEnvError",
    "allowlist",
    "declared_observer_sprint",
    "is_sensitive_env_name",
    "load_env_file",
    "main",
    "observer_binding",
    "runtime_env",
    "runtime_env_path",
]


def main(argv=None) -> int:
    """Run the secretary-facing role-env command."""
    return _shared_main(
        argv,
        prog="python3 -m secretary.role_env",
        description=__doc__,
    )


if __name__ == "__main__":
    raise SystemExit(main())
