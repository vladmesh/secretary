from tests.test_dispatcher_observer import DEAD_PID, install_skill_registry

BLOCKED_PANE_WAIT_BODY = (
    '{\n  "id": "c5bb8352-65ff-4f8a-bd3a-fb0cdb97655c",\n  "ok": true,\n  "result": {\n'
    '    "wait": {\n      "handle": "term_c0755f85",\n      "condition": "tui-idle",\n'
    '      "satisfied": false,\n      "status": "running",\n      "exitCode": null,\n'
    '      "blockedReason": "codex-update-prompt"\n    }\n  }\n}'
)
TIMEOUT_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "0b1ba8ed",\n  "ok": false,\n  "error": {\n'
    '    "code": "timeout",\n    "message": "timeout"\n  }\n}'
)
STALE_HANDLE_WAIT_FAILURE = (
    "orca terminal wait --terminal observer:sprint:1 --for tui-idle --timeout-ms 6000 --json "
    'failed: {\n  "id": "7ea4ada1",\n  "ok": false,\n  "error": {\n'
    '    "code": "terminal_handle_stale",\n    "message": "terminal_handle_stale"\n  }\n}'
)

__all__ = ["BLOCKED_PANE_WAIT_BODY", "DEAD_PID", "STALE_HANDLE_WAIT_FAILURE", "TIMEOUT_WAIT_FAILURE", "install_skill_registry"]
