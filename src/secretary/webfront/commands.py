"""`secretary web-front`: set the password, render the front's configuration, audit it.

Three verbs, and the split between them is the point. `set-password` is the only one that touches a
plaintext, and it never puts one on a command line or on stdout. `render` reads the *hash* out of
the secret store and writes the configuration under the data directory, so the repository holds no
credential and the running front holds no copy of one that the store does not own. `check` reads a
rendered configuration back and reports every published route it would answer without a password --
the same predicate `tests/test_web_front.py` runs, available on the host after a hand edit.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

from secretary.cli_output import print_json
from secretary.config import instance_data_dir
from secretary.secret_store import (
    MATERIALIZE_FILE,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    read_secret,
    set_secret,
)
from secretary.state_repo import StateRepoError
from secretary.web.app import ROUTES
from secretary.webfront.caddyfile import (
    HASH_SECRET_ID,
    PASSWORD_SECRET_ID,
    USERNAME,
    FrontConfig,
    FrontConfigError,
    render,
)
from secretary.webfront.guard import CaddyfileSyntaxError, unguarded_routes, upstreams

EXIT_VALIDATION = 2
EXIT_STATE = 3

DEFAULT_ACTOR = "operator"
#: Where a rendered configuration and Caddy's own storage live, under the data directory.
FRONT_DIRNAME = "webfront"
CONFIG_NAME = "Caddyfile"
STORAGE_NAME = "caddy"
#: The env file the owner reads their own password back from, mode 0600, outside the repository.
PASSWORD_FILE_NAME = "owner-password.env"
PASSWORD_VARIABLE = "SECRETARY_WEB_FRONT_PASSWORD"
#: Bytes of entropy in a generated password: 32 URL-safe characters out of `secrets`.
GENERATED_BYTES = 24


def add_web_front_subcommands(subparsers) -> None:
    group = subparsers.add_parser(
        "web-front",
        help="the password-guarded TLS front that publishes the loopback web transport",
    )
    commands = group.add_subparsers(dest="web_front_command")

    password = commands.add_parser(
        "set-password",
        help="store the owner's password and its bcrypt hash; a value never travels through argv",
    )
    password.add_argument("--instance", required=True)
    password.add_argument("--actor", default=DEFAULT_ACTOR)
    source = password.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="read the password from standard input")
    source.add_argument(
        "--generate",
        action="store_true",
        help="generate one from `secrets`, store it, and print nothing but where to read it",
    )
    password.add_argument("--caddy", default="caddy", help="the caddy executable that hashes it")
    password.set_defaults(handler=run_set_password)

    config = commands.add_parser(
        "render", help="write the front's configuration, taking the hash from the secret store"
    )
    config.add_argument("--instance", required=True)
    config.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    config.add_argument(
        "--site",
        action="append",
        required=True,
        metavar="ADDRESS",
        help="an https site address to answer on; repeat for a name and its addresses",
    )
    config.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="listen on this interface only; the rehearsal and rollback handle (see OPERATIONS.md)",
    )
    config.add_argument("--output", help="where to write it; the data directory's own path by default")
    config.add_argument("--upstream-port", type=int, default=None)
    config.set_defaults(handler=run_render)

    audit = commands.add_parser(
        "check", help="report every published route a rendered configuration answers unguarded"
    )
    audit.add_argument("--instance", required=True)
    audit.add_argument("--data-dir", default=os.environ.get("SECRETARY_DATA_DIR"))
    audit.add_argument("--config", help="the file to read; the data directory's own path by default")
    audit.set_defaults(handler=run_check)

    group.set_defaults(handler=lambda args: _usage(group))


# -- verbs ---------------------------------------------------------------------------------------


def run_set_password(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    if args.generate:
        password = pysecrets.token_urlsafe(GENERATED_BYTES)
    else:
        password = sys.stdin.read().strip("\r\n")
    if not password:
        return _fail("set-password", "validation", "the password is empty")
    try:
        digest = hash_password(password, executable=args.caddy)
    except FrontConfigError as exc:
        return _fail("set-password", "validation", str(exc))
    try:
        set_secret(
            instance_dir,
            secret_id=PASSWORD_SECRET_ID,
            value=password.encode("utf-8"),
            scope="installation",
            purpose="web front owner password",
            actor=args.actor,
            environment=PASSWORD_VARIABLE,
            materialize={
                "target": MATERIALIZE_FILE,
                "path": str(_front_dir(instance_dir, None) / PASSWORD_FILE_NAME),
            },
        )
        set_secret(
            instance_dir,
            secret_id=HASH_SECRET_ID,
            value=digest.encode("utf-8"),
            scope="installation",
            purpose="web front owner password bcrypt hash, read by `web-front render`",
            actor=args.actor,
        )
    except SecretStoreValidationError as exc:
        return _fail("set-password", "validation", str(exc))
    except SecretStoreStateError as exc:
        return _fail("set-password", "state", str(exc))
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("set-password", "runtime", str(exc))
    print_json(
        {
            "ok": True,
            "op": "set-password",
            "account": USERNAME,
            "password_secret": PASSWORD_SECRET_ID,
            "hash_secret": HASH_SECRET_ID,
            "generated": bool(args.generate),
            "read_it_back": (
                f"secretary secret materialize --instance {args.instance} --target file, then read "
                f"{PASSWORD_VARIABLE} from "
                f"{_front_dir(instance_dir, None) / PASSWORD_FILE_NAME}"
            ),
            "next": "secretary web-front render, then restart the front unit",
        }
    )
    return 0


def run_render(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    front = _front_dir(instance_dir, args.data_dir)
    output = Path(args.output).expanduser() if args.output else front / CONFIG_NAME
    try:
        digest = read_secret(instance_dir, HASH_SECRET_ID).decode("utf-8").strip()
    except SecretStoreStateError as exc:
        return _fail(
            "render",
            "state",
            f"{exc}; run `secretary web-front set-password` before rendering a front that "
            "would otherwise have no password to check",
        )
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("render", "runtime", str(exc))
    config = FrontConfig(
        sites=tuple(args.site),
        password_hash=digest,
        storage=front / STORAGE_NAME,
        bind=tuple(args.bind),
        **({"upstream_port": args.upstream_port} if args.upstream_port else {}),
    )
    try:
        text = render(config)
    except FrontConfigError as exc:
        return _fail("render", "validation", str(exc))
    findings = unguarded_routes(text, ROUTES)
    if findings:
        # Belt and braces: the renderer cannot produce this, and the file is not written if it does.
        return _fail("render", "state", "; ".join(findings))
    output.parent.mkdir(parents=True, exist_ok=True)
    (front / STORAGE_NAME).mkdir(parents=True, exist_ok=True)
    _write_private(output, text)
    print_json(
        {
            "ok": True,
            "op": "render",
            "config": str(output),
            "sites": list(config.sites),
            "bind": list(config.bind),
            "upstream": list(upstreams(text)),
            "routes_guarded": len(ROUTES),
            "storage": str(front / STORAGE_NAME),
        }
    )
    return 0


def run_check(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    front = _front_dir(instance_dir, args.data_dir)
    path = Path(args.config).expanduser() if args.config else front / CONFIG_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _fail("check", "state", f"could not read {path}: {exc}")
    try:
        findings = unguarded_routes(text, ROUTES)
        proxied = upstreams(text)
    except CaddyfileSyntaxError as exc:
        return _fail("check", "validation", f"{path} is not a configuration this reader can audit: {exc}")
    print_json(
        {
            "ok": not findings,
            "op": "check",
            "config": str(path),
            "routes": len(ROUTES),
            "unguarded": list(findings),
            "upstream": list(proxied),
        }
    )
    return 0 if not findings else EXIT_STATE


# -- helpers -------------------------------------------------------------------------------------


def hash_password(password: str, *, executable: str = "caddy") -> str:
    """The bcrypt hash `basicauth` checks, from the same binary that will check it.

    The plaintext goes in on stdin; it is never an argument, so it is never in this host's process
    table. `caddy hash-password` reads one line, which is why the newline is written explicitly.
    """
    try:
        finished = subprocess.run(
            [executable, "hash-password"],
            input=password + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise FrontConfigError(
            f"could not run {executable!r} to hash the password ({exc}); the front's own binary "
            "produces the hash it checks, so it must be installed first"
        ) from None
    if finished.returncode != 0:
        raise FrontConfigError(f"{executable} hash-password failed: {finished.stderr.strip()}")
    digest = finished.stdout.strip()
    if not digest.startswith("$2"):
        raise FrontConfigError(f"{executable} hash-password did not produce a bcrypt hash")
    return digest


def _front_dir(instance_dir: Path, data_dir: str | None) -> Path:
    root = Path(data_dir).expanduser() if data_dir else instance_data_dir(instance_dir)
    return root / FRONT_DIRNAME


def _write_private(path: Path, text: str) -> None:
    """Write the configuration where only its owner can read it: it carries a password hash."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _instance_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return path.parent if path.name == "instance.yaml" else path


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


def _fail(operation: str, error: str, message: str) -> int:
    print(
        json.dumps({"ok": False, "op": operation, "error": error, "message": message}, sort_keys=True),
        file=sys.stderr,
    )
    if error == "validation":
        return EXIT_VALIDATION
    if error == "state":
        return EXIT_STATE
    return 1
