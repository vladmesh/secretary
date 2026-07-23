"""Bootstrap the host-owned Kanboard and Orca prerequisites.

The checkpoint deliberately does not carry these services or their credentials.
They are reproducible host state: this module installs the pinned transports,
generates the local credentials, and builds the small Kanboard schema that the
task protocol requires.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
import yaml

from secretary._fsutil import write_text_atomic
from secretary.installation import (
    InstallError,
    _clone_or_reuse,
    _ensure_installation_user,
    _run,
    _set_installation_owner,
)
from secretary.tasks import KanboardClient, TaskError


KANBOARD_IMAGE = "kanboard/kanboard:v1.2.46"
ORCA_VERSION = "v1.4.150"
ORCA_APPIMAGE_URL = (
    "https://github.com/stablyai/orca/releases/download/"
    f"{ORCA_VERSION}/orca-linux.AppImage"
)
PIPELINE_COLUMNS = ("Идеи", "Ready", "In progress", "Validate", "Blocked", "Done")
BOOTSTRAP_STAMP = ".secretary-bootstrap"


class BootstrapError(RuntimeError):
    pass


def _host_supported(os_release: Path = Path("/etc/os-release")) -> None:
    try:
        fields = dict(
            line.split("=", 1) for line in os_release.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        raise BootstrapError("could not identify the operating system") from None
    if fields.get("ID", "").strip('"') != "ubuntu":
        raise BootstrapError("bootstrap supports Ubuntu only")


def _project_lanes(instance: Path) -> set[str]:
    lanes: set[str] = set()
    projects = instance / "projects"
    if not projects.is_dir():
        return lanes
    for path in projects.glob("*.yaml"):
        try:
            item = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise BootstrapError(f"could not read project registry entry {path.name}") from None
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            lanes.add(item["id"])
    return lanes


def ensure_pipeline_board(instance: Path, *, client: KanboardClient | None = None) -> int:
    """Create the Pipeline board, columns and registry swimlanes without moving cards."""
    try:
        api = client or KanboardClient()
        board = api.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict) or not board.get("id"):
            board_id = api.call("createProject", name="Pipeline")
            if not isinstance(board_id, int) or board_id <= 0:
                raise BootstrapError("Kanboard did not create the Pipeline board")
        else:
            board_id = int(board["id"])
        columns = api.call("getColumns", project_id=board_id) or []
        if not isinstance(columns, list):
            raise BootstrapError("Kanboard returned invalid Pipeline columns")
        titles = [str(column.get("title") or "") for column in columns if isinstance(column, dict)]
        if titles != list(PIPELINE_COLUMNS):
            # Kanboard defaults getAllTasks to open cards.  status_id=0 asks
            # for closed cards, which must count too: removing a column moves
            # every card it contains to the trash.
            tasks = api.call("getAllTasks", project_id=board_id, status_id=0) or []
            if tasks:
                raise BootstrapError("Pipeline board has cards but an incompatible column schema")
            for index, title in enumerate(PIPELINE_COLUMNS):
                if index < len(columns) and isinstance(columns[index], dict) and columns[index].get("id"):
                    api.call("updateColumn", column_id=int(columns[index]["id"]), title=title)
                else:
                    api.call("addColumn", project_id=board_id, title=title)
            # Kanboard 1.2.46 creates four defaults. Remove surplus only while empty.
            for column in columns[len(PIPELINE_COLUMNS):]:
                if isinstance(column, dict) and column.get("id"):
                    api.call("removeColumn", column_id=int(column["id"]))
        lanes = api.call("getActiveSwimlanes", project_id=board_id) or []
        known = {
            str(lane.get("name")) for lane in lanes
            if isinstance(lane, dict) and isinstance(lane.get("name"), str)
        }
        for name in sorted(_project_lanes(instance) - known):
            api.call("addSwimlane", project_id=board_id, name=name)
        return board_id
    except TaskError as exc:
        raise BootstrapError(exc.message) from None


def _runtime_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise BootstrapError("runtime.env must be a regular 0600 file")
        for raw in path.read_text(encoding="utf-8").splitlines():
            if "=" in raw and not raw.lstrip().startswith("#"):
                key, value = raw.split("=", 1)
                values[key] = value
    return values


def _write_runtime(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, "".join(f"{key}={values[key]}\n" for key in sorted(values)))
    path.chmod(0o600)


def _compose_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, f"""services:
  kanboard:
    image: {KANBOARD_IMAGE}
    restart: unless-stopped
    ports:
      - 127.0.0.1:8080:80
    environment:
      API_AUTHENTICATION_TOKEN: ${{KANBOARD_API_TOKEN}}
    volumes:
      - kanboard-data:/var/www/app/data
volumes:
  kanboard-data:
""")
    path.chmod(0o600)


def _install_platform(*, dry_run: bool) -> None:
    if dry_run:
        return
    needs_docker = shutil.which("docker") is None
    needs_compose = not _docker_compose_available()
    needs_orca = shutil.which("orca") is None
    if needs_docker or needs_compose or needs_orca:
        if os.geteuid() != 0:
            raise BootstrapError("host prerequisites are absent; rerun bootstrap as root")
        _run(["apt-get", "update"], label="refresh apt")
        packages = ["curl", _fuse_package()]
        if needs_docker:
            packages.append("docker.io")
        if needs_compose:
            packages.append(_compose_package())
        _run(
            ["apt-get", "install", "--yes", *packages],
            label="install Docker and Orca prerequisites",
        )
    if needs_orca:
        if os.geteuid() != 0:
            raise BootstrapError("Orca is absent; rerun bootstrap as root")
        target = Path("/opt/secretary/orca-linux.AppImage")
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["curl", "--fail", "--location", "--output", str(target), ORCA_APPIMAGE_URL], label="download pinned Orca", timeout=300)
        target.chmod(0o755)
        wrapper = Path("/usr/local/bin/orca")
        write_text_atomic(wrapper, f"#!/bin/sh\nexec {target} \"$@\"\n")
        wrapper.chmod(0o755)


def _compose_package() -> str:
    """Return the Compose v2 package exposed by this distribution's own apt archive."""
    for package in ("docker-compose-v2", "docker-compose-plugin"):
        try:
            result = subprocess.run(
                ["apt-cache", "show", package], capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BootstrapError("could not inspect apt packages for Docker Compose") from None
        if result.returncode == 0:
            return package
    raise BootstrapError("no Docker Compose v2 package is available from configured apt sources")


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _fuse_package(os_release: Path = Path("/etc/os-release")) -> str:
    try:
        fields = dict(
            line.split("=", 1) for line in os_release.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except OSError:
        raise BootstrapError("could not identify the operating system") from None
    version = fields.get("VERSION_ID", "").strip('"')
    if fields.get("ID", "").strip('"') == "ubuntu" and version >= "24.04":
        return "libfuse2t64"
    return "libfuse2"


def _start_orca_service(user: str) -> None:
    if os.geteuid() != 0:
        raise BootstrapError("host bootstrap must run as root")
    unit = Path("/etc/systemd/system/secretary-orca.service")
    write_text_atomic(unit, f"""[Unit]
Description=Secretary Orca runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
ExecStart=/usr/local/bin/orca serve --port 6768 --pairing-address 127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
""")
    unit.chmod(0o644)
    _run(["systemctl", "daemon-reload"], label="reload systemd")
    _run(["systemctl", "enable", "--now", "secretary-orca.service"], label="start Orca runtime")


def _wait_for_kanboard(values: dict[str, str], *, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            KanboardClient({
                "KANBOARD_URL": values["KANBOARD_URL"], "KANBOARD_API_USER": "jsonrpc",
                "KANBOARD_API_TOKEN": values["KANBOARD_API_TOKEN"],
            }).call("getVersion")
            return
        except TaskError:
            if time.monotonic() >= deadline:
                raise BootstrapError("Kanboard did not become ready") from None
            time.sleep(1)


def _mark_bootstrap_checkout(target: Path) -> None:
    """Mark the one clean checkout that may proceed through its first install."""
    stamp = target / BOOTSTRAP_STAMP
    write_text_atomic(stamp, "created by secretary bootstrap\n")
    exclude = target / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = (f"/{BOOTSTRAP_STAMP}", "/runtime.env")
    known = set(existing.splitlines())
    missing = [entry for entry in entries if entry not in known]
    if missing:
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        write_text_atomic(exclude, existing + suffix + "".join(f"{entry}\n" for entry in missing))


def bootstrap(args: argparse.Namespace) -> int:
    target = Path(args.instance_dir).expanduser().resolve()
    runtime = target / "runtime.env"
    try:
        if not args.dry_run and os.geteuid() != 0:
            raise BootstrapError("host bootstrap must run as root")
        if not args.dry_run:
            _host_supported()
        # Bootstrap may be safely rerun for an existing dedicated user.
        _ensure_installation_user(args.installation_user, recovery=True, dry_run=args.dry_run)
        _clone_or_reuse(args.instance_remote, target, recovery=True, dry_run=args.dry_run)
        values = _runtime_values(runtime)
        values.setdefault("KANBOARD_URL", "http://127.0.0.1:8080/jsonrpc.php")
        # Kanboard's supported application-token API authenticates as `jsonrpc`.
        # It avoids trying to mutate admin credentials through an API that cannot do so.
        values.setdefault("KANBOARD_API_USER", "jsonrpc")
        values.setdefault("KANBOARD_API_TOKEN", secrets.token_urlsafe(32))
        if not args.dry_run:
            _write_runtime(runtime, values)
            _mark_bootstrap_checkout(target)
            _set_installation_owner(target, args.installation_user)
            _install_platform(dry_run=False)
            _start_orca_service(args.installation_user)
            compose = Path("/opt/secretary/kanboard-compose.yml")
            _compose_file(compose)
            _run(["docker", "compose", "--env-file", str(runtime), "-f", str(compose), "up", "--detach"], label="start Kanboard", timeout=180)
            _wait_for_kanboard(values)
            ensure_pipeline_board(target, client=KanboardClient(values))
        print("secretary bootstrap\nstatus: " + ("preview" if args.dry_run else "ok"))
        return 0
    except (BootstrapError, InstallError, TaskError, OSError, RuntimeError) as exc:
        print(f"secretary bootstrap\nstatus: failed: {exc}")
        return 1
