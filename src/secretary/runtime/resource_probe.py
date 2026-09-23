"""The real, cheap provider probes the shipped head registry names as `resources.*.probe`.

`python3 -P -m secretary.runtime.resource_probe --resource <id>` makes one provider call for
`claude-sub`, `openai-sub` or `openrouter` and exits 0 when the resource answered, 1 when it did
not (one scrubbed, capped `resource <id> probe failed; ...` line on stderr) and 2 for a resource id
with no probe here. This module only answers; it keeps no cache and writes no file.
`secretary.head_health` runs the registry's probe command, reads this exit code and output, and is
the one owner of the verdict, its vocabulary and its TTL cache.

A probe that cannot run proves nothing about the resource being up, so a timeout, a missing
binary, a missing key or any transport error is a failure here, never an exception.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from secretary.runtime import heads as heads_mod
from secretary.runtime.redact import redact

# A single slow or broken probe is killed rather than hanging the dispatcher's tick. Both
# env-overridable so a live check can tighten them.
PROBE_TIMEOUT_S = int(os.environ.get("TA_PROBE_TIMEOUT_S", "20"))
PROBE_REASON_TEXT_LIMIT = int(os.environ.get("TA_PROBE_REASON_TEXT_LIMIT", "400"))


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    probe_class: str
    command: str | None = None
    status: str = "ok"
    exit_code: int | None = None
    timeout_s: float | None = None
    http_status: int | None = None
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    exception: str | BaseException | None = None


def _clean_summary(value: object) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    text = " ".join(redact(text).strip().split())
    if not text:
        return None
    if len(text) > PROBE_REASON_TEXT_LIMIT:
        return text[:PROBE_REASON_TEXT_LIMIT] + "...[truncated]"
    return text


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _display_command(command: str | list[str]) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def _run_subprocess_probe(
    command: list[str],
    probe_class: str,
    *,
    env: Mapping[str, str] | None = None,
    display_command: str | None = None,
) -> ProbeResult:
    shown = display_command or _display_command(command)
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        return ProbeResult(
            False,
            probe_class,
            command=shown,
            status="timeout",
            timeout_s=float(e.timeout or PROBE_TIMEOUT_S),
            stdout=e.output,
            stderr=e.stderr,
            exception=_exception_text(e),
        )
    except OSError as e:
        return ProbeResult(
            False, probe_class, command=shown, status="exception", exception=_exception_text(e)
        )
    if p.returncode == 0:
        return ProbeResult(True, probe_class, command=shown)
    return ProbeResult(
        False,
        probe_class,
        command=shown,
        status="non-zero-exit",
        exit_code=p.returncode,
        stdout=p.stdout,
        stderr=p.stderr,
    )


def probe_failure_reason(resource_id: str, result: ProbeResult) -> dict[str, object]:
    reason: dict[str, object] = {
        "resource": resource_id,
        "probe_class": result.probe_class,
        "status": result.status,
    }
    command = _clean_summary(result.command)
    if command:
        reason["command"] = command
    if result.exit_code is not None:
        reason["exit_code"] = result.exit_code
    if result.timeout_s is not None:
        reason["timeout_s"] = result.timeout_s
    if result.http_status is not None:
        reason["http_status"] = result.http_status
    for key in ("stderr", "stdout", "exception"):
        summary = _clean_summary(getattr(result, key))
        if summary:
            reason[key] = summary
    return reason


def format_probe_failure(resource_id: str, result: ProbeResult) -> str:
    reason = probe_failure_reason(resource_id, result)
    parts = [
        f"resource {resource_id} probe failed",
        f"class={reason['probe_class']}",
        f"status={reason['status']}",
    ]
    for key in ("command", "exit_code", "timeout_s", "http_status", "stderr", "stdout", "exception"):
        if key in reason:
            parts.append(f"{key}={reason[key]}")
    return "; ".join(parts)


_OPENROUTER_ENV_FILE = Path(os.environ.get("TA_OPENROUTER_ENV_FILE", str(Path.home() / ".hermes" / ".env")))
# Which assignment inside _OPENROUTER_ENV_FILE carries the key. The canonical hermes .env writes
# OPENROUTER_API_KEY; the old decommissioned-repo .env used open_router_key. Accept both so the
# probe resolves against the live hermes file without a per-host tweak; TA_OPENROUTER_ENV_KEY pins
# a single explicit name when a host names it something else.
_OPENROUTER_ENV_KEYS: tuple[str, ...] = (
    (os.environ["TA_OPENROUTER_ENV_KEY"],)
    if os.environ.get("TA_OPENROUTER_ENV_KEY")
    else ("OPENROUTER_API_KEY", "open_router_key")
)


def _read_openrouter_key() -> str | None:
    override = os.environ.get("TA_OPENROUTER_KEY")
    if override:
        return override
    try:
        text = _OPENROUTER_ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() in _OPENROUTER_ENV_KEYS:
            return val.strip().strip('"').strip("'") or None
    return None


def probe_claude_sub() -> ProbeResult:
    """One haiku token through the shared OAuth-authenticated `claude` CLI (claude-sub is one
    subscription, no per-profile credential): a failure exactly when the subscription is
    rate-limited or the CLI cannot reach the API."""
    return _run_subprocess_probe(
        ["claude", "-p", "ping", "--model", "haiku", "--dangerously-skip-permissions"], "builtin:claude-sub"
    )


def _http_failure_status(status: int | None) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate-limit"
    return "http-error"


def _read_http_error_body(err: urllib.error.HTTPError) -> bytes | None:
    try:
        return err.read()
    except Exception:
        return None


def probe_openrouter() -> ProbeResult:
    """One 1-token chat completion against OpenRouter's gemini-flash: a failure on a missing key, a
    non-2xx response, a timeout, or any transport error; never raises."""
    command = "POST https://openrouter.ai/api/v1/chat/completions model=google/gemini-2.5-flash max_tokens=1"
    key = _read_openrouter_key()
    if not key:
        return ProbeResult(
            False, "builtin:openrouter", command=command, status="auth", exception="missing OpenRouter key"
        )
    body = json.dumps(
        {
            "model": "google/gemini-2.5-flash",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None)
            if status is not None and 200 <= status < 300:
                return ProbeResult(True, "builtin:openrouter", command=command)
            return ProbeResult(
                False,
                "builtin:openrouter",
                command=command,
                status=_http_failure_status(status),
                http_status=status,
            )
    except urllib.error.HTTPError as e:
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status=_http_failure_status(e.code),
            http_status=e.code,
            stderr=_read_http_error_body(e),
            exception=_exception_text(e),
        )
    except TimeoutError as e:
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status="timeout",
            timeout_s=float(PROBE_TIMEOUT_S),
            exception=_exception_text(e),
        )
    except Exception as e:  # noqa: BLE001 — any transport outcome is just a failed probe
        return ProbeResult(
            False,
            "builtin:openrouter",
            command=command,
            status="transport-error",
            exception=_exception_text(e),
        )


def probe_openai_sub() -> ProbeResult:
    """One `codex exec` turn through the ChatGPT-authed CODEX_HOME (openai-sub is one
    subscription, no per-profile credential). `-s read-only` and a bare "ping" keep it
    side-effect-free and tool-free, so no bypass flag is needed. CODEX_HOME is set explicitly
    because this is a plain subprocess, not a spawned terminal that would inherit it."""
    env = {**os.environ, "CODEX_HOME": heads_mod.CODEX_HOME}
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "ping"]
    return _run_subprocess_probe(
        cmd,
        "builtin:openai-sub",
        env=env,
        display_command=f"CODEX_HOME={heads_mod.CODEX_HOME} {_display_command(cmd)}",
    )


BUILTIN_PROBES: dict[str, Callable[[], ProbeResult]] = {
    "claude-sub": probe_claude_sub,
    "openrouter": probe_openrouter,
    "openai-sub": probe_openai_sub,
}


def main(argv: list[str] | None = None) -> int:
    """Run the named built-in resource probe: 0 healthy, 1 failed, 2 no such probe."""
    parser = argparse.ArgumentParser(prog="secretary.runtime.resource_probe")
    parser.add_argument("--resource", required=True)
    args = parser.parse_args(argv)
    probe = BUILTIN_PROBES.get(args.resource)
    if probe is None:
        print(
            f"health probe: no builtin probe for {args.resource!r} (known: {', '.join(sorted(BUILTIN_PROBES))})",
            file=sys.stderr,
        )
        return 2
    result = probe()
    if not result.ok:
        print(format_probe_failure(args.resource, result), file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
