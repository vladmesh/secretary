"""The installed Kanboard Compose project: where its file lives and what its service is called.

`bootstrap` writes and starts this file; the data plane copies the board storage out of the
container that file's service runs (`data.raw_kanboard_dump`). Both need the same two facts, and
neither may own them: `installation` already imports `data`, so a constant kept in `bootstrap`
would close an import cycle the moment `data` read it. They live here instead, defined once.
"""

from __future__ import annotations

from pathlib import Path

# The path `bootstrap` writes and passes to `docker compose -f`. Compose records it verbatim on
# every container it creates (`com.docker.compose.project.config_files`), which is what makes the
# installed container findable without guessing `<project>-<service>-1`.
KANBOARD_COMPOSE_FILE = Path("/opt/secretary/kanboard-compose.yml")
KANBOARD_COMPOSE_SERVICE = "kanboard"

__all__ = ["KANBOARD_COMPOSE_FILE", "KANBOARD_COMPOSE_SERVICE"]
