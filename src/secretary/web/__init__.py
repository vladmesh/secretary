"""The second transport over `secretary.webproto`, and deliberately nothing more.

`secretary web-read` and `secretary web-run` were the first: argparse in, JSON out, a typed
protocol code turned into an exit status in one table. This package is the same shape with a
different vocabulary — a request line in, JSON or HTML out, and a protocol code turned into an HTTP
status in one table (:mod:`secretary.web.statuses`). It reads through
:class:`~secretary.webproto.reads.ReadLayer` and runs through
:class:`~secretary.webproto.ops.OperationLayer`, and it has no snapshot, no state derivation, no
liveness rule and no mutation of its own. If a page needs a fact this installation does not already
answer, the answer is a change to the layer, not a second collector here.

Two properties are the point, and both have tests:

**Only named operations are reachable.** The route table in :mod:`secretary.web.app` is the whole
surface, and every route is one layer call. There is no endpoint that takes a command, a script, a
path or a module name to run, and `tests/test_web_transport.py` fails if the table grows one.

**Loopback only, and it stayed that way once the pipeline was published.** This service has no
password, no TLS and no authorisation of any kind, so anybody who can reach the port owns the
pipeline: it starts runs. Binding it to anything but a loopback address is refused in code
(:mod:`secretary.web.server`), not merely discouraged in a document. It now runs as a unit on a live
installation, and that refusal is exactly what makes the guarded front (:mod:`secretary.webfront`)
the only way in from off the host rather than one of two doors.
"""

from __future__ import annotations

from secretary.web.app import ROUTES, Response, WebApp
from secretary.web.server import DEFAULT_HOST, DEFAULT_PORT, LOOPBACK_ONLY, build_server, serve
from secretary.web.statuses import HTTP_STATUS_BY_CODE, UNMAPPED_CODE_STATUS, status_for

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HTTP_STATUS_BY_CODE",
    "LOOPBACK_ONLY",
    "ROUTES",
    "UNMAPPED_CODE_STATUS",
    "Response",
    "WebApp",
    "build_server",
    "serve",
    "status_for",
]
