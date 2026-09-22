"""The installed Kanboard Compose project: where its file lives and what its service is called.

An installation bootstrapped before the PostgreSQL-only bootstrap (secretary-1666) has this file;
the data plane copies the board storage out of the container that file's service runs
(`data.raw_kanboard_dump`). Nothing writes the file any more.
"""

from __future__ import annotations

from pathlib import Path

# The path bootstrap used to write and pass to `docker compose -f`. Compose records it verbatim on
# every container it creates (`com.docker.compose.project.config_files`), which is what makes the
# installed container findable without guessing `<project>-<service>-1`.
KANBOARD_COMPOSE_FILE = Path("/opt/secretary/kanboard-compose.yml")
KANBOARD_COMPOSE_SERVICE = "kanboard"

__all__ = ["KANBOARD_COMPOSE_FILE", "KANBOARD_COMPOSE_SERVICE"]
