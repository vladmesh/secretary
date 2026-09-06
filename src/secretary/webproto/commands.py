"""`secretary web-read`: the read layer, callable before any web transport exists.

One command group with one subcommand per operation, so the layer can be exercised, diffed and
scripted from a shell -- and so the card that adds a real transport starts from a surface an
operator has already read with their own eyes rather than from an untried API.

This module is the only file under `webproto` that knows a caller exists. It parses arguments,
prints JSON and maps a typed read error onto an exit status; the layer itself does none of those
things, which is what keeps a second transport from having to re-implement any of it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from secretary.webproto.errors import ReadError
from secretary.webproto.journal import DEFAULT_LIMIT
from secretary.webproto.reads import TASK_SNAPSHOT_EVENTS, ReadLayer

#: Exit statuses, the same ones `secretary task` uses for the same two situations.
EXIT_VALIDATION = 2
EXIT_BACKEND = 1
_EXIT_BY_CODE = {"not_found": 2, "validation": 2, "backend_unavailable": 1}


def add_web_read_subcommands(subparsers) -> None:
    """Register the group. Read-only: none of these three takes an actor or writes anything."""
    group = subparsers.add_parser(
        "web-read",
        help="read the transport-independent system, task and event snapshots",
    )
    commands = group.add_subparsers(dest="web_read_command")

    system = commands.add_parser(
        "system", help="installation health, projects, current tasks and running agents"
    )
    _common(system)
    system.set_defaults(handler=run_web_read_system)

    task = commands.add_parser("task", help="one card: state, project, recent events and result")
    _common(task)
    task.add_argument("--ref", required=True, help="the card reference, as `secretary task show` takes it")
    task.add_argument(
        "--events",
        type=int,
        default=TASK_SNAPSHOT_EVENTS,
        help="how many of the card's most recent events to include",
    )
    task.set_defaults(handler=run_web_read_task)

    events = commands.add_parser("events", help="one page of a card's events, and a cursor to continue")
    _common(events)
    events.add_argument("--ref", required=True, help="the card reference")
    events.add_argument(
        "--cursor",
        help="the `next_cursor` of an earlier page; omit to start at the beginning of the journal",
    )
    events.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="page size")
    events.set_defaults(handler=run_web_read_events)

    group.set_defaults(handler=_usage)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True, help="path to an instance dir or instance.yaml")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("SECRETARY_DATA_DIR"),
        help="override the instance's configured data directory",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="collect installation health without inspecting the live host",
    )
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")


def _usage(_args: argparse.Namespace) -> int:
    print(json.dumps({"error": {"code": "usage", "message": "web-read subcommand required"}}))
    return EXIT_VALIDATION


def _layer(args: argparse.Namespace) -> ReadLayer:
    return ReadLayer(args.instance, data_dir=args.data_dir, offline=bool(args.offline))


def run_web_read_system(args: argparse.Namespace) -> int:
    return _emit(args, lambda: _layer(args).system_snapshot(), _system_lines)


def run_web_read_task(args: argparse.Namespace) -> int:
    return _emit(args, lambda: _layer(args).task_snapshot(args.ref, events=args.events), _task_lines)


def run_web_read_events(args: argparse.Namespace) -> int:
    return _emit(
        args,
        lambda: _layer(args).task_events(args.ref, args.cursor, limit=args.limit),
        _event_lines,
    )


def _emit(args: argparse.Namespace, operation, render) -> int:
    """Run one read, print it, and turn a typed refusal into the group's exit status."""
    try:
        snapshot = operation()
    except ReadError as exc:
        print(json.dumps({"error": exc.to_json()}), file=sys.stderr)
        return _EXIT_BY_CODE.get(exc.code, EXIT_BACKEND)
    if args.json:
        print(json.dumps(snapshot, sort_keys=True))
        return 0
    for line in render(snapshot):
        print(line)
    return 0


def _source(section: dict[str, Any]) -> str:
    source = section.get("source") or {}
    if source.get("state") == "available":
        return "available"
    age = source.get("data_age_seconds")
    aged = "" if age is None else f", showing data {int(age)}s old"
    return f"unavailable ({source.get('reason')}){aged}"


def _system_lines(snapshot: dict[str, Any]):
    installation = snapshot["installation"]
    yield f"instance: {installation['instance']} ({installation['name'] or 'unnamed'})"
    yield f"health: {_source(installation['health'])}"
    yield f"projects: {len(snapshot['projects']['items'])} registered, {_source(snapshot['projects'])}"
    yield f"tasks: {len(snapshot['tasks']['items'])} current, {_source(snapshot['tasks'])}"
    yield f"agents: {_source(snapshot['agents'])}"
    for agent in snapshot["agents"]["items"]:
        yield f"  {agent['ref']} {agent['role']}: {agent['state']} — {agent['reason']}"


def _task_lines(snapshot: dict[str, Any]):
    card = snapshot["card"]["value"]
    yield f"{snapshot['ref']}: {card['state'] if card else 'card ' + _source(snapshot['card'])}"
    if card:
        yield f"title: {card['title']}"
    yield f"project: {snapshot['project']['id'] or 'unknown'}"
    for agent in snapshot["agents"]["items"]:
        yield f"  {agent['role']}: {agent['state']} — {agent['reason']}"
    outcome = snapshot["work"]["outcome"]
    yield f"outcome: {outcome['kind']}:{outcome['value']} at {outcome['at']}" if outcome else "outcome: none"
    yield f"events: {len(snapshot['events']['items'])} shown, next cursor {snapshot['events']['next_cursor']}"


def _event_lines(snapshot: dict[str, Any]):
    yield f"{snapshot['ref']}: {len(snapshot['items'])} events, {_source(snapshot)}"
    for item in snapshot["items"]:
        yield f"  {item['occurred_at']} {item['kind']} {item['reason'] or item['outcome'] or ''}".rstrip()
    yield f"next cursor: {snapshot['next_cursor']}" + (" (more)" if snapshot["has_more"] else "")
