"""The real, cheap provider probes the shipped head registry names as `resources.*.probe`.

`python3 -P -m secretary.runtime.resource_probe --resource <id>` makes one provider call for
`claude-sub`, `openai-sub` or `openrouter` and exits 0 when the resource answered, 1 when it did
not (one scrubbed, capped `resource <id> probe failed; ...` line on stderr) and 2 for a resource id
with no probe here. This module only answers; it keeps no cache and writes no file.
`secretary.head_health` runs the registry's probe command, reads this exit code and output, and is
the one owner of the verdict, its vocabulary and its TTL cache.

A probe that cannot run proves nothing about the resource being up, so a timeout, a missing
binary, a missing key or any transport error is a failure here, never an exception. Two failure
statuses are read by name by `head_health`: `status=timeout` (the provider gave no answer inside
the timeout, which is `timed_out` there) and `status=provider-unavailable` (the provider answered
with its own failure while the local login is valid, which is `unavailable` there).

Timeouts are per resource (secretary-1799). Codex refuses slowly: on the 2026-09-25 outage it
reconnected its websocket five times, fell back to HTTPS, reconnected five more and only then
printed the 401, well past the old flat 20 s. `probe_timeout_s` is 75 s for `openai-sub` and 20 s
for the others; `TA_PROBE_TIMEOUT_S` moves the default and `TA_PROBE_TIMEOUT_S_<RESOURCE>` (the
id upper-cased, `-` as `_`, e.g. `TA_PROBE_TIMEOUT_S_OPENAI_SUB`) sets one resource. The outer
timeout `head_health` puts around the probe command is derived from the same number, so the
classifier in here always gets to answer before the command is killed.
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
from dataclasses import dataclass, replace
from pathlib import Path

from secretary.runtime.codex_home import installation_codex_home
from secretary.runtime.codex_preflight import CodexHomeLoginMissing
from secretary.runtime.provider_errors import KIND_AUTH, KIND_RECONNECT, KIND_SERVER, classify_provider_error
from secretary.runtime.redact import redact

# A single slow or broken probe is killed rather than hanging the dispatcher's tick. Both
# env-overridable so a live check can tighten them.
PROBE_TIMEOUT_S = int(os.environ.get("TA_PROBE_TIMEOUT_S", "20"))
PROBE_REASON_TEXT_LIMIT = int(os.environ.get("TA_PROBE_REASON_TEXT_LIMIT", "400"))
#: The default inner timeout of a resource with no entry below.
DEFAULT_PROBE_TIMEOUT_S = 20
#: Resources whose provider answers more slowly than the default, and how long they get.
RESOURCE_PROBE_TIMEOUTS_S: dict[str, int] = {"openai-sub": 75}
#: The inner failure status of a provider that answered with its own failure (see the docstring).
STATUS_PROVIDER_UNAVAILABLE = "provider-unavailable"


def _env_seconds(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return value if value > 0 else None


def probe_timeout_env_name(resource_id: str) -> str:
    """The environment variable that sets one resource's probe timeout."""
    return "TA_PROBE_TIMEOUT_S_" + "".join(c if c.isalnum() else "_" for c in resource_id.upper())


def probe_timeout_s(resource_id: str) -> int:
    """How long this resource's probe waits for its provider, read from the environment per call.

    Per-resource variable first, then the resource's own default, then `TA_PROBE_TIMEOUT_S`, then
    20 s. Read per call rather than at import so the dispatcher and the probe it spawns, which
    share one environment, can never disagree about it.
    """
    specific = _env_seconds(probe_timeout_env_name(resource_id))
    if specific is not None:
        return specific
    if resource_id in RESOURCE_PROBE_TIMEOUTS_S:
        return RESOURCE_PROBE_TIMEOUTS_S[resource_id]
    return _env_seconds("TA_PROBE_TIMEOUT_S") or DEFAULT_PROBE_TIMEOUT_S


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
    timeout_s: int | None = None,
) -> ProbeResult:
    shown = display_command or _display_command(command)
    timeout = timeout_s or PROBE_TIMEOUT_S
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        return ProbeResult(
            False,
            probe_class,
            command=shown,
            status="timeout",
            timeout_s=float(e.timeout or timeout),
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
        ["claude", "-p", "ping", "--model", "haiku", "--dangerously-skip-permissions"],
        "builtin:claude-sub",
        timeout_s=probe_timeout_s("claude-sub"),
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
        with urllib.request.urlopen(req, timeout=probe_timeout_s("openrouter")) as resp:
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
            timeout_s=float(probe_timeout_s("openrouter")),
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
    try:
        home = installation_codex_home().path
    except CodexHomeLoginMissing as e:
        # No login to probe with is a failed probe, and its message names the fix.
        return ProbeResult(False, "builtin:openai-sub", status="no-login", exception=_exception_text(e))
    env = {**os.environ, "CODEX_HOME": home}
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "ping"]
    result = _run_subprocess_probe(
        cmd,
        "builtin:openai-sub",
        env=env,
        display_command=f"CODEX_HOME={home} {_display_command(cmd)}",
        timeout_s=probe_timeout_s("openai-sub"),
    )
    if result.status == "non-zero-exit" and codex_provider_side_failure(
        _as_text(result.stdout) + "\n" + _as_text(result.stderr), Path(home)
    ):
        return replace(result, status=STATUS_PROVIDER_UNAVAILABLE)
    return result


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def codex_chatgpt_login(home: Path) -> bool:
    """Whether this CODEX_HOME holds a ChatGPT-mode login (`auth.json` with `auth_mode: chatgpt`).

    Read for its mode only; no token leaves this function.
    """
    try:
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    return (
        isinstance(auth, dict)
        and str(auth.get("auth_mode") or "").lower() == "chatgpt"
        and bool(auth.get("tokens"))
    )


def codex_provider_side_failure(text: str, home: Path) -> bool:
    """Whether a failed `codex exec` was refused by the provider rather than for the account.

    A 5xx and an exhausted reconnect loop are always the provider's. A 401/403 is the provider's
    only while the local login is a valid ChatGPT one: on 2026-09-25 the ChatGPT/Codex backend
    answered every request with `401 Unauthorized: Incorrect API key provided: sk-svcac…` although
    no API key was in use at all -- a backend fault, and logging in again would not have fixed it.
    With no such login, a 401 is the account's and stays `unauthenticated`.
    """
    found = classify_provider_error(text)
    if found is None:
        return False
    if found.kind in (KIND_SERVER, KIND_RECONNECT):
        return True
    if found.kind == KIND_AUTH:
        return codex_chatgpt_login(home) and "incorrect api key provided" in text.lower()
    return False


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
