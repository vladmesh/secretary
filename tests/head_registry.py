"""Fixtures for an installed, recovery-validated head registry."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from ummanu.head_registry import RegistryPair, generated_pair, snapshot_header


def write_installed_pair(instance: Path, snapshot: str, *, live_root: bool = False) -> Path:
    """Write a self-consistent registry pair without depending on a checkout.

    Fixtures that model a post-upgrade installation need the same pair that a
    recovered installation reads: `<data>/heads/`, so the instance's `instance.yaml`
    must already name its data directory.  ``live_root`` writes the pair where an
    upgrade before ummanu-26 committed it instead, the live root's `heads/`, which no
    reader consults any more (ummanu-39).  The
    canonical source deliberately need not exist: a reader validates the stored
    pair without consulting a product checkout, and the test fixture owns only the
    installed files.
    """
    pair = live_root_pair(instance) if live_root else generated_pair(instance)
    pair.snapshot.parent.mkdir(parents=True, exist_ok=True)
    canonical = instance / "heads" / "heads.toml"
    rendered = snapshot_header(canonical) + snapshot
    target = pair.snapshot
    target.write_text(rendered, encoding="utf-8")
    pair.source.write_text(
        yaml.safe_dump(
            {
                "canonical": str(canonical),
                "canonical_owner": "instance",
                "product_root": "/fixture/product",
                "revision": "fixture",
                "snapshot_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            },
            default_flow_style=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return target


def live_root_pair(instance: Path) -> RegistryPair:
    """The live root's pair, where `ummanu upgrade` wrote and committed it before ummanu-26."""
    heads = instance / "heads"
    return RegistryPair(heads / "heads.yaml", heads / "source.yaml")
