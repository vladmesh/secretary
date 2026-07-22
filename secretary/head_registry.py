"""Materialize the installation head registry from the product TOML canon."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import write_text_atomic
from triggered_agents.agents.pipeline.heads import HeadRegistryError, load_registry


HEADS_RELATIVE = Path("triggered_agents") / "agents" / "pipeline" / "heads.toml"
SNAPSHOT_RELATIVE = Path("heads") / "heads.yaml"
SNAPSHOT_HEADER = (
    "# Generated from triggered_agents/agents/pipeline/heads.toml by `secretary upgrade`.\n"
    "# Do not edit this snapshot by hand.\n"
)


class HeadRegistryConfigError(RuntimeError):
    """The product canon or installation snapshot cannot be loaded."""


def canonical_heads(product_root: Path) -> dict[str, Any]:
    path = product_root / HEADS_RELATIVE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # Reuse the triggered-agents validator so both consumers reject the same malformed canon.
        load_registry(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, HeadRegistryError) as exc:
        raise HeadRegistryConfigError(f"cannot load canonical head registry {path}: {exc}") from None
    expected: dict[str, Any] = {}
    for key in ("resources", "profiles", "role_defaults"):
        value = data.get(key)
        if not isinstance(value, dict):
            raise HeadRegistryConfigError(f"canonical head registry {path} has no [{key}] table")
        expected[key] = value
    return expected


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
