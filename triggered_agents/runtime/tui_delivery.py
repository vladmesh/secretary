"""The product's one way to put a prompt in front of a live interactive head.

Every head that runs as a TUI — a pipeline worker, a reviewer, an observer, and the curator, retro
and steward service heads — is brought up with a command that carries no prompt, so something has
to type it into the pane afterwards. That "afterwards" is the whole problem: `terminal send`
succeeding only means Orca accepted keystrokes, a pane can still be painting, working, or holding
a dialog that swallows what it was given. The answer is one readiness classification, one resend
policy, one confirmation boundary and one failure, here — not one per caller.

It lives in `runtime` rather than next to either caller because both sides need it and only one of
them may import the other: the dispatcher (`secretary`) already reads this package, and the
triggered-agents tick cannot read `secretary` back. Nothing here knows about boards, roles or
sessions; a caller passes the way it runs Orca and the criterion that proves its own head took the
prompt.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

RunJson = Callable[[list[str]], dict[str, Any]]

TUI_IDLE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_TIMEOUT_MS", "60000")))
# Orca decides `tui-idle` from the pane's agent status and, failing that, from a quiescence window
# it polls. A probe shorter than that window would report every quiet pane as busy, so it is set
# above both rather than tuned for the fastest answer.
TUI_IDLE_PROBE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_PROBE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_PROBE_TIMEOUT_MS", "6000")))
TUI_DELIVERY_RETRIES = int(os.environ.get("SECRETARY_TUI_DELIVERY_RETRIES", os.environ.get("TA_TUI_DELIVERY_RETRIES", "2")))
TUI_DELIVERY_TIMEOUT_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12")))
TUI_DELIVERY_POLL_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_POLL_S", os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25")))
TUI_DELIVERY_RESEND_GRACE_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_RESEND_GRACE_S", os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1")))

_WAIT_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"([a-z_]+)"')


class TuiDeliveryError(RuntimeError):
    pass


# What one delivery attempt achieved. `accepted` means the pane took the prompt into a turn while
# the caller's own proof of delivery is expected to arrive later, outside this call.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"

# What Orca answered about a pane. `blocked` is a pane held in a dialog: not ready for a prompt,
# and not working on one either. `unknown` is the probe failing, which is not a busy head.
READINESS_READY = "ready"
READINESS_BUSY = "busy"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"


def wait_for_tui_idle(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> None:
    """Wait until Orca reports the pane ready for input. A refusal reaches the caller.

    This is also what a freshly created head is given to come up in: a TUI paints, reads its
    config and answers Orca's readiness probe well after the pty exists, and the wait for that is
    the same wait as for a pane that is merely busy.
    """
    run_json([
        "orca", "terminal", "wait",
        "--terminal", handle,
        "--for", "tui-idle",
        "--timeout-ms", str(TUI_IDLE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
        "--json",
    ])


def terminal_readiness(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> str:
    """Ask Orca whether the pane is ready for input, and answer in three states, not two.

    This is the one readiness question the product asks about an interactive head, whatever
    provider runs in it: the runtime derives it from the pane's own agent status and falls back to
    a quiescence window, so no screen is read here.

    `READINESS_BUSY` is the condition not being met by a pane that is working, which Orca reports
    as a satisfied-false answer or as a failed command carrying `code: timeout`. A pane it names a
    `blockedReason` for is `READINESS_BLOCKED`: also not ready, but held in a dialog rather than
    working, so a prompt sent to it went nowhere. `READINESS_UNKNOWN` is the probe itself failing,
    and it must not be read as an ordinary busy head: a caller that cannot ask the question is not
    looking at a working observer, it is looking at nothing.
    """
    try:
        data = run_json([
            "orca", "terminal", "wait",
            "--terminal", handle,
            "--for", "tui-idle",
            "--timeout-ms", str(TUI_IDLE_PROBE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
            "--json",
        ])
    except Exception as exc:
        return _refused_wait_readiness(exc)
    wait = data.get("wait") if isinstance(data, dict) and isinstance(data.get("wait"), dict) else data
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    return READINESS_READY


def _refused_wait_readiness(exc: Exception) -> str:
    """Classify a `terminal wait` the host refused, from the body Orca printed with it.

    The CLI exits non-zero both for a condition it could not satisfy and for a failure, and the
    host turns the two into the same exception, so the answer is in the text rather than in the
    outcome. It prints that text as JSON, and the host carries it into the failure it raises:

      * a `wait` object saying `satisfied: false` is a pane Orca has looked at and found working
        or blocked behind a dialog. That is busy, and busy waits for readiness;
      * `code: timeout` is the same condition not being met before the probe's own deadline;
      * anything else, a body that cannot be read included, is a probe that was never answered.
    """
    body = _json_object(str(exc))
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    wait = result.get("wait") if isinstance(result, dict) else None
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if not code:
        # A body too damaged to parse can still carry its code in the text.
        codes = _WAIT_ERROR_CODE_RE.findall(str(exc))
        code = codes[-1] if codes else ""
    return READINESS_BUSY if code == "timeout" else READINESS_UNKNOWN


def _answered_readiness(wait: dict[str, Any]) -> str:
    if wait.get("satisfied"):
        return READINESS_READY
    return READINESS_BLOCKED if wait.get("blockedReason") else READINESS_BUSY


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        parsed = json.loads(text[start:])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def deliver_interactive_prompt(
    handle: str,
    prompt: str,
    *,
    run_json: RunJson,
    confirm: Callable[[float], bool] | None = None,
    ack_out_of_band: bool = False,
) -> str:
    """Deliver a prompt into a live interactive head, on one path for every role that has one.

    Terminal send succeeding only means Orca accepted keystrokes. Wait for the pane to be ready,
    send, then keep re-entering the prompt while the pane stays idle, which is what a swallowed
    prompt looks like. Exhausting the retries raises, so the caller can take its own failure path.

    Callers pass `confirm`, the criterion they always had: their head's turn having visibly
    started. A caller whose proof arrives later sets `ack_out_of_band` and passes no callback at
    all; it gets `DELIVERY_ACCEPTED` as soon as the pane has taken the prompt.
    """
    if ack_out_of_band and confirm is not None:
        raise ValueError("out-of-band delivery cannot use a synchronous confirmation callback")
    if confirm is None and not ack_out_of_band:
        raise ValueError("interactive delivery requires a confirmation criterion")
    wait_for_tui_idle(handle, run_json=run_json)
    sent_at = time.time()
    run_json([
        "orca", "terminal", "send",
        "--terminal", handle,
        "--text", prompt,
        "--enter",
        "--json",
    ])
    return _confirm_interactive_turn(
        handle,
        sent_at,
        run_json=run_json,
        confirm=confirm,
        ack_out_of_band=ack_out_of_band,
    )


def _confirm_interactive_turn(
    handle: str,
    sent_at: float,
    *,
    run_json: RunJson,
    confirm: Callable[[float], bool] | None,
    ack_out_of_band: bool = False,
) -> str:
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    resends = 0
    accepted = False
    readiness = READINESS_READY
    while time.monotonic() < deadline:
        if confirm is not None and confirm(sent_at):
            return DELIVERY_CONFIRMED
        readiness = terminal_readiness(handle, run_json=run_json)
        if readiness == READINESS_UNKNOWN:
            # Not a swallowed prompt and not a working head: the pane cannot be asked at all.
            # Guessing either way here would hide the failure the caller has to act on.
            raise TuiDeliveryError(
                f"the pane could not be probed after the prompt was sent (resends={resends})"
            )
        if readiness == READINESS_BUSY:
            # The pane went to work on something: the prompt is in, whether or not the caller's
            # own proof of it has appeared yet.
            accepted = True
            if ack_out_of_band:
                return DELIVERY_ACCEPTED
        elif resends < TUI_DELIVERY_RETRIES and time.monotonic() >= next_resend_at:
            # Ready or held in a dialog: either way the pane is not working on this prompt, so it
            # is entered again. That is what carries a prompt past a dialog that swallowed it.
            accepted = False
            run_json([
                "orca", "terminal", "send",
                "--terminal", handle,
                "--text", "",
                "--enter",
                "--json",
            ])
            resends += 1
            next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
        time.sleep(max(TUI_DELIVERY_POLL_S, 0.01))
    raise TuiDeliveryError(
        f"interactive prompt delivery was not confirmed after {TUI_DELIVERY_TIMEOUT_S:.1f}s "
        f"(reason={'accepted-but-unconfirmed' if accepted else f'pane-stayed-{readiness}'}, "
        f"resends={resends})"
    )
