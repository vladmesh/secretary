from __future__ import annotations

from pathlib import Path

# A pid that is real but not this process: `kill(pid, 0)` raises, so the watchdog reads the head as
# dead. Pid 2 is the kernel's kthreadd on Linux, hence a live pid nobody can be launched as; 999999
# is above the default pid_max and is reliably free.
DEAD_PID = 999999

# What the host raises when Orca refuses `terminal wait`. The bodies are the live CLI's: it exits
# non-zero for a condition it could not satisfy as well as for a failure, printing the answer as
# JSON on stdout, and the host carries that text into the failure it raises. Reading it is what
# tells a busy pane from a probe that was never answered.
TIMEOUT_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "0b1ba8ed",\n  "ok": false,\n  "error": {\n'
    '    "code": "timeout",\n    "message": "timeout"\n  }\n}'
)
# The live CLI exits non-zero for a pane it has looked at and found busy, printing an `ok: true`
# body with `satisfied: false`. Captured from the production audit log, `observer_launch_deferred`
# event `evt_24fb1640c4ea4a998f9f80e060d722fb` on `sprint:879`.
BLOCKED_PANE_WAIT_BODY = (
    '{\n  "id": "c5bb8352-65ff-4f8a-bd3a-fb0cdb97655c",\n  "ok": true,\n  "result": {\n'
    '    "wait": {\n      "handle": "term_c0755f85",\n      "condition": "tui-idle",\n'
    '      "satisfied": false,\n      "status": "running",\n      "exitCode": null,\n'
    '      "blockedReason": "codex-update-prompt"\n    }\n  }\n}'
)
STALE_HANDLE_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "7ea4ada1",\n  "ok": false,\n  "error": {\n'
    '    "code": "terminal_handle_stale",\n    "message": "terminal_handle_stale"\n  }\n}'
)


def install_skill_registry(root: Path, *, delivered: bool = True) -> Path:
    """A role-skill registry of this test's own, pointed at by SECRETARY_ROLE_SKILLS_MANIFEST.

    The launch gate reads the shell's skill directory, and the shells of the live installation are
    not a fixture: a test that let the tick look at them would pass or fail on whether somebody had
    run `role-skills sync` on this machine. The empty instance beside it does the same job for the
    other half of the registry: the overlay of the live installation is not a fixture either.
    Returns the observer skill's path in the fake shell, which `delivered=False` leaves absent.
    """
    manifest = root / "registry" / "manifest.toml"
    shell_root = root / "registry" / "codex-shell"
    claude_root = root / "registry" / "claude-shell"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        '[roles.observer]\nskills = ["observe-sprint"]\n\n'
        '[targets.codex-test]\nshell = "codex"\n'
        f'root = "{shell_root}"\nroles = ["observer"]\n\n'
        '[targets.claude-test]\nshell = "claude"\n'
        f'root = "{claude_root}"\nroles = ["observer"]\n',
        encoding="utf-8",
    )
    (root / "registry" / "instance").mkdir(parents=True, exist_ok=True)
    skill = shell_root / "observe-sprint" / "SKILL.md"
    if delivered:
        for target in (skill, claude_root / "observe-sprint" / "SKILL.md"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# canonical observer skill\n", encoding="utf-8")
    return skill

__all__ = [
    "BLOCKED_PANE_WAIT_BODY", "DEAD_PID", "STALE_HANDLE_WAIT_FAILURE", "TIMEOUT_WAIT_FAILURE",
    "install_skill_registry",
]
