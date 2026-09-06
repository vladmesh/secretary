"""The socket half: `http.server` from the standard library, bound to loopback and nothing else.

There is no web framework in this project's dependencies and this card adds none — `pyproject.toml`
holds PyYAML, jsonschema and cryptography, and a dashboard that reads three documents and posts two
does not need more. `ThreadingHTTPServer` is enough: requests are short, each one is a handful of
file reads, and a browser that opens two connections must not deadlock behind one.

**Where this may listen is not a preference, and DoD 5 did not change that.** The service has no
password, no TLS and no authorisation of any kind, and its POST routes start real heads on this
installation. Anybody who can reach the port therefore owns the pipeline. So a non-loopback address
is refused here, in code, and the slice that published this installation kept the refusal exactly
as it was rather than relaxing it: the front (:mod:`secretary.webfront`) terminates TLS and checks
a password and then proxies to `127.0.0.1`, so this refusal is what makes the front the only way in
from off this host. Weakening it would not add a feature; it would add a second, unguarded door.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from secretary.web.app import MAX_BODY_BYTES, WebApp

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

#: Why a non-loopback bind is refused, quoted verbatim into the refusal and into OPERATIONS.md.
LOOPBACK_ONLY = (
    "this service has no password, no TLS and no authorisation, and its routes start real heads on "
    "this installation, so it binds a loopback address only. External access is published by the "
    "guarded front instead (`secretary web-front`, DoD 5), which terminates TLS, checks a password "
    "and proxies here; this refusal is what makes that front the only way in"
)


class LoopbackOnly(Exception):
    """A bind this transport refuses. Raised before a socket exists, never after."""


def resolve_bind(host: str) -> tuple[int, str]:
    """The socket family and the literal address to bind, or a refusal — before any socket exists.

    A name is not an address, and a name is what an operator types. `localhost` is loopback on
    every host anyone has ever met, but that is a convention of `/etc/hosts` and NSS rather than a
    property of the spelling: a host may map it, or `localhost.localdomain`, to a routable address,
    and a check that compared spellings would then hand exactly that address to `socket.bind` and
    publish a service with no password and no TLS. So the name is resolved here and every address
    it resolves to must be loopback; one that is not refuses the bind. What is bound afterwards is
    the literal address this resolution produced, not the name — nothing gets to resolve it a
    second time, to something else, between the check and the socket.
    """
    candidate = (host or "").strip() or DEFAULT_HOST
    literal = candidate.strip("[]")
    try:
        infos = socket.getaddrinfo(literal, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LoopbackOnly(f"{host!r} does not resolve to an address ({exc}): {LOOPBACK_ONLY}") from None
    if not infos:
        raise LoopbackOnly(f"{host!r} does not resolve to an address: {LOOPBACK_ONLY}")
    resolved: list[tuple[int, str]] = []
    for family, _socktype, _proto, _canonname, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise LoopbackOnly(f"{host!r} is not an IP address: {LOOPBACK_ONLY}")
        found = str(sockaddr[0]).partition("%")[0]
        try:
            address = ipaddress.ip_address(found)
        except ValueError:
            raise LoopbackOnly(f"{host!r} resolves to {found!r}, which is not an address: {LOOPBACK_ONLY}") from None
        if not address.is_loopback:
            raise LoopbackOnly(
                f"{host!r} resolves to {found}, which is not a loopback address: {LOOPBACK_ONLY}"
            )
        resolved.append((family, found))
    return resolved[0]


def check_bind(host: str) -> str:
    """The literal loopback address to bind, or a refusal. See :func:`resolve_bind`."""
    return resolve_bind(host)[1]


class _Handler(BaseHTTPRequestHandler):
    """The thinnest adapter there is: request line in, `WebApp.handle` out.

    It decides nothing. Every status it writes was decided by the application, which took it from
    the one code-to-status table, and every body it writes was produced there too.
    """

    protocol_version = "HTTP/1.1"
    server_version = "secretary-web"
    sys_version = ""

    def do_GET(self) -> None:  # the base class names the verbs
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_HEAD(self) -> None:
        self._answer("GET", head=True)

    def _answer(self, method: str, *, head: bool = False) -> None:
        path, _, query = self.path.partition("?")
        try:
            body = self._read_body()
        except ValueError as exc:
            self._write(413, str(exc).encode("utf-8"), "text/plain; charset=utf-8", head=False)
            return
        response = self.server.app.handle(method, path, query=query, body=body)
        self._write(response.status, response.body, response.content_type, head=head, extra=response.headers)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("this request declares a Content-Length that is not a number") from None
        if length > MAX_BODY_BYTES:
            raise ValueError(f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here")
        return self.rfile.read(length) if length > 0 else b""

    def _write(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head: bool,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # No frame, no third party, no script this service did not serve itself: the pages are one
        # file each and load nothing from anywhere.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # the base class names this argument
        """One line per request on stderr: the diagnostic OPERATIONS.md points at."""
        print(f"{self.address_string()} {format % args}", file=sys.stderr)


class WebServer(ThreadingHTTPServer):
    """A threading server that carries the application it answers from."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], app: WebApp, *, family: int = socket.AF_INET
    ) -> None:
        self.app = app
        self.address_family = family
        super().__init__(address, _Handler)


def build_server(app: WebApp, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> WebServer:
    """A bound server, or a refusal — and the refusal comes before the socket."""
    family, address = resolve_bind(host)
    return WebServer((address, int(port)), app, family=family)


def serve(app: WebApp, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Bind, announce where to point a browser, and serve until interrupted."""
    server = build_server(app, host=host, port=port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    shown = f"[{bound_host}]" if ":" in str(bound_host) else bound_host
    print(f"secretary web on http://{shown}:{bound_port} — {LOOPBACK_ONLY}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", file=sys.stderr)
    finally:
        server.shutdown()
        server.server_close()
    return 0
