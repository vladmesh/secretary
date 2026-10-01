"""Read-only projection of canonical owners, fenced to a native scope incarnation.

This consumes the lifecycle's owner, journal and launch identity. It creates no
files and acquires only shared locks on existing owner locks. A retained owner
alone cannot establish that a systemd unit still belongs to that launch.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory import CGROUP_ROOT, MemoryScopeError
from . import protocol
from .journal import RUN_STARTED, read_events
from .scoped_lifecycle import ScopedHeadLifecycle, launch_identity


@dataclass(frozen=True)
class RuntimeScopeInventory:
    data_dir: Path
    observed: frozenset[str]
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    disappeared: frozenset[str] = frozenset()
    errors: dict[str, str] = field(default_factory=dict)

    def revalidate(self) -> RuntimeScopeInventory:
        """Refresh before apply; an old snapshot never blesses a replacement."""
        fresh = read_runtime_scopes(self.data_dir, set(self.observed))
        errors = dict(fresh.errors)
        for unit, scope in self.scopes.items():
            current = fresh.scopes.get(unit)
            if unit not in fresh.disappeared and (
                current is None or current["identity"] != scope["identity"]
            ):
                errors[unit] = "runtime scope identity changed since inventory; retry inspection"
        return RuntimeScopeInventory(fresh.data_dir, fresh.observed, fresh.scopes,
                                     fresh.disappeared, errors)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open(stack: ExitStack, name: str | Path, *, parent: int | None = None,
          directory: bool = False, uid: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                 (os.O_DIRECTORY if directory else 0), dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            or uid is not None and info.st_uid != uid or info.st_mode & 0o022):
        raise MemoryScopeError("runtime ownership path is not private to the installation owner")
    return fd


def _directory(stack: ExitStack, path: Path) -> int:
    """Anchor every path component, refusing symlinks instead of resolving them."""
    if not path.is_absolute():
        raise MemoryScopeError("runtime inventory needs an absolute selected data root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    stack.callback(os.close, fd)
    for part in path.parts[1:]:
        if part in (".", ".."):
            raise MemoryScopeError("runtime ownership path escapes the selected data root")
        # Shared ancestors such as /tmp need not belong to the runtime account.
        fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        stack.callback(os.close, fd)
    return fd


def _json(fd: int) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MemoryScopeError("runtime ownership JSON has duplicate fields")
            result[key] = value
        return result

    with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=unique)


def _unit_state(unit: str) -> dict[str, str]:
    properties = ("Id", "LoadState", "ActiveState", "SubState", "ControlGroup",
                  "InvocationID", "ActiveEnterTimestampMonotonic", "Transient", "BindsTo")
    result = subprocess.run(
        ["systemctl", "--system", "show", unit, "--property=" + ",".join(properties)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode or result.stderr.strip():
        raise MemoryScopeError("native runtime scope observation failed; retry systemctl show")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if any(name not in fields for name in properties):
        raise MemoryScopeError("native runtime scope observation is incomplete")
    return fields


def _absent(state: dict[str, str]) -> bool:
    return (state["LoadState"] == "not-found" and state["ActiveState"] == "inactive"
            and not state["ControlGroup"])


def _membership(group: Path) -> tuple[tuple[int, int], bool]:
    info = group.stat()
    if group.is_symlink() or group.resolve() != group:
        raise MemoryScopeError("runtime cgroup path was substituted")
    fields = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
    if fields.get("populated") not in {"0", "1"}:
        raise MemoryScopeError("runtime scope has no recursive membership evidence")
    return _identity(info), fields["populated"] == "1"


def _live_launch(record: dict[str, Any], group: Path, directory: Path) -> bool:
    """A currently verified launcher and its scoped payload bind pre-start work."""
    pid = record["launch_pid"]
    if launch_identity(pid) != record["launch_identity"]:
        return False
    # The gated process execs sudo/systemd-run. Check its native argv, not its PID
    # alone, and demand the exact run directory and unit passed to that launcher.
    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    words = [word.decode() for word in args if word]
    if record["unit"] not in words or str(directory) not in words:
        raise MemoryScopeError("live scope launcher does not match canonical unit or data root")
    for option, value in (("--run-id", record["run_id"]), ("--role", record["role"]),
                          ("--task", record["task"]), ("--cwd", record["workspace"])):
        if option not in words or words[words.index(option) + 1] != value:
            raise MemoryScopeError("live scope launcher does not match canonical role/task/workspace")
    for child in (group, *group.rglob("*")):
        if not child.is_dir():
            continue
        for token in (child / "cgroup.procs").read_text().split():
            current = int(token)
            seen: set[int] = set()
            while current > 1 and current not in seen:
                if current == pid:
                    return launch_identity(pid) == record["launch_identity"]
                seen.add(current)
                try:
                    fields = Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    break
                current = int(fields[1])
    return False


def _journal_proof(stack: ExitStack, fd: int, uid: int, record: dict[str, Any],
                   state: dict[str, str], directory: Path) -> None:
    """Retained heartbeat bounds the native incarnation even after head death.

    systemd's monotonic activation must lie between the generation's launcher
    birth and its journaled head birth on the same boot. A reused unit activates
    after that head's birth and cannot borrow its journal or heartbeat. No wall
    clock, PID liveness or role-name inference supplies this proof.
    """
    journal = _open(stack, protocol.JOURNAL_NAME, parent=fd, uid=uid)
    events = read_events(Path(f"/proc/self/fd/{journal}"))
    if events.malformed or not events.ordered or events.truncated_tail:
        raise MemoryScopeError("runtime journal ownership evidence is damaged")
    starts = events.of_kind(RUN_STARTED)
    if not starts:
        raise MemoryScopeError("runtime scope has no journaled launch proof")
    if len(starts) != 1:
        # The deployed journal does not carry a generation. Without a live
        # generation-bound launcher, multiple incarnations are ambiguous.
        raise MemoryScopeError("multiple journaled launches lack a live generation-bound launcher; retry lifecycle recovery")
    started = starts[-1]
    if started.get("socket_path") != str(directory / protocol.SOCKET_NAME):
        raise MemoryScopeError("runtime journal belongs to a different canonical data root")
    for key in ("run_id", "role", "task"):
        if started.get(key) != record[key]:
            raise MemoryScopeError("runtime journal does not match canonical ownership")
    # The journal names the deployed heartbeat location, which can be outside
    # the run directory. Anchor and validate that pointer as well.
    heartbeat_path = Path(started.get("pid_file", ""))
    heartbeat_parent = _directory(stack, heartbeat_path.parent)
    heartbeat = _json(_open(stack, heartbeat_path.name, parent=heartbeat_parent, uid=uid))
    if not isinstance(heartbeat, dict):
        raise MemoryScopeError("runtime heartbeat ownership evidence is not an object")
    for key in ("run_id", "role", "task"):
        if heartbeat.get(key) != record[key]:
            raise MemoryScopeError("runtime heartbeat does not match canonical ownership")
    boot, ticks = record["launch_identity"].rsplit(":", 1)
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if (type(heartbeat.get("version")) is not int or heartbeat.get("version") != 1
            or type(heartbeat.get("pid")) is not int or heartbeat["pid"] <= 0
            or heartbeat.get("pid") != started.get("head_pid")
            or boot != current_boot or heartbeat.get("boot_id") != boot):
        raise MemoryScopeError("runtime launch belongs to a different native identity or boot")
    activation = int(state["ActiveEnterTimestampMonotonic"])
    hz = os.sysconf("SC_CLK_TCK")
    launch_time = int(ticks) * 1_000_000 // hz
    head_time = int(heartbeat["proc_starttime_ticks"]) * 1_000_000 // hz
    if not launch_time <= activation <= head_time:
        raise MemoryScopeError("native scope incarnation is outside the recorded launch; possible unit reuse")


def read_runtime_scopes(data_dir: Path, observed: set[str]) -> RuntimeScopeInventory:
    """Project all runtime roots for this installation, without following PO pointers."""
    original = frozenset(observed)
    wanted = frozenset(name for name in original if name.endswith(".scope"))
    scopes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    disappeared: set[str] = set()
    owners: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not wanted:
        return RuntimeScopeInventory(data_dir, original)
    try:
        with ExitStack() as stack:
            data_fd = _directory(stack, data_dir)
            uid = os.fstat(data_fd).st_uid
            if os.fstat(data_fd).st_mode & 0o022:
                raise MemoryScopeError("selected runtime data root is writable by another owner")
            for relative in (Path("heads"), Path("po-heads"), Path("webproto/heads")):
                try:
                    root = _directory(stack, data_dir / relative)
                except FileNotFoundError:
                    continue
                root_info = os.fstat(root)
                if root_info.st_uid != uid or root_info.st_mode & 0o022:
                    raise MemoryScopeError("canonical runtime root is not private to this installation owner")
                for name in os.listdir(root):
                    info = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise MemoryScopeError("canonical runtime directory is a symlink")
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    with ExitStack() as owner_stack:
                        directory = data_dir / relative / name
                        fd = _open(owner_stack, name, parent=root, directory=True, uid=uid)
                        try:
                            owner_fd = _open(owner_stack, "scope-owner.json", parent=fd, uid=uid)
                        except FileNotFoundError:
                            continue  # genuinely deployed unscoped records are not scope owners
                        record = ScopedHeadLifecycle.validate_owner(_json(owner_fd))
                        if protocol.run_dir_for(directory.parent, record["run_id"]) != directory:
                            raise MemoryScopeError("canonical owner directory does not match its run identity")
                        unit = record["unit"]
                        if unit in owners:
                            raise MemoryScopeError("duplicate canonical runtime scope owners")
                        owners[unit] = directory, record
                        if unit not in wanted:
                            continue
                        lock = _open(owner_stack, "scope-owner.lock", parent=fd, uid=uid)
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        # Admission/cleanup writers cannot change the owner while this
                        # observation is made. Check substitutions which ignore the lock.
                        locked_record = ScopedHeadLifecycle.read_owner(Path(f"/proc/self/fd/{fd}"))
                        if locked_record != record:
                            raise MemoryScopeError("runtime owner changed before its observation lock")
                        state = _unit_state(unit)
                        if _absent(state):
                            disappeared.add(unit)
                            continue
                        if (state["Id"] != unit or state["LoadState"] != "loaded"
                                or state["Transient"] != "yes" or not state["InvocationID"]
                                or state["ControlGroup"] != f"/system.slice/{unit}"):
                            raise MemoryScopeError("unit is not the canonical native transient scope")
                        if any(not isinstance(record.get(key), str) or not record[key]
                               for key in ("role", "task", "workspace")):
                            raise MemoryScopeError("runtime owner has incomplete role/task/workspace identity")
                        if not Path(record["workspace"]).is_absolute() or "launch_pid" not in record:
                            raise MemoryScopeError("runtime owner has no native generation launch proof")
                        group = CGROUP_ROOT / "system.slice" / unit
                        native, populated = _membership(group)
                        if record["cleanup_complete"] and populated:
                            raise MemoryScopeError("a populated runtime scope falsely claims completed cleanup")
                        if not _live_launch(record, group, directory):
                            _journal_proof(owner_stack, fd, uid, record, state, directory)
                        after = _unit_state(unit)
                        if _absent(after):
                            disappeared.add(unit)
                            continue
                        native_after, populated = _membership(group)
                        if record["cleanup_complete"] and populated:
                            raise MemoryScopeError("a completed runtime scope gained members during inspection")
                        if (after != state or native_after != native
                                or ScopedHeadLifecycle.read_owner(directory) != record
                                or _identity(os.fstat(_directory(owner_stack, directory))) != _identity(os.fstat(fd))
                                or _identity((directory / "scope-owner.json").lstat()) != _identity(os.fstat(owner_fd))
                                or _identity(os.fstat(_directory(owner_stack, data_dir))) != _identity(os.fstat(data_fd))):
                            raise MemoryScopeError("runtime ownership or native scope changed during inspection")
                        scopes[unit] = {
                            "run_id": record["run_id"], "generation": record["generation"],
                            "role": record["role"], "task": record["task"], "workspace": record["workspace"],
                            "launch_allowed": record["launch_allowed"], "cleanup_complete": record["cleanup_complete"],
                            "populated": populated, "binds_to": state.get("BindsTo", "").split(),
                            "active": state["ActiveState"], "owner": str(directory / "scope-owner.json"),
                        "identity": (record["run_id"], record["generation"], record["role"], record["task"],
                                         record["workspace"], record["launch_pid"], record["launch_identity"],
                                         _identity(os.fstat(fd)), native, state["InvocationID"],
                                         tuple(state.get("BindsTo", "").split())),
                        }
            # Missing ownership cannot establish absence. Refresh every observed
            # ownerless name too, including a name omitted by an earlier snapshot.
            for unit in wanted - owners.keys():
                state = _unit_state(unit)
                after = _unit_state(unit)
                if after != state:
                    raise MemoryScopeError("ownerless runtime unit changed during inspection; retry observation")
                if _absent(after):
                    disappeared.add(unit)
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        # No partial success from an ambiguous ownership inventory. Diagnostics
        # never include command lines, environment, journal payloads or outcomes.
        scopes.clear()
        errors["runtime_scopes"] = str(exc) if isinstance(exc, MemoryScopeError) else (
            f"{type(exc).__name__} reading canonical runtime ownership or native membership; "
            "check selected data-root permissions and systemd access, then retry inspection")
    return RuntimeScopeInventory(data_dir, original, scopes, frozenset(disappeared), errors)
