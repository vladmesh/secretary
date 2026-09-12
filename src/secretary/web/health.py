"""A bounded loopback read probe for the web transport, and the target an installed unit serves.

`step_memory` has `probe_memory`: a restart that is not followed by a read is not evidence that the
new process came up, only that systemd accepted the command. The web transport needs the same
evidence and almost none of the machinery, because it has no authentication of its own and its
cheapest read is a GET — so this module is one bounded request against the address the *installed
unit* names, not against a default this module invented.

Reading the address out of the unit is the point rather than a convenience. The unit is what the
running process was started from, so a probe derived from it cannot quietly check a different
address than the one being served; and because the answer is still run through
:func:`secretary.web.server.check_bind`, a unit that had been edited to publish the transport off
loopback refuses the probe instead of letting this module reach out over the network.
"""

from __future__ import annotations

import http.client
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from secretary.web.server import DEFAULT_HOST, DEFAULT_PORT, check_bind

#: The cheapest read the transport answers: one JSON document, no card, no history.
DEFAULT_PROBE_PATH = "/api/system"
#: A probe reads enough to know the body arrived and never more. It is a liveness read.
MAX_PROBE_BODY_BYTES = 4096
DEFAULT_PROBE_TIMEOUT_SECONDS = 20.0
DEFAULT_PROBE_RETRY_SECONDS = 0.5

_HOST_RE = re.compile(r"--host[=\s]+(\S+)")
_PORT_RE = re.compile(r"--port[=\s]+(\d+)")


class WebProbeError(RuntimeError):
    """The web transport did not answer a bounded loopback read. The message names the target."""


@dataclass(frozen=True)
class WebTarget:
    """Where a probe reads, spelled the way the report prints it."""

    host: str
    port: int
    path: str = DEFAULT_PROBE_PATH

    @property
    def authority(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def url(self) -> str:
        return f"http://{self.authority}:{self.port}{self.path}"


def target_from_unit(content: bytes | str | None, *, path: str = DEFAULT_PROBE_PATH) -> WebTarget:
    """The address the installed unit serves, or the shipped default when it names none.

    A unit whose `ExecStart` names a non-loopback address is refused here, by `check_bind`, rather
    than probed: this module would otherwise be the one place in the product that sends a request
    to whatever address a unit file happened to contain.
    """
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else (content or "")
    host_match = _HOST_RE.search(text)
    port_match = _PORT_RE.search(text)
    host = host_match.group(1) if host_match else DEFAULT_HOST
    port = int(port_match.group(1)) if port_match else DEFAULT_PORT
    return WebTarget(check_bind(host), port, path)


def _connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _read_once(target: WebTarget, *, timeout: float, connect: Callable[..., Any]) -> int:
    connection = connect(target.host, target.port, timeout)
    try:
        connection.request("GET", target.path)
        response = connection.getresponse()
        response.read(MAX_PROBE_BODY_BYTES)
        return int(response.status)
    finally:
        connection.close()


def probe_web(
    target: WebTarget,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    retry_seconds: float = DEFAULT_PROBE_RETRY_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    connect: Callable[..., Any] = _connection,
) -> int:
    """One bounded loopback GET, retried until the deadline. Returns 200 or raises.

    A restarted unit needs a moment to bind, so a refused connection is retried; and a process that
    came up and answers 5xx is retried too, because the two are the same question — is this
    transport serving yet — and only the deadline is allowed to answer "no". Nothing here is
    unbounded: every attempt carries the socket timeout, and the loop carries the deadline.
    """
    deadline = clock() + max(0.0, timeout_seconds)
    attempts = 0
    last = "no attempt completed"
    while True:
        attempts += 1
        try:
            status = _read_once(target, timeout=max(0.5, min(timeout_seconds, 10.0)), connect=connect)
        except (OSError, http.client.HTTPException) as exc:
            # The class, not the message: a socket error's text carries the address it dialled.
            last = type(exc).__name__
        else:
            if status == 200:
                return status
            last = f"HTTP {status}"
        if clock() >= deadline:
            raise WebProbeError(
                f"{target.url} did not answer 200 within {timeout_seconds:g}s "
                f"({attempts} attempt(s), last: {last})"
            )
        sleep(retry_seconds)
