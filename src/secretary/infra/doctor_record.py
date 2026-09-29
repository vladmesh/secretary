"""Latest real doctor result. The native timer writes; web reads never evaluate doctor.

The starting document invalidates the previous success before evaluation. Replacement in
the same directory is atomic, so a killed producer leaves an explicit unfinished attempt.
No history, raw process output, environment or credentials are published.
"""

from __future__ import annotations

import fcntl
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.config import validate_instance

RESULT_PATH = Path("doctor/latest.json")
FRESH_SECONDS = 180
TIMEOUT_SECONDS = 40
MAX_BYTES = 2 * 1024 * 1024


def utc(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def identity(instance: Path, data_dir: Path) -> dict[str, str]:
    config = instance / "instance.yaml" if instance.is_dir() else instance
    return {"instance": str(config.resolve()), "data_dir": str(data_dir.resolve())}


def publish(path: Path, document: dict[str, Any]) -> None:
    """Flush a private sibling, then replace the sole latest document, then flush its directory."""
    payload = (json.dumps(document, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(payload) > MAX_BYTES:
        raise ValueError("doctor document exceeds its size bound")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".latest-", delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _output_limit() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))


def collect(command: list[str], *, timeout: float = TIMEOUT_SECONDS) -> tuple[int, dict[str, Any]]:
    """A bounded child of this installed product; kill its entire process group on timeout."""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(
            command, stdout=output, stderr=subprocess.DEVNULL, env=environment,
            start_new_session=True, preexec_fn=_output_limit,
        ) as process:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise TimeoutError("doctor exceeded its collection deadline") from None
        output.seek(0)
        raw = output.read(MAX_BYTES + 1)
    if code not in (0, 1, 2):
        raise ValueError(f"doctor process exited with {code}")
    if len(raw) >= MAX_BYTES:
        raise ValueError("doctor output exceeds its size bound")
    payload = json.loads(raw)
    validate_result(payload, code)
    # The findings are the CLI evaluator's unchanged findings. Status has its own web source.
    return code, {key: payload[key] for key in ("schema_version", "ok", "findings")}


def validate_result(payload: Any, code: Any) -> None:
    if (
        not isinstance(payload, dict) or payload.get("schema_version") != 1
        or type(code) is not int or code not in (0, 1, 2)
        or type(payload.get("ok")) is not bool or not isinstance(payload.get("findings"), list)
    ):
        raise ValueError("invalid doctor result envelope")
    findings = payload["findings"]
    if any(not isinstance(item, dict) or not isinstance(item.get("code"), str) or not item["code"] for item in findings):
        raise ValueError("invalid doctor finding")
    if payload["ok"] != (not findings) or (code == 0 and findings) or (code == 1 and not findings):
        raise ValueError("doctor exit and findings disagree")


def record(instance: Path, *, data_dir: Path | None = None, offline: bool = False,
           host_fixture: str | None = None, timeout: float = TIMEOUT_SECONDS) -> int:
    report = validate_instance(instance)
    root = data_dir or report.data_dir
    if root is None:
        print("doctor record: configured data root is unavailable", file=sys.stderr)
        return 2
    path = root / RESULT_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / "record.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("doctor record: collection already in progress", file=sys.stderr)
                return 2
            document = {
                "schema_version": 1, "installation": identity(instance, root),
                "run_at": utc(time.time()), "completed_at": None, "exit_code": None,
                "mode": "offline" if offline else "host_fixture" if host_fixture else "live",
                "outcome": "collecting", "reason": "doctor attempt has not completed", "result": None,
            }
            publish(path, document)
            command = [sys.executable, "-P", "-m", "secretary", "doctor", "--instance", str(instance), "--json"]
            if offline:
                command.append("--offline")
            if host_fixture:
                command.extend(("--host-fixture", host_fixture))
            try:
                code, result = collect(command, timeout=timeout)
                document.update(exit_code=code, result=result, outcome="unavailable" if code == 2 else "result",
                                reason="doctor reported diagnostic unavailability" if code == 2 else None)
            except (OSError, ValueError, TimeoutError) as exc:
                # Never publish raw output/exception strings, which could contain secrets.
                document.update(outcome="failed", reason=f"doctor collection failed ({type(exc).__name__})")
            document["completed_at"] = utc(time.time())
            publish(path, document)
            return 0 if document["outcome"] == "result" else 2
    except (OSError, ValueError):
        print("doctor record: result publication failed; previous timestamp is not refreshed", file=sys.stderr)
        return 2


def read_latest(instance: Path, data_dir: Path, *, now: float, offline: bool = False) -> dict[str, Any]:
    """Read and validate only local bytes. A failure keeps its identity and never means no findings."""
    path = data_dir / RESULT_PATH
    reading: dict[str, Any] = {"state": "missing", "reason": "no recorded doctor attempt", "run_at": None,
                               "completed_at": None, "exit_code": None, "findings": [], "path": str(path)}
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("record exceeds size bound")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("unsupported recorded doctor schema")
        if document.get("installation") != identity(instance, data_dir):
            reading.update(state="wrong_installation", reason="doctor result belongs to another installation or data root")
            return reading
        started = _timestamp(document["run_at"])
        completed = _timestamp(document["completed_at"]) if document.get("completed_at") is not None else None
        outcome = document["outcome"]
        if outcome not in ("collecting", "failed", "unavailable", "result"):
            raise ValueError("unknown recorded outcome")
        result = document.get("result")
        if outcome in ("result", "unavailable"):
            validate_result(result, document.get("exit_code"))
            if (outcome == "unavailable") != (document["exit_code"] == 2):
                raise ValueError("outcome and exit disagree")
        elif result is not None or document.get("exit_code") is not None:
            raise ValueError("failed attempt contains a diagnostic result")
        if (outcome == "collecting") != (completed is None) or (completed is not None and completed < started):
            raise ValueError("invalid doctor completion time")
        if started > now + 5 or (completed is not None and completed > now + 5):
            raise ValueError("doctor time is in the future")
        mode = document.get("mode")
        if mode not in ("live", "offline", "host_fixture"):
            raise ValueError("invalid doctor mode")
        reading.update(state="available" if outcome == "result" else outcome,
                       reason=document.get("reason"), run_at=document["run_at"],
                       completed_at=document.get("completed_at"), exit_code=document.get("exit_code"),
                       findings=result["findings"] if result is not None else [], mode=mode,
                       age_seconds=max(0, now - started))
        if not offline and mode != "live":
            reading.update(state="wrong_mode", reason="recorded doctor did not inspect the live installation")
        elif now - started > FRESH_SECONDS:
            reading.update(state="stale", reason=f"latest doctor attempt is older than {FRESH_SECONDS} seconds ({outcome})")
        return reading
    except FileNotFoundError:
        return reading
    except OSError:
        reading.update(state="unavailable", reason="recorded doctor document cannot be read")
    except (ValueError, KeyError, TypeError, OverflowError):
        reading.update(state="malformed", reason="recorded doctor document is malformed or unsupported")
    return reading


def _timestamp(value: Any) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("doctor time must be UTC")
    return datetime.fromisoformat(value).timestamp()


def add_subcommand(subparsers: Any) -> None:
    command = subparsers.add_parser("doctor-record", help="atomically record one bounded real doctor attempt")
    command.add_argument("--instance", required=True, type=Path)
    command.add_argument("--data-dir", type=Path, help="override the result data root")
    command.add_argument("--offline", action="store_true")
    command.add_argument("--host-fixture")
    command.set_defaults(handler=lambda args: record(args.instance, data_dir=args.data_dir,
                                                   offline=args.offline, host_fixture=args.host_fixture))
