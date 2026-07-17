"""Worker-workspace side of the dispatcher — the Orca/host bits, kept apart from the board logic.

The dispatcher reasons over the board (ops.py) and delegates every side effect that touches a
worktree or a head to the functions here: read a project's base branch, create the Orca worktree,
run the provisioner (setup+smoke), drop the one-time TASK.md, launch the worker head, poll its
activity, tear the workspace down. Split out so the dispatcher's decisions stay unit-testable on a
fake board while these host calls are stubbed. No Kanboard here, no LLM.

Provisioning runs the SAME script `orca.yaml scripts.setup` points at
(`control-panel/pipeline/provision.py`), just invoked directly so we capture its exit code and log
and can move the card to Blocked before a head is spawned. Orca still owns worktree creation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from ...runtime import claude_env, redact
from . import codex_sessions, heads, model, prompt_delivery, stand, task_protocol, terminal_session
from .state import STATE

ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
GH = os.environ.get("GH_BIN") or shutil.which("gh") or "gh"
PROJECTS_DIR = Path(os.environ.get("TA_PROJECTS_DIR", str(Path.home() / "projects")))
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
CONTROL_PANEL = Path(os.environ.get("TA_CONTROL_PANEL", str(Path.home() / "control-panel")))
PROVISION = CONTROL_PANEL / "pipeline" / "provision.py"
# Every workspace teardown lives under here — the guard in teardown() refuses anything outside it,
# and the same STATE (state/pipeline/) is where dispatcher.py logs, so a sudo-fallback trip shows
# up in the one runs.jsonl the pipeline already watches.
WORKSPACES_ROOT = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces").resolve()
ORCA_TIMEOUT_S = 120       # worktree create runs repo git; give it room, but never hang forever
PROVISION_TIMEOUT_S = int(os.environ.get("TA_PROVISION_TIMEOUT_S", "900"))
GH_TIMEOUT_S = 60          # a gh call may hit the network; bounded so a tick never hangs on it
_GH_LOG_TAIL_LINES = 40
TUI_IDLE_TIMEOUT_MS = int(os.environ.get("TA_TUI_IDLE_TIMEOUT_MS", "60000"))
TUI_DELIVERY_RETRIES = prompt_delivery.TUI_DELIVERY_RETRIES
TUI_DELIVERY_CHECK_DELAY_S = prompt_delivery.TUI_DELIVERY_CHECK_DELAY_S
TUI_DELIVERY_TIMEOUT_S = prompt_delivery.TUI_DELIVERY_TIMEOUT_S
TUI_DELIVERY_RESEND_GRACE_S = prompt_delivery.TUI_DELIVERY_RESEND_GRACE_S
# Head profile for the layer-3 reviewer. A plain config knob (not a per-card choice): the reviewer
# is independent of the worker, so the card's `head` field is not reused here — this names a
# profile in heads.toml, same registry every worker head resolves against.
REVIEWER_HEAD = os.environ.get("TA_REVIEWER_HEAD", "codex-reviewer")


class WorkspaceError(RuntimeError):
    """A host-side workspace step failed (orca, git, provision transport)."""


class InjectDeliveryError(prompt_delivery.InjectDeliveryError, WorkspaceError):
    pass


# Board comments are the card's public journal, so provision logs / error texts get scrubbed
# before posting. runtime.redact catches known .env values and token shapes; on top of that:
# KEY=value assignments whose name smells like a secret, and long base64/hex-ish blobs
# (no `/`, so filesystem paths survive).
_ASSIGN_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)\s*=\s*\S+")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_git_sha(blob: str) -> bool:
    """A git sha — full (40 hex) or abbreviated (7-40 hex) — is plain hex, no `+`/`=`/mixed-case
    entropy a real token would carry. Masking it turns a commit reference in a CI-failure comment
    into noise for no security gain."""
    return bool(_HEX_RE.match(blob))


def scrub_secrets(text: str) -> str:
    """Mask secret-looking material in `text` before it reaches a board comment. `_BLOB_RE` casts
    a wide net over long alnum runs, so a git sha or any other hex-shaped identifier is spared —
    only the rest (base64/token-looking blobs) gets masked."""
    if not text:
        return text
    text = redact.redact(text)
    text = _ASSIGN_RE.sub(rf"\1={redact.REDACTED}", text)
    return _BLOB_RE.sub(lambda m: m.group(0) if _is_git_sha(m.group(0)) else f"{redact.REDACTED}:blob", text)


def _orca_json(args: list[str]) -> dict:
    import json

    try:
        p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        # A hung orca must surface as a WorkspaceError (the module's spawn-failure contract), not a
        # raw TimeoutExpired that escapes create_workspace/launch_worker/spawn_reviewer and lands in
        # the caller's generic error path with a misleading message.
        raise WorkspaceError(f"orca {' '.join(args)} timed out after {ORCA_TIMEOUT_S}s") from e
    if p.returncode != 0:
        raise WorkspaceError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _git(cwd: str | Path, args: list[str], timeout: float = ORCA_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run `git -C cwd <args>`, bounded the same way `_orca_json` bounds the orca CLI — a hung git
    (a wedged fsmonitor, a stale lock) must never hang a dispatcher tick forever."""
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise WorkspaceError(f"git -C {cwd} {' '.join(args)} timed out after {timeout}s") from e


def _git_ok(cwd: str | Path, args: list[str], timeout: float = ORCA_TIMEOUT_S) -> str:
    """`_git`, raising WorkspaceError on a non-zero exit — the git-hygiene steps below (fetch,
    branch rename, landing a PR head) have no meaningful degraded path, so a failure here must
    abort bring-up the same way an orca failure does."""
    p = _git(cwd, args, timeout)
    if p.returncode != 0:
        raise WorkspaceError(f"git -C {cwd} {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def project_root(project: str) -> Path:
    """Корень репо проекта: ~/projects/<name>, иначе ~/<name> (там живут control-panel,
    triggered-agents и прочая инфраструктура секретаря)."""
    p = PROJECTS_DIR / project
    if p.is_dir():
        return p
    home = PROJECTS_DIR.parent / project
    return home if home.is_dir() else p


def _load_manifest(project: str) -> dict:
    """workspace.toml лукап цепочкой: сначала в самом репо проекта (project_root), иначе
    центральный манифест контриб-проектов control-panel/pipeline/manifests/<project>.toml —
    контриб-форк не коммитит workspace.toml в свой репо (agent-kanban-232), декларация живёт в
    control-panel вместо этого. Ни там, ни там — пустой манифест, вызывающий откатывается на
    дефолты (base_branch main, не contrib). Зеркало той же цепочки в provision.py
    (control-panel), кроме финала: там отсутствие манифеста в обоих местах — FAIL, здесь —
    безопасные дефолты (это read_base_branch/is_contrib, не провижининг)."""
    local = project_root(project) / "workspace.toml"
    if local.is_file():
        return tomllib.loads(local.read_text(encoding="utf-8"))
    central = CONTROL_PANEL / "pipeline" / "manifests" / f"{project}.toml"
    if central.is_file():
        return tomllib.loads(central.read_text(encoding="utf-8"))
    return {}


def read_base_branch(project: str) -> str:
    """base_branch from the project's manifest (see _load_manifest), default 'main'."""
    return _load_manifest(project).get("workspace", {}).get("base_branch", "main")


def resolve_base_branch(project: str, card_base_branch: str | None) -> str:
    """A card's own base_branch (model.META_BASE_BRANCH) wins over the project's manifest
    lookup — the sprint-shim case (dnd-simulator): a card can pin its worktree/PR/merge target to
    `sprint/NNN-slug` instead of the manifest's `main`. Empty/None falls back to read_base_branch
    exactly as before this field existed."""
    return card_base_branch or read_base_branch(project)


def is_contrib(project: str) -> bool:
    """Whether the project's manifest declares `[workspace] contrib = true` — a fork whose
    bring-up must branch off the upstream remote instead of origin (see create_workspace)."""
    return bool(_load_manifest(project).get("workspace", {}).get("contrib", False))


def ci_expected(project: str) -> bool:
    """Whether Validate should wait for a GitHub CI rollup. `[validate] ci = "none"` declares that
    no GitHub checks are expected; absent config keeps the historical required-CI behavior."""
    mode = _load_manifest(project).get("validate", {}).get("ci", "required")
    return str(mode).strip().lower() != "none"


# --- Agent worktrees (curator/pipeline/retro/steward's own worktrees, not a task workspace) ---
# Each triggered-agent gets its own named Orca worktree under AGENTS_ROOT (deploy/provision.py
# creates it, pinned to origin/main at provision time). Agents never commit to this repo, so from
# then on the worktree only needs to move forward with origin — the dispatcher's precheck does
# that on every tick instead of a human doing it by hand after every push.
AGENTS_ROOT = WORKSPACES_ROOT / "triggered-agents"
_AGENTS_SPEC_DIR = Path(__file__).resolve().parents[2] / "agents"


def list_agent_worktrees() -> list[tuple[str, str]]:
    """(name, path) for every triggered-agent with a spec (triggered_agents/agents/<name>/
    automation.toml — the same set deploy/provision.py provisions) whose own worktree already
    exists on disk under AGENTS_ROOT. An agent not yet provisioned has nothing to fast-forward, so
    it's silently absent here rather than warned about."""
    names = sorted(p.name for p in _AGENTS_SPEC_DIR.iterdir() if (p / "automation.toml").is_file())
    return [(n, str(AGENTS_ROOT / n)) for n in names if (AGENTS_ROOT / n).is_dir()]


def ff_worktree(path: str, base_branch: str) -> dict:
    """Fast-forward `path`'s checked-out branch to origin/<base_branch>, strictly --ff-only.
    Returns {"ok", "reason", "before", "after"}. Never resets or force-touches the tree: an
    impossible ff (local commits, a diverged history) is left exactly as found, with "reason"
    carrying git's own explanation. Never raises — a gone worktree, a git binary hiccup, or a
    hung git (via `_git`'s timeout) all come back as a plain not-ok result, same as every other
    host call the dispatcher tick must survive without aborting."""
    try:
        before = _git(path, ["rev-parse", "HEAD"])
        if before.returncode != 0:
            return {"ok": False, "reason": (before.stderr or before.stdout).strip() or "not a git worktree"}
        fetch = _git(path, ["fetch", "--quiet", "origin", base_branch])
        if fetch.returncode != 0:
            return {"ok": False, "reason": f"fetch failed: {(fetch.stderr or fetch.stdout).strip()}"}
        merge = _git(path, ["merge", "--ff-only", f"origin/{base_branch}"])
        if merge.returncode != 0:
            return {"ok": False, "reason": (merge.stderr or merge.stdout).strip() or "not fast-forwardable"}
        after = _git(path, ["rev-parse", "HEAD"])
    except WorkspaceError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "reason": None, "before": before.stdout.strip(),
            "after": after.stdout.strip() if after.returncode == 0 else None}


def _repo_info(root: Path) -> dict:
    """`orca repo show`'s repo object for `root` — carries `worktreeBaseRef` (Orca's host-local
    default base ref) and `gitRemoteIdentity.remoteName` (the git remote pointing at the fork's
    upstream, e.g. "upstream")."""
    return _orca_json(["repo", "show", "--repo", f"path:{root}"]).get("repo", {})


def ensure_contrib_base_ref(root: Path, base_branch: str) -> str:
    """Idempotently point `root`'s Orca default base ref at upstream/<base_branch>: the manifest
    (git) declares contrib, this brings Orca's host-local worktreeBaseRef (the mechanism) into
    line with it, so a plain `orca worktree create` without an explicit --base-branch also lands
    right. A no-op when already set — `orca repo set-base-ref` is otherwise a needless host write
    on every bring-up. Returns the upstream remote's git name for the caller to fetch from."""
    info = _repo_info(root)
    remote = (info.get("gitRemoteIdentity") or {}).get("remoteName") or "upstream"
    want = f"{remote}/{base_branch}"
    if info.get("worktreeBaseRef") != want:
        _orca_json(["repo", "set-base-ref", "--repo", f"path:{root}", "--ref", want])
    return remote


def create_workspace(project: str, name: str, base_branch: str) -> str:
    """Fetch base_branch fresh from the right remote, then create an Orca worktree off it (never
    the project's own local checkout state — a stale/switched local branch must not leak into a
    fresh worker/reviewer worktree). Returns the worktree path. Setup is skipped here — we run the
    provisioner ourselves next so we own its exit code and log.

    Contrib forks (manifest `[workspace] contrib = true`, e.g. agent-kanban) branch off the
    upstream remote instead of origin, so a worker's branch never carries the fork's own history
    forward from a stale origin/base — PRs stay clean for the upstream author (agent-kanban-232).
    `ensure_contrib_base_ref` keeps Orca's own default base ref converged to the same declaration.

    `--activate` reveals the new worktree in the Orca app. Without it a freshly created worktree
    has no window for the app to adopt a terminal into, so the head `launch_worker` starts next
    falls back to a background handle and the workspace shows empty in the GUI."""
    root = project_root(project)
    remote = ensure_contrib_base_ref(root, base_branch) if is_contrib(project) else "origin"
    _git_ok(root, ["fetch", remote, base_branch])
    data = _orca_json([
        "worktree", "create", "--repo", f"path:{root}",
        "--name", name, "--base-branch", f"{remote}/{base_branch}", "--setup", "skip", "--no-parent",
        "--activate",
    ])
    wt = data.get("worktree", data)
    path = wt.get("path")
    if not path:
        raise WorkspaceError(f"orca worktree create returned no path for {name!r}")
    return path


def _worktree_holding(repo_workspace: str, branch: str) -> str | None:
    """Path of another worktree of the same repo currently checked out on `branch`, via `git
    worktree list --porcelain`, or None if `branch` is free (or held only by `repo_workspace`
    itself). A stale ref from an earlier bring-up on the same card/PR is otherwise indistinguishable
    from a live one — this is how `_claim_branch` tells them apart."""
    out = _git_ok(repo_workspace, ["worktree", "list", "--porcelain"])
    me = str(Path(repo_workspace).resolve())
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and path and str(Path(path).resolve()) != me:
            return path
    return None


def _claim_branch(workspace: str, branch: str) -> None:
    """Make `workspace` the sole holder of `branch` (git branch -M), freeing it first from
    wherever an earlier bring-up on the same card/PR left it: a Blocked worker's worktree left
    alive for a human, or a reviewer branch that outlived its own worktree's teardown (worktree
    removal drops the checkout, not the ref). Origin is the pipeline's sync point — every actor's
    Done/verdict is already there before this ever runs — so reclaiming a stale local worktree
    loses nothing the pipeline promises to keep. The stale worktree goes through the normal
    teardown path (stop terminals, orca rm, rm -rf/sudo fallback), not a raw `git worktree
    remove`, so orca's own bookkeeping never points at a directory that's quietly gone; `worktree
    prune` after clears the now-stale administrative entry so the rename below never trips over
    it. A plain `-m` fails outright the moment `branch` already exists at all (even unheld) — `-M`
    is what makes the second and later bring-up on the same ref/PR idempotent."""
    other = _worktree_holding(workspace, branch)
    if other:
        teardown(other)
        _git_ok(workspace, ["worktree", "prune"])
    _git_ok(workspace, ["branch", "-M", branch])


def set_branch(workspace: str, branch: str) -> None:
    """Land the worktree on `branch` (see `_claim_branch`) before any head starts — every actor
    gets its own named ref (`pipeline/<ref>`, `review/<ref>`), never whatever name `orca worktree
    create` happened to pick, so no head ever has to create or rename its own branch."""
    _claim_branch(workspace, branch)


def land_pr_head(workspace: str, pr_branch: str, review_branch: str,
                 expected_sha: str | None = None) -> None:
    """Fetch `pr_branch` (the worker's own branch, i.e. the PR's head — same-repo, never a fork)
    from origin into the reviewer's worktree and land it under `review_branch`. This is the
    fetch-not-checkout the reviewer uses instead of `gh pr checkout`: that command tries to check
    out the PR's own branch name locally, which collides ('already used by worktree') with the
    worker's live worktree still sitting on that exact branch.

    `expected_sha`, when given (a contrib card — see spawn_reviewer), pins the reset to that exact
    commit instead of the branch's live tip: a worker that keeps pushing between
    validate._validate_contrib_card's sha check and this call must not slip newer content past the
    sha the review is supposed to cover. The fetch still brings `expected_sha` in regardless — it
    is always an ancestor of the fetched tip, since force-push is against the pipeline's rules."""
    _git_ok(workspace, ["fetch", "origin", pr_branch])
    _git_ok(workspace, ["reset", "--hard", expected_sha or "FETCH_HEAD"])
    _claim_branch(workspace, review_branch)


def remote_head_sha(project: str, branch: str) -> str | None:
    """Current head sha of `branch` on `project`'s origin, read straight off the remote via `git
    ls-remote` — no local fetch, no mutation of project_root (create_workspace's own git plumbing
    root uses the same path). None when `branch` doesn't exist on origin yet or the remote can't be
    reached this tick; validate._validate_contrib_card treats either the same as gh being briefly
    unavailable, giving a still-pushing worker another tick before escalating."""
    root = project_root(project)
    try:
        out = _git_ok(root, ["ls-remote", "origin", f"refs/heads/{branch}"], timeout=GH_TIMEOUT_S)
    except WorkspaceError:
        return None
    line = out.strip()
    return line.split()[0] if line else None


def provision(workspace: str) -> tuple[bool, str]:
    """Run the workspace provisioner (setup+smoke). Return (ok, combined log)."""
    protocol_ok, protocol_log = task_protocol.preflight()
    if not protocol_ok:
        return False, protocol_log
    if not PROVISION.is_file():
        return False, f"provisioner missing: {PROVISION}"
    env = dict(os.environ, ORCA_WORKTREE_PATH=workspace)
    try:
        p = subprocess.run(
            ["python3", str(PROVISION), "--worktree", workspace],
            capture_output=True, text=True, env=env, timeout=PROVISION_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"provision timed out after {PROVISION_TIMEOUT_S}s"
    return p.returncode == 0, protocol_log + "\n" + (p.stdout or "") + (p.stderr or "")


def _write_excluded(workspace: str, name: str, content: str) -> str:
    """Write <ws>/<name> and exclude it from git so a one-time head artifact never gets committed."""
    path = Path(workspace) / name
    path.write_text(content, encoding="utf-8")
    _git_exclude(workspace, name)
    return str(path)


def write_task(workspace: str, content: str) -> str:
    """Write the one-time spec to <ws>/TASK.md, excluded from git."""
    return _write_excluded(workspace, "TASK.md", content)


def _git_exclude(workspace: str, name: str) -> None:
    r = subprocess.run(["git", "-C", workspace, "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return
    rel = r.stdout.strip()
    exclude = Path(rel) if os.path.isabs(rel) else Path(workspace) / rel
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    if name not in existing:
        with exclude.open("a", encoding="utf-8") as f:
            f.write(("" if not existing or existing[-1] == "" else "\n") + name + "\n")


def workspace_path(project: str, name: str) -> str:
    """`<WORKSPACES_ROOT>/<project>/<name>` as a string. `name` is a workspace's base name — the
    same string stored as a card's claim (naming.worker_workspace_base via dispatcher._worker_id)
    — so this lets the dispatcher rebuild the path a claim points at without touching disk."""
    return str(WORKSPACES_ROOT / project / name)


def workspace_exists(project: str, name: str) -> bool:
    """Whether `<WORKSPACES_ROOT>/<project>/<name>` already exists on disk — the collision check
    for a workspace name (naming.dedupe): a re-claim while the previous attempt's worktree is
    still alive (e.g. left on Blocked) must not collide with it."""
    return Path(workspace_path(project, name)).is_dir()


def _terminal_entry_dead_reason(term: dict) -> str | None:
    return terminal_session.entry_dead_reason(term)


def _terminal_entry_live(term: dict) -> bool:
    return terminal_session.entry_live(term)


def _read_terminal_text(handle: str) -> str:
    return terminal_session.read_terminal_text(handle, _orca_json)


def _terminal_expected_kind_dead_reason(term: dict, expected_kind: str | None, handle: str,
                                        read_terminal_text=None) -> str | None:
    return terminal_session.expected_kind_dead_reason(
        term,
        expected_kind,
        handle,
        read_terminal_text=read_terminal_text or _read_terminal_text,
        unavailable_errors=(WorkspaceError, subprocess.TimeoutExpired),
    )


def terminal_status(handle: str, workspace: str | None = None,
                    expected_kind: str | None = None) -> dict:
    """Status for exactly `handle`, optionally scoped to `workspace`.

    `terminal send` has a dangerous degraded mode for this pipeline: an old handle that no longer
    names a live head can leave the nudge landing somewhere else in the worktree, commonly a
    plain shell. Listing live terminals first makes an exited/ghost/missing handle a clean false.
    A writable terminal whose preview is back at a shell prompt is also treated as dead, so the
    caller can relaunch instead of probing by sending the rework prompt.

    `expected_kind` carries the launch contract recorded in cards.json. For Codex TUI heads, Orca
    does not expose a stable agent id, so the tracked handle must also look like the Codex TUI
    screen. A plain shell or long-running shell command in the same worktree fails closed instead
    of receiving a follow-up prompt.

    `known=false` means Orca itself could not be queried. Callers should keep their ordinary
    watchdog path for that case instead of treating a transport failure as proof that the tracked
    terminal died."""
    status = terminal_session.terminal_status(
        handle,
        workspace,
        expected_kind,
        orca_json=_orca_json,
        unavailable_errors=(WorkspaceError, subprocess.TimeoutExpired),
    )
    if expected_kind == "codex-tui" and workspace:
        activity = codex_sessions.latest_activity_for(workspace)
        if activity:
            if status.get("live") and activity > (status.get("last_activity") or 0):
                status = {**status, "last_activity": activity}
            elif status.get("known") and status.get("reason") == "not-codex-tui":
                status = {"known": True, "live": True, "reason": "live", "last_activity": activity}
    return status


def terminal_live(handle: str, workspace: str | None = None,
                  expected_kind: str | None = None) -> bool:
    """Whether `handle` is a live Orca terminal, optionally scoped to `workspace`."""
    return bool(terminal_status(handle, workspace, expected_kind).get("live"))


def _terminal_belongs_to_workspace(term: dict, workspace: str) -> bool:
    return terminal_session.belongs_to_workspace(term, workspace)


def terminal_activity(handle: str, workspace: str) -> float | None:
    """Latest output time for exactly `handle` in `workspace`, epoch seconds.

    Workspace-level max activity is unsafe for the watchdog: a plain shell left in the worktree can
    keep producing output after the worker/reviewer head is gone. This helper only accepts the
    saved terminal handle, scoped to the expected workspace, and only while the entry still looks
    like a live head instead of a shell prompt."""
    status = terminal_status(handle, workspace)
    if not status.get("live"):
        return None
    return status.get("last_activity")


def rename_terminal(handle: str, title: str) -> bool:
    """Best-effort: (re)apply a human-readable title to `handle`'s tab. Claude Code's own
    session dynamically rewrites its tab title once it starts working (spinner + an inferred
    blurb of the current step — confirmed live, see PR description), so a rename at spawn alone
    does not stick; callers reapply this every tick to pin the pipeline's title back."""
    if not handle:
        return False
    try:
        p = subprocess.run([ORCA, "terminal", "rename", "--terminal", handle, "--title", title],
                           capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _worker_prompt() -> str:
    return (
        "Ты — воркер task-пайплайна. Твоя задача целиком в TASK.md в корне воркспейса — прочти "
        "его первым и следуй ему. Роль на доске — worker (BOARD_ROLE уже выставлен): карточку сам "
        f"не двигаешь, {task_protocol.launch_instruction()}. TASK.md в репо не коммить."
    )


def _launch_spec(head: str | None, workspace: str) -> heads.LaunchSpec:
    return heads.render_launch(head or heads.DEFAULT_PROFILE, role="worker", prompt=_worker_prompt(),
                               workspace=workspace)


def terminal_kind(head: str | None) -> str | None:
    return heads.terminal_kind(head or heads.DEFAULT_PROFILE)


def ensure_trust(workspace: str) -> None:
    """Проставить folder trust Claude Code для свежего worktree. Без этого голова виснет на
    интерактивном вопросе «доверяешь ли папке» (та же логика — deploy/provision.py у агентов)."""
    try:
        claude_env.ensure_trust(CLAUDE_JSON, workspace)
    except claude_env.ClaudeConfigError as e:
        raise WorkspaceError(str(e)) from e


def ensure_theme() -> None:
    """Проставить тему в ~/.claude.json — иначе свежая голова виснет на первом онбординге
    («выбери стиль текста»), тот же класс бага, что и folder trust, но ключ глобальный, а не
    per-workspace."""
    try:
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        raise WorkspaceError(str(e)) from e


def _terminal_identity_tokens(term: dict) -> set[str]:
    tokens = set()
    for key in ("handle", "id", "ptyId", "leafId", "paneKey"):
        value = term.get(key)
        if value:
            tokens.add(f"{key}:{value}")
    tab_id = term.get("tabId")
    leaf_id = term.get("leafId")
    if tab_id and leaf_id:
        tokens.add(f"tab_leaf:{tab_id}:{leaf_id}")
    pane_key = term.get("paneKey")
    if isinstance(pane_key, str) and ":" in pane_key:
        tab, leaf = pane_key.split(":", 1)
        if tab and leaf:
            tokens.add(f"tab_leaf:{tab}:{leaf}")
            tokens.add(f"leafId:{leaf}")
    return tokens


def _close_orphan_terminals(workspace: str, keep_handle: str,
                            keep_terminal: dict | None = None) -> None:
    """Best-effort: close every terminal in `workspace` other than `keep_handle`. `create_workspace`'s
    `--activate` makes Orca reveal the fresh worktree with an empty default shell tab already open
    (confirmed empirically — orca has no `--activate --no-default-terminal` to suppress it, see
    ~/projects/orca/PR-IDEAS.md); the head's own terminal, just created by launch_worker/
    spawn_reviewer, then leaves that shell tab behind as a permanent orphan no head reads or closes
    on its own. A listing/close failure is swallowed, same discipline as stop_terminals — a
    leftover empty tab is cosmetic, not a bring-up blocker, so it must never fail the caller.

    `keep_handle` empty (launch_worker/spawn_reviewer's `term.get("handle") or term.get("id") or
    ""` fallback, for a create response that carried neither) must never fall through to "close
    everything" — an empty string cannot equal any real handle in the loop below, so without this
    guard the just-spawned head itself would be closed alongside the orphan shell (review verdict,
    triggered-agents-247). Bailing out here is exactly the pre-fix behavior for that same
    degenerate response: no orphan gets closed, but the head survives."""
    if not keep_handle:
        return
    keep_tokens = _terminal_identity_tokens({"handle": keep_handle})
    if keep_terminal:
        keep_tokens.update(_terminal_identity_tokens(keep_terminal))
    try:
        data = _orca_json(["terminal", "list", "--worktree", f"path:{workspace}", "--limit", "50"])
    except (WorkspaceError, subprocess.TimeoutExpired):
        return
    for t in data.get("terminals") or []:
        handle = t.get("handle") or t.get("id")
        if not handle or handle == keep_handle:
            continue
        if keep_tokens and (_terminal_identity_tokens(t) & keep_tokens):
            continue
        try:
            _orca_json(["terminal", "close", "--terminal", handle])
        except (WorkspaceError, subprocess.TimeoutExpired):
            pass


def _close_terminal(handle: str) -> None:
    if not handle:
        return
    try:
        _orca_json(["terminal", "close", "--terminal", handle])
    except (WorkspaceError, subprocess.TimeoutExpired):
        pass


def _deliver_initial_prompt(handle: str, launch: heads.LaunchSpec, workspace: str) -> None:
    if not launch.initial_prompt:
        return
    if not handle:
        raise WorkspaceError("terminal create returned no handle for TUI prompt delivery")
    try:
        prompt_delivery.deliver_initial_prompt(
            launch.initial_prompt,
            workspace,
            handle,
            lambda: _orca_json(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                "--timeout-ms", str(TUI_IDLE_TIMEOUT_MS)]),
            lambda: _orca_json(["terminal", "send", "--terminal", handle,
                                "--text", launch.initial_prompt, "--enter"]),
            lambda: _orca_json(["terminal", "send", "--terminal", handle, "--text", "", "--enter"]),
            lambda: _read_terminal_text(handle),
            log_event=lambda **fields: STATE.log_run("tui-delivery", **fields),
            check_delay_s=TUI_DELIVERY_CHECK_DELAY_S,
            timeout_s=TUI_DELIVERY_TIMEOUT_S,
            resend_grace_s=TUI_DELIVERY_RESEND_GRACE_S,
        )
    except prompt_delivery.InjectDeliveryError as e:
        raise InjectDeliveryError(str(e)) from e


def _create_head_terminal(workspace: str, title: str, launch: heads.LaunchSpec) -> str:
    data = _orca_json(["terminal", "create", "--worktree", f"path:{workspace}",
                       "--title", title, "--command", launch.command])
    term = data.get("terminal", data)
    handle = term.get("handle") or term.get("id") or ""
    try:
        _deliver_initial_prompt(handle, launch, workspace)
    except Exception:
        _close_terminal(handle)
        raise
    _close_orphan_terminals(workspace, handle, term)
    return handle


def launch_worker(workspace: str, head: str | None, worker_id: str, title: str) -> str:
    """Spawn the worker head in the workspace; return the terminal handle. `title` seeds the
    tab's display name; the caller (dispatcher) pins it back every tick via rename_terminal since
    Claude Code overwrites it once the head starts working."""
    ensure_trust(workspace)
    ensure_theme()
    return _create_head_terminal(workspace, title, _launch_spec(head, workspace))


def _reviewer_prompt() -> str:
    return (
        "Ты — независимая голова-ревьюер task-пайплайна (слой 3 валидации). Твоя работа целиком в "
        "REVIEW.md в корне воркспейса — прочти его первым и следуй ему. Роль на доске — reviewer "
        "(BOARD_ROLE уже выставлен): прав на код нет, не коммить и не пушь; артефакты — один "
        "вердикт-коммент и, при необходимости, карточки-идеи через board-CLI."
    )


def _reviewer_launch_spec(head: str | None = None, workspace: str | None = None) -> heads.LaunchSpec:
    return heads.render_launch(head or REVIEWER_HEAD, role="reviewer", prompt=_reviewer_prompt(),
                               workspace=workspace)


def reviewer_terminal_kind(head: str | None = None) -> str | None:
    return heads.terminal_kind(head or REVIEWER_HEAD)


def spawn_reviewer(project: str, worker_id: str, base_branch: str, review_md: str, title: str,
                   pr_branch: str, review_branch: str, head_sha: str | None = None,
                   review_head: str | None = None) -> tuple[str, str]:
    """Bring up the layer-3 reviewer head: a fresh worktree off base_branch, landed on the PR's
    head content under its own `review_branch` (land_pr_head — fetch, not `gh pr checkout`, so the
    reviewer never touches the worker's own branch), the REVIEW.md prompt, then the head. No
    provisioning — the reviewer only reads code and drives gh/board-CLI, so it needs no app deps.
    Returns (workspace, terminal handle). `title` seeds the tab's display name (see launch_worker).
    `review_head` is the reviewer profile chosen for this card; omitted means REVIEWER_HEAD.

    `head_sha`, given only for a contrib card (validate._spawn_reviewer), pins the reviewer's
    worktree to the exact sha the worker's report claimed rather than the branch's live tip — see
    land_pr_head. None for a regular PR card (out of scope: PR-card sha pinning has a human merge
    gate downstream already)."""
    ws = create_workspace(project, worker_id, base_branch)
    try:
        land_pr_head(ws, pr_branch, review_branch, head_sha)
        _write_excluded(ws, "REVIEW.md", review_md)
        ensure_trust(ws)
        ensure_theme()
        handle = _create_head_terminal(ws, title, _reviewer_launch_spec(review_head, ws))
    except Exception as e:
        teardown(ws)   # the worktree already exists; don't leave an orphan on a failed launch
        # Normalize to WorkspaceError so every bring-up failure (orca error/timeout, an OSError from
        # writing REVIEW.md) reaches the dispatcher's single spawn-failure path and its retry cap.
        if isinstance(e, WorkspaceError):
            raise
        raise WorkspaceError(f"reviewer head bring-up failed: {e}") from e
    return ws, handle


def relaunch_reviewer(workspace: str, worker_id: str, title: str,
                      review_head: str | None = None) -> str:
    """Re-spawn just the reviewer head's terminal in an already-provisioned review workspace —
    used by dispatcher.resume() to continue a hard-paused reviewer: the worktree and REVIEW.md are
    exactly as spawn_reviewer left them (stop_terminals never touches either), so there is nothing
    left to redo but launch_worker's own tail (ensure_trust/ensure_theme, terminal create, close
    orphans) against the reviewer's own command instead of the worker's."""
    ensure_trust(workspace)
    ensure_theme()
    return _create_head_terminal(workspace, title, _reviewer_launch_spec(review_head, workspace))


def activity(workspace: str) -> float | None:
    """Latest terminal output time in the workspace, epoch seconds, or None if unknown.

    Legacy helper for callers that truly need workspace-level activity. Pipeline watchdogs must
    use terminal_activity(handle, workspace) so an unrelated shell cannot keep a card alive."""
    try:
        data = _orca_json(["terminal", "list", "--worktree", f"path:{workspace}", "--limit", "50"])
    except (WorkspaceError, subprocess.TimeoutExpired):
        return None
    last_ms = [t.get("lastOutputAt") for t in (data.get("terminals") or []) if t.get("lastOutputAt")]
    return max(last_ms) / 1000.0 if last_ms else None


def notify(handle: str, text: str) -> bool:
    """Nudge the live worker head: type `text` into its terminal (its Claude session) and Enter.
    Best-effort — a missing handle or an orca error is swallowed. The board comment is the durable
    record of a CI-red return; this is only a convenience so the worker sees it without a refresh."""
    if not handle:
        return False
    try:
        p = subprocess.run([ORCA, "terminal", "send", "--terminal", handle, "--text", text, "--enter"],
                           capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


# --- gh PR polling (Validate layer 1: mechanics, zero LLM) ------------------------------------
# The dispatcher polls each Validate card's PR here. Every gh touch returns None on any trouble
# (gh missing, non-zero exit, network/API error, unparsable output): None means "unknown, retry
# next tick", never a verdict — a transient gh outage must not move a card or ping a human.

_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _gh_json(args: list[str]):
    import json

    try:
        p = subprocess.run([GH, *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _check_result(item: dict) -> str:
    """One rollup entry -> 'pass' | 'fail' | 'pending'. Handles both a GitHub-Actions CheckRun
    (status/conclusion) and a legacy commit StatusContext (state)."""
    if item.get("__typename") == "StatusContext" or ("state" in item and "status" not in item):
        state = str(item.get("state", "")).upper()
        if state in ("FAILURE", "ERROR"):
            return "fail"
        if state == "SUCCESS":
            return "pass"
        return "pending"
    if str(item.get("status", "")).upper() != "COMPLETED":
        return "pending"
    return "fail" if str(item.get("conclusion", "")).upper() in _FAIL_CONCLUSIONS else "pass"


def _rollup(items: list[dict]) -> tuple[str, dict | None]:
    """Overall CI state from the rollup: 'PENDING' while any job is still running (even next to an
    already-failed one — a flaky-looking early failure must not bounce the card before the rest of
    the suite finishes), then 'FAILURE' (with the first failing entry) once every job is terminal,
    'SUCCESS' if none failed, or 'NONE' when the PR has no checks at all."""
    if not items:
        return "NONE", None
    first_fail = None
    pending = False
    for it in items:
        r = _check_result(it)
        if r == "fail" and first_fail is None:
            first_fail = it
        elif r == "pending":
            pending = True
    if pending:
        return "PENDING", None
    if first_fail is not None:
        return "FAILURE", first_fail
    return "SUCCESS", None


def _failed_log(item: dict, lines: int = _GH_LOG_TAIL_LINES) -> str | None:
    """Tail of the failed job's log via `gh run view --log-failed`, or None if not a Actions
    run (e.g. an external StatusContext) or gh cannot fetch it. Raw — the dispatcher scrubs."""
    m = _RUN_URL_RE.search(item.get("detailsUrl") or item.get("targetUrl") or "")
    if not m:
        return None
    repo, run_id = m.group(1), m.group(2)
    try:
        p = subprocess.run([GH, "run", "view", run_id, "-R", repo, "--log-failed"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    tail = "\n".join((p.stdout or "").strip().splitlines()[-lines:])
    return tail or None


def poll_pr(pr_url: str) -> dict | None:
    """Poll a PR's merge state and CI rollup via gh. Returns
        {"merged": bool, "state": str, "rollup": "SUCCESS"|"FAILURE"|"PENDING"|"NONE",
         "failed_job": str|None, "failed_log": str|None, "head_sha": str|None, "base_sha": str|None,
         "mergeable": "CLEAN"|"BEHIND"|"CONFLICTING"|"UNKNOWN", "base_branch": str|None}
    or None when gh cannot answer (missing, error, PR gone) — an unknown to retry, not a verdict.
    `mergeable` (model.merge_status) is the base-freshness signal, apart from `rollup` (CI);
    `base_branch` is the PR's ACTUAL base ref — recovery merges the base the PR really targets, not
    a card guess. Failed-job log is fetched only on FAILURE."""
    data = _gh_json(["pr", "view", pr_url, "--json", "state,mergedAt,statusCheckRollup,mergeable,"
                     "mergeStateStatus,headRefOid,baseRefOid,baseRefName"])
    if not isinstance(data, dict):
        return None
    rollup, failed = _rollup(data.get("statusCheckRollup") or [])
    out = {
        "merged": bool(data.get("mergedAt")),
        "state": data.get("state", ""),
        "rollup": rollup,
        "failed_job": None,
        "failed_log": None,
        "mergeable": model.merge_status(data.get("mergeable"), data.get("mergeStateStatus")),
        "head_sha": data.get("headRefOid") or None,
        "base_sha": data.get("baseRefOid") or None,
        "base_branch": data.get("baseRefName") or None,
    }
    if failed is not None:
        out["failed_job"] = failed.get("name") or failed.get("context") or "?"
        out["failed_log"] = _failed_log(failed)
    return out


def merge_pr(pr_url: str) -> dict:
    """Squash-merge a PR via gh. Returns {"ok": bool, "error": str|None}.

    Unlike poll_pr/pr_branch/run_stand, this is never a "retry next tick" call: the dispatcher
    calls it at most once per green review (see validate_review.review_green), and any failure
    here — including gh being unavailable — is a final outcome the caller reports and Blocks on,
    not an unknown to poll again."""
    try:
        p = subprocess.run([GH, "pr", "merge", pr_url, "--squash"],
                           capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    if p.returncode == 0:
        return {"ok": True, "error": None}
    return {"ok": False, "error": (p.stderr or p.stdout).strip() or f"gh exit {p.returncode}"}


def pr_files(pr_url: str) -> list[str] | None:
    """Paths changed by a PR (its cumulative diff — the same set a squash merge lands as one
    commit), via gh — same None-on-trouble contract as poll_pr/pr_branch/pr_base_branch. gh still
    answers `pr view --json files` for a PR gh itself reports merged/closed, so this needs no
    local git fetch/diff against the merge commit (validate._apply_provision_after_merge calls it
    right after gh has already reported the PR merged)."""
    data = _gh_json(["pr", "view", pr_url, "--json", "files"])
    if not isinstance(data, dict):
        return None
    return [f.get("path") for f in (data.get("files") or []) if f.get("path")]


# --- Post-merge provision apply (triggered-agents-256) ----------------------------------------
# deploy/provision.py refuses to run from anywhere but the canonical checkout (~/triggered-agents,
# see its own CANONICAL_ROOT guard, triggered-agents-257) — the dispatcher runs from ITS OWN named
# worktree instead (a sibling under the same workspaces root, never that checkout), so applying a
# freshly-merged provision.py/automation.toml can't just run the copy sitting in the dispatcher's
# own cwd. Nothing ever commits to the canonical checkout, so fetching+hard-resetting it to
# origin/main right before every apply is safe and mirrors the same discipline deploy/provision.py
# already applies to every per-agent worktree (ensure_worktree) — without this the checkout stays
# on whatever commit a human last happened to `git pull`, and the freshly-merged logic/spec would
# never actually run.
TRIGGERED_AGENTS_CANONICAL_ROOT = Path(os.environ.get("TA_CANONICAL_ROOT") or Path.home() / "triggered-agents")
_DEPLOY_PROVISION = TRIGGERED_AGENTS_CANONICAL_ROOT / "deploy" / "provision.py"
PROVISION_APPLY_TIMEOUT_S = int(os.environ.get("TA_PROVISION_APPLY_TIMEOUT_S", "300"))


def apply_provision(agents: list[str]) -> dict:
    """Fast-forward the canonical triggered-agents checkout to origin/main, then run its
    deploy/provision.py for `agents` (empty list -> every agent with a spec, mirroring
    deploy/provision.py's own argv-empty convention). Returns {"ok": bool, "log": str} — every
    step's combined stdout+stderr, one after another, untruncated (the caller scrubs/tails before
    logging it). Never raises: a failed fetch/reset/provision run all fold into a non-ok result —
    this is a one-shot post-merge action with no retry (validate._apply_provision_after_merge).

    A named agent (a non-empty `agents`, i.e. only that agent's own automation.toml changed) is
    filtered against the FRESH post-reset tree before the provision.py call: a merge that DELETES
    an agent's automation.toml (decommissioning it — the exact ta-board precedent this card was
    written around) still names that agent in the diff, but running `provision.py <agent>` for a
    spec that no longer exists is a guaranteed SystemExit — a false 'apply failed' signal on every
    single decommission merge, not a real failure. An agent whose spec is simply gone this way is
    silently nothing-to-provision here (noted in the log, not an error) — tearing down its now-
    orphaned live unit is the drift check's "extra" case (steward/drift.py), a deliberate human
    step, not an automatic one. The empty-list ("all") case is untouched: deploy/provision.py's
    own argv-empty path already computes its agent list fresh from the same post-reset tree, so a
    removed agent is naturally absent from it with no filtering needed here."""
    root = str(TRIGGERED_AGENTS_CANONICAL_ROOT)
    log_parts = []
    for step in (["git", "-C", root, "fetch", "--quiet", "origin", "main"],
                 ["git", "-C", root, "reset", "--hard", "origin/main"]):
        try:
            p = subprocess.run(step, capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            log_parts.append(f"$ {' '.join(step)}\n{e}")
            return {"ok": False, "log": "\n".join(log_parts)}
        log_parts.append(f"$ {' '.join(step)}\n{p.stdout}{p.stderr}")
        if p.returncode != 0:
            return {"ok": False, "log": "\n".join(log_parts)}
    if agents:
        specs_dir = TRIGGERED_AGENTS_CANONICAL_ROOT / "triggered_agents" / "agents"
        existing = sorted(a for a in agents if (specs_dir / a / "automation.toml").is_file())
        missing = sorted(set(agents) - set(existing))
        if missing:
            log_parts.append(
                f"no automation.toml on origin/main for: {', '.join(missing)} (decommissioned in "
                f"this merge, or never existed) — nothing to provision for them, skipped")
        if not existing:
            return {"ok": True, "log": "\n".join(log_parts)}
        agents = existing
    cmd = ["python3", str(_DEPLOY_PROVISION), *agents]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=PROVISION_APPLY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        log_parts.append(f"$ {' '.join(cmd)}\n{e}")
        return {"ok": False, "log": "\n".join(log_parts)}
    log_parts.append(f"$ {' '.join(cmd)}\n{p.stdout}{p.stderr}")
    return {"ok": p.returncode == 0, "log": "\n".join(log_parts)}


# --- Stand deploy + e2e (Validate layer 2) ----------------------------------------------------
# The dispatcher decides on the board; the heavy host work (git checkout, compose, e2e) lives in
# stand.py. These delegates keep the dispatcher talking to a single host boundary (worker.py) and
# stay stubbable in the dispatcher unit tests.


def read_stand_config(project: str) -> dict | None:
    """The project's `[stand]` manifest section, or None when it has no stand (layer 2 skipped)."""
    return stand.read_config(project_root(project))


def pr_branch(pr_url: str) -> str | None:
    """The PR's head branch via gh, or None if gh cannot answer (same contract as poll_pr)."""
    data = _gh_json(["pr", "view", pr_url, "--json", "headRefName"])
    if not isinstance(data, dict):
        return None
    return data.get("headRefName") or None


def pr_base_branch(pr_url: str) -> str | None:
    """The PR's actual base branch via gh, or None if gh cannot answer (same contract as poll_pr) —
    read by validate_review.review_green right before merge_pr, to catch a PR opened against the
    wrong base (e.g. a sprint-shim card whose worker ignored TASK.md and let `gh pr create`
    default to main) instead of silently squash-merging it there."""
    data = _gh_json(["pr", "view", pr_url, "--json", "baseRefName"])
    if not isinstance(data, dict):
        return None
    return data.get("baseRefName") or None


def run_stand(project: str, branch: str, cfg: dict) -> dict | None:
    """Deploy `branch` to the project's stand and run e2e. Returns stand.run's result dict, or
    None on an unexpected host error (treated as 'unknown, retry next tick', never a verdict)."""
    try:
        return stand.run(project, branch, cfg, project_root(project))
    except Exception:  # noqa: BLE001 — a stand host crash is an unknown, not a red verdict
        return None


def _guard_workspace_path(workspace: str) -> Path:
    """Refuse a path outside WORKSPACES_ROOT before teardown runs any rm — including the root
    itself, whose removal would take every other project's workspace with it. teardown ends in
    rm -rf/sudo rm -rf, so a wrong path here is a real deletion, not just a board mistake."""
    path = Path(workspace).resolve()
    if path == WORKSPACES_ROOT or WORKSPACES_ROOT not in path.parents:
        raise WorkspaceError(f"teardown refuses a path outside {WORKSPACES_ROOT}: {workspace}")
    return path


def stop_terminals(workspace: str) -> None:
    """Best-effort: close every terminal open in `workspace` before its worktree goes away, so the
    head is not left an orphan with a deleted cwd. Failure here is not fatal — teardown still
    proceeds to remove the worktree."""
    try:
        subprocess.run([ORCA, "terminal", "stop", "--worktree", f"path:{workspace}"],
                       capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _rm_step(args: list[str]) -> subprocess.CompletedProcess:
    """Run one teardown removal step bounded by ORCA_TIMEOUT_S — the same discipline `_orca_json`
    and `stop_terminals` already apply to every other orca/host touch, so a stuck orca daemon or a
    wedged rm (e.g. a stale NFS handle) can never hang a dispatcher tick forever. A timeout counts
    as a failed step (non-zero return), not an exception, so the existing fallback chain still
    runs the next step instead of the tick crashing on it."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def teardown(workspace: str) -> None:
    """Stop the workspace's terminals, then best-effort remove the Orca worktree. A dev-stage
    backend without USER leaves root-owned __pycache__ in the tree, so a plain remove can fail on
    permissions — fall back to rm -rf, then sudo rm -rf (logged: after the personal_site PR#24
    non-root fix this should stay silent, so a trip here is a signal the root-owned grief is
    back)."""
    path = _guard_workspace_path(workspace)
    stop_terminals(workspace)
    r = _rm_step([ORCA, "worktree", "rm", "--worktree", f"path:{workspace}", "--force"])
    if r.returncode == 0 and not path.exists():
        return
    if path.exists():
        p = _rm_step(["rm", "-rf", workspace])
        if p.returncode != 0 and path.exists():
            STATE.log_run("teardown-sudo-fallback", workspace=workspace)
            _rm_step(["sudo", "rm", "-rf", workspace])
