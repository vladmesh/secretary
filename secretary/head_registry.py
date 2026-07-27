"""The installation head registry: materialized from the product TOML canon, read by the tick.

Two files live side by side under the installation's ``heads/`` directory. ``heads.yaml`` is the
registry the running installation uses; ``source.yaml`` records which product checkout and which
revision ``secretary upgrade`` generated it from. The pair is the whole point: a live tick reads
only the installation's own files, so a product checkout that moves — a branch, an uncommitted
edit, a half-finished refactor — cannot change or stop a running installation. Only an upgrade
moves the installation, and the pin says what it moved to.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import write_text_atomic
from triggered_agents.agents.pipeline.heads import (
    HeadRegistryError,
    load_registry,
    validate_registry,
)


HEADS_RELATIVE = Path("triggered_agents") / "agents" / "pipeline" / "heads.toml"
SNAPSHOT_RELATIVE = Path("heads") / "heads.yaml"
SOURCE_RELATIVE = Path("heads") / "source.yaml"
SNAPSHOT_HEADER = (
    "# Generated from triggered_agents/agents/pipeline/heads.toml by `secretary upgrade`.\n"
    "# Do not edit this snapshot by hand.\n"
)
SOURCE_HEADER = (
    "# The product checkout and revision `secretary upgrade` generated heads.yaml from.\n"
    "# Do not edit this pin by hand.\n"
)
UNKNOWN_REVISION = "unknown"


class HeadRegistryConfigError(RuntimeError):
    """The product canon or installation snapshot cannot be loaded."""


def _validated_registry(data: dict[str, Any], origin: Path) -> dict[str, Any]:
    """The three registry tables, checked the same way wherever they came from."""
    tables: dict[str, Any] = {}
    for key in ("resources", "profiles", "role_defaults"):
        value = data.get(key)
        if not isinstance(value, dict):
            raise HeadRegistryConfigError(f"head registry {origin} has no [{key}] table")
        tables[key] = value
    try:
        validate_registry(tables["resources"], tables["profiles"])
    except HeadRegistryError as exc:
        raise HeadRegistryConfigError(f"head registry {origin} is invalid: {exc}") from None
    for role, head in tables["role_defaults"].items():
        if head not in tables["profiles"]:
            raise HeadRegistryConfigError(
                f"head registry {origin} routes role {role!r} to unknown head {head!r}"
            )
    return tables


def canonical_heads(product_root: Path) -> dict[str, Any]:
    path = product_root / HEADS_RELATIVE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # Reuse the triggered-agents validator so both consumers reject the same malformed canon.
        load_registry(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, HeadRegistryError) as exc:
        raise HeadRegistryConfigError(f"cannot load canonical head registry {path}: {exc}") from None
    return _validated_registry(data, path)


def render_snapshot(heads: dict[str, Any]) -> str:
    return SNAPSHOT_HEADER + yaml.safe_dump(
        heads,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def snapshot_path(instance_path: Path) -> Path:
    instance_file = instance_path / "instance.yaml" if instance_path.is_dir() else instance_path
    return instance_file.parent / SNAPSHOT_RELATIVE


def load_snapshot(instance_path: Path) -> dict[str, Any]:
    path = snapshot_path(instance_path)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HeadRegistryConfigError(f"cannot load installation head snapshot {path}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadRegistryConfigError(f"installation head snapshot {path} has an unsupported shape")
    return loaded


def source_path(instance_path: Path) -> Path:
    instance_file = instance_path / "instance.yaml" if instance_path.is_dir() else instance_path
    return instance_file.parent / SOURCE_RELATIVE


def product_revision(product_root: Path) -> str:
    """The commit the checkout is on, or ``unknown`` when it cannot be read.

    A checkout with no git — a fixture, an unpacked tarball — is still a legitimate source; the pin
    then records the path alone rather than refusing to be written.
    """
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={product_root}", "-C", str(product_root),
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN_REVISION
    if result.returncode != 0:
        return UNKNOWN_REVISION
    return (result.stdout or "").strip() or UNKNOWN_REVISION


def read_source(instance_path: Path) -> dict[str, Any] | None:
    """The recorded canon source, or None when this installation has never been upgraded."""
    path = source_path(instance_path)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HeadRegistryConfigError(f"cannot load head registry source pin {path}: {exc}") from None
    if not isinstance(loaded, dict):
        raise HeadRegistryConfigError(f"head registry source pin {path} has an unsupported shape")
    return loaded


def record_source(instance_path: Path, product_root: Path, *, dry_run: bool = False) -> bool:
    """Write which checkout and revision the snapshot came from. Returns whether it moved."""
    target = source_path(instance_path)
    desired = SOURCE_HEADER + yaml.safe_dump(
        {
            "product_root": str(Path(product_root).expanduser().resolve(strict=False)),
            "revision": product_revision(product_root),
        },
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except (OSError, UnicodeError) as exc:
        raise HeadRegistryConfigError(f"cannot read head registry source pin {target}: {exc}") from None
    if current == desired:
        return False
    if not dry_run:
        try:
            write_text_atomic(target, desired)
        except RuntimeError as exc:
            raise HeadRegistryConfigError(str(exc)) from None
    return True


def installed_heads(instance_path: Path) -> dict[str, Any]:
    """The registry this installation runs off, validated. No product checkout is consulted.

    This is what a live tick reads. It deliberately does not compare against any checkout's
    ``heads.toml``: the installation moves when `secretary upgrade` moves it and at no other time.
    A snapshot that is itself broken — missing a table, naming an unknown resource or adapter,
    routing a role to a head that does not exist — still stops the caller, by name.
    """
    return _validated_registry(load_snapshot(instance_path), snapshot_path(instance_path))


def materialize_snapshot(instance_path: Path, product_root: Path, *, dry_run: bool = False) -> bool:
    """Write the canonical snapshot. Returns whether the target differs."""
    target = snapshot_path(instance_path)
    desired = render_snapshot(canonical_heads(product_root))
    try:
        current = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    except (OSError, UnicodeError) as exc:
        raise HeadRegistryConfigError(f"cannot read installation head snapshot {target}: {exc}") from None
    if current == desired:
        return False
    if not dry_run:
        try:
            write_text_atomic(target, desired)
        except RuntimeError as exc:
            raise HeadRegistryConfigError(str(exc)) from None
    return True


def assert_snapshot_current(instance_path: Path, product_root: Path) -> dict[str, Any]:
    canonical = canonical_heads(product_root)
    snapshot = load_snapshot(instance_path)
    if snapshot != canonical:
        target = snapshot_path(instance_path)
        raise HeadRegistryConfigError(
            f"installation head snapshot {target} is stale; run `secretary upgrade --instance "
            f"{instance_path}` to regenerate it"
        )
    return canonical
