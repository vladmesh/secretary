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

Provenance is held to the same standard as the rest.  A receipt only reports the project a check
imported when the check process itself said so, which is why the standard shape is a module this
wrapper launches; an arbitrary shell may `cd` elsewhere or reach another interpreter before any
work starts, so that shape records no import and is never reused in place of a run.  The runner's
verdict is likewise scanned off the stream while it goes past rather than reconstructed from the
diagnostic tail, because output printed after a summary must not be able to erase it.

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
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from signal import NSIG
from pathlib import Path
from typing import IO, Any, Iterable, Mapping

from secretary._fsutil import write_text_atomic

SCHEMA_VERSION = 1
RECEIPT_DIR_NAME = Path("state") / "checks"
#: Bounds on the stored artifact.  The tail is the only unbounded input, so it is capped in bytes
#: and in lines; a single pathological line cannot grow the receipt either.
TAIL_BYTES = 8192
TAIL_LINES = 120
MAX_COMMAND_CHARS = 4096
#: Bounds on the streaming parser's own state: a line longer than this cannot be a runner summary,
#: and a verdict's detail is a handful of counts.
_MAX_LINE_BYTES = 4096
#: The widest normal exit status a POSIX process can hand back.
_MAX_EXIT_STATUS = 255
_MAX_DETAIL_CHARS = 512
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


#: The bootstrap runs *inside* the process that then runs the check, so the provenance it records
#: is the import that check actually performed, not a guess made by a separate probe beforehand.
#: `python -c` puts the working directory first on `sys.path`, exactly as `python -m unittest`
#: does, so the module runs with the import path it would have had without this wrapper.
_PROVENANCE_BOOTSTRAP = """\
import json, os, runpy, sys

_record, _module = sys.argv[1], sys.argv[2]
sys.argv = [_module, *sys.argv[3:]]
try:
    import secretary as _project
    _imported = getattr(_project, "__file__", "") or ""
except Exception:
    _imported = ""
with open(_record, "w", encoding="utf-8") as _handle:
    json.dump(
        {"python": sys.executable, "cwd": os.getcwd(), "imported_secretary": _imported}, _handle
    )
runpy.run_module(_module, run_name="__main__", alter_sys=True)
"""

_CHECK_SET_SCHEMA = 1
_SHAPE_MODULE = "module"
_SHAPE_SHELL = "shell"
_ORIGIN_CHECK_PROCESS = "check-process"
_ORIGIN_UNOBSERVABLE = "unobservable"
_UNOBSERVED_PROVENANCE = {
    "origin": _ORIGIN_UNOBSERVABLE,
    "python": "",
    "cwd": "",
    "imported_secretary": "",
    "inside_workspace": False,
}


@dataclass(frozen=True)
class CheckSpec:
    """One accepted shape of a broad check, and what its receipt may claim.

    ``module`` is the documented standard shape: this wrapper builds the argv itself and runs the
    suite in a process that reports its own import provenance, so a receipt can attest what was
    imported.  ``shell`` accepts any command a project needs, and buys that generality by
    attesting nothing about imports: a shell command may change directory or import environment
    between the wrapper and the interpreter that ends up doing the work, and a receipt that
    guessed at provenance there would be guessing about exactly the thing worth knowing.
    """

    shape: str
    module: str = ""
    module_args: tuple[str, ...] = ()
    command: str = ""

    @classmethod
    def for_module(cls, module: str, args: Iterable[str] = ()) -> "CheckSpec":
        module = module.strip()
        if not module or module.startswith("-"):
            raise BroadCheckError("empty_module", "a module check needs a module name")
        return cls(_SHAPE_MODULE, module, tuple(args))

    @classmethod
    def for_shell(cls, command: str) -> "CheckSpec":
        if not command.strip():
            raise BroadCheckError("empty_command", "a broad check needs a command")
        return cls(_SHAPE_SHELL, command=command)

    @property
    def check_set(self) -> dict[str, object]:
        """The canonical structured identity of this check: what is digested and stored.

        A rendered command line cannot carry an argument vector faithfully — `--module-arg 'one two'`
        and `--module-arg one --module-arg two` render identically while running different checks,
        and a receipt keyed on that rendering hands the first one's result to the second
        (secretary-1406 review).  The argument vector is kept as a list, and every route that keys,
        looks up or validates a receipt uses this representation rather than the display string.
        """
        if self.shape == _SHAPE_MODULE:
            return {
                "schema": _CHECK_SET_SCHEMA,
                "shape": _SHAPE_MODULE,
                "module": self.module,
                "args": list(self.module_args),
            }
        return {"schema": _CHECK_SET_SCHEMA, "shape": _SHAPE_SHELL, "command": self.command}

    @property
    def digest(self) -> str:
        return check_set_digest(self.check_set)

    @property
    def identity(self) -> str:
        """A human rendering for reports and logs.  Never used to key or match a receipt."""
        if self.shape == _SHAPE_MODULE:
            return " ".join(["python", "-m", self.module, *self.module_args])
        return self.command

    @property
    def attests_provenance(self) -> bool:
        return self.shape == _SHAPE_MODULE

    def argv(self, record: Path | None) -> list[str]:
        if self.shape == _SHAPE_MODULE:
            return [
                sys.executable, "-c", _PROVENANCE_BOOTSTRAP, str(record), self.module,
                *self.module_args,
            ]
        return ["bash", "-lc", self.command]

    def displayed_argv(self) -> list[str]:
        if self.shape == _SHAPE_MODULE:
            return [
                sys.executable, "-c", "<provenance bootstrap>", "<provenance record>", self.module,
                *self.module_args,
            ]
        return ["bash", "-lc", self.command]


def as_spec(check: "CheckSpec | str") -> CheckSpec:
    return check if isinstance(check, CheckSpec) else CheckSpec.for_shell(check)


def receipt_dir(root: Path) -> Path:
    return Path(root) / RECEIPT_DIR_NAME


def check_set_digest(check_set: Mapping[str, object]) -> str:
    """Digest the canonical check-set, the way the mechanical gate digests its own check set."""
    canonical = json.dumps(
        check_set, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8", "surrogateescape")).hexdigest()


def receipt_path(root: Path, check: "CheckSpec | str") -> Path:
    return receipt_dir(root) / f"broad-{as_spec(check).digest[:16]}.json"


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


def _read_provenance(record: Path | None, root: Path) -> dict[str, object]:
    """Read what the check process said about its own import, or claim nothing at all.

    A separate preflight probe answers a different question than the one asked: it reports what
    *it* would import, while the check may run somewhere else entirely.  Only the record written
    from inside the running check counts here, and its absence — a shell shape, a crash before the
    bootstrap got that far, an unreadable record — is reported as unobserved rather than filled in
    from the wrapper's own environment (secretary-1406 review).
    """
    if record is None:
        return dict(_UNOBSERVED_PROVENANCE)
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(_UNOBSERVED_PROVENANCE)
    if not isinstance(payload, Mapping):
        return dict(_UNOBSERVED_PROVENANCE)
    imported = str(payload.get("imported_secretary") or "")
    inside = False
    if imported:
        try:
            inside = Path(imported).resolve().is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            inside = False
    return {
        "origin": _ORIGIN_CHECK_PROCESS,
        "python": str(payload.get("python") or ""),
        "cwd": str(payload.get("cwd") or ""),
        "imported_secretary": imported,
        "inside_workspace": inside,
    }


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


class _SummaryScanner:
    """Read the runner's verdict off the stream as it goes by, in constant memory.

    The verdict must not depend on the diagnostic tail: a runner prints ``OK (skipped=8)`` and then
    an ``atexit`` handler, a cleanup hook or a subprocess can print megabytes after it, pushing the
    summary out of any bounded tail (secretary-1406 review).  So the scanner keeps only what a
    summary can be — a test count, a duration, a verdict word and its short detail — and forgets
    every line that is not one.
    """

    def __init__(self) -> None:
        self._carry = bytearray()
        self._dropping = False
        self._tests: int | None = None
        self._runner_duration: float | None = None
        self._summary = ""
        self._detail = ""

    def feed(self, chunk: bytes) -> None:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                break
            self._complete(chunk[start:end])
            start = end + 1
        rest = chunk[start:]
        if rest:
            if len(self._carry) + len(rest) > _MAX_LINE_BYTES:
                # No runner summary is this long, so the line is dropped rather than buffered;
                # the scanner resynchronises at the next newline.
                self._carry.clear()
                self._dropping = True
            else:
                self._carry.extend(rest)

    def _complete(self, line: bytes) -> None:
        if self._dropping:
            self._dropping = False
            self._carry.clear()
            return
        if self._carry:
            line = bytes(self._carry) + line
            self._carry.clear()
        if len(line) > _MAX_LINE_BYTES:
            return
        self._line(line.decode("utf-8", "replace").rstrip("\r"))

    def _line(self, text: str) -> None:
        ran = _RAN_RE.match(text)
        if ran is not None:
            self._tests = int(ran.group(1))
            self._runner_duration = float(ran.group(2))
            return
        failed = _FAILED_RE.match(text)
        if failed is not None:
            self._summary, self._detail = "FAILED", failed.group("detail")[:_MAX_DETAIL_CHARS]
            return
        ok = _OK_RE.match(text)
        if ok is not None:
            self._summary = "OK"
            self._detail = (ok.group("detail") or "")[:_MAX_DETAIL_CHARS]

    def finish(self) -> dict[str, object]:
        if self._carry and not self._dropping:
            self._line(bytes(self._carry).decode("utf-8", "replace").rstrip("\r"))
        self._carry.clear()
        parsed: dict[str, object] = {}
        if self._tests is not None:
            parsed["tests"] = self._tests
        if self._runner_duration is not None:
            parsed["runner_duration_seconds"] = self._runner_duration
        if self._summary:
            parsed["summary"] = self._summary
        for match in _DETAIL_RE.finditer(self._detail):
            parsed[match.group("name").strip().replace(" ", "_")] = int(match.group("count"))
        return parsed


def parse_unittest_summary(text: str) -> dict[str, object]:
    """Parse the runner's own verdict where it prints one; absence is not failure."""
    scanner = _SummaryScanner()
    scanner.feed(text.encode("utf-8", "surrogateescape"))
    return scanner.finish()


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
    check: "CheckSpec | str",
    *,
    root: Path,
    stream: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, dict[str, object]]:
    """Run one broad check, keep its combined output visible, and write its receipt.

    Returns the process's own exit status alongside the receipt: the caller reports exactly what
    the check decided, and the receipt never becomes a second, softer answer to that question.
    """
    spec = as_spec(check)
    if len(json.dumps(spec.check_set)) > MAX_COMMAND_CHARS:
        raise BroadCheckError(
            "command_too_long", f"command exceeds {MAX_COMMAND_CHARS} characters"
        )
    root = Path(root)
    if not root.is_dir():
        raise BroadCheckError("missing_root", f"{root} is not a directory")
    target = receipt_path(root, spec)
    _assert_ignored(root, target)
    environment = dict(os.environ if env is None else env)
    sink = sys.stderr if stream is None else stream

    with tempfile.TemporaryDirectory(prefix="secretary-broad-check-") as scratch:
        # The provenance record lives outside the workspace: writing it inside would edit the very
        # content the receipt claims to describe.
        record = Path(scratch) / "provenance.json" if spec.attests_provenance else None
        return _run_and_record(
            spec, root=root, target=target, record=record, environment=environment, sink=sink,
            timeout_seconds=timeout_seconds,
        )


def _run_and_record(
    spec: CheckSpec,
    *,
    root: Path,
    target: Path,
    record: Path | None,
    environment: dict[str, str],
    sink: IO[str],
    timeout_seconds: float | None,
) -> tuple[int, dict[str, object]]:
    identity = content_identity(root)
    started_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    started = time.monotonic()
    tail = _BoundedTail()
    scanner = _SummaryScanner()
    incomplete_reason = ""
    # stdout and stderr share one pipe on purpose: two pipes would reorder a failing test's
    # traceback against the dots that located it, and the tail is exactly where that matters.
    process = subprocess.Popen(  # noqa: S603 - the command is the worker's own documented check
        spec.argv(record),
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
                scanner.feed(chunk)
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
                spec=spec, root=root, identity=identity,
                provenance=_read_provenance(record, root), started_at=started_at,
                duration=time.monotonic() - started, exit_code=exit_code, tail=tail,
                parsed=scanner.finish(), incomplete_reason=incomplete_reason,
            ),
        )
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()

    if exit_code < 0 and not incomplete_reason:
        incomplete_reason = f"killed by signal {-exit_code}"
    payload = _build_payload(
        spec=spec, root=root, identity=identity, provenance=_read_provenance(record, root),
        started_at=started_at, duration=time.monotonic() - started, exit_code=exit_code,
        tail=tail, parsed=scanner.finish(), incomplete_reason=incomplete_reason,
    )
    _write_receipt(target, payload)
    return exit_code, payload


def _build_payload(
    *,
    spec: CheckSpec,
    root: Path,
    identity: ContentIdentity,
    provenance: dict[str, object],
    started_at: str,
    duration: float,
    exit_code: int,
    tail: _BoundedTail,
    parsed: dict[str, object],
    incomplete_reason: str,
) -> dict[str, object]:
    tail_text = tail.text()
    # The writer does not compose these fields by hand: it records the one model, so what a reader
    # reconstructs at the boundary is by construction what was written.
    result = RunResult.observe(exit_code, incomplete_reason)
    if result is None:
        raise BroadCheckError(
            "unrepresentable_result",
            f"the check returned {exit_code!r}, which is not a result this wrapper can record",
        )
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "command": spec.identity,
        "command_shape": spec.shape,
        "check_set": spec.check_set,
        "argv": spec.displayed_argv(),
        "command_or_check_set_digest": spec.digest,
        "cwd": str(Path(root).resolve()),
        "project_provenance": provenance,
        "content_identity": identity.as_dict(),
        "started_at": started_at,
        "ended_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "duration_seconds": round(max(duration, 0.0), 3),
        **result.as_fields(),
        "parsed": parsed,
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


@dataclass(frozen=True)
class RunResult:
    """The one canonical model of what a check process did.

    Every recorded result field is a function of two facts: the raw ``Popen.returncode`` and the
    reason the runner has for calling the run unfinished.  Deriving `signal`, `status`, `verdict`,
    the stored reason and the shell status from that one model — rather than checking them against
    each other one predicate at a time — is what keeps a reader from having to guess which of two
    disagreeing fields to believe, and is why a stored `" "` reason or an exit code of `256` are
    refused without anybody having thought of them individually (secretary-1406 review).

    The domain is what this POSIX wrapper can actually observe: a normal status of 0..255, or a
    negative code naming a signal this platform defines.  Anything else was never written by a run.
    """

    exit_code: int
    incomplete_reason: str

    @classmethod
    def observe(cls, exit_code: object, incomplete_reason: object) -> "RunResult | None":
        """Build the model from a raw process result, or refuse a result nothing could produce."""
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return None
        if not isinstance(incomplete_reason, str):
            return None
        if exit_code < 0:
            if not (1 <= -exit_code < NSIG):
                return None
            # A signalled run is unfinished by construction; if the runner has nothing more
            # specific to say, the canonical reason is the signal itself.
            reason = incomplete_reason.strip() or f"killed by signal {-exit_code}"
        else:
            if exit_code > _MAX_EXIT_STATUS:
                return None
            reason = incomplete_reason.strip()
        return cls(exit_code, reason)

    @property
    def signal(self) -> int:
        return -self.exit_code if self.exit_code < 0 else 0

    @property
    def status(self) -> str:
        return _STATUS_INCOMPLETE if self.incomplete_reason else _STATUS_COMPLETE

    @property
    def verdict(self) -> str:
        if self.status == _STATUS_INCOMPLETE:
            return _VERDICT_UNKNOWN
        return _VERDICT_PASSED if self.exit_code == 0 else _VERDICT_FAILED

    @property
    def shell_status(self) -> int:
        """The status this result gives a caller: `128+N` for a signal, otherwise its own.

        One rule, used whether the result was just observed or read back from a receipt, so a
        reused receipt answers exactly what the run it replaces answered.
        """
        return self.exit_code if self.exit_code >= 0 else 128 - self.exit_code

    def as_fields(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "signal": self.signal,
            "status": self.status,
            "incomplete_reason": self.incomplete_reason,
            "verdict": self.verdict,
        }

    @classmethod
    def restore(cls, payload: Mapping[str, object]) -> "RunResult | None":
        """Reconstruct the model from stored fields, and insist the store agrees with it exactly."""
        result = cls.observe(payload.get("exit_code"), payload.get("incomplete_reason"))
        if result is None:
            return None
        fields = result.as_fields()
        if any(payload.get(key) != value for key, value in fields.items()):
            return None
        return result


def recorded_result(receipt: Mapping[str, object]) -> RunResult | None:
    """The model behind an already-loaded receipt; readers take their answers from here."""
    return RunResult.restore(receipt)


def result_refusal(payload: Mapping[str, object]) -> str:
    """Why this receipt's recorded result could not have been written by a run, or ``""``.

    The digest proves a payload was not edited after something computed it; it says nothing about
    whether the numbers inside describe a run that happened.  This states the difference, and it
    states it by rebuilding the canonical model and comparing every stored field with it, so the
    answer cannot drift from what the writer actually writes.
    """
    result = RunResult.observe(payload.get("exit_code"), payload.get("incomplete_reason"))
    if result is None:
        return (
            f"exit code {payload.get('exit_code')!r} with reason "
            f"{payload.get('incomplete_reason')!r} is not a result this wrapper can record"
        )
    disagreements = [
        f"{key}={payload.get(key)!r} (a run would record {value!r})"
        for key, value in result.as_fields().items()
        if payload.get(key) != value
    ]
    if disagreements:
        return "the stored result disagrees with itself: " + ", ".join(disagreements)
    return ""


def load_receipt(path: Path) -> dict[str, object] | None:
    """The one semantic boundary: a receipt reaches a reader through here or not at all.

    `usable_receipt` — and therefore `check show` and `check broad --reuse`, which have no other
    way in — call this first, so an unreadable, undigestible, structurally short or internally
    contradictory artifact is never authorized, never has its status preserved, and never reaches
    `_shell_status`.  Corruption outranks both.
    """
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
        "command", "command_shape", "check_set", "command_or_check_set_digest", "cwd",
        "project_provenance",
        "content_identity", "started_at", "ended_at", "duration_seconds", "exit_code", "signal",
        "status", "incomplete_reason", "verdict", "parsed", "tail",
    )
    if any(key not in payload for key in required):
        return None
    # Every field the result invariants are stated over is written by every run, so its absence is
    # itself a refusal rather than something to work around.
    if RunResult.restore(payload) is None:
        return None
    check_set = payload.get("check_set")
    if not isinstance(check_set, Mapping) or check_set.get("schema") != _CHECK_SET_SCHEMA:
        return None
    # The receipt carries the whole check set, so a reader recomputes the digest rather than
    # trusting the name it was filed under.
    if check_set_digest(check_set) != payload.get("command_or_check_set_digest"):
        return None
    return payload


@dataclass(frozen=True)
class ReceiptLookup:
    """Whether an existing receipt may stand in for running the broad check again.

    Constructed only by :func:`usable_receipt`, and the one place a caller may ask "may I skip the
    run?" is :meth:`authorized`.  Every route — ``check show``, ``check broad --reuse``, anything
    later — therefore passes the same predicate; there is no field a caller can read to reach a
    softer conclusion of its own (secretary-1406 review, twice over).
    """

    usable: bool
    reason: str
    receipt: dict[str, object] | None
    path: Path

    def authorized(self) -> dict[str, object] | None:
        """The receipt that may replace a run, or nothing at all."""
        return self.receipt if self.usable else None

    def authorized_result(self) -> RunResult | None:
        """The canonical result an authorized receipt recorded, for a caller that owes a status.

        A caller never reads `exit_code` out of a receipt for itself: it asks here, and the answer
        comes from the model the load boundary already reconstructed and matched.
        """
        receipt = self.authorized()
        return None if receipt is None else recorded_result(receipt)

    def as_dict(self) -> dict[str, object]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "path": str(self.path),
            "receipt": self.receipt,
        }


def candidate_import_refusal(receipt: Mapping[str, object], root: Path) -> str:
    """The candidate-trust boundary: why this receipt's import may not be trusted, or ``""``.

    Observed provenance is necessary and not sufficient.  A check process reports honestly that it
    imported some project; only an import resolved *inside this candidate workspace* says the run
    that produced the receipt was a run of this code.  A missing record, an unreadable one, an
    empty path, a path that no longer resolves, and a path outside the candidate are all refusals,
    and they are refusals here, once, for every caller.
    """
    provenance = receipt.get("project_provenance")
    if not isinstance(provenance, Mapping):
        return "the receipt records no import provenance"
    if provenance.get("origin") != _ORIGIN_CHECK_PROCESS:
        # A shell shape may change directory or import environment before the interpreter starts,
        # so nothing observed what the check imported.
        return (
            "import provenance was not observed from the check process, so this receipt attests "
            "no checkout"
        )
    imported = str(provenance.get("imported_secretary") or "")
    if not imported:
        return "the check process imported no project, so it validated no checkout"
    try:
        resolved = Path(imported).resolve()
        inside = resolved.is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return f"the imported project path could not be resolved: {imported}"
    if not inside:
        # Recomputed against this workspace rather than read off the receipt's own flag: the
        # question is where the import lands for the reader, now.
        return f"the check process imported {imported}, which is outside this candidate workspace"
    return ""


def usable_receipt(root: Path, check: "CheckSpec | str") -> ReceiptLookup:
    """Answer the only question a scrolled-away pane raises: has this run already happened here?

    Usable means the artifact is intact, the run finished, the check process imported this
    candidate workspace, and the receipt describes the content in this checkout right now.  A red
    result is usable evidence too — it is a concrete answer, and the caller decides whether fixing
    the cause justifies a new run.
    """
    spec = as_spec(check)
    path = receipt_path(root, spec)
    receipt = load_receipt(path)
    if receipt is None:
        return ReceiptLookup(False, "no intact receipt for this check", None, path)
    if (
        receipt.get("command_or_check_set_digest") != spec.digest
        or receipt.get("check_set") != spec.check_set
    ):
        # The stored check set is compared, not only the name the file was filed under, so an
        # argument vector that renders the same as another one cannot answer for it.
        return ReceiptLookup(False, "receipt is for a different check", receipt, path)
    if receipt.get("status") != _STATUS_COMPLETE:
        reason = str(receipt.get("incomplete_reason") or "run did not finish")
        return ReceiptLookup(False, f"run did not finish: {reason}", receipt, path)
    refusal = candidate_import_refusal(receipt, root)
    if refusal:
        return ReceiptLookup(False, refusal, receipt, path)
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
    if isinstance(provenance, Mapping):
        imported = str(provenance.get("imported_secretary") or "")
        origin = str(provenance.get("origin") or "")
    else:
        imported, origin = "", ""
    if origin != _ORIGIN_CHECK_PROCESS:
        # `cwd` above is where the check was launched, which a shell command is free to leave;
        # only an observed provenance line says where it ended up importing from.
        imported = "(not observed; this check shape attests no import)"
    elif imported:
        inside = " (inside workspace)" if provenance.get("inside_workspace") else " (outside workspace)"
        imported += inside
    lines = [
        f"- command: {receipt.get('command', '')} [{receipt.get('command_shape', '')}]",
        f"- digest: {receipt.get('command_or_check_set_digest', '')}",
        f"- launched in: {receipt.get('cwd', '')}",
        f"- imported project: {imported or '(unresolved)'}",
        f"- head_sha: {head or '(unresolved)'}",
        f"- started_at: {receipt.get('started_at', '')} ({receipt.get('duration_seconds', 0)}s)",
        f"- exit_code: {receipt.get('exit_code', '')} ({receipt.get('status', '')}"
        f"/{receipt.get('verdict', '')})",
    ]
    if counts:
        lines.append(f"- parsed: {counts}")
    return "\n".join(lines)
