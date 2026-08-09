"""Fixtures for an installed, recovery-validated head registry."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from secretary.head_registry import snapshot_header


def write_installed_pair(instance: Path, snapshot: str) -> Path:
    """Write a self-consistent registry pair without depending on a checkout.

    Fixtures that model a post-upgrade installation need the same pair that a
    recovered installation reads.  The canonical source deliberately need not
    exist: recovery validates the stored pair without consulting a product
    checkout, and the test fixture owns only the installed files.
    """
    heads = instance / "heads"
    heads.mkdir(parents=True, exist_ok=True)
    canonical = heads / "heads.toml"
    rendered = snapshot_header(canonical) + snapshot
    target = heads / "heads.yaml"
    target.write_text(rendered, encoding="utf-8")
    (heads / "source.yaml").write_text(
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
