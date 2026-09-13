"""Settling PO head turns when the web service starts (`secretary.po.runner.PoRunner.recover`)."""

from __future__ import annotations

from pathlib import Path

from secretary.config import validate_instance
from secretary.po.runner import PoRunner


def recover_po_turns(
    instance: str | Path, data_dir: str | Path | None, *, runner: PoRunner | None = None
) -> list[str]:
    """Settle the PO turns a previous run of this service left `running`; lines for the journal.

    `runner` is the web process's own runner when it has one (`PoLayer.start_service`). The dashboard
    does not depend on the PO store, so a store that cannot be reached becomes a line and the service
    still starts.
    """
    try:
        if runner is None:
            if data_dir is None:
                report = validate_instance(Path(instance))
                if report.data_dir is None:
                    raise RuntimeError("the instance config names no data directory")
                data_dir = report.data_dir
            runner = PoRunner.for_instance(instance, data_dir)
        recovered = runner.recover()
    except Exception as exc:  # noqa: BLE001 - recovery failing must not keep the dashboard down
        return [f"secretary web: PO turn recovery did not run: {type(exc).__name__}: {exc}"]
    return [
        f"secretary web: PO turn {turn.session_id}/{turn.seq} interrupted: {turn.reason}"
        for turn in recovered
    ]
