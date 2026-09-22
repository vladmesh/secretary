"""Construction boundary for the production dispatcher runtime.

The dispatcher state machine remains in :mod:`secretary.dispatcher` while the installed
CLI/web entry points build it here. Keeping backend selection, instance data-dir resolution and
host construction in this small module prevents new callers from treating the legacy dispatcher
monolith as a general-purpose API.
"""

from __future__ import annotations

from pathlib import Path

from secretary.board.backend import CARD, SPRINT, board_client
from secretary.checkpoint import CheckpointPusher, CheckpointWriter
from secretary.config import DataDirError, instance_data_dir
from secretary.dispatch.host import CommandHostRuntime, InstanceCatalog
from secretary.dispatcher import DispatcherRuntime
from secretary.dispatch.types import DispatcherError
from secretary.tasks import TaskReader, TaskWriter, task_audit_for


def default_data_dir(instance_path: Path) -> Path:
    try:
        return instance_data_dir(_instance_file(instance_path))
    except DataDirError as exc:
        raise DispatcherError("invalid_instance", f"invalid instance: {exc}", 2) from None


def _instance_file(path: Path) -> Path:
    return path / "instance.yaml" if path.is_dir() else path


def runtime_from_args(
    instance: str, data_dir: str | None, *, host_mode: str, owner: str
) -> DispatcherRuntime:
    instance_path = Path(instance)
    data = Path(data_dir).expanduser() if data_dir else default_data_dir(instance_path)
    # DispatcherRuntime also constructs a SprintReader from this client. The client is built by
    # the switch rather than by naming one backend here.
    client = board_client(instance_path, serves=(CARD, SPRINT))
    catalog = InstanceCatalog(instance_path)
    # The audit follows the client: the same requests/board_events tables the writer commits to.
    # The command host reads the same one, so TASK.md
    # feedback selection and report/verdict waits never disagree about what happened.
    audit = task_audit_for(client, data)
    return DispatcherRuntime(
        TaskReader(client),
        TaskWriter(client, data_dir=data),
        audit,
        data,
        catalog,
        CommandHostRuntime(catalog, data, mode=host_mode, audit=audit),
        owner=owner,
        checkpoint=CheckpointWriter(data, catalog.instance_dir),
        checkpoint_push=CheckpointPusher(catalog.instance_dir),
    )
