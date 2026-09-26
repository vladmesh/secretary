"""`secretary web-serve`: the third command over the same layer, beside `web-read` and `web-run`.

It builds the eight layers from the same arguments those groups take -- `--instance`, `--data-dir`,
`--heads-registry` -- hands them to the application, and serves. Nothing about a snapshot, a state,
a run or a sprint is decided here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable

from secretary.web.app import WebApp
from secretary.web.doctor import DoctorLayer
from secretary.web.provider_usage import ProviderUsageLayer
from secretary.web.server import DEFAULT_HOST, DEFAULT_PORT, LoopbackOnly, serve
from secretary.webproto.card_ops import CardOperationLayer
from secretary.webproto.command_reads import CommandReadLayer
from secretary.webproto.ops import OperationLayer
from secretary.webproto.pause_ops import PauseOperationLayer
from secretary.webproto.pause_reads import PauseReadLayer
from secretary.webproto.po_auth import PoTokenLayer
from secretary.webproto.po_ops import PoLayer
from secretary.webproto.reads import ReadLayer, hold_store_exclusion
from secretary.webproto.sprint_ops import SprintOperationLayer
from secretary.webproto.sprint_reads import SprintReadLayer

#: The same status `web-read` and `web-run` exit with when they were asked for something they
#: cannot do.
EXIT_VALIDATION = 2


def add_web_serve_subcommands(subparsers) -> None:
    """Register the group beside `web-read` and `web-run`."""
    group = subparsers.add_parser(
        "web-serve",
        help="serve the local dashboard and card pages over the web-read and web-run operations",
    )
    group.add_argument("--instance", required=True, help="path to an instance dir or instance.yaml")
    group.add_argument(
        "--data-dir",
        default=os.environ.get("SECRETARY_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    group.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="the loopback address to bind; a non-loopback address is refused (no TLS, no password)",
    )
    group.add_argument("--port", type=int, default=DEFAULT_PORT, help="the port to bind")
    group.add_argument(
        "--heads-registry",
        default=os.environ.get("TA_HEADS_REGISTRY"),
        help="read head profiles from this registry instead of the installation's own",
    )
    group.add_argument(
        "--offline",
        action="store_true",
        help="collect installation health without inspecting the live host",
    )
    group.set_defaults(handler=run_web_serve)


def health_layers(
    instance: str,
    *,
    data_dir: str | None = None,
    offline: bool = False,
    now: Callable[[], float] = time.time,
) -> tuple[ReadLayer, DoctorLayer]:
    """The read layer and the doctor lamp over one health cache, as `web-serve` wires them.

    The lamp's layer holds the only cached reading of recorded health in the process, and the read
    layer's `system_snapshot` takes the dashboard's health section from that same reading rather
    than collecting its own: one collection per `CACHE_SECONDS` window, and a panel and a lamp that
    cannot disagree within it.
    """
    reads = ReadLayer(
        instance,
        data_dir=data_dir,
        offline=offline,
        health_reader=lambda: doctor.health_snapshot(),
    )
    doctor = DoctorLayer(reads.health_snapshot, now=now)
    return reads, doctor


def run_web_serve(args: argparse.Namespace) -> int:
    # The board store's git-exclusion guard, run once here and not on every request: from now on a
    # read resolves the store without a `git` call, and no request can write `.gitignore`. A
    # refusal is held too, and every store read of this process answers with it.
    refused = hold_store_exclusion(args.instance)
    if refused is not None:
        print(f"board store: {refused}", file=sys.stderr)
    # A client of the PO service (`secretary-po.service`): the web starts and recovers no PO turn.
    po = PoLayer(args.instance, data_dir=args.data_dir)
    reads, doctor = health_layers(args.instance, data_dir=args.data_dir, offline=bool(args.offline))
    app = WebApp(
        reads,
        OperationLayer(args.instance, data_dir=args.data_dir, registry_path=args.heads_registry),
        SprintReadLayer(args.instance, data_dir=args.data_dir),
        SprintOperationLayer(args.instance, data_dir=args.data_dir),
        PauseReadLayer(args.instance, data_dir=args.data_dir),
        # The same construction `secretary pause`/`resume` make for the production dispatcher: a
        # drain or a resume issued from the browser is the operator's own command, not a rehearsal.
        PauseOperationLayer(args.instance, data_dir=args.data_dir),
        CommandReadLayer(args.instance, data_dir=args.data_dir),
        CardOperationLayer(args.instance, data_dir=args.data_dir),
        ProviderUsageLayer(),
        doctor,
        po_auth=PoTokenLayer(args.instance, data_dir=args.data_dir),
        po=po,
    )
    try:
        return serve(app, host=args.host, port=args.port)
    except LoopbackOnly as refused:
        print(json.dumps({"error": {"code": "validation", "message": str(refused)}}), file=sys.stderr)
        return EXIT_VALIDATION
