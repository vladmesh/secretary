"""Secretary-facing entry point for the shared role-scoped runtime environment."""

from __future__ import annotations

from triggered_agents.runtime.role_env import (
    BOARD_ENV,
    BOARD_ROLES,
    LAUNCHER_ONLY_ENV,
    LAUNCH_BOUND_ENV,
    NONSECRET_ENV,
    OBSERVER_GENERATION_ENV,
    OBSERVER_SPRINT_ENV,
    ROLE_ALLOWLIST,
    ROLE_REQUIRED,
    RUNTIME_ENV,
    RUNTIME_ENV_DEFAULT,
    RUNTIME_ENV_FILE_ENV,
    RUNTIME_ENV_FILE_ENVS,
    SECRETARY_RUNTIME_ENV_FILE_ENV,
    UNIT_BOUND_ENV,
    RoleEnvError,
    allowlist,
    declared_observer_sprint,
    load_env_file,
    main,
    observer_binding,
    runtime_env,
    runtime_env_path,
)

__all__ = [
    "BOARD_ENV",
    "BOARD_ROLES",
    "LAUNCHER_ONLY_ENV",
    "LAUNCH_BOUND_ENV",
    "NONSECRET_ENV",
    "OBSERVER_GENERATION_ENV",
    "OBSERVER_SPRINT_ENV",
    "ROLE_ALLOWLIST",
    "ROLE_REQUIRED",
    "RUNTIME_ENV",
    "RUNTIME_ENV_DEFAULT",
    "RUNTIME_ENV_FILE_ENV",
    "RUNTIME_ENV_FILE_ENVS",
    "SECRETARY_RUNTIME_ENV_FILE_ENV",
    "UNIT_BOUND_ENV",
    "RoleEnvError",
    "allowlist",
    "declared_observer_sprint",
    "load_env_file",
    "main",
    "observer_binding",
    "runtime_env",
    "runtime_env_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
