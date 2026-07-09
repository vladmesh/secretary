"""Load and validate instance config against the bundled JSON Schemas.

The validation layer turns malformed config into readable messages with a path
to the offending field, so ``doctor`` never shows a traceback for bad input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

# schema name -> file in secretary/schemas/
SCHEMAS = {
    "instance": "instance.schema.json",
    "project-binding": "project-binding.schema.json",
    "adapter": "adapter.schema.json",
    "data-manifest": "data-manifest.schema.json",
}


class ConfigError(Exception):
    """A config file could not be read or parsed (not a schema violation)."""


@dataclass(frozen=True)
class SchemaError:
    """One schema violation, with a human path to the field."""

    source: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.source}: {self.path}: {self.message}"


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    try:
        filename = SCHEMAS[name]
    except KeyError:
        raise ValueError(f"unknown schema: {name}") from None
    text = resources.files("secretary.schemas").joinpath(filename).read_text("utf-8")
    return json.loads(text)


def load_config(path: Path) -> Any:
    """Read a YAML or JSON config file, raising ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    if not path.is_file():
        raise ConfigError(f"config is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ConfigError(f"cannot decode config as UTF-8: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config: {exc}") from exc
    try:
        # YAML is a JSON superset, so this loads both .yaml and .json.
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse config: {exc}") from exc


def _field_path(absolute_path: Any) -> str:
    parts: list[str] = []
    for token in absolute_path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            parts.append(f".{token}" if parts else str(token))
    return "".join(parts) or "<root>"


_LIMIT_KEYWORDS = {
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minProperties",
    "maxProperties",
}


def _safe_message(error: Any) -> str:
    """Describe a violation from the schema keyword and path only.

    jsonschema's own ``error.message`` embeds the offending instance value,
    which can be a secret. We rebuild the message from schema-side data (the
    keyword, its expected value, and property names) and never emit the value.
    """
    keyword = error.validator
    expected = error.validator_value

    if keyword == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [p for p in expected if p not in instance]
        names = ", ".join(missing) or ", ".join(expected)
        noun = "property" if len(missing) == 1 else "properties"
        return f"missing required {noun}: {names}"
    if keyword == "additionalProperties":
        allowed = set(error.schema.get("properties", {}))
        instance = error.instance if isinstance(error.instance, dict) else {}
        extra = sorted(k for k in instance if k not in allowed)
        noun = "property" if len(extra) == 1 else "properties"
        return f"unexpected {noun}: {', '.join(extra)}"
    if keyword == "type":
        names = expected if isinstance(expected, str) else "/".join(expected)
        return f"expected type {names}"
    if keyword == "enum":
        return "value must be one of: " + ", ".join(repr(v) for v in expected)
    if keyword == "const":
        return f"value must equal {expected!r}"
    if keyword == "pattern":
        return f"value must match pattern {expected!r}"
    if keyword in _LIMIT_KEYWORDS:
        return f"violates {keyword} = {expected}"
    return f"failed {keyword} constraint"


def validate(data: Any, schema_name: str, source: str) -> list[SchemaError]:
    """Validate ``data`` against a named schema. Returns errors, never raises."""
    validator = Draft202012Validator(load_schema(schema_name))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [
        SchemaError(source=source, path=_field_path(e.absolute_path), message=_safe_message(e))
        for e in errors
    ]


@dataclass
class InstanceReport:
    """Result of validating an instance config tree."""

    instance_path: Path
    name: str
    projects: int
    adapters: int
    has_manifest: bool
    errors: list[SchemaError]

    @property
    def ok(self) -> bool:
        return not self.errors


def _resolve_instance(path: Path) -> Path:
    """Accept either an instance dir or a direct path to instance.yaml."""
    if path.is_dir():
        return path / "instance.yaml"
    return path


def validate_instance(path: Path) -> InstanceReport:
    """Validate instance.yaml plus its bindings, adapters and data manifest.

    Read/parse failures are surfaced as SchemaError entries rather than raised,
    so callers can print one clean summary instead of a stack trace.
    """
    instance_file = _resolve_instance(path)
    instance_dir = instance_file.parent
    errors: list[SchemaError] = []

    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        return InstanceReport(
            instance_path=instance_file,
            name="",
            projects=0,
            adapters=0,
            has_manifest=False,
            errors=[SchemaError(str(instance_file), "<file>", str(exc))],
        )

    errors += validate(instance, "instance", instance_file.name)
    name = instance.get("name", "") if isinstance(instance, dict) else ""

    projects = _validate_dir(instance_dir / "projects", "project-binding", errors)
    adapters = _validate_dir(instance_dir / "adapters", "adapter", errors)

    manifest_file = instance_dir / "data-manifest.json"
    has_manifest = manifest_file.exists()
    if has_manifest:
        try:
            manifest = load_config(manifest_file)
        except ConfigError as exc:
            errors.append(SchemaError(manifest_file.name, "<file>", str(exc)))
        else:
            errors += validate(manifest, "data-manifest", manifest_file.name)

    return InstanceReport(
        instance_path=instance_file,
        name=name,
        projects=projects,
        adapters=adapters,
        has_manifest=has_manifest,
        errors=errors,
    )


def _validate_dir(directory: Path, schema_name: str, errors: list[SchemaError]) -> int:
    """Validate every *.yaml file in ``directory``. Returns the count seen."""
    if not directory.is_dir():
        return 0
    count = 0
    for config_file in sorted(directory.glob("*.yaml")):
        count += 1
        try:
            data = load_config(config_file)
        except ConfigError as exc:
            errors.append(SchemaError(config_file.name, "<file>", str(exc)))
            continue
        errors += validate(data, schema_name, config_file.name)
    return count
