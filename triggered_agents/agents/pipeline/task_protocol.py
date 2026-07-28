"""Worker-side bridge to the Phase 5 ``secretary task`` write protocol."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from ...runtime.paths import PRODUCT_DIRNAME, configured_product_root


ROLLBACK_ENV = "TA_WORKER_LEGACY_BOARD_WRITES"
SECRETARY_REPO_ENV = "TA_SECRETARY_REPO"
# The same fallback written as a shell expression: the commands below are rendered into card
# comments and run by a head in its own shell, so the home has to be the one that head runs as
# rather than the one this process resolved.
SECRETARY_REPO_SHELL = f'"${{{SECRETARY_REPO_ENV}:-$HOME/{PRODUCT_DIRNAME}}}${{PYTHONPATH:+:$PYTHONPATH}}"'
_RUNTIME_CREDENTIALS = ("KANBOARD_URL", "KANBOARD_API_USER", "KANBOARD_API_TOKEN")


def use_legacy_path(environ: dict[str, str] | None = None) -> bool:
    """Whether the explicit temporary rollback routes worker writes to board-CLI."""
    env = os.environ if environ is None else environ
    return env.get(ROLLBACK_ENV) == "1"


def writer(environ: dict[str, str] | None = None) -> str:
    """The sole worker write path selected by the rollback switch."""
    return "legacy board-CLI" if use_legacy_path(environ) else "secretary task"


def command(action: str, reference: str, *, environ: dict[str, str] | None = None) -> str:
    """Render the selected worker comment or report command."""
    if action not in {"comment", "report"}:
        raise ValueError(f"unsupported worker write action: {action}")
    if use_legacy_path(environ):
        return f"python3 -m triggered_agents pipeline --role worker {action} --ref {reference}"
    return f"{command_prefix()} task {action} --ref {reference} --role worker"


def launch_instruction(environ: dict[str, str] | None = None) -> str:
    """Tell the worker which write path TASK.md has rendered."""
    return f"only comment and report through {writer(environ)}"


def secretary_repo(environ: dict[str, str] | None = None) -> Path:
    """The checkout this process imports the product from. Resolved per call, never at import:
    the fallback hangs off a home, and a caller passing an environment is asking about that one."""
    return configured_product_root(os.environ if environ is None else environ)


def pythonpath_prefix(environ: dict[str, str] | None = None) -> str:
    """The PYTHONPATH assignment that makes the provisioned secretary source importable.

    Left as a shell expression by default: these prefixes are rendered into card text a head runs
    later in its own shell, where its own ``TA_SECRETARY_REPO`` and home are the right answer.

    A caller that is building the launch command itself passes its environment and gets the
    checkout written out. That command carries ``TA_SECRETARY_REPO=<root>`` as its own assignment
    prefix, and whether an assignment in a simple command is visible to a later word of the same
    command is unspecified — so the launcher resolves the checkout instead of hoping the head's
    shell resolves it in the order it was written.
    """
    if environ is None:
        return f"PYTHONPATH={SECRETARY_REPO_SHELL}"
    return (
        f"PYTHONPATH={shlex.quote(str(secretary_repo(environ)))}"
        '"${PYTHONPATH:+:$PYTHONPATH}"'
    )


def command_prefix() -> str:
    """Shell prefix that makes the provisioned secretary source importable to a worker."""
    return f"{pythonpath_prefix()} python3 -m secretary"


def preflight(environ: dict[str, str] | None = None) -> tuple[bool, str]:
    """Check the worker's selected write path without exposing runtime values in errors."""
    env = dict(os.environ if environ is None else environ)
    missing = [name for name in _RUNTIME_CREDENTIALS if not env.get(name)]
    if missing:
        return False, f"{writer(env)} runtime configuration is unavailable (missing " + ", ".join(missing) + ")"
    if use_legacy_path(env):
        try:
            result = subprocess.run(
                ["python3", "-m", "triggered_agents", "pipeline", "--role", "worker", "report", "--help"],
                env=env, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False, "legacy board-CLI runtime is unavailable or incompatible"
        if result.returncode != 0 or "--kind" not in (result.stdout or ""):
            return False, "legacy board-CLI runtime is incompatible; it must support `report --role worker`"
        return True, "worker task protocol rollback enabled; using legacy board-CLI"
    repo = secretary_repo(env)
    if not (repo / "secretary" / "__main__.py").is_file():
        return False, "secretary task runtime is unavailable; configure TA_SECRETARY_REPO with a compatible checkout"
    env["PYTHONPATH"] = str(repo) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = subprocess.run(
            ["python3", "-m", "secretary", "task", "report", "--help"], cwd=repo,
            env=env, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "secretary task runtime is unavailable or incompatible"
    if result.returncode != 0 or "--role" not in (result.stdout or ""):
        return False, "secretary task runtime is incompatible; it must support `task report --role worker`"
    return True, "secretary task protocol ready"
