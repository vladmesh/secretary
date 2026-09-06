"""`secretary web-serve`: the third command over the same layer, beside `web-read` and `web-run`.

It builds the four layers from the same arguments those groups take -- `--instance`, `--data-dir`,
`--heads-registry` -- hands them to the application, and serves. Nothing about a snapshot, a state,
a run or a sprint is decided here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from secretary.web.app import WebApp
from secretary.web.server import DEFAULT_HOST, DEFAULT_PORT, LoopbackOnly, serve
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import ReadLayer
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


def run_web_serve(args: argparse.Namespace) -> int:
    app = WebApp(
        ReadLayer(args.instance, data_dir=args.data_dir, offline=bool(args.offline)),
        OperationLayer(args.instance, data_dir=args.data_dir, registry_path=args.heads_registry),
        SprintReadLayer(args.instance, data_dir=args.data_dir),
        SprintOperationLayer(args.instance, data_dir=args.data_dir),
    )
    try:
        return serve(app, host=args.host, port=args.port)
    except LoopbackOnly as refused:
        print(json.dumps({"error": {"code": "validation", "message": str(refused)}}), file=sys.stderr)
        return EXIT_VALIDATION
