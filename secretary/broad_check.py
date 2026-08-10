"""Workspace-local structured receipt for one broad check run.

A worker runs a broad suite once per report generation.  The evidence it produces used to live
only in the terminal, so a scrolled-away TUI pane was indistinguishable from a suite that had
never run, and the cheapest way back to a summary was to run the whole suite again.  This module
runs the broad command once, keeps the combined output visible while it runs, and writes what the
run actually decided into a bounded local artifact: the command and its digest, where it ran and
which project it imported, when it started and how long it took, the process exit status, the
parsed verdict and counts where the runner prints them, and a bounded diagnostic tail.

The receipt is evidence about content, not about a wall clock.  It records the checkout's content
identity (HEAD object id plus a digest of the tracked diff and untracked files), so a later reader
can tell "this is a receipt for exactly the code in front of me" from "this describes something
else".  Everything else fails closed: an incomplete run, a corrupt or truncated artifact, a
checkout with no resolvable identity, or a receipt for other content is not usable evidence, and
the reader is told which of those it is rather than being handed a hopeful summary.

This is deliberately not the exact-SHA gate receipt in ``dispatcher_gate_receipt``.  That one is
machinery-owned attestation that travels downstream to reviewers and observers; this one is a
worker's own note-to-self inside an ignored workspace path and never leaves it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Mapping

from secretary._fsutil import write_text_atomic

SCHEMA_VERSION = 1
RECEIPT_DIR_NAME = Path("state") / "checks"
#: Bounds on the stored artifact.  The tail is the only unbounded input, so it is capped in bytes
#: and in lines; a single pathological line cannot grow the receipt either.
TAIL_BYTES = 8192
TAIL_LINES = 120
MAX_COMMAND_CHARS = 4096
_READ_CHUNK = 65536
_GIT_TIMEOUT = 60

_STATUS_COMPLETE = "complete"
_STATUS_INCOMPLETE = "incomplete"
_VERDICT_PASSED = "passed"
_VERDICT_FAILED = "failed"
_VERDICT_UNKNOWN = "unknown"

_RAN_RE = re.compile(r"^Ran (\d+) tests? in ([0-9.]+)s$", re.MULTILINE)
_OK_RE = re.compile(r"^OK(?: \((?P<detail>[^)]*)\))?\s*$", re.MULTILINE)
_FAILED_RE = re.compile(r"^FAILED \((?P<detail>[^)]*)\)\s*$", re.MULTILINE)
_DETAIL_RE = re.compile(r"(?P<name>[a-z][a-z ]*)=(?P<count>\d+)")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BroadCheckError(Exception):
    """A refusal to produce or trust a receipt; the message names what failed closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ContentIdentity:
    """What the receipt is a receipt *about*: the exact content of a checkout."""

    head_sha: str
    worktree_digest: str

    @property
    def resolved(self) -> bool:
        return bool(self.head_sha) and bool(self.worktree_digest)

    def as_dict(self) -> dict[str, str]:
        return {"head_sha": self.head_sha, "worktree_digest": self.worktree_digest}

    def matches(self, other: "ContentIdentity") -> bool:
        return self.resolved and other.resolved and self.as_dict() == other.as_dict()


def receipt_dir(root: Path) -> Path:
    return Path(root) / RECEIPT_DIR_NAME


def command_digest(command: str) -> str:
    """The same check-set identity the mechanical gate digests, so the two agree on naming."""
    return hashlib.sha256(command.encode("utf-8", "surrogateescape")).hexdigest()


def receipt_path(root: Path, command: str) -> Path:
    return receipt_dir(root) / f"broad-{command_digest(command)[:16]}.json"


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def content_identity(root: Path) -> ContentIdentity:
    """Digest the checkout's content, not merely its commit.

    A worker reports from a dirty worktree far more often than from a clean one, so a HEAD object
    id alone would happily match a receipt taken before the last three edits.  The digest covers
    the tracked diff against HEAD and every untracked, non-ignored file's content; ignored paths
    (the receipt directory among them) are excluded by construction, so writing a receipt cannot
    invalidate the receipt it just wrote.  A checkout whose identity cannot be resolved returns an
    unresolved value, and unresolved never matches anything.
    """
    head = _git(root, ["rev-parse", "HEAD"])
    if head is None or head.returncode != 0:
        return ContentIdentity("", "")
    head_sha = head.stdout.strip()
    diff = _git(root, ["diff", "HEAD"])
    others = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if diff is None or diff.returncode != 0 or others is None or others.returncode != 0:
        return ContentIdentity(head_sha, "")
    digest = hashlib.sha256()
    digest.update(diff.stdout.encode("utf-8", "surrogateescape"))
    for name in sorted(part for part in others.stdout.split("\0") if part):
        digest.update(b"\0")
        digest.update(name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        try:
            digest.update(hashlib.sha256((Path(root) / name).read_bytes()).hexdigest().encode())
        except OSError:
            # A file that vanished between listing and reading is itself a content fact.
            digest.update(b"unreadable")
    return ContentIdentity(head_sha, digest.hexdigest())


def _project_provenance(root: Path, env: Mapping[str, str]) -> dict[str, object]:
    """Which ``secretary`` package a check launched from this cwd would import.

    The interesting failure this catches is a worker validating the installed copy while believing
    it validated the candidate worktree, so the answer is obtained the same way the check gets it:
    the interpreter is started with the workspace as cwd and without ``-P``, exactly as
    ``python3 -m unittest`` runs, and the resulting module path is compared with the workspace.
    """
    probe = "import secretary, sys; sys.stdout.write(secretary.__file__ or '')"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return {"python": sys.executable, "imported_secretary": "", "inside_workspace": False}
    imported = completed.stdout.strip() if completed.returncode == 0 else ""
    inside = False
    if imported:
        try:
            inside = Path(imported).resolve().is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            inside = False
    return {"python": sys.executable, "imported_secretary": imported, "inside_workspace": inside}


class _BoundedTail:
    """Keep the last bytes of a stream without keeping the stream."""

    def __init__(self, limit: int = TAIL_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False
        self.total_bytes = 0
        self.total_lines = 0

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        self.total_lines += chunk.count(b"\n")
        self._buffer.extend(chunk)
        if len(self._buffer) > self._limit:
            del self._buffer[: len(self._buffer) - self._limit]
            self.truncated = True

    def text(self) -> str:
        body = bytes(self._buffer).decode("utf-8", "replace")
        lines = body.splitlines()
        if self.truncated and lines:
            # The first retained line is almost certainly cut mid-way; a partial line reads as a
            # real one in a report, so drop it rather than quote half a traceback.
            lines = lines[1:]
        if len(lines) > TAIL_LINES:
            lines = lines[-TAIL_LINES:]
            self.truncated = True
        return "\n".join(lines)


def parse_unittest_summary(text: str) -> dict[str, object]:
    """Parse the runner's own verdict where it prints one; absence is not failure."""
    parsed: dict[str, object] = {}
    ran = None
    for ran in _RAN_RE.finditer(text):
        pass
    if ran is not None:
        parsed["tests"] = int(ran.group(1))
        parsed["runner_duration_seconds"] = float(ran.group(2))
    detail = ""
    summary = ""
    failed = None
    for failed in _FAILED_RE.finditer(text):
        pass
    if failed is not None:
        summary, detail = "FAILED", failed.group("detail")
    else:
        ok = None
        for ok in _OK_RE.finditer(text):
            pass
        if ok is not None:
            summary, detail = "OK", ok.group("detail") or ""
    if summary:
        parsed["summary"] = summary
    for match in _DETAIL_RE.finditer(detail):
        parsed[match.group("name").strip().replace(" ", "_")] = int(match.group("count"))
    return parsed


def _receipt_digest(payload: Mapping[str, object]) -> str:
    body = {key: value for key, value in payload.items() if key != "receipt_digest"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _assert_ignored(root: Path, path: Path) -> None:
    """Refuse to write a receipt anywhere git would offer to commit it."""
    inside = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    ignored = _git(root, ["check-ignore", "-q", str(path)])
    if ignored is None or ignored.returncode != 0:
        raise BroadCheckError(
            "receipt_not_ignored",
            f"{path} is not git-ignored; a broad-check receipt must never be committable",
        )


def run_broad_check(
    command: str,
    *,
    root: Path,
    stream: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one broad command, keep its combined output visible, and write its receipt.

    Returns the process's own exit status alongside the receipt: the caller reports exactly what
    the command decided, and the receipt never becomes a second, softer answer to that question.
    """
    if not command.strip():
        raise BroadCheckError("empty_command", "a broad check needs a command")
    if len(command) > MAX_COMMAND_CHARS:
        raise BroadCheckError(
            "command_too_long", f"command exceeds {MAX_COMMAND_CHARS} characters"
        )
    root = Path(root)
    if not root.is_dir():
        raise BroadCheckError("missing_root", f"{root} is not a directory")
    target = receipt_path(root, command)
    _assert_ignored(root, target)
    environment = dict(os.environ if env is None else env)
    sink = sys.stderr if stream is None else stream

    identity = content_identity(root)
    provenance = _project_provenance(root, environment)
    started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    started = time.monotonic()
    tail = _BoundedTail()
    incomplete_reason = ""
    # stdout and stderr share one pipe on purpose: two pipes would reorder a failing test's
    # traceback against the dots that located it, and the tail is exactly where that matters.
    process = subprocess.Popen(  # noqa: S603 - the command is the worker's own documented check
        ["bash", "-lc", command],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    try:
        assert process.stdout is not None
        deadline = None if not timeout_seconds else started + float(timeout_seconds)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    # A command that hangs while printing nothing is the case a read-triggered
                    # deadline would never notice, so the wait itself carries the ceiling.
                    incomplete_reason = f"timed out after {timeout_seconds}s"
                    process.kill()
                    break
                if not selector.select(timeout=remaining if remaining is not None else 1.0):
                    continue
                chunk = process.stdout.read1(_READ_CHUNK)
                if not chunk:
                    break
                tail.feed(chunk)
                sink.write(chunk.decode("utf-8", "replace"))
                sink.flush()
        exit_code = process.wait()
    except BaseException as exc:  # a killed or interrupted runner still owes an honest receipt
        process.kill()
        exit_code = process.wait()
        incomplete_reason = incomplete_reason or f"runner interrupted: {type(exc).__name__}"
        _write_receipt(
            target,
            _build_payload(
                command=command, root=root, identity=identity, provenance=provenance,
                started_at=started_at, duration=time.monotonic() - started, exit_code=exit_code,
                tail=tail, incomplete_reason=incomplete_reason,
            ),
        )
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()

    if exit_code < 0 and not incomplete_reason:
        incomplete_reason = f"killed by signal {-exit_code}"
    payload = _build_payload(
        command=command, root=root, identity=identity, provenance=provenance,
        started_at=started_at, duration=time.monotonic() - started, exit_code=exit_code,
        tail=tail, incomplete_reason=incomplete_reason,
    )
    _write_receipt(target, payload)
    return exit_code, payload


def _build_payload(
    *,
    command: str,
    root: Path,
    identity: ContentIdentity,
    provenance: dict[str, object],
    started_at: str,
    duration: float,
    exit_code: int,
    tail: _BoundedTail,
    incomplete_reason: str,
) -> dict[str, object]:
    tail_text = tail.text()
    status = _STATUS_INCOMPLETE if incomplete_reason else _STATUS_COMPLETE
    if status == _STATUS_INCOMPLETE:
        verdict = _VERDICT_UNKNOWN
    else:
        verdict = _VERDICT_PASSED if exit_code == 0 else _VERDICT_FAILED
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "command_or_check_set_digest": command_digest(command),
        "cwd": str(Path(root).resolve()),
        "project_provenance": provenance,
        "content_identity": identity.as_dict(),
        "started_at": started_at,
        "ended_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "duration_seconds": round(max(duration, 0.0), 3),
        "exit_code": exit_code,
        "signal": -exit_code if exit_code < 0 else 0,
        "status": status,
        "incomplete_reason": incomplete_reason,
        "verdict": verdict,
        "parsed": parse_unittest_summary(tail_text),
        "output_bytes": tail.total_bytes,
        "output_lines": tail.total_lines,
        "tail_truncated": tail.truncated,
        "tail": tail_text,
    }
    payload["receipt_digest"] = _receipt_digest(payload)
    return payload


def _write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    """Publish the artifact in one rename, so a reader never observes a half-written receipt."""
    body = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    try:
        write_text_atomic(path, body)
    except RuntimeError as exc:
        raise BroadCheckError("receipt_unwritable", str(exc)) from None


def load_receipt(path: Path) -> dict[str, object] | None:
    """Read a receipt, or nothing.  A truncated or edited artifact is not a smaller receipt."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    digest = payload.get("receipt_digest")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        return None
    if digest != _receipt_digest(payload):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    required = (
        "command", "command_or_check_set_digest", "cwd", "content_identity", "started_at",
        "ended_at", "duration_seconds", "exit_code", "status", "verdict", "tail",
    )
    if any(key not in payload for key in required):
        return None
    if payload.get("status") not in (_STATUS_COMPLETE, _STATUS_INCOMPLETE):
        return None
    if payload.get("verdict") not in (_VERDICT_PASSED, _VERDICT_FAILED, _VERDICT_UNKNOWN):
        return None
    return payload


@dataclass(frozen=True)
class ReceiptLookup:
    """Whether an existing receipt may stand in for running the broad check again."""

    usable: bool
    reason: str
    receipt: dict[str, object] | None
    path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "path": str(self.path),
            "receipt": self.receipt,
        }


def usable_receipt(root: Path, command: str) -> ReceiptLookup:
    """Answer the only question a scrolled-away pane raises: has this run already happened here?

    Usable means the artifact is intact, the run finished, and it describes the content in this
    checkout right now.  A red result is usable evidence too — it is a concrete answer, and the
    caller decides whether fixing the cause justifies a new run.
    """
    path = receipt_path(root, command)
    receipt = load_receipt(path)
    if receipt is None:
        return ReceiptLookup(False, "no intact receipt for this command", None, path)
    if receipt.get("command_or_check_set_digest") != command_digest(command):
        return ReceiptLookup(False, "receipt is for a different command", receipt, path)
    if receipt.get("status") != _STATUS_COMPLETE:
        reason = str(receipt.get("incomplete_reason") or "run did not finish")
        return ReceiptLookup(False, f"run did not finish: {reason}", receipt, path)
    recorded = receipt.get("content_identity")
    if not isinstance(recorded, Mapping):
        return ReceiptLookup(False, "receipt records no content identity", receipt, path)
    stored = ContentIdentity(
        str(recorded.get("head_sha") or ""), str(recorded.get("worktree_digest") or "")
    )
    current = content_identity(root)
    if not current.resolved:
        return ReceiptLookup(False, "this checkout has no resolvable content identity", receipt, path)
    if not stored.matches(current):
        return ReceiptLookup(False, "content changed since the receipt was written", receipt, path)
    return ReceiptLookup(True, "receipt describes this exact content", receipt, path)


def summarize(receipt: Mapping[str, Any]) -> str:
    """One-screen rendering for a report body, so nobody reruns a suite to quote it."""
    parsed = receipt.get("parsed")
    counts = ""
    if isinstance(parsed, Mapping) and parsed:
        counts = ", ".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    identity = receipt.get("content_identity")
    head = identity.get("head_sha", "") if isinstance(identity, Mapping) else ""
    provenance = receipt.get("project_provenance")
    imported = provenance.get("imported_secretary", "") if isinstance(provenance, Mapping) else ""
    lines = [
        f"- command: {receipt.get('command', '')}",
        f"- digest: {receipt.get('command_or_check_set_digest', '')}",
        f"- cwd: {receipt.get('cwd', '')}",
        f"- imported project: {imported or '(unresolved)'}",
        f"- head_sha: {head or '(unresolved)'}",
        f"- started_at: {receipt.get('started_at', '')} ({receipt.get('duration_seconds', 0)}s)",
        f"- exit_code: {receipt.get('exit_code', '')} ({receipt.get('status', '')}"
        f"/{receipt.get('verdict', '')})",
    ]
    if counts:
        lines.append(f"- parsed: {counts}")
    return "\n".join(lines)
