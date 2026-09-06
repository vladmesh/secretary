"""The guarded front: a ready-made reverse proxy in front of the loopback web transport.

`secretary.web` binds a loopback address and refuses anything else, and that refusal is the
guarantee this package is built on rather than an obstacle it works around. The application has no
password, no TLS and no authorisation of its own, and it is never given any: everything that
reaches it from outside this host arrives through Caddy, which terminates TLS, checks a password
against a bcrypt hash and only then proxies to `127.0.0.1`. There is no second way in, because the
application still cannot be bound anywhere a second way could reach.

Nothing here implements authentication. Caddy's `basicauth` does, the Ubuntu archive ships it, and
the hash it checks comes out of the installation's own secret store at render time -- so the
repository holds a template with a placeholder and never a credential, and the rendered file is
machine state under the data directory.

Two modules, two jobs:

* :mod:`secretary.webfront.caddyfile` renders the configuration.
* :mod:`secretary.webfront.guard` reads a rendered configuration back and answers which of the
  transport's published routes it would serve without asking for the password. That is a parser
  rather than a template assertion on purpose: the question "is any route unguarded" has to be
  answerable about a file that somebody edited, and answerable about routes the route table grows
  later without anybody remembering to extend a list here.
"""

from __future__ import annotations

from secretary.webfront.caddyfile import (
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    HASH_SECRET_ID,
    PASSWORD_SECRET_ID,
    USERNAME,
    FrontConfig,
    render,
)
from secretary.webfront.guard import CaddyfileSyntaxError, unguarded_routes, upstreams

__all__ = [
    "DEFAULT_UPSTREAM_HOST",
    "DEFAULT_UPSTREAM_PORT",
    "HASH_SECRET_ID",
    "PASSWORD_SECRET_ID",
    "USERNAME",
    "CaddyfileSyntaxError",
    "FrontConfig",
    "render",
    "unguarded_routes",
    "upstreams",
]
