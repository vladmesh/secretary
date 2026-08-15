"""Location and Orca identity of the dispatcher-owned observer repository."""

from __future__ import annotations

from pathlib import Path

OBSERVER_REPO_NAME = "observers"


def observer_root_repo(data_dir: Path) -> Path:
    """Return the lazily-created repository shared by sprint observers."""
    return data_dir / "dispatcher" / "observer-root" / OBSERVER_REPO_NAME
