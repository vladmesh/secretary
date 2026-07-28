"""The installation head registry: materialized from a TOML canon, read by the tick.

Two files live side by side under the installation's ``heads/`` directory. ``heads.yaml`` is the
registry the running installation uses; ``source.yaml`` records which canonical file, which product
checkout and which revision ``secretary upgrade`` generated it from. The pair is the whole point: a
live tick reads only the installation's own files, so a product checkout that moves — a branch, an
uncommitted edit, a half-finished refactor — cannot change or stop a running installation. Only an
upgrade moves the installation, and the pin says what it moved to.

Which heads exist is installation configuration, not product code: the accounts, models and
fallback chains one host pays for are not the ones another host has. So an installation may own
``heads/heads.toml`` in its instance directory, and that file is then the canon. An installation
that owns no such file falls back to the product's small default registry, which is enough to bring
a host up on a Claude or an OpenAI subscription and nothing more. A canon that is *there* but
unusable — malformed, unreadable, a directory, a dangling symlink — is a broken installation, not a
portable one: it stops the upgrade by name rather than quietly reverting the host to product heads
the operator never chose.
"""

from __future__ import annotations

import stat
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
    validate_role_defaults,
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
    """The three registry tables, checked the same way wherever they came from.

    The shared validators check the nested shapes too, and every failure they raise is turned into
    one bounded error naming ``origin``. Nothing malformed may leave this function as a raw
    AttributeError or TypeError: an upgrade step and `secretary status` both report on what they
    find at a path, so a broken registry has to arrive as a message about that path.
    """
    tables: dict[str, Any] = {}
    for key in ("resources", "profiles", "role_defaults"):
        value = data.get(key)
        if not isinstance(value, dict):
            raise HeadRegistryConfigError(f"head registry {origin} has no [{key}] table")
        tables[key] = value
    try:
        validate_registry(tables["resources"], tables["profiles"])
        validate_role_defaults(tables["role_defaults"], tables["profiles"])
    except HeadRegistryError as exc:
        raise HeadRegistryConfigError(f"head registry {origin} is invalid: {exc}") from None
    return tables


def _instance_dir(instance_path: Path) -> Path:
    """The instance directory, whether the caller named it or its ``instance.yaml``.

    A path that cannot even be stat'd still has to yield a directory, because the caller's job is
    then to raise a bounded error naming a file under it. So an unreadable path falls back to its
    own shape instead of letting a raw ``PermissionError`` out of a probe.
    """
    try:
        is_dir = instance_path.is_dir()
    except OSError:
        is_dir = instance_path.name != "instance.yaml"
    instance_file = instance_path / "instance.yaml" if is_dir else instance_path
    return instance_file.parent


def canonical_path(product_root: Path, instance_path: Path | None = None) -> tuple[Path, str]:
    """The registry that wins, and which side of the boundary owns it.

    An installation that ships ``heads/heads.toml`` has decided what its own heads are; the product
    default is only for one that has not. "Has not" means the file is genuinely absent — anything
    else at that path is a canon the operator meant to have, so it fails here by name rather than
    letting the host silently run product heads.

    Every probe goes through one ``lstat``, and every way it can fail is answered here. A
    ``Path.is_file()`` would raise a bare ``PermissionError`` out of an unreadable ``heads/``
    directory, past the ``HeadRegistryConfigError`` an upgrade step knows how to report, and the
    operator would get a traceback instead of the path that is wrong.
    """
    if instance_path is None:
        return product_root / HEADS_RELATIVE, PRODUCT_ORIGIN
    owned = _instance_dir(instance_path) / INSTANCE_HEADS_RELATIVE
    try:
        mode = owned.lstat().st_mode
    except FileNotFoundError:
        return product_root / HEADS_RELATIVE, PRODUCT_ORIGIN
    except OSError as exc:
        raise HeadRegistryConfigError(
            f"cannot inspect instance head registry {owned}: {exc}"
        ) from None
    if stat.S_ISLNK(mode):
        try:
            mode = owned.stat().st_mode
        except FileNotFoundError:
            raise HeadRegistryConfigError(
                f"instance head registry {owned} is a dangling symlink"
            ) from None
        except OSError as exc:
            raise HeadRegistryConfigError(
                f"cannot inspect instance head registry {owned}: {exc}"
            ) from None
    if stat.S_ISREG(mode):
        return owned, INSTANCE_ORIGIN
    if stat.S_ISDIR(mode):
        raise HeadRegistryConfigError(f"instance head registry {owned} is a directory, not a file")
    raise HeadRegistryConfigError(f"instance head registry {owned} is not a regular file")


def canonical_heads(product_root: Path, instance_path: Path | None = None) -> dict[str, Any]:
    path, _ = canonical_path(product_root, instance_path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        # Reuse the triggered-agents validator so both consumers reject the same malformed canon.
        load_registry(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, HeadRegistryError) as exc:
        raise HeadRegistryConfigError(f"cannot load canonical head registry {path}: {exc}") from None
    return _validated_registry(data, path)


def render_snapshot(heads: dict[str, Any], canonical: Path) -> str:
    return snapshot_header(canonical) + yaml.safe_dump(
        heads,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


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
