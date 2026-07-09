from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _required(data: dict[str, Any], keys: tuple[str, ...], subject: str) -> list[str]:
    return [f"{subject}: missing {key}" for key in keys if key not in data]


def validate_json_schema(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object"]
        required = schema.get("required", [])
        errors.extend(_required(data, tuple(required), path))
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(data) - set(properties)):
                errors.append(f"{path}: unexpected {key}")
        for key, child_schema in properties.items():
            if key in data:
                errors.extend(validate_json_schema(data[key], child_schema, f"{path}.{key}"))
        return errors

    if expected_type == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array"]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data):
                errors.extend(validate_json_schema(item, item_schema, f"{path}[{index}]"))
        return errors

    if expected_type == "string":
        if not isinstance(data, str):
            return [f"{path}: expected string"]
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(data) < min_length:
            errors.append(f"{path}: expected at least {min_length} characters")

    if expected_type == "integer":
        if isinstance(data, bool) or not isinstance(data, int):
            return [f"{path}: expected integer"]
        minimum = schema.get("minimum")
        if isinstance(minimum, int) and data < minimum:
            errors.append(f"{path}: expected at least {minimum}")

    if expected_type == "boolean" and not isinstance(data, bool):
        return [f"{path}: expected boolean"]

    allowed = schema.get("enum")
    if isinstance(allowed, list) and data not in allowed:
        errors.append(f"{path}: expected one of {allowed}")

    return errors


def _validate_with_schema(data: dict[str, Any], schema_name: str) -> list[str]:
    schema = load_json(ROOT / "schemas" / schema_name)
    return validate_json_schema(data, schema)


def validate_instance_config(data: dict[str, Any]) -> list[str]:
    return _validate_with_schema(data, "instance.schema.json")


def validate_project_binding(data: dict[str, Any]) -> list[str]:
    return _validate_with_schema(data, "project-binding.schema.json")


def validate_adapter(data: dict[str, Any]) -> list[str]:
    return _validate_with_schema(data, "adapter.schema.json")


def validate_data_manifest(data: dict[str, Any]) -> list[str]:
    return _validate_with_schema(data, "data-manifest.schema.json")
