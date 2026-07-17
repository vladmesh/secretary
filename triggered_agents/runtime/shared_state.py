"""Shared state locations that must be stable across checkouts."""
from __future__ import annotations

import os
from pathlib import Path

WORKSPACES_ROOT = Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces").resolve()
AGENTS_PROJECT = os.environ.get("SECRETARY_ORCA_PROJECT", "secretary")


def resolve_pipeline_state_dir(workspaces_root: Path | None = None) -> Path:
    """State dir owned by the live pipeline dispatcher worktree.

    `TA_PIPELINE_STATE_DIR` is a test and host-layout override. Without it, the dispatcher state
    lives in the reserved secretary/pipeline worktree under the shared Orca workspaces root.
    """
    override = os.environ.get("TA_PIPELINE_STATE_DIR")
    if override:
        return Path(override)
    root = WORKSPACES_ROOT if workspaces_root is None else Path(workspaces_root)
    return root / AGENTS_PROJECT / "pipeline" / "state" / "pipeline"
