"""Durable ownership and settlement of card and observer Git residue.

CleanupJournal is the only producer of dispatcher/cleanup.json. CleanupOwner is
its replay owner; inventory is its supported reader. Board archive and close
only request settlement. They never destroy work from inside a board transaction.
The installation lock also covers claims, launch/replacement and dispatcher ticks.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import functools
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from secretary.dispatch.types import HostError
from secretary.infra import git_worktree

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_held = threading.local()


@contextlib.contextmanager
def ownership_lock(data_dir: Path) -> Iterator[None]:
    """Reentrant in one thread, exclusive across threads and installed processes."""
    path = str(Path(data_dir).resolve() / "dispatcher" / "cleanup.lock")
    with _guard:
        lock = _locks.setdefault(path, threading.RLock())
    with lock:
        held = getattr(_held, "paths", set())
        if path in held:
            yield
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _held.paths = held | {path}
            try:
                yield
            finally:
                _held.paths = held
                fcntl.flock(handle, fcntl.LOCK_UN)


def serialized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with ownership_lock(self.data_dir):
            return method(self, *args, **kwargs)
    return wrapped


def _git(repo: Path, *args: str, allow: bool = False) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, timeout=30)
    if result.returncode and not allow:
        raise HostError(f"cleanup Git {args[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout.strip() if not result.returncode else ""


def _canonical(value: str | Path) -> Path:
    path = Path(value).expanduser().absolute()
    if path != path.resolve():
        raise HostError(f"cleanup path is substituted or noncanonical: {path}")
    return path


def _ref_tip(repo: Path, ref: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                            capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1 and not result.stderr.strip():
        return ""
    raise HostError("cleanup candidate ref evidence is unreadable")


def _registered(repo: Path) -> list[dict[str, str]]:
    result = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise HostError("cleanup worktree registrations are unreadable")
    rows: list[dict[str, str]] = []
    row: dict[str, str] = {}
    for item in result.stdout.split("\0"):
        if not item:
            if row:
                rows.append(row)
                row = {}
        else:
            key, _, value = item.partition(" ")
            row[key] = value
    if row:
        rows.append(row)
    return rows


def _identity(repo: Path, workspace: str, branch: str) -> dict[str, Any]:
    repo = _canonical(repo)
    if _git(repo, "rev-parse", "--show-toplevel") != str(repo):
        raise HostError("cleanup catalog repository Git root differs")
    common = _canonical(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    ref = "refs/heads/" + branch if branch else ""
    tip = _git(repo, "rev-parse", "--verify", ref) if ref else ""
    result: dict[str, Any] = {"repo": str(repo), "common": str(common), "branch": ref, "tip": tip}
    if not workspace:
        return result
    path = _canonical(workspace)
    listed = [row for row in _registered(repo) if row.get("worktree") == str(path)]
    if len(listed) != 1 or not path.is_dir() or path == repo:
        raise HostError("cleanup workspace is not an exact registered linked worktree")
    row = listed[0]
    if (row.get("branch", "") != ref or (ref and row.get("HEAD") != tip)
            or "locked" in row):
        raise HostError("cleanup workspace ref, tip or registration differs")
    actual_common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    admin = _canonical(_git(path, "rev-parse", "--absolute-git-dir"))
    if actual_common != str(common) or not admin.is_relative_to(common / "worktrees"):
        raise HostError("cleanup workspace common directory differs")
    if _git(path, "rev-parse", "--show-toplevel") != str(path):
        raise HostError("cleanup workspace Git root differs")
    if not (path / ".git").is_file() or (path / ".git").is_symlink():
        raise HostError("cleanup workspace Git registration is substituted")
    stat = path.stat()
    admin_stat = admin.stat()
    result.update(workspace=str(path), device=stat.st_dev, inode=stat.st_ino,
                  admin=str(admin), gitfile=(path / ".git").read_text(),
                  admin_device=admin_stat.st_dev, admin_inode=admin_stat.st_ino,
                  admin_gitdir=(admin / "gitdir").read_text(),
                  admin_commondir=(admin / "commondir").read_text(),
                  admin_head=(admin / "HEAD").read_text(),
                  tip=row["HEAD"])
    return result


class CleanupJournal:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "dispatcher" / "cleanup.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"version": 1, "intents": {}, "generated": {}}
        except (OSError, ValueError) as exc:
            raise HostError(f"cleanup evidence unreadable: {exc}") from exc
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("intents"), dict)
                or not isinstance(value.get("generated"), dict)):
            raise HostError("cleanup evidence has an unsupported shape")
        return value

    def save(self, value: dict[str, Any]) -> None:
        """Fsync both the intent and its publication before allowing effects."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".cleanup-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @serialized
    def generated(self, path: Path, body: str) -> None:
        value = self.read()
        value["generated"][str(path.absolute())] = hashlib.sha256(body.encode()).hexdigest()
        self.save(value)

    @serialized
    def remember(self, task: dict[str, Any], record: dict[str, Any], *,
                 identity: dict[str, Any] | None = None, disposition: str = "owned") -> str:
        value = self.read()
        attempt = str(record.get("attempt_id") or "")
        key = hashlib.sha256((str(task["ref"]) + ":" + attempt).encode()).hexdigest()
        previous = value["intents"].get(key)
        if previous and previous.get("disposition") != "owned":
            if not previous.get("identity") and identity and not previous["progress"].get("removal_started"):
                previous["identity"] = identity
                self.save(value)
            return key
        intent = previous or {"task": copy.deepcopy(task), "record": copy.deepcopy(record),
                              "identity": identity, "heads": [], "progress": {},
                              "status": "owned", "reason": "", "disposition": "owned"}
        if identity is not None:
            if intent.get("identity"):
                old = intent["identity"]
                for field in ("repo", "common", "workspace", "device", "inode", "admin", "gitfile", "branch",
                              "admin_device", "admin_inode", "admin_gitdir", "admin_commondir"):
                    if old.get(field) != identity.get(field):
                        raise HostError("cleanup ownership changed within the recorded attempt")
            intent["identity"] = identity
        intent["record"] = copy.deepcopy(record)
        for field in ("worker_head_run", "review_head_run", "head_run"):
            head = record.get(field)
            if isinstance(head, dict) and head.get("run_id") and head not in intent["heads"]:
                # Keep all generations, including heads replaced during this attempt.
                intent["heads"].append(copy.deepcopy(head))
        launch = record.get("launch_intent") or {}
        head = launch.get("head_run")
        if isinstance(head, dict) and head.get("run_id") and head not in intent["heads"]:
            intent["heads"].append(copy.deepcopy(head))
        intent["disposition"] = disposition
        if disposition != "owned":
            intent["status"] = "pending"
        value["intents"][key] = intent
        self.save(value)
        return key

    @serialized
    def request(self, task: dict[str, Any], disposition: str,
                record: dict[str, Any] | None = None) -> str:
        if record is None:
            state = self.data_dir / "dispatcher" / "production-state.json"
            try:
                payload = json.loads(state.read_text())
                records = payload.get("records", {})
                if not isinstance(records, dict):
                    raise ValueError("invalid records")
                record = records.get(task["ref"], {})
            except FileNotFoundError:
                record = {}
            except (OSError, ValueError, AttributeError) as exc:
                raise HostError("cleanup cannot read dispatcher ownership") from exc
        if not record:
            value = self.read()
            matches = [key for key, intent in value["intents"].items()
                       if intent["task"]["id"] == task["id"] and intent["disposition"] == "owned"]
            if matches:
                for key in matches:
                    value["intents"][key]["disposition"] = disposition
                    value["intents"][key]["status"] = "pending"
                self.save(value)
                return matches[-1]
        return self.remember(task, record or {}, disposition=disposition)

    @serialized
    def observer_handoff(self, sprint: dict[str, Any]) -> str | None:
        """Stage the external observer obligation in the existing close transaction."""
        try:
            payload = json.loads((self.data_dir / "dispatcher" / "production-state.json").read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise HostError("observer close handoff cannot read current ownership") from exc
        observers = payload.get("observers", {})
        if not isinstance(observers, dict):
            raise HostError("observer close handoff has unreadable ownership")
        record = observers.get(sprint["ref"])
        if not record:
            return None
        raw = copy.deepcopy(record)
        raw.update(attempt_id=str(record.get("generation", "")) + ":" + str(record.get("launches", 0)),
                   worker=record.get("generation", ""))
        task = {"id": sprint["id"], "ref": sprint["ref"], "sprint": sprint["ref"],
                "project": "observers", "kind": "observer", "claim": {}}
        return self.remember(task, raw, disposition="observer-close")

    def summary(self, *, sprint: str = "") -> list[dict[str, Any]]:
        return [{"id": key, "ref": intent["task"]["ref"], "status": intent["status"],
                 "disposition": intent["disposition"], "reason": intent["reason"],
                 "identity": intent.get("identity"), "commit_proof": intent.get("commit_proof"),
                 "progress": intent["progress"]}
                for key, intent in sorted(self.read()["intents"].items())
                if not sprint or intent["task"].get("sprint") == sprint]

    def admission_refusal(self, reference: str) -> str:
        for intent in self.read()["intents"].values():
            if (intent["task"]["ref"] == reference and intent["status"] in {"pending", "preserved"}
                    and not intent["progress"].get("heads_stopped")):
                return "previous cleanup has not verified head settlement for " + reference
        return ""


class CleanupOwner:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.data_dir = Path(runtime.data_dir)
        self.journal = CleanupJournal(self.data_dir)

    @serialized
    def remember(self, task: dict[str, Any], record: Any) -> str:
        binding = self.runtime.catalog.binding(task["project"])
        branch = "pipeline/" + task["ref"]
        identity = None
        if record.workspace and Path(record.workspace).exists():
            expected = self.data_dir / "workspaces" / task["project"] / record.worker
            if Path(record.workspace).absolute() == expected.absolute():
                try:
                    identity = _identity(Path(binding["repo"]), record.workspace, branch)
                except HostError:
                    # Retain unknown/legacy records too, before reconciliation
                    # can drop them. Absence of proof authorizes no effects.
                    pass
        return self.journal.remember(task, record.to_json(), identity=identity)

    @serialized
    def cleanup(self, task: dict[str, Any], record: Any, disposition: str) -> dict[str, Any]:
        key = self.remember(task, record)
        self.journal.request(task, disposition, record.to_json())
        return self.replay_one(key)

    def _state(self) -> dict[str, Any]:
        path = self.data_dir / "dispatcher" / "production-state.json"
        try:
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return {"records": {}, "observers": {}}
        except (OSError, ValueError) as exc:
            raise HostError("current dispatcher ownership is unreadable") from exc
        if not isinstance(value, dict) or not isinstance(value.get("records", {}), dict):
            raise HostError("current dispatcher ownership is malformed")
        if value.get("phase") == "unavailable":
            raise HostError("current dispatcher ownership was unavailable")
        return value

    @serialized
    def cleanup_observer(self, record: Any) -> dict[str, Any]:
        from secretary.observer_root import observer_root_repo
        sprint = self.runtime.sprints.show(record.sprint, include_cards=False)
        task = {"id": sprint["id"], "ref": record.sprint, "sprint": record.sprint,
                "project": "observers", "kind": "observer", "claim": {}}
        raw = record.to_json()
        raw.update(attempt_id=record.generation + ":" + str(record.launches), worker=record.generation)
        identity = None
        if record.workspace:
            if record.workspace != self.runtime.host.observer_workspace(record.sprint):
                raise HostError("observer workspace is not the exact sprint workspace")
            if Path(record.workspace).exists():
                try:
                    identity = _identity(observer_root_repo(self.data_dir), record.workspace, "")
                except HostError:
                    pass  # Stop the exact head, but preserve unproven Git placement.
        disposition = "observer-close" if sprint["status"] == "closed" else "observer-stop"
        key = self.journal.remember(task, raw, identity=identity, disposition=disposition)
        return self.replay_one(key)

    def _validate_owner(self, intent: dict[str, Any]) -> dict[str, Any]:
        task = intent["task"]
        if task.get("kind") == "observer":
            current = self.runtime.sprints.show(task["ref"], include_cards=False)
            if current["id"] != task["id"]:
                raise HostError("cleanup observer sprint identity changed")
            if intent["disposition"] == "observer-close":
                if current["status"] != "closed":
                    raise HostError("observer closeout has no completed close handoff")
                for other in self.journal.read()["intents"].values():
                    if (other["task"].get("sprint") == task["ref"] and other["task"].get("kind") != "observer"
                            and not (other["status"] == "completed" or
                                     (other["status"] == "preserved"
                                      and other["progress"].get("preservation_verified")
                                      and other["progress"].get("heads_stopped")
                                      and other["progress"].get("claim_settled")))):
                        raise HostError("observer closeout waits for card cleanup before final external stop")
            observers = self._state().get("observers", {})
            if not isinstance(observers, dict):
                raise HostError("observer ownership unreadable")
            other = observers.get(task["ref"])
            if other and (other.get("generation") != intent["record"]["generation"]
                          or other.get("launches") != intent["record"]["launches"]):
                raise HostError("cleanup observer was replaced")
            return {**current, "claim": {}}
        current = self.runtime.reader.show(task["ref"])
        if current["id"] != task["id"] or current["project"] != task["project"]:
            raise HostError("cleanup card identity changed")
        if current.get("state") in {"in_progress", "validate", "review", "assessment", "ready"}:
            if intent["disposition"] != "done" or current.get("state") != "assessment":
                if not current.get("closed"):
                    raise HostError("cleanup card is still admitted for work")
        record = intent["record"]
        claim = current.get("claim") or {}
        if claim.get("worker") and claim.get("worker") != record.get("worker"):
            raise HostError("cleanup claim is foreign or unknown")
        if claim.get("claimed_at") and claim != task.get("claim"):
            raise HostError("cleanup claim changed")
        state = self._state()
        for ref, other in state.get("records", {}).items():
            if not isinstance(other, dict):
                raise HostError("current dispatcher record is unreadable")
            same_target = (other.get("workspace") and other.get("workspace") == record.get("workspace"))
            if ref == task["ref"] or same_target:
                if (ref != task["ref"] or other.get("attempt_id") != record.get("attempt_id")
                        or other.get("worker") != record.get("worker")):
                    raise HostError("cleanup target has another active owner")
                for field in ("worker_head_run", "review_head_run"):
                    head = other.get(field)
                    if head and not any(head.get("run_id") == raw.get("run_id") and
                                        head.get("scope_generation") == raw.get("scope_generation")
                                        for raw in intent["heads"]):
                        raise HostError("cleanup target has a newer head")
                launch_head = (other.get("launch_intent") or {}).get("head_run")
                if launch_head and not any(launch_head.get("run_id") == raw.get("run_id") and
                                           launch_head.get("scope_generation") == raw.get("scope_generation")
                                           for raw in intent["heads"]):
                    raise HostError("cleanup target has a newer launch intent")
        return current

    def _scope_fence(self, intent: dict[str, Any]) -> None:
        if intent["disposition"] == "catch-up":
            from secretary.dispatch.watchdog import pid_file_path
            from secretary.runtime.head.identity import head_process_status
            for role in ("worker", "review"):
                path = Path(pid_file_path(role, intent["task"]["ref"]))
                if path.exists():
                    status = head_process_status(str(path))
                    if status.get("state") != "dead":
                        raise HostError("archived branch still has live or unknown head identity")
        fence = getattr(self.runtime.host, "fence_cleanup_scopes", None)
        if callable(fence):
            from secretary.runtime.head import HeadRun, TaskRef
            task = intent["task"]
            reference = (TaskRef.sprint(task["ref"]) if task.get("kind") == "observer"
                         else TaskRef.card(task["ref"]))
            fence(intent["record"].get("workspace", ""), reference,
                  [HeadRun.from_json(raw) for raw in intent["heads"]])

    def _binding(self, intent: dict[str, Any]) -> tuple[Path, str]:
        if intent["task"].get("kind") == "observer":
            from secretary.observer_root import observer_root_repo
            repo = _canonical(observer_root_repo(self.data_dir))
            if (intent["identity"]["repo"] != str(repo) or intent["identity"]["common"] !=
                    _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")):
                raise HostError("cleanup observer repository changed")
            return repo, ""
        binding = self.runtime.catalog.binding(intent["task"]["project"])
        repo = _canonical(binding["repo"])
        base = (intent["task"].get("workspace") or {}).get("base_branch")
        allowed = [binding.get("default_branch") or "main", *(binding.get("integration_bases") or [])]
        base = base or allowed[0]
        if base not in allowed or base.startswith("pipeline/"):
            raise HostError("cleanup integration branch is not registered")
        identity = intent["identity"]
        if identity["repo"] != str(repo) or identity["common"] != _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise HostError("cleanup repository registration changed")
        return repo, base

    def _stop(self, intent: dict[str, Any]) -> None:
        from secretary.runtime.head import HeadRun, StopInitiator
        self._provenance("cleanup-before-stop")
        environment = getattr(self.runtime.host, "_decide_workspace_environment_ownership", None)
        if intent["record"].get("workspace") and callable(environment):
            self._environment_owner(intent, Path(intent["record"]["workspace"]))
        # A malformed/unknown head is never absence evidence. Keep the original
        # identities after a stop; its receipt can be retried after a crash.
        record = intent["record"]
        if intent["task"].get("kind") == "observer" and not record.get("head_run"):
            if record.get("handle") or record.get("pid_file") or record.get("head_possible"):
                raise HostError("cleanup observer head ownership is missing")
        roles = () if intent["task"].get("kind") == "observer" else (("worker", "worker_head_run"), ("review", "review_head_run"))
        for role, field in roles:
            if (record.get("handle" if role == "worker" else "review_handle")
                    or record.get(role + "_pid_file")) and not record.get(field):
                raise HostError("cleanup head ownership is missing")
        latest = {(raw["run_id"], raw.get("scope_generation", "")): raw for raw in intent["heads"]}
        runs = []
        for raw in latest.values():
            run = HeadRun.from_json(raw)
            expected_kind = "sprint" if intent["task"].get("kind") == "observer" else "card"
            if (run.workspace != record.get("workspace") or run.task_ref.ref != intent["task"]["ref"]
                    or run.task_ref.kind != expected_kind):
                raise HostError("cleanup head belongs to another workspace or card")
            guard = getattr(self.runtime.host, "_guard_head_run", None)
            if callable(guard):
                guard(run, run.role, pid_file=run.pid_file, leaf=run.leaf,
                      task=run.task_ref.ref if run.task_ref.kind == "sprint" else "card:" + run.task_ref.ref)
            if not run.scope_generation and run.settled:
                continue  # Deployed unscoped runs retain their confirmed stop receipt.
            runs.append(run)
        # Fence every recorded identity before stopping the first head. A
        # foreign reviewer must not cause us to stop a worker and only then refuse.
        for run in runs:
            receipt = self.runtime.host.head_runtime_for(run).stop(
                run, StopInitiator(actor="secretary-dispatcher", reason="owned residue cleanup"))
            if not receipt.ok:
                raise HostError("cleanup head stop pending: " + receipt.reason)
            settled = getattr(receipt, "run", None)
            if (not isinstance(settled, HeadRun) or not settled.same_run(run) or not settled.settled
                    or settled.scope_generation != run.scope_generation
                    or settled.spec != run.spec or settled.workspace != run.workspace
                    or settled.task_ref != run.task_ref or settled.role != run.role):
                raise HostError("cleanup stop receipt does not settle the recorded run")
            if settled.to_json() not in intent["heads"]:
                intent["heads"].append(settled.to_json())
                self._checkpoint_intent(intent)

    def _provenance(self, boundary: str) -> None:
        require = getattr(self.runtime.host, "_require_production_runtime", None)
        if callable(require):
            require(boundary)

    def _settle_claim(self, intent: dict[str, Any]) -> None:
        current = self._validate_owner(intent)
        claim = current.get("claim") or {}
        if claim.get("worker"):
            if not current.get("closed") and current.get("state") != "done":
                raise HostError("cleanup awaits terminal board claim settlement")
            self.runtime.writer.settle_cleanup_claim(current, intent["record"].get("worker", ""))
        intent["progress"]["claim_settled"] = True

    def _dirty(self, intent: dict[str, Any], path: Path) -> list[str]:
        # Include ignored files. Only bytes written by the prompt producer and
        # the existing exact environment namespace contract can be disposable.
        result = subprocess.run(["git", "-C", str(path), "status", "--porcelain=v1", "--ignored",
                                 "--untracked-files=all", "-z"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise HostError("cleanup workspace status is unreadable")
        status = result.stdout
        dirty = []
        generated = self.journal.read()["generated"]
        environment = self._environment_owner(intent, path)
        for row in status.split("\0"):
            if not row:
                continue
            name = row[3:]
            file = path / name
            if row[:2] in {"??", "!!"}:
                if name.startswith(".secretary-task-env/") and environment == "dispatcher":
                    continue
                expected = generated.get(str(file))
                if expected and not file.is_symlink() and file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == expected:
                    continue
            dirty.append(row)
        return dirty

    def _environment_owner(self, intent: dict[str, Any], path: Path) -> str:
        try:
            return self.runtime.host._decide_workspace_environment_ownership(str(path))
        except HostError:
            namespace = path / ".secretary-task-env"
            saved = intent.get("generated_environment")
            if saved and intent["progress"].get("environment_removal_started"):
                namespace = _canonical(namespace)
                if namespace.exists():
                    stat = namespace.stat()
                    if (stat.st_dev, stat.st_ino) == (saved["device"], saved["inode"]):
                        return "dispatcher"
            raise

    def _checkpoint_intent(self, intent: dict[str, Any]) -> None:
        key = hashlib.sha256((intent["task"]["ref"] + ":" +
                              str(intent["record"].get("attempt_id") or "")).encode()).hexdigest()
        value = self.journal.read()
        value["intents"][key] = copy.deepcopy(intent)
        self.journal.save(value)

    def _shared_removal_proof(self, intent: dict[str, Any]) -> str:
        """An attempt can reuse a directory whose later owner settled its Git effects."""
        identity = intent["identity"]
        fields = ("repo", "common", "workspace", "device", "inode", "admin", "gitfile", "branch")
        for key, other in self.journal.read()["intents"].items():
            if (other["task"]["id"] == intent["task"]["id"] and other["progress"].get("workspace_removed")
                    and other.get("identity") and
                    all(other["identity"].get(field) == identity.get(field) for field in fields)):
                return key
        return ""

    def _admitted_registration(self, intent: dict[str, Any], repo: Path) -> None:
        """The retained admin entry can finish a previously admitted Git effect."""
        identity = intent["identity"]
        path = _canonical(identity["workspace"])
        if not intent["progress"].get("removal_started") or path.exists() or path.is_symlink():
            raise HostError("cleanup missing directory has no admitted removal proof")
        common = _canonical(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        admin = _canonical(identity["admin"])
        if (str(common) != identity["common"] or admin.parent != common / "worktrees"
                or not admin.is_dir()):
            raise HostError("cleanup retained admin/common directory changed")
        stat = admin.stat()
        if (stat.st_dev, stat.st_ino) != (identity.get("admin_device"), identity.get("admin_inode")):
            raise HostError("cleanup retained admin identity changed")
        for name in ("gitdir", "commondir", "HEAD"):
            file = admin / name
            if (not file.is_file() or file.is_symlink()
                    or file.read_text() != identity.get("admin_" + name.lower())):
                raise HostError("cleanup retained admin registration changed")
        if ((admin / "gitdir").read_text().strip() != str(path / ".git")
                or (admin / (admin / "commondir").read_text().strip()).resolve() != common):
            raise HostError("cleanup retained admin path mapping changed")
        rows = [row for row in _registered(repo) if row.get("worktree") == str(path)]
        if (len(rows) != 1 or rows[0].get("branch", "") != identity["branch"]
                or rows[0].get("HEAD") != identity["tip"] or "locked" in rows[0]):
            raise HostError("cleanup retained worktree registration or HEAD changed")
        if identity["branch"] and _ref_tip(repo, identity["branch"]) != identity["tip"]:
            raise HostError("cleanup retained worktree ref changed")

    def _verify_commits(self, intent: dict[str, Any], repo: Path, *,
                        preservation_verified: bool = True) -> None:
        """Every exact HEAD needs a persistent publication/retention witness."""
        identity = intent["identity"]
        tip = identity["tip"]
        if self._published(repo, tip):
            refs = _git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip, "refs/remotes/")
            if not refs:
                raise HostError("cleanup commit retention witness disappeared")
            intent["commit_proof"] = {"tip": tip, "publication": "remote-tracking", "refs": refs.splitlines()}
            return
        if intent["task"].get("kind") == "observer":
            # The existing observer producer cuts detached worktrees from this
            # empty, parentless root commit. Its named branch already retains
            # it. No user commit or arbitrary local ref gains this authority.
            from secretary.dispatch.host import OBSERVER_REPO_BRANCH
            ref = "refs/heads/" + OBSERVER_REPO_BRANCH
            if (_ref_tip(repo, ref) == tip and _git(repo, "rev-list", "--parents", "-n", "1", tip) == tip
                    and not _git(repo, "ls-tree", "-r", tip)):
                intent["commit_proof"] = {"tip": tip, "publication": "owned observer root", "refs": [ref]}
                return
        if not preservation_verified:
            raise HostError("cleanup interrupted removal awaits commit retention proof")
        raise Preserved("unpublished commits; workspace and candidate ref retained", verified=True)

    def _remove_workspace(self, intent: dict[str, Any], repo: Path) -> None:
        self._provenance("cleanup-before-worktree-remove")
        identity = intent["identity"]
        workspace = identity.get("workspace")
        if not workspace:
            return
        path = _canonical(workspace)
        rows = [row for row in _registered(repo) if row.get("worktree") == workspace]
        if not path.exists() and not rows:
            admin = _canonical(identity["admin"])
            if admin.exists():
                raise HostError("cleanup retained admin registration changed its workspace mapping")
            # Interrupted Git removal is resumable only from an effect already
            # admitted against this exact identity and durably recorded.
            if not intent["progress"].get("removal_started"):
                shared = self._shared_removal_proof(intent)
                if not shared:
                    raise HostError("cleanup workspace disappeared without removal evidence")
                intent["progress"]["workspace_disposed_by"] = shared
            self._verify_commits(intent, repo)
            return
        missing = not path.exists() and not path.is_symlink()
        if missing:
            self._admitted_registration(intent, repo)
            self._verify_commits(intent, repo, preservation_verified=False)
        else:
            fresh = _identity(repo, workspace, identity["branch"].removeprefix("refs/heads/") if identity["branch"] else "")
            if fresh != identity:
                raise HostError("cleanup workspace, registration or HEAD changed")
            dirty = self._dirty(intent, path)
            if dirty:
                raise Preserved("dirty tracked, untracked or ignored work: " + "; ".join(dirty[:8]), verified=True)
            self._verify_commits(intent, repo)
        # No forced removal: first delete only exact generated bytes whose
        # ownership was validated above. Git independently refuses dirty work.
        generated = self.journal.read()["generated"]
        for name, digest in generated.items():
            file = Path(name)
            if file.parent == path and file.is_file() and not file.is_symlink():
                if hashlib.sha256(file.read_bytes()).hexdigest() == digest:
                    file.unlink()
        if not missing and self._environment_owner(intent, path) == "dispatcher":
            namespace = _canonical(path / ".secretary-task-env")
            stat = namespace.stat()
            intent["generated_environment"] = {"device": stat.st_dev, "inode": stat.st_ino,
                                               "owner": "secretary-dispatcher", "workspace": workspace,
                                               "schema_version": 1}
            intent["progress"]["environment_removal_started"] = True
            self._checkpoint_intent(intent)
            shutil.rmtree(namespace)
        # Admit Git only after exact identity, author-work and retention proof.
        intent["progress"]["removal_started"] = True
        self._checkpoint_intent(intent)
        def run_git(args, cwd):
            capture = getattr(self.runtime.host, "run_capture", None)
            if callable(capture):
                return capture(["git", "-C", str(cwd), *args], "owned cleanup Git")
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
        if missing:
            removed = git_worktree.remove(run_git, repo, path,
                                         admitted_missing=lambda: self._admitted_registration(intent, repo))
        else:
            removed = git_worktree.remove(run_git, repo, path)
        if not removed:
            raise HostError("cleanup worktree removal failed; directory or registration remains")

    def _published(self, repo: Path, tip: str) -> bool:
        return bool(_git(repo, "for-each-ref", "--format=%(refname)", "--contains=" + tip,
                         "refs/remotes/", allow=False))

    def _delete_branch(self, intent: dict[str, Any], repo: Path, base: str) -> None:
        self._provenance("cleanup-before-ref-delete")
        identity = intent["identity"]
        ref, tip = identity["branch"], identity["tip"]
        if not ref:
            return
        if ref != "refs/heads/pipeline/" + intent["task"]["ref"]:
            raise Preserved("foreign branch namespace")
        current = _ref_tip(repo, ref)
        if not current:
            if intent["progress"].get("ref_delete_admitted"):
                return
            if self._shared_removal_proof(intent):
                main = _git(repo, "rev-parse", "--verify", "refs/heads/" + base)
                merged = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tip, main],
                                        capture_output=True, text=True, timeout=30)
                if merged.returncode == 0 and self._published(repo, tip):
                    return
            raise Preserved("candidate ref missing without settlement evidence")
        if current != tip:
            raise Preserved("candidate ref changed; retained current tip " + current, verified=True)
        if any(row.get("branch") == ref for row in _registered(repo)):
            raise Preserved("branch still used by a registered worktree")
        main = _git(repo, "rev-parse", "--verify", "refs/heads/" + base)
        result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tip, main],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 1:
            raise Preserved("unmerged candidate ref retained at " + tip, verified=True)
        if result.returncode:
            raise HostError("cleanup merge proof unavailable")
        if not self._published(repo, tip):
            raise Preserved("unpublished candidate commits retained at " + tip, verified=True)
        # The integration witness and candidate tip are locked and verified in
        # one native Git ref transaction. Never use branch -D after a stale probe.
        command = f"start\nverify refs/heads/{base} {main}\ndelete {ref} {tip}\nprepare\ncommit\n"
        intent["progress"]["ref_delete_admitted"] = {"ref": ref, "tip": tip, "integration_tip": main}
        self._checkpoint_intent(intent)
        result = subprocess.run(["git", "-C", str(repo), "update-ref", "--stdin"], input=command,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise HostError("cleanup ref transaction refused a changed or locked tip")

    @serialized
    def replay_one(self, key: str) -> dict[str, Any]:
        value = self.journal.read()
        intent = value["intents"][key]
        if intent["status"] in {"owned", "completed"}:
            return intent
        intent["status"] = "pending"
        intent["reason"] = ""
        # Retained receipts survive, but current refusal must revoke admission's
        # settlement signal rather than exposing a prior successful observation.
        intent["progress"]["heads_stopped"] = False
        intent["progress"]["preservation_verified"] = False
        self.journal.save(value)
        try:
            self._validate_owner(intent)
            self._scope_fence(intent)
            self._stop(intent)
            intent["progress"]["heads_stopped"] = True
            self.journal.save(value)
            if not intent.get("identity"):
                raise Preserved("missing exact workspace/attempt ownership proof")
            repo, base = self._binding(intent)
            if not intent["identity"].get("workspace"):
                self._verify_commits(intent, repo)
            self._remove_workspace(intent, repo)
            intent["progress"]["workspace_removed"] = True
            self.journal.save(value)
            self._validate_owner(intent)
            intent["progress"]["ref_started"] = True
            self.journal.save(value)
            self._delete_branch(intent, repo, base)
            intent["progress"]["ref_removed"] = True
            self._settle_claim(intent)
            intent["status"] = "completed"
        except Preserved as exc:
            intent["status"] = "preserved"
            intent["reason"] = str(exc)
            if intent["progress"].get("heads_stopped"):
                try:
                    self._settle_claim(intent)
                    intent["progress"]["preservation_verified"] = exc.verified
                except Exception as settlement:
                    intent["status"] = "pending"
                    intent["reason"] += "; " + str(settlement)
        except Exception as exc:
            intent["status"] = "pending"
            intent["reason"] = str(exc)[:1000]
        self.journal.save(value)
        return intent

    @serialized
    def replay(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise HostError("cleanup replay limit must be between 1 and 100")
        value = self.journal.read()
        keys = [key for key, intent in sorted(value["intents"].items())
                if intent["status"] in {"pending", "preserved"}]
        cursor = value.get("replay_cursor", "")
        keys = [key for key in keys if key > cursor] + [key for key in keys if key <= cursor]
        selected = keys[:limit]
        result = [self.replay_one(key) for key in selected]
        if selected:
            fresh = self.journal.read()
            fresh["replay_cursor"] = selected[-1]
            self.journal.save(fresh)
        return result

    @serialized
    def inventory(self, *, catch_up: bool = False) -> dict[str, Any]:
        """Read actual registered Git residue, including archived cards with no record.

        Catch-up adopts only branch-only residue with a matching archived/Done
        card and audited dispatcher claim. Old workspaces lacking exact runtime
        identity remain visible and preserved, rather than guessed from a glob.
        """
        rows = []
        recorded = self.journal.read()["intents"]
        bindings = getattr(self.runtime.catalog, "registered_bindings", None)
        if bindings is None:
            bindings = self.runtime.catalog.bindings
        for project, binding in sorted(bindings.items()):
            try:
                repo = _canonical(binding["repo"])
                worktrees = _registered(repo)
                refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/pipeline/")
                for line in refs.splitlines():
                    ref, tip = line.split(" ", 1)
                    card_ref = ref.removeprefix("refs/heads/pipeline/")
                    row = {"project": project, "repo": str(repo), "ref": ref, "tip": tip,
                           "status": "preserved", "reason": "ownership not proven",
                           "worktrees": [w for w in worktrees if w.get("branch") == ref]}
                    base = binding.get("default_branch") or "main"
                    merge = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tip,
                                            "refs/heads/" + base], capture_output=True, text=True, timeout=30)
                    row["merged"] = merge.returncode == 0 if merge.returncode in (0, 1) else None
                    row["published"] = self._published(repo, tip)
                    try:
                        owners = {key: intent for key, intent in recorded.items()
                                  if ((intent.get("identity") or {}).get("repo") == str(repo)
                                      and (intent.get("identity") or {}).get("branch") == ref)
                                  or (intent["task"]["ref"] == card_ref
                                      and intent["task"].get("project") == project)}
                        if owners:
                            row["recorded_owners"] = [
                                {"cleanup_id": key, "attempt_id": intent["record"].get("attempt_id"),
                                 "identity": intent.get("identity"), "status": intent["status"]}
                                for key, intent in owners.items()]
                            if any(not intent.get("identity") or
                                   intent["identity"]["repo"] != str(repo) or
                                   intent["identity"]["branch"] != ref or
                                   intent["identity"]["tip"] != tip or
                                   intent["task"]["ref"] != card_ref or
                                   intent["task"].get("project") != project
                                   for intent in owners.values()):
                                raise Preserved("recorded ownership conflicts with current ref; retained current tip " + tip)
                            row["reason"] = "retained exact owner; replay existing cleanup obligations"
                            row["cleanup_ids"] = list(owners)
                            if catch_up:
                                task = self.runtime.reader.show(card_ref)
                                for intent in owners.values():
                                    if intent["status"] == "owned":
                                        if task.get("state") != "done" and not task.get("closed"):
                                            raise Preserved("card is still active")
                                        self.journal.request(task, "archive" if task.get("closed") else "done",
                                                             intent["record"])
                            if any(intent["status"] == "completed" for intent in owners.values()):
                                row["reason"] = "ref present after completed cleanup; retained recorded provenance"
                            rows.append(row)
                            continue
                        task = self.runtime.reader.show(card_ref)
                        events = self.runtime.audit.events(card_ref)
                        owned = any(e.get("kind") == "claimed" or (e.get("payload", {}).get("to") == "in_progress"
                                    and e.get("actor", {}).get("role") == "dispatcher") for e in events)
                        if task["project"] != project or not owned:
                            raise Preserved("card/project or audited claim proof missing")
                        if task.get("state") != "done" and not task.get("closed"):
                            raise Preserved("card is still active")
                        if row["worktrees"]:
                            raise Preserved("historical worktree needs exact attempt and head ownership evidence")
                        row["reason"] = "owned branch-only residue; eligible for exact-tip replay"
                        if catch_up:
                            identity = _identity(repo, "", ref.removeprefix("refs/heads/"))
                            key = self.journal.remember(task, {"attempt_id": "archived-branch:" + tip,
                                                       "worker": "", "workspace": ""}, identity=identity,
                                                       disposition="catch-up")
                            row["cleanup_id"] = key
                    except Exception as exc:
                        row["reason"] = str(exc)[:500]
                    rows.append(row)
                for worktree in worktrees:
                    if not worktree.get("branch", "").startswith("refs/heads/pipeline/") and worktree.get("worktree") != str(repo):
                        rows.append({"project": project, "repo": str(repo), "worktree": worktree,
                                     "status": "preserved", "reason": "foreign, detached or legacy workspace"})
            except Exception as exc:
                rows.append({"project": project, "status": "pending", "reason": str(exc)[:500]})
        return {"intents": self.journal.summary(), "residue": rows}


class Preserved(HostError):
    """Owned work deliberately retained; distinct from a retryable failed effect."""

    def __init__(self, reason: str, *, verified: bool = False):
        super().__init__(reason)
        self.verified = verified
