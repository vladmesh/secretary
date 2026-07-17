"""Per-resource health for pipeline heads: TTL-cached probes, claim-time fallback selection,
watchdog freeze lookup.

A resource (heads.toml `[resources.*]`) is a shared account/limit several head profiles draw
from — red is a property of the resource, not of any one profile, so a card whose preferred
profile sits on a red resource can still claim onto a fallback profile that draws from a
*different*, green resource (`resolve_head`), and a worker already running on a red-resource
profile has its watchdog clock frozen rather than mistaken for a dead head (`resource_of`, used
by dispatcher._advance).

`refresh` is the only thing that ever runs a probe. It is meant to be called once per tick (from
precheck, which runs unconditionally every timer fire, and again from tick() for the rare case a
resource's TTL lapsed between the two) — probes cost real tokens/quota, so the TTL cache
(`PROBE_TTL_S`, ~5 min) is what keeps that cost to "once per window", not "once per 3-min tick".
Cache lives in a JSON file (state/pipeline/resource_health.json), not in memory: precheck and
tick are two separate `python3 -m triggered_agents` processes per timer fire (via
deploy/ta-gate.sh), so nothing survives in-process between them.

A resource's red<->green flip is logged to runs.jsonl (`head-health`) exactly once per flip, not
on every re-probe of an unchanged status — a resource pinned red for hours must not spam the log
every tick, same principle as runtime/health.py's error-vs-freshness split.

When a probe records red, the `head-health` line carries a `reason` object. Look there first when
resource_health.json says a resource is red. stdout, stderr, and exception text are scrubbed and
capped before logging so telemetry stays useful without storing secrets or whole CLI transcripts.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...runtime.redact import redact
from . import heads as heads_mod
from .state import STATE

HEALTH_FILE = STATE.dir / "resource_health.json"

# ~5 min between real probes of the same resource; a single slow/broken probe gets killed rather
# than hanging a tick. Both env-overridable so an e2e/live check can tighten them.
PROBE_TTL_S = int(os.environ.get("TA_PROBE_TTL_S", "300"))
PROBE_TIMEOUT_S = int(os.environ.get("TA_PROBE_TIMEOUT_S", "20"))
PROBE_REASON_TEXT_LIMIT = int(os.environ.get("TA_PROBE_REASON_TEXT_LIMIT", "400"))
# Comma-separated resource ids to report red without running their real probe at all — a live e2e
# proving the fallback machinery behaves right under a red resource (steward chain lands on the
# hermes head, product heads claim-skip) needs to redden claude-sub on demand, without actually
# touching the shared subscription. Bypasses the TTL cache too, so flipping it takes effect on the
# very next refresh().
_FORCE_RED_ENV = "TA_HEALTH_FORCE_RED"

GREEN = "green"
RED = "red"


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


def _load() -> dict:
    if not HEALTH_FILE.is_file():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(cache: dict) -> None:
    STATE.ensure_dir()
    tmp = HEALTH_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HEALTH_FILE)


def _forced_red() -> set[str]:
    return {r for r in os.environ.get(_FORCE_RED_ENV, "").split(",") if r}


def _clean_summary(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = redact(text)
    text = " ".join(text.strip().split())
    if not text:
        return None
    if len(text) > PROBE_REASON_TEXT_LIMIT:
        return text[:PROBE_REASON_TEXT_LIMIT] + "...[truncated]"
    return text


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _display_command(command: str | list[str]) -> str:
    return command if isinstance(command, str) else shlex.join(command)


def _run_subprocess_probe(command: str | list[str], probe_class: str, *,
                          shell: bool = False, env: dict | None = None,
                          display_command: str | None = None) -> ProbeResult:
    shown = display_command or _display_command(command)
    try:
        p = subprocess.run(
            command, shell=shell, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as e:
        return ProbeResult(
            False, probe_class, command=shown, status="timeout",
            timeout_s=float(e.timeout or PROBE_TIMEOUT_S), stdout=e.output, stderr=e.stderr,
            exception=_exception_text(e))
    except OSError as e:
        return ProbeResult(False, probe_class, command=shown, status="exception",
                           exception=_exception_text(e))
    if p.returncode == 0:
        return ProbeResult(True, probe_class, command=shown)
    return ProbeResult(
        False, probe_class, command=shown, status="non-zero-exit", exit_code=p.returncode,
        stdout=p.stdout, stderr=p.stderr)


def _run_probe_cmd(cmd: str) -> ProbeResult:
    """Green iff `cmd` exits 0 within PROBE_TIMEOUT_S. A timeout, a missing binary, or any
    other OSError all count as red because a probe that cannot run proves nothing about the
    resource being up."""
    return _run_subprocess_probe(cmd, "shell-command", shell=True)


def _coerce_probe_result(value, *, command: str) -> ProbeResult:
    if isinstance(value, ProbeResult):
        return value
    return ProbeResult(bool(value), "shell-command", command=command,
                       status="ok" if value else "probe-failed")


def _probe_failure_reason(resource_id: str, result: ProbeResult) -> dict:
    reason = {
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


def _reason_key(reason: dict) -> str:
    return json.dumps(reason, ensure_ascii=False, sort_keys=True)


def format_probe_failure(resource_id: str, result: ProbeResult) -> str:
    reason = _probe_failure_reason(resource_id, result)
    parts = [f"resource {resource_id} probe failed",
             f"class={reason['probe_class']}", f"status={reason['status']}"]
    for key in ("command", "exit_code", "timeout_s", "http_status", "stderr", "stdout",
                "exception"):
        if key in reason:
            parts.append(f"{key}={reason[key]}")
    return "; ".join(parts)


def _forced_red_result(resource_id: str) -> ProbeResult:
    return ProbeResult(
        False, "forced-red", status="forced-red",
        exception=f"{_FORCE_RED_ENV} contains {resource_id!r}")


def _log_health_event(resource_id: str, old_status: str | None, new_status: str,
                      reason: dict | None, *, confirmed: bool = False) -> None:
    fields = {"resource": resource_id, "from": old_status, "to": new_status}
    if confirmed:
        fields["confirmed"] = True
    if reason:
        fields["reason"] = reason
    STATE.log_run("head-health", **fields)


def refresh(registry: heads_mod.Registry | None = None) -> dict[str, str]:
    """Re-probe every resource whose cached entry is missing or older than PROBE_TTL_S; return
    {resource_id: GREEN/RED} for every resource in the registry (cached entries included). Logs
    one `head-health` runs.jsonl event per resource whose status actually flipped since its last
    cached value, plus a red confirmation when the failure reason is new or was never logged.
    Fresh-cache reuse and identical red confirmations stay quiet. A resource named in
    TA_HEALTH_FORCE_RED is always RED here, real probe skipped entirely, TTL cache bypassed;
    see _FORCE_RED_ENV."""
    reg = registry or heads_mod.load_registry()
    cache = _load()
    now = time.time()
    dirty = False
    statuses: dict[str, str] = {}
    forced = _forced_red()
    for rid, res in reg.resources.items():
        entry = cache.get(rid)
        if rid not in forced and entry and now - entry.get("checked_at", 0) < PROBE_TTL_S:
            statuses[rid] = entry["status"]
            continue
        command = res.get("probe", "true")
        result = _forced_red_result(rid) if rid in forced else _coerce_probe_result(
            _run_probe_cmd(command), command=command)
        new_status = GREEN if result.ok else RED
        old_status = entry["status"] if entry else None
        reason = None if result.ok else _probe_failure_reason(rid, result)
        reason_key = _reason_key(reason) if reason else None
        cache_entry = {"status": new_status, "checked_at": now}
        if reason:
            cache_entry["reason"] = reason
            cache_entry["reason_key"] = reason_key
            logged_key = entry.get("logged_reason_key") if entry else None
            if old_status != RED or logged_key != reason_key:
                _log_health_event(rid, old_status, new_status, reason,
                                  confirmed=(old_status == RED))
                cache_entry["logged_reason_key"] = reason_key
            elif logged_key:
                cache_entry["logged_reason_key"] = logged_key
        dirty = True
        statuses[rid] = new_status
        if reason is None and old_status is not None and old_status != new_status:
            _log_health_event(rid, old_status, new_status, None)
        cache[rid] = cache_entry
    if dirty:
        _save(cache)
    return statuses


def resource_of(profile_id: str, registry: heads_mod.Registry | None = None) -> str | None:
    """`profile_id`'s resource id, or None if the profile is unknown to the registry (a reconciled
    record with no stored head, or a profile removed from heads.toml since bring-up) — callers
    treat that as "nothing to freeze/fall back on", never as an error mid-tick."""
    reg = registry or heads_mod.load_registry()
    try:
        return reg.profile(profile_id)["resource"]
    except heads_mod.HeadRegistryError:
        return None


def resolve_head(preferred: str, statuses: dict[str, str],
                 registry: heads_mod.Registry | None = None) -> str | None:
    """The profile to actually launch for `preferred`: itself if its resource is green, else the
    first profile — breadth-first over the ordered fallback chain, recursively — whose resource is
    green. None when `preferred` and everything reachable through its fallback chain sits on a red
    resource: the caller must not claim in that case (claim-skip, card stays Ready)."""
    reg = registry or heads_mod.load_registry()
    seen: set[str] = set()
    queue = [preferred]
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            prof = reg.profile(pid)
        except heads_mod.HeadRegistryError:
            continue
        if statuses.get(prof["resource"], GREEN) == GREEN:
            return pid
        queue.extend(prof.get("fallback") or [])
    return None


def next_retry_head(current: str, tried: set[str], statuses: dict[str, str],
                    registry: heads_mod.Registry | None = None) -> tuple[str | None, bool]:
    """The next profile for a watchdog retry-switch to land `current` on: breadth-first over
    `current`'s own fallback chain (same walk as resolve_head), skipping anything already in
    `tried` (every head this card's watchdog has already used this life, `current` included) and
    any red resource. Returns (head, False) on a hit.

    A miss carries a second flag distinguishing why, so dispatcher._watchdog_retry can tell "spend
    the switch budget's one shot on nothing" from "there's a real target, just not up right now":
      (None, True)  nothing untried left to try at all (empty/exhausted chain, or `current` itself
                    unknown to the registry) — stop retrying, nothing here will ever turn green.
      (None, False) untried candidates exist but every one sits on a red resource right now —
                    requeue without spending the budget; the next claim's own red-skip
                    (_claim_next/resolve_head) picks the card up once a resource recovers."""
    reg = registry or heads_mod.load_registry()
    try:
        queue = list(reg.profile(current).get("fallback") or [])
    except heads_mod.HeadRegistryError:
        return None, True
    visited = {current}   # cycle guard only — a tried-but-not-visited node's OWN fallback is still
                          # worth walking, it may lead to something genuinely untried further out
    found_untried = False
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        try:
            prof = reg.profile(pid)
        except heads_mod.HeadRegistryError:
            continue
        queue.extend(prof.get("fallback") or [])
        if pid in tried:
            continue
        found_untried = True
        if statuses.get(prof["resource"], GREEN) == GREEN:
            return pid, False
    return None, not found_untried


# Real, cheap probes for the builtin resources this repo names in heads.toml. Deliberately
# not resource-id-dispatched inside `refresh`/`_run_probe_cmd` above (those stay pure "run this
# shell command" — heads.toml's `probe` field is the single source of what each resource runs);
# these are just what that field's command, for these ids, happens to invoke.
_OPENROUTER_ENV_FILE = Path(os.environ.get("TA_OPENROUTER_ENV_FILE",
                                          str(Path.home() / "projects" / "project_inspect" / ".env")))
_OPENROUTER_ENV_KEY = "open_router_key"


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
        if key.strip() == _OPENROUTER_ENV_KEY:
            return val.strip().strip('"').strip("'") or None
    return None


def probe_claude_sub_result() -> ProbeResult:
    """One haiku token through the shared OAuth-authenticated `claude` CLI (see heads.toml:
    claude-sub is one subscription, no per-profile credential) — red exactly when the
    subscription is rate-limited or the CLI can't reach the API, otherwise green. Costs one cheap
    call per PROBE_TTL_S, never per tick."""
    return _run_subprocess_probe(
        ["claude", "-p", "ping", "--model", "haiku", "--dangerously-skip-permissions"],
        "builtin:claude-sub")


def probe_claude_sub() -> bool:
    return probe_claude_sub_result().ok


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


def probe_openrouter_result() -> ProbeResult:
    """One 1-token chat completion against OpenRouter's gemini-flash — red on a missing key, a
    non-2xx response, a timeout, or any transport error; never raises."""
    command = "POST https://openrouter.ai/api/v1/chat/completions model=google/gemini-2.5-flash max_tokens=1"
    key = _read_openrouter_key()
    if not key:
        return ProbeResult(False, "builtin:openrouter", command=command, status="auth",
                           exception="missing OpenRouter key")
    body = json.dumps({
        "model": "google/gemini-2.5-flash",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:  # noqa: S310 (fixed host)
            status = getattr(resp, "status", None)
            if status is not None and 200 <= status < 300:
                return ProbeResult(True, "builtin:openrouter", command=command)
            return ProbeResult(False, "builtin:openrouter", command=command,
                               status=_http_failure_status(status), http_status=status)
    except urllib.error.HTTPError as e:
        return ProbeResult(
            False, "builtin:openrouter", command=command,
            status=_http_failure_status(e.code), http_status=e.code,
            stderr=_read_http_error_body(e), exception=_exception_text(e))
    except TimeoutError as e:
        return ProbeResult(False, "builtin:openrouter", command=command, status="timeout",
                           timeout_s=float(PROBE_TIMEOUT_S), exception=_exception_text(e))
    except Exception as e:  # noqa: BLE001 — any transport outcome is just "red"
        return ProbeResult(False, "builtin:openrouter", command=command, status="transport-error",
                           exception=_exception_text(e))


def probe_openrouter() -> bool:
    return probe_openrouter_result().ok


def probe_openai_sub_result() -> ProbeResult:
    """One `codex exec` turn through the ChatGPT-authed CODEX_HOME (see heads.CODEX_HOME:
    openai-sub is one subscription, no per-profile credential) — red exactly when the subscription
    is rate-limited or the `codex` CLI can't reach the API, otherwise green. `-s read-only` and a
    bare "ping" keep it side-effect-free and tool-free (no approval-hook cancel), so no bypass flag
    is needed. CODEX_HOME is set explicitly because this is a plain subprocess, not an orca-spawned
    terminal that would inherit it. Costs one cheap call per PROBE_TTL_S, never per tick."""
    env = {**os.environ, "CODEX_HOME": heads_mod.CODEX_HOME}
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "ping"]
    return _run_subprocess_probe(
        cmd, "builtin:openai-sub", env=env,
        display_command=f"CODEX_HOME={heads_mod.CODEX_HOME} {_display_command(cmd)}")


def probe_openai_sub() -> bool:
    return probe_openai_sub_result().ok


BUILTIN_PROBES = {
    "claude-sub": probe_claude_sub,
    "openrouter": probe_openrouter,
    "openai-sub": probe_openai_sub,
}

BUILTIN_PROBE_RESULTS = {
    "claude-sub": probe_claude_sub_result,
    "openrouter": probe_openrouter_result,
    "openai-sub": probe_openai_sub_result,
}


def run_builtin_probe_result(resource_id: str) -> ProbeResult:
    return BUILTIN_PROBE_RESULTS[resource_id]()


def run_builtin_probe(resource_id: str) -> bool:
    """Dispatch to the real check for `resource_id` — the thing heads.toml's `probe = "python3 -m
    triggered_agents pipeline probe --resource <id>"` command actually runs. Raises KeyError for
    an id with no builtin (a resource that only ever needs "true"/"false" has no reason to go
    through this CLI at all)."""
    return run_builtin_probe_result(resource_id).ok
