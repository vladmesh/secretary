"""The PO head's sessions for the dashboard: list, read, create, send, stop, close.

One `PoRunner` per web process, built when the layers are assembled (:meth:`PoLayer.start_service`)
and kept: its waiter threads own the turns it started, and a second runner in the same process would
not know about them. Every rule about turns — one running per session, how a stop settles, what
reaches the feed — and about request ids stays in `secretary.po.runner` and `secretary.po.store`
(`po_requests`, decided in the transaction that creates the session or the turn); this layer checks the
model list and translates the store's vocabulary into this package's typed codes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from secretary.config import ConfigError, DataDirError, instance_data_dir, load_config
from secretary.po.models import models_from_instance
from secretary.po.runner import PoRunner, RunnerError
from secretary.po.store import (
    OWNER,
    RUNNING,
    SESSION_CLOSED,
    SESSION_OPEN,
    FeedEntry,
    PoStoreError,
    RequestConflict,
    Session,
    SessionClosed,
    SessionNotFound,
    Turn,
    TurnInProgress,
)
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import (
    InstallationUnavailable,
    PoRequestConflict,
    PoSessionClosed,
    PoSessionNotFound,
    PoTurnInProgress,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.po_recovery import recover_po_turns


class PoLayer(ProtocolBoundary):
    """One installation's PO sessions. Construction does no I/O; `runner` and `models` are test seams."""

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        runner: PoRunner | None = None,
        runner_factory: Callable[[Path], PoRunner] | None = None,
        models: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._runner = runner
        self._runner_factory = runner_factory or (
            lambda data_dir: PoRunner.for_instance(self.instance, data_dir)
        )
        self._models = dict(models) if models is not None else None
        self._lock = threading.Lock()

    # --- service start ----------------------------------------------------------------------

    def start_service(self) -> list[str]:
        """Build this process's runner and settle what a previous run left `running`; journal lines.

        Never raises: the dashboard does not depend on the PO store, and a runner that cannot be
        built now is built by the first `/po` request that needs it.
        """
        try:
            runner = self._runner_or_refuse()
        except Exception as exc:  # noqa: BLE001 - the service starts without the PO store
            message = exc.message if hasattr(exc, "message") else f"{type(exc).__name__}: {exc}"
            return [f"secretary web: PO turn recovery did not run: {message}"]
        return recover_po_turns(self.instance, runner.data_dir, runner=runner)

    # --- reads ------------------------------------------------------------------------------

    def po_models(self) -> dict[str, Any]:
        return {
            "kind": "po_models",
            "models": {cli: list(values) for cli, values in self._model_list().items()},
        }

    def po_running_count(self) -> dict[str, Any]:
        store = self._runner_or_refuse().store
        return {"kind": "po_running", "running": len(self._store(store.running_turns))}

    def po_overview(self, closed: bool = False) -> dict[str, Any]:
        """Open sessions, or with `closed` the closed ones; either way the closed count and running turns."""
        store = self._runner_or_refuse().store
        sessions = self._store(lambda: store.sessions(SESSION_CLOSED if closed else SESSION_OPEN))
        closed_count = len(sessions) if closed else self._store(lambda: store.session_count(SESSION_CLOSED))
        running = self._store(store.running_turns)
        busy = {turn.session_id for turn in running}
        items = [
            {
                **_session(session, running=session.session_id in busy),
                "first_message": session.first_message,
                "last_activity_at": _time(session.last_activity_at),
            }
            for session in sessions
        ]
        return {
            "kind": "po_overview",
            "closed": closed,
            "closed_count": closed_count,
            "sessions": items,
            "running": len(running),
            "models": self.po_models()["models"],
        }

    def po_session(self, session_id: str) -> dict[str, Any]:
        store = self._runner_or_refuse().store
        session = self._store(lambda: store.session(session_id))
        turns = self._store(lambda: store.turns(session_id))
        feed = self._store(lambda: store.feed(session_id))
        running = next((turn for turn in turns if turn.state == RUNNING), None)
        return {
            "kind": "po_session",
            "session": _session(session, running=running is not None),
            "turns": [_turn(turn) for turn in turns],
            "feed": [_entry(entry) for entry in feed],
            "running": running is not None,
            "running_seq": running.seq if running is not None else None,
            "last_turn": _turn(turns[-1]) if turns else None,
        }

    # --- writes -----------------------------------------------------------------------------

    def po_create_session(self, *, request_id: str, cli: str, model: str) -> dict[str, Any]:
        """One session per request id (`PoStore.claim_session`); a repeat answers the same session."""
        request_id = _required(request_id, "request_id")
        models = self._model_list()
        if cli not in models or not models[cli]:
            offered = ", ".join(name for name, values in models.items() if values)
            raise ValidationRefused(f"a PO session runs one of: {offered}; not {cli!r}")
        if model not in models[cli]:
            raise ValidationRefused(
                f"{model!r} is not a model this installation offers for {cli}: {', '.join(models[cli])}"
            )
        runner = self._runner_or_refuse()
        session, created = self._store(lambda: runner.create_session_request(cli, model, request_id))
        return {
            "kind": "po_session_created",
            "request_id": request_id,
            "session_id": session.session_id,
            "repeated": not created,
        }

    def po_send(self, *, request_id: str, session_id: str, text: str) -> dict[str, Any]:
        """One turn per request id (`PoStore.claim_turn`), bound to this session and this exact text.

        A repeat of the same form answers with the turn the first submission created — running,
        completed, or failed with its reason — and starts nothing, even when that first submission
        wrote the turn and then failed to launch its CLI. The id reused for anything else is refused.
        """
        request_id = _required(request_id, "request_id")
        if not str(text or "").strip():
            raise ValidationRefused("an empty message starts no turn")
        runner = self._runner_or_refuse()
        turn, created = self._store(lambda: runner.send_request(session_id, text, request_id))
        return {
            "kind": "po_turn_started",
            "request_id": request_id,
            "session_id": session_id,
            "seq": turn.seq,
            "state": turn.state,
            "repeated": not created,
        }

    def po_stop(self, *, session_id: str, seq: int) -> dict[str, Any]:
        """Stop turn `seq` if it is the one running; a stale stop form stops nothing newer."""
        runner = self._runner_or_refuse()
        turn = self._store(lambda: runner.stop_turn(session_id, seq))
        return {
            "kind": "po_stop",
            "session_id": session_id,
            "seq": seq,
            "stopped": turn is not None,
            "turn": _turn(turn) if turn is not None else None,
        }

    def po_close(self, *, session_id: str) -> dict[str, Any]:
        """Close a session as the owner (`PoStore.close_session`); already closed answers it unchanged.

        A running turn is `owner_conflict` and nothing is written. No request id: a close repeated is the
        same close.
        """
        store = self._runner_or_refuse().store
        session = self._store(lambda: store.close_session(session_id, OWNER))
        return {"kind": "po_session_closed", "session": _session(session, running=False)}

    # --- inside the boundary ----------------------------------------------------------------

    def _runner_or_refuse(self) -> PoRunner:
        with self._lock:
            if self._runner is None:
                data_dir = self._resolved_data_dir()
                try:
                    self._runner = self._runner_factory(data_dir)
                except Exception as exc:
                    raise RuntimeUnavailable(
                        f"the PO session store is not available: {type(exc).__name__}: {exc}"
                    ) from exc
            return self._runner

    def _resolved_data_dir(self) -> Path:
        if self._data_dir is None:
            try:
                self._data_dir = instance_data_dir(self.instance)
            except DataDirError as exc:
                raise InstallationUnavailable(str(exc)) from None
        return self._data_dir

    def _model_list(self) -> dict[str, tuple[str, ...]]:
        if self._models is not None:
            return self._models
        path = self.instance / "instance.yaml" if self.instance.is_dir() else self.instance
        try:
            return models_from_instance(load_config(path))
        except ConfigError as exc:
            raise InstallationUnavailable(str(exc)) from None

    @staticmethod
    def _store(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except SessionNotFound as exc:
            raise PoSessionNotFound(str(exc)) from None
        except TurnInProgress as exc:
            raise PoTurnInProgress(str(exc)) from None
        except RequestConflict as exc:
            raise PoRequestConflict(str(exc)) from None
        except SessionClosed as exc:
            raise PoSessionClosed(str(exc)) from None
        except (PoStoreError, RunnerError) as exc:
            raise RuntimeUnavailable(str(exc)) from None
        except ImportError as exc:  # no PostgreSQL driver in this interpreter: the store is unavailable
            raise RuntimeUnavailable(f"the PO session store is not available: {exc}") from None


def _required(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationRefused(f"{name} is required")
    return text


def _time(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _session(session: Session, *, running: bool) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "cli": session.cli,
        "model": session.model,
        "created_at": _time(session.created_at),
        "state": session.state,
        "closed_at": _time(session.closed_at),
        "closed_by": session.closed_by,
        "running": running,
    }


def _turn(turn: Turn) -> dict[str, Any]:
    return {
        "seq": turn.seq,
        "state": turn.state,
        "started_at": _time(turn.started_at),
        "finished_at": _time(turn.finished_at),
        "reason": turn.reason,
    }


def _entry(entry: FeedEntry) -> dict[str, Any]:
    return {
        "turn_seq": entry.turn_seq,
        "role": entry.role,
        "text": entry.text,
        "created_at": _time(entry.created_at),
    }


__all__ = ["PoLayer"]
