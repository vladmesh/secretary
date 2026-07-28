"""The installation head registry: materialized from a TOML canon, read by the tick.

Two files live side by side under the installation's ``heads/`` directory. ``heads.yaml`` is the
registry the running installation uses; ``source.yaml`` records which canonical file, which
product checkout and which revision ``secretary upgrade`` generated it from. The pair is the whole
point: a live tick reads only the installation's own files, so a product checkout that moves — a
branch, an uncommitted edit, a half-finished refactor — cannot change or stop a running
installation. Only an upgrade moves the installation, and the pin says what it moved to.

Which heads exist is installation configuration, not product code: the accounts, models and
fallback chains one host pays for are not the ones another host has. So an installation may own
``heads/heads.toml`` in its instance repository and that file is the canon. A portable
installation that owns no such file falls back to the product's small default registry, which is
enough to bring a host up on a Claude or Codex subscription and nothing more.
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
INSTANCE_HEADS_RELATIVE = Path("heads") / "heads.toml"
SNAPSHOT_RELATIVE = Path("heads") / "heads.yaml"
SOURCE_RELATIVE = Path("heads") / "source.yaml"
SOURCE_HEADER = (
    "# The canonical registry, checkout and revision `secretary upgrade` generated heads.yaml from.\n"
    "# Do not edit this pin by hand.\n"
)
UNKNOWN_REVISION = "unknown"
PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"


def snapshot_header(canonical: Path) -> str:
    """Name the file this snapshot came from, so a reader is never left guessing which canon won."""
    return (
        f"# Generated from {canonical} by `secretary upgrade`.\n"
        "# Do not edit this snapshot by hand.\n"
    )


class HeadRegistryConfigError(RuntimeError):
    """The canonical registry or installation snapshot cannot be loaded."""


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


def canonical_path(product_root: Path, instance_path: Path | None = None) -> tuple[Path, str]:
    """The registry that wins, and which side of the boundary owns it.

    An installation that ships ``heads/heads.toml`` has decided what its own heads are; the product
    default is only for one that has not.
    """
    if instance_path is not None:
        owned = _instance_dir(instance_path) / INSTANCE_HEADS_RELATIVE
        if owned.is_file():
            return owned, INSTANCE_ORIGIN
    return product_root / HEADS_RELATIVE, PRODUCT_ORIGIN


def canonical_heads(product_root: Path, instance_path: Path | None = None) -> dict[str, Any]:
    path, _ = canonical_path(product_root, instance_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # Reuse the triggered-agents validator so both consumers reject the same malformed canon.
        load_registry(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, HeadRegistryError) as exc:
        raise HeadRegistryConfigError(f"cannot load canonical head registry {path}: {exc}") from None
    return _validated_registry(data, path)


def render_snapshot(heads: dict[str, Any], canonical: Path | None = None) -> str:
    return snapshot_header(canonical if canonical is not None else Path(HEADS_RELATIVE)) + yaml.safe_dump(
        heads,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def _instance_dir(instance_path: Path) -> Path:
    """The instance directory, whether the caller named it or its ``instance.yaml``."""
    instance_file = instance_path / "instance.yaml" if instance_path.is_dir() else instance_path
    return instance_file.parent


def snapshot_path(instance_path: Path) -> Path:
    return _instance_dir(instance_path) / SNAPSHOT_RELATIVE


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
    return _instance_dir(instance_path) / SOURCE_RELATIVE


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
    """Write which canon, checkout and revision the snapshot came from. Returns whether it moved.

    ``canonical`` and ``canonical_owner`` are the point of the pin now that two files can be the
    canon: reading ``revision`` alone would credit the product for a registry the instance owns.
    """
    target = source_path(instance_path)
    canonical, origin = canonical_path(product_root, instance_path)
    desired = SOURCE_HEADER + yaml.safe_dump(
        {
            "canonical": str(canonical),
            "canonical_owner": origin,
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
    canonical, _ = canonical_path(product_root, instance_path)
    desired = render_snapshot(canonical_heads(product_root, instance_path), canonical)
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
    canonical = canonical_heads(product_root, instance_path)
    snapshot = load_snapshot(instance_path)
    if snapshot != canonical:
        target = snapshot_path(instance_path)
        raise HeadRegistryConfigError(
            f"installation head snapshot {target} is stale; run `secretary upgrade --instance "
            f"{instance_path}` to regenerate it"
        )
    return canonical
