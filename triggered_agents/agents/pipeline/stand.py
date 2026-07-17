"""Stand deploy + e2e — the host side of Validate layer 2 (design-task-pipeline.md, «Стенд +
e2e до мержа»).

Layer 1 (worker.poll_pr) only reads gh. This module does the expensive part: it checks out a
PR branch into the project's persistent stand worktree, brings the stand's compose namespace up
on this VPS (its own project name / ports, so it never collides with the dev or prod stack),
optionally waits for a health URL, runs the scripted e2e suite, and tears the namespace down. No
LLM, no Kanboard — the dispatcher reads the exit code and log tail and drives the board.

Exclusivity of the stand is free: the pipeline runs one code task per project at a time, so at
most one PR is ever deployed to a project's stand. The suspect is always exactly one PR.

The stand config is the `[stand]` section of the project's committed `workspace.toml`, read from
the base repo (not the PR worktree) so a PR cannot rewrite its own gate. Absent section -> the
project has no layer 2 and `read_config` returns None. Required keys: `namespace`, `compose`
(list, relative to the repo root), `e2e_command`. Everything else has a default.

Trust boundary: the compose files and e2e script that run here come from the PR checkout and
execute on the host with the dispatcher's full environment. The pipeline assumes the PR author is
trusted (the same person who could push to the repo); this is not a sandbox and does not defend
against a hostile PR. That assumption is fine for a single-maintainer pipeline and must be
revisited before opening the board to untrusted contributors.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import naming

DOCKER = os.environ.get("DOCKER_BIN") or shutil.which("docker") or "docker"
GIT = os.environ.get("GIT_BIN") or shutil.which("git") or "git"
# Where per-project stand worktrees live. Persistent across runs: git fetch + a detached checkout
# reuses the same tree, so only the diff is re-fetched and compose layers rebuild incrementally.
STANDS_ROOT = Path(os.environ.get("TA_STANDS_DIR", str(Path.home() / ".cache" / "ta-pipeline" / "stands")))
_LOG_TAIL_LINES = 60


class StandError(RuntimeError):
    """A stand step failed in a way the dispatcher reports as a red run (config, git, compose)."""


def read_config(repo_root: Path) -> dict | None:
    """The `[stand]` section of `<repo_root>/workspace.toml`, or None if the project has no stand.

    Only presence of the section is decided here; missing required keys surface later as a red
    run (a clear, visible failure on the card) rather than a silent skip that would let a
    misconfigured stand slip a card past the gate.
    """
    manifest = repo_root / "workspace.toml"
    if not manifest.is_file():
        return None
    try:
        cfg = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        # A broken base manifest must be a localized, visible failure on the card, not a crash
        # that takes the whole dispatcher tick down with it (the caller catches StandError).
        raise StandError(f"{manifest} is not readable/valid TOML: {e}") from e
    stand = cfg.get("stand")
    return stand if isinstance(stand, dict) and stand else None


def _validate_config(cfg: dict) -> tuple[str, list[str], str]:
    namespace = cfg.get("namespace")
    compose = cfg.get("compose")
    e2e_command = cfg.get("e2e_command")
    missing = [k for k, v in (("namespace", namespace), ("compose", compose),
                              ("e2e_command", e2e_command)) if not v]
    if missing:
        raise StandError(f"[stand] manifest is missing required key(s): {', '.join(missing)}")
    if not isinstance(compose, list):
        raise StandError("[stand] compose must be a list of compose file paths")
    return namespace, compose, e2e_command


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
         timeout: float | None = None) -> tuple[int, str]:
    """Run a command, capturing combined stdout+stderr. A timeout is reported as a non-zero
    result with a note, never a raised TimeoutExpired that would escape as a traceback."""
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        return 124, out + f"\n[stand] `{' '.join(cmd)}` timed out after {timeout}s"
    except OSError as e:
        # docker/git missing from PATH, daemon socket gone, bad cwd — a real red run, not a
        # silent no-op. Must not raise: run()'s `finally: down` calls _run too, and an escaping
        # OSError would bubble past it and be swallowed upstream as "unavailable" (infinite retry).
        return 127, f"[stand] `{' '.join(cmd)}` could not run: {e}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _compose_base(namespace: str, compose: list[str], exec_root: Path, env_file: str | None) -> list[str]:
    """Compose invocation prefix. The `-f`/`--env-file` paths resolve against `exec_root` — the PR
    checkout — so the stack under test is the branch's own compose, not the base repo's."""
    args = [DOCKER, "compose", "-p", namespace]
    for f in compose:
        args += ["-f", f if os.path.isabs(f) else str(exec_root / f)]
    if env_file:
        args += ["--env-file", env_file if os.path.isabs(env_file) else str(exec_root / env_file)]
    return args


def _checkout_branch(repo_root: Path, stand_dir: Path, branch: str, stand_branch: str) -> str:
    """Fetch `branch` from origin and land the persistent stand worktree onto it, on its own named
    ref `stand_branch` (`stand/<project>`) — never detached. Returns the log.

    A named branch, not a detached HEAD, sidesteps git's "branch already checked out in another
    worktree" guard the same way `stand_branch` being distinct from `branch` does, and leaves
    `git status` on the stand tree reading a real ref instead of a detached commit. The worktree
    is created off the main repo the first time and reused after; a tree left detached by a run
    from before this convention existed is folded onto `stand_branch` too, rather than only fixed
    for brand-new stands.
    """
    log_parts = []
    if not (stand_dir / ".git").exists():
        stand_dir.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run([GIT, "-C", str(repo_root), "worktree", "add", "-b", stand_branch,
                          str(stand_dir), "HEAD"], timeout=120)
        log_parts.append(out)
        if code != 0:
            raise StandError(f"git worktree add failed:\n{out}")
    else:
        code, out = _run([GIT, "-C", str(stand_dir), "checkout", "-B", stand_branch], timeout=30)
        log_parts.append(out)
        if code != 0:
            raise StandError(f"git checkout -B {stand_branch} failed:\n{out}")
    for cmd in (
        [GIT, "-C", str(stand_dir), "fetch", "--force", "origin", branch],
        [GIT, "-C", str(stand_dir), "reset", "--hard", "FETCH_HEAD"],
    ):
        code, out = _run(cmd, timeout=120)
        log_parts.append(out)
        if code != 0:
            raise StandError(f"`{' '.join(cmd)}` failed:\n{out}")
    return "\n".join(p for p in log_parts if p.strip())


def _wait_health(url: str, timeout: float) -> tuple[bool, str]:
    """Poll `url` until it answers 2xx or `timeout` elapses. Compose --wait already gates on
    container healthchecks; this is a second gate on an HTTP entrypoint that may lack one."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (fixed localhost URL)
                if 200 <= r.status < 300:
                    return True, f"[stand] health {url} -> {r.status}"
                last = f"{url} -> HTTP {r.status}"
        except (urllib.error.URLError, OSError) as e:
            last = f"{url} unreachable: {e}"
        time.sleep(3)
    return False, f"[stand] health check timed out after {timeout}s: {last}"


def run(project: str, branch: str, cfg: dict, repo_root: Path) -> dict:
    """Deploy `branch` to the project's stand and run its e2e suite.

    Returns {"ok": bool, "stage": str, "log": str}. `stage` is where it stopped on failure
    ("config" | "checkout" | "up" | "health" | "e2e"); on success it is "e2e". The log is the
    raw combined output (the dispatcher scrubs secrets before it reaches the board). The compose
    namespace is always torn down afterwards, pass or fail, so the host is left clean.
    """
    log: list[str] = []

    def tail(stage: str, ok: bool) -> dict:
        text = "\n".join(p for p in log if p and p.strip())
        return {"ok": ok, "stage": stage, "log": "\n".join(text.splitlines()[-_LOG_TAIL_LINES:])}

    try:
        namespace, compose, e2e_command = _validate_config(cfg)
    except StandError as e:
        log.append(str(e))
        return tail("config", False)

    env_file = cfg.get("env_file")
    stand_dir = STANDS_ROOT / project
    # Compose/e2e run from the PR checkout (stand_dir); only the config came from the base repo.
    base = _compose_base(namespace, compose, stand_dir, env_file)
    e2e_env = dict(os.environ)
    for k, v in (cfg.get("e2e_env") or {}).items():
        e2e_env[str(k)] = str(v)

    try:
        log.append(_checkout_branch(repo_root, stand_dir, branch, naming.stand_branch(project)))
    except StandError as e:
        log.append(str(e))
        return tail("checkout", False)

    try:
        up = base + ["up", "-d", "--build", "--wait", "--wait-timeout",
                     str(int(cfg.get("up_timeout_seconds", 300)))]
        code, out = _run(up, cwd=stand_dir, env=e2e_env, timeout=int(cfg.get("up_timeout_seconds", 300)) + 120)
        log.append(out)
        if code != 0:
            return tail("up", False)

        health_url = cfg.get("health_url")
        if health_url:
            ok, out = _wait_health(health_url, float(cfg.get("health_timeout_seconds", 120)))
            log.append(out)
            if not ok:
                return tail("health", False)

        code, out = _run(["bash", "-c", e2e_command], cwd=stand_dir, env=e2e_env,
                         timeout=int(cfg.get("e2e_timeout_seconds", 600)))
        log.append(out)
        return tail("e2e", code == 0)
    finally:
        _run(base + ["down", "-v", "--remove-orphans"], cwd=stand_dir, env=e2e_env, timeout=180)
