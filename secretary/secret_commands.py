"""`secretary secret` — init, set and list for the encrypted store.

Three rules shape this surface. A value never travels through argv, so `set`
reads stdin or a file and nothing else. No command here prints a value: `list`
prints catalog metadata, and reading a value is an internal API until the broker
card gives it a safe consumer. The recovery phrase is shown once, on stderr, and
the store is not created until the user has typed some of it back.
"""

from __future__ import annotations

import argparse
import json
import secrets as pysecrets
import sys
from pathlib import Path

from secretary.secret_store import (
    CONFIRM_WORDS,
    SecretStoreError,
    SecretStoreStateError,
    SecretStoreValidationError,
    generate_recovery_phrase,
    initialize_store,
    is_initialized,
    list_secrets,
    normalize_phrase,
    set_secret,
)
from secretary.state_repo import StateRepoError

SECRET_EXIT_VALIDATION = 2
SECRET_EXIT_STATE = 3

DEFAULT_ACTOR = "operator"


def add_secret_subcommands(subparsers) -> None:
    secret = subparsers.add_parser(
        "secret", help="manage the encrypted secret store in the instance repo"
    )
    secret_subcommands = secret.add_subparsers(dest="secret_command")

    init = secret_subcommands.add_parser(
        "init",
        help="generate the recovery phrase and create the store; refuses to overwrite one",
    )
    init.add_argument("--instance", required=True)
    init.add_argument("--actor", default=DEFAULT_ACTOR)
    init.add_argument(
        "--words",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    init.set_defaults(handler=run_secret_init)

    set_command = secret_subcommands.add_parser(
        "set", help="store one value read from stdin or a file; never from the command line"
    )
    set_command.add_argument("--instance", required=True)
    set_command.add_argument("--actor", default=DEFAULT_ACTOR)
    set_command.add_argument("--id", required=True, dest="secret_id")
    set_command.add_argument(
        "--scope", required=True, help="'installation' or 'project:<id>'"
    )
    set_command.add_argument("--purpose", required=True)
    set_command.add_argument(
        "--environment", help="environment variable this value materializes into"
    )
    source = set_command.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="read the value from this file")
    source.add_argument(
        "--stdin", action="store_true", help="read the value from standard input"
    )
    set_command.set_defaults(handler=run_secret_set)

    list_command = secret_subcommands.add_parser(
        "list", help="print catalog metadata; values are never printed"
    )
    list_command.add_argument("--instance", required=True)
    list_command.set_defaults(handler=run_secret_list)

    secret.set_defaults(handler=lambda args: _usage(secret))


def _usage(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


def run_secret_init(args: argparse.Namespace) -> int:
    instance_dir = _instance_dir(args.instance)
    if is_initialized(instance_dir):
        return _fail(
            "init",
            "state",
            "secret store is already initialized; init will not overwrite it",
        )
    words = args.words or 16
    try:
        phrase = generate_recovery_phrase(words)
    except SecretStoreValidationError as exc:
        return _fail("init", "validation", str(exc))

    _show_phrase(phrase)
    if not _confirm_phrase(phrase):
        return _fail(
            "init",
            "validation",
            "recovery phrase not confirmed; the store was not initialized",
        )
    try:
        result = initialize_store(instance_dir, phrase=phrase, actor=args.actor)
    except SecretStoreValidationError as exc:
        return _fail("init", "validation", str(exc))
    except SecretStoreStateError as exc:
        return _fail("init", "state", str(exc))
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("init", "runtime", str(exc))
    _print_json(
        {
            "ok": True,
            "op": "init",
            "key": str(result.key_path),
            "catalog": str(result.catalog_path),
            "commit": result.commit,
        }
    )
    return 0


def run_secret_set(args: argparse.Namespace) -> int:
    try:
        value = _read_value(args)
    except SecretStoreValidationError as exc:
        return _fail("set", "validation", str(exc))
    try:
        result = set_secret(
            _instance_dir(args.instance),
            secret_id=args.secret_id,
            value=value,
            scope=args.scope,
            purpose=args.purpose,
            environment=args.environment,
            actor=args.actor,
        )
    except SecretStoreValidationError as exc:
        return _fail("set", "validation", str(exc))
    except SecretStoreStateError as exc:
        return _fail("set", "state", str(exc))
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("set", "runtime", str(exc))
    _print_json(
        {
            "ok": True,
            "op": "set",
            "id": result.secret_id,
            "scope": result.scope,
            "bytes": len(value),
            "created": result.created,
            "commit": result.commit,
        }
    )
    return 0


def run_secret_list(args: argparse.Namespace) -> int:
    try:
        entries = list_secrets(_instance_dir(args.instance))
    except SecretStoreStateError as exc:
        return _fail("list", "state", str(exc))
    except (SecretStoreError, StateRepoError) as exc:
        return _fail("list", "runtime", str(exc))
    _print_json({"ok": True, "op": "list", "secrets": [dict(entry) for entry in entries]})
    return 0


def _read_value(args: argparse.Namespace) -> bytes:
    """Read the value as bytes, so multiline and binary survive untouched."""
    if args.file:
        path = Path(args.file).expanduser()
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            raise SecretStoreValidationError(f"value file not found: {path}") from None
        except OSError as exc:
            raise SecretStoreValidationError(f"could not read {path}: {exc}") from None
    else:
        value = sys.stdin.buffer.read()
    if not value:
        raise SecretStoreValidationError("secret value is empty")
    return value


def _show_phrase(phrase: str) -> None:
    """Show the phrase once, on stderr so a redirected stdout cannot capture it."""
    words = phrase.split()
    lines = [
        "",
        "Recovery phrase. Written down now or lost forever; it is not stored on this host.",
        "",
    ]
    lines += [f"  {index + 1:2d}. {word}" for index, word in enumerate(words)]
    lines += ["", f"Confirm {CONFIRM_WORDS} words to prove you wrote it down.", ""]
    print("\n".join(lines), file=sys.stderr)


def _confirm_phrase(phrase: str) -> bool:
    words = phrase.split()
    positions = sorted(_confirmation_positions(len(words)))
    for position in positions:
        try:
            answer = input(f"word {position + 1}: ")
        except EOFError:
            return False
        if normalize_phrase_or_empty(answer) != words[position]:
            return False
    return True


def normalize_phrase_or_empty(answer: str) -> str:
    try:
        return normalize_phrase(answer)
    except SecretStoreValidationError:
        return ""


def _confirmation_positions(count: int) -> set[int]:
    positions: set[int] = set()
    while len(positions) < min(CONFIRM_WORDS, count):
        positions.add(pysecrets.randbelow(count))
    return positions


def _instance_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return path.parent if path.name == "instance.yaml" else path


def _fail(operation: str, error: str, message: str) -> int:
    _print_json({"ok": False, "op": operation, "error": error, "message": message})
    if error == "validation":
        return SECRET_EXIT_VALIDATION
    if error == "state":
        return SECRET_EXIT_STATE
    return 1


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


__all__ = [
    "add_secret_subcommands",
    "run_secret_init",
    "run_secret_list",
    "run_secret_set",
]
