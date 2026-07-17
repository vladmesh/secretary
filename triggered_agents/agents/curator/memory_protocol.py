"""Adapter from curator runs to the secretary memory writer protocol."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...runtime.state import AgentState

DEFAULT_SECRETARY_INSTANCE = Path("/home/dev/secretary-instance")
DEFAULT_SECRETARY_REPO = Path("/home/dev/secretary")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class MemoryProtocolError(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("message") or payload.get("error") or "memory protocol failed")


@dataclass(frozen=True)
class MemoryWriteRequest:
    instance: Path
    actor: str
    scope: str
    slug: str
    fact_file: Path
    source: str | None = None
    tags: str = ""
    pinned: bool = False
    supersedes: str = ""
    data_dir: Path | None = None
    secretary_repo: Path | None = None


def default_secretary_instance() -> Path:
    return Path(os.environ.get("SECRETARY_INSTANCE", str(DEFAULT_SECRETARY_INSTANCE)))


def write_fact(
    state: AgentState,
    request: MemoryWriteRequest,
    *,
    runner: Runner | None = None,
) -> dict:
    """Propose and commit one fact, reusing the saved proposal on retry."""
    runner = runner or subprocess.run
    state_path = _state_path(state, request.scope, request.slug)
    saved = _read_json(state_path)
    if saved is None:
        proposal = _run_memory_json(runner, _propose_args(request), request)
        _write_json(state_path, {
            "propose_id": proposal["propose_id"],
            "actor": request.actor,
            "scope": request.scope,
            "slug": request.slug,
            "fact": proposal.get("fact"),
            "source": proposal.get("source"),
        })
        propose_id = str(proposal["propose_id"])
    else:
        propose_id = str(saved["propose_id"])

    try:
        result = _run_memory_json(runner, _commit_args(request, propose_id), request)
    except MemoryProtocolError as exc:
        action = _terminal_commit_action(exc.payload)
        if action == "already_exists":
            _unlink(state_path)
            return _already_exists_result(request, propose_id, saved)
        if action == "discard":
            _unlink(state_path)
        raise
    _unlink(state_path)
    return result


def _propose_args(request: MemoryWriteRequest) -> list[str]:
    args = _base_args(request, "propose") + [
        "--actor", request.actor,
        "--scope", request.scope,
        "--slug", request.slug,
        "--file", str(request.fact_file),
    ]
    source = request.source or request.actor
    args += ["--source", source]
    if request.tags:
        args += ["--tags", request.tags]
    if request.pinned:
        args.append("--pinned")
    if request.supersedes:
        args += ["--supersedes", request.supersedes]
    return args


def _commit_args(request: MemoryWriteRequest, propose_id: str) -> list[str]:
    return _base_args(request, "commit") + [
        "--actor", request.actor,
        "--propose-id", propose_id,
    ]


def _base_args(request: MemoryWriteRequest, command: str) -> list[str]:
    args = [sys.executable, "-m", "secretary", "memory", command, "--instance", str(request.instance)]
    if request.data_dir is not None:
        args += ["--data-dir", str(request.data_dir)]
    return args


def _run_memory_json(runner: Runner, args: list[str], request: MemoryWriteRequest) -> dict:
    completed = runner(
        args,
        check=False,
        capture_output=True,
        text=True,
        env=_secretary_env(request.secretary_repo),
    )
    payload = _parse_json(completed.stdout)
    if completed.returncode != 0:
        if payload is None:
            payload = {
                "ok": False,
                "error": "runtime",
                "message": completed.stderr.strip() or completed.stdout.strip(),
            }
        raise MemoryProtocolError(payload)
    if payload is None:
        raise MemoryProtocolError({
            "ok": False,
            "error": "runtime",
            "message": "secretary memory returned non-json output",
        })
    if not payload.get("ok"):
        raise MemoryProtocolError(payload)
    return payload


def _terminal_commit_action(payload: dict) -> str | None:
    error = payload.get("error")
    message = str(payload.get("message") or "")
    if error == "validation" and message.startswith("memory fact already exists:"):
        return "already_exists"
    if error in {"validation", "permission"}:
        return "discard"
    return None


def _already_exists_result(
    request: MemoryWriteRequest,
    propose_id: str,
    saved: dict | None,
) -> dict:
    fact = _fact_id(request)
    if saved and isinstance(saved.get("fact"), str):
        fact = str(saved["fact"])
    return {
        "ok": True,
        "op": "skip",
        "reason": "already_exists",
        "fact": fact,
        "actor": request.actor,
        "source": request.source or request.actor,
        "changed_facts": [],
        "propose_id": propose_id,
    }


def _secretary_env(secretary_repo: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    repo = secretary_repo or Path(os.environ.get("TA_SECRETARY_REPO", str(DEFAULT_SECRETARY_REPO)))
    paths = [str(repo)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _state_path(state: AgentState, scope: str, slug: str) -> Path:
    scope_part = _scope_dir(scope)
    name = f"{scope_part}__{_clean_slug(slug)}.json"
    return state.dir / "memory_protocol" / name


def _fact_id(request: MemoryWriteRequest) -> str:
    return f"{_scope_dir(request.scope)}/{_clean_slug(request.slug)}"


def _scope_dir(scope: str) -> str:
    if scope == "global":
        return "global"
    if scope.startswith("project:"):
        return _clean_slug(scope.split(":", 1)[1])
    return _clean_slug(scope)


def _clean_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return cleaned or "fact"


def _parse_json(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) and data.get("propose_id") else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
