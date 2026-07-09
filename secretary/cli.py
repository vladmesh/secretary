from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


NOT_IMPLEMENTED = "not implemented in Phase 1 skeleton"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secretary")
    subparsers = parser.add_subparsers(dest="command")

    doctor = subparsers.add_parser("doctor", help="inspect an instance without changing the host")
    doctor.add_argument("--dry-run", action="store_true", help="required for the Phase 1 doctor")
    doctor.add_argument("--instance", required=True, help="path to a mock instance config")
    doctor.set_defaults(handler=run_doctor)

    for name in ("reconcile", "backup", "restore"):
        command = subparsers.add_parser(name)
        command.add_argument("args", nargs="*")
        command.set_defaults(handler=not_implemented(name))

    project = subparsers.add_parser("project")
    project_subcommands = project.add_subparsers(dest="project_command")
    project_add = project_subcommands.add_parser("add")
    project_add.add_argument("path_or_url", nargs="?")
    project_add.set_defaults(handler=not_implemented("project add"))
    project.set_defaults(handler=not_implemented("project"))

    for name in ("task", "memory"):
        command = subparsers.add_parser(name)
        command.add_argument("args", nargs="*")
        command.set_defaults(handler=not_implemented(name))

    return parser


def run_doctor(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("secretary doctor requires --dry-run in the Phase 1 skeleton")
        return 2

    instance_path = Path(args.instance)
    try:
        instance = load_instance_config(instance_path)
    except ConfigError as exc:
        print(f"secretary doctor: {exc}")
        return 1

    print("Secretary doctor report")
    print("mode: dry-run")
    print(f"instance: {instance_path}")
    print(f"name: {instance.get('name', 'unnamed')}")
    print(f"projects: {len(instance.get('projects', []))}")
    print(f"adapters: {len(instance.get('adapters', []))}")
    print("host changes: none")
    print("status: ok")
    return 0


def not_implemented(command: str):
    def handler(_args: argparse.Namespace) -> int:
        print(f"secretary {command}: {NOT_IMPLEMENTED}")
        return 1

    return handler


class ConfigError(Exception):
    pass


def load_instance_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"instance config not found: {path}")
    if not path.is_file():
        raise ConfigError(f"instance config is not a file: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read instance config: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"cannot decode instance config as UTF-8: {exc}") from exc
    try:
        if path.suffix == ".json":
            data = json.loads(text)
        else:
            data = parse_mock_yaml(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(f"cannot parse instance config: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("instance config must be a mapping")
    for key in ("projects", "adapters"):
        value = data.get(key, [])
        if not isinstance(value, list):
            raise ConfigError(f"{key} must be a list")
    return data


def parse_mock_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list: list[Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue

        if line.startswith("  - "):
            if current_list is None:
                raise ValueError("list item without a list key")
            item = line[4:].strip()
            current_list.append(parse_scalar(item))
            continue

        if line.startswith(" "):
            raise ValueError(f"unsupported indentation: {raw_line}")

        if ":" not in line:
            raise ValueError(f"expected key: value line: {raw_line}")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise ValueError("empty key")

        if value == "":
            current_list = []
            data[key] = current_list
        else:
            current_list = None
            data[key] = parse_scalar(value)

    return data


def parse_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value == "null":
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value
