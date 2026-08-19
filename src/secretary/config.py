"""Load and validate instance config against the bundled JSON Schemas.

The validation layer turns malformed config into readable messages with a path
to the offending field, so ``doctor`` never shows a traceback for bad input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from secretary.sprints import budget_thresholds, open_sprint_limit_invalid
from secretary.tasks import TaskError

# schema name -> file in secretary/schemas/
SCHEMAS = {
    "instance": "instance.schema.json",
    "project-binding": "project-binding.schema.json",
    "adapter": "adapter.schema.json",
    "provision-result": "provision-result.schema.json",
    "provision-task": "provision-task.schema.json",
    "gate-result": "gate-result.schema.json",
    "data-manifest": "data-manifest.schema.json",
    "onboarding-contract": "onboarding-contract.schema.json",
    "status": "status.schema.json",
    "secret-catalog": "secret-catalog.schema.json",
}


class ConfigError(Exception):
    """A config file could not be read or parsed (not a schema violation)."""


class DataDirError(Exception):
    """An instance cannot provide a schema-valid configured data directory."""


@dataclass(frozen=True)
class SchemaError:
    """One schema violation, with a human path to the field."""

    source: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.source}: {self.path}: {self.message}"


@cache
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
    except UnicodeError:
        # str(exc) would carry offending bytes; keep the message content-free.
        raise ConfigError("cannot decode config as UTF-8") from None
    except OSError as exc:
        # strerror is the OS reason (e.g. "Permission denied"), never file body.
        raise ConfigError(f"cannot read config: {exc.strerror or 'unreadable'}") from None
    try:
        # YAML is a JSON superset, so this loads both .yaml and .json.
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot parse config: {_safe_yaml_error(exc)}") from None


def _safe_yaml_error(exc: yaml.YAMLError) -> str:
    """Describe a YAML parse failure with only a generic category and location.

    Neither ``str(exc)`` (which embeds the source snippet) nor ``exc.problem``
    (which can embed an alias/token name) is safe to print, so we report just
    "invalid YAML syntax" plus the line/column where the parser stopped.
    """
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return f"invalid YAML syntax (line {mark.line + 1}, column {mark.column + 1})"
    return "invalid YAML syntax"


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
        # Missing names come from the schema's own required list, not user input.
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [p for p in expected if p not in instance] or list(expected)
        noun = "property" if len(missing) == 1 else "properties"
        return f"missing required {noun}: {', '.join(missing)}"
    if keyword == "additionalProperties":
        # Extra keys are user-controlled, so report the count only, never the name.
        allowed = set(error.schema.get("properties", {}))
        instance = error.instance if isinstance(error.instance, dict) else {}
        extra = [k for k in instance if k not in allowed]
        noun = "property" if len(extra) == 1 else "properties"
        return f"{len(extra)} unexpected {noun}"
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
    adapter_drafts: int
    has_manifest: bool
    manifest_path: Path | None
    errors: list[SchemaError]
    warnings: list[SchemaError]
    bindings: list[dict[str, Any]]
    host: dict[str, Any]
    instance: dict[str, Any]
    data_dir: Path | None

    @property
    def ok(self) -> bool:
        return not self.errors


def _resolve_instance(path: Path) -> Path:
    """Accept either an instance dir or a direct path to instance.yaml."""
    expanded = path.expanduser()
    instance_file = expanded / "instance.yaml" if expanded.is_dir() else expanded
    return instance_file.resolve(strict=False)


def _configured_data_dir(instance_file: Path, instance: dict[str, Any]) -> Path:
    """Canonicalize a schema-valid configured path at the instance boundary."""
    configured = instance["data_dir"]
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = instance_file.parent / candidate
    return candidate.resolve(strict=False)


def instance_data_dir(path: Path) -> Path:
    """Return the canonical data directory configured by an instance.

    ``path`` may name the instance directory or its ``instance.yaml``. The
    configuration is schema-validated before its value is used, ``~`` is
    expanded, and a relative value is rooted at the resolved instance file's
    parent. This is the sole configuration boundary for configured data paths;
    command-line and environment overrides remain caller-owned paths.
    """
    instance_file = _resolve_instance(path)
    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        raise DataDirError(str(exc)) from None
    errors = validate(instance, "instance", instance_file.name)
    if errors:
        raise DataDirError(
            f"invalid instance {instance_file}: " + "; ".join(map(str, errors))
        )
    assert isinstance(instance, dict)
    return _configured_data_dir(instance_file, instance)


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
            adapter_drafts=0,
            has_manifest=False,
            manifest_path=None,
            errors=[SchemaError(str(instance_file), "<file>", str(exc))],
            warnings=[],
            bindings=[],
            host={},
            instance={},
            data_dir=None,
        )

    errors += validate(instance, "instance", instance_file.name)
    data_dir = (
        _configured_data_dir(instance_file, instance)
        if isinstance(instance, dict) and not errors
        else None
    )
    if isinstance(instance, dict):
        # Resolve omitted values before comparing the limits.  Runtime does the same,
        # so a partial setting cannot pass validation then stop every dispatcher tick.
        try:
            budget_thresholds(instance)
        except TaskError:
            errors.append(SchemaError(instance_file.name, "sprint_budget", "hard threshold must not be below signal threshold"))
        # The schema says which values are accepted; this says what the installation
        # does with one it refuses, because failing closed is silent otherwise: the
        # limit stays at one open sprint and an operator who set 3 sees no effect.
        if open_sprint_limit_invalid(instance):
            errors.append(SchemaError(
                instance_file.name,
                "open_sprint_limit",
                "must be the integer 1 or 2; this installation holds one open sprint until it is",
            ))
    name = instance.get("name", "") if isinstance(instance, dict) else ""
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    if not isinstance(host, dict):
        host = {}

    projects = _validate_dir(instance_dir / "projects", "project-binding", errors)
    adapters = _validate_dir(instance_dir / "adapters", "adapter", errors)
    adapter_drafts = _validate_dir(
        instance_dir / "adapter-drafts", "onboarding-contract", errors
    )
    bindings = _load_bindings(instance_dir / "projects")

    manifest_file = _find_manifest(instance_dir, data_dir)
    has_manifest = manifest_file is not None
    warnings: list[SchemaError] = []
    if has_manifest:
        try:
            manifest = load_config(manifest_file)
        except ConfigError as exc:
            errors.append(SchemaError(manifest_file.name, "<file>", str(exc)))
        else:
            errors += validate(manifest, "data-manifest", manifest_file.name)
    else:
        warnings.append(
            SchemaError(
                "data-manifest.json",
                "<file>",
                "data manifest absent",
            )
        )

    return InstanceReport(
        instance_path=instance_file,
        name=name,
        projects=projects,
        adapters=adapters,
        adapter_drafts=adapter_drafts,
        has_manifest=has_manifest,
        manifest_path=manifest_file,
        errors=errors,
        warnings=warnings,
        bindings=bindings,
        host=host,
        instance=instance if isinstance(instance, dict) else {},
        data_dir=data_dir,
    )


def _find_manifest(instance_dir: Path, data_dir: Path | None) -> Path | None:
    if data_dir is not None:
        data_manifest = data_dir / "data-manifest.json"
        if data_manifest.exists():
            return data_manifest

    legacy_manifest = instance_dir / "data-manifest.json"
    if legacy_manifest.exists():
        return legacy_manifest
    return None


def _load_bindings(directory: Path) -> list[dict[str, Any]]:
    """Read project bindings for the host inventory, ignoring unreadable files.

    Schema violations are reported separately by ``_validate_dir``; here we only
    need the parsed mappings, so anything that will not parse is skipped.
    """
    if not directory.is_dir():
        return []
    bindings: list[dict[str, Any]] = []
    for config_file in sorted(directory.glob("*.yaml")):
        try:
            data = load_config(config_file)
        except ConfigError:
            continue
        if isinstance(data, dict):
            bindings.append(data)
    return bindings


def _validate_dir(
    directory: Path,
    schema_name: str,
    errors: list[SchemaError],
) -> int:
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
