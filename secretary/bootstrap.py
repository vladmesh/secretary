"""Bootstrap the host-owned Kanboard and Orca prerequisites.

The checkpoint deliberately does not carry these services or their transport
configuration. They are reproducible host state: this module installs the
pinned transports, creates the deterministic local board configuration, and
builds the small Kanboard schema that the task protocol requires.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from secretary._fsutil import write_text_atomic
from secretary.board_transport import BoardTransport, ensure_from_runtime_values, transport_path
from secretary.config import validate_instance
from secretary.host_apply import pinned_orca_executable
from secretary.installation import (
    InstallError,
    _clone_or_reuse,
    _ensure_installation_user,
    _run,
    _set_installation_owner,
)
from secretary.runtime_env import RuntimeEnvMissing, read_runtime_env
from secretary.tasks import KanboardClient, TaskError, all_project_cards


KANBOARD_IMAGE = "kanboard/kanboard:v1.2.46"
ORCA_VERSION = "v1.4.152"
ORCA_APPIMAGE_URL = (
    "https://github.com/stablyai/orca/releases/download/"
    f"{ORCA_VERSION}/orca-linux.AppImage"
)
PIPELINE_COLUMNS = (
    "Issues", "Ready", "In progress", "Validate", "Assessment", "Blocked", "Done",
)
# The layout every live board carried before `Assessment` existed. It is not a supported
# schema: it is the one older layout `board migrate-assessment` knows how to repair in place,
# so a populated board sitting on it gets a pointer at that command instead of the generic
# refusal below.
LEGACY_PIPELINE_COLUMNS = ("Issues", "Ready", "In progress", "Validate", "Blocked", "Done")
ASSESSMENT_COLUMN = "Assessment"
ASSESSMENT_POSITION = PIPELINE_COLUMNS.index(ASSESSMENT_COLUMN) + 1
# The one half-finished layout the migration itself can leave behind: Kanboard appends a new
# column at the end, so a committed `addColumn` whose answer was lost, or a reposition that then
# failed, leaves the six known columns plus a trailing `Assessment`. The next run finishes it.
PARTIAL_PIPELINE_COLUMNS = (*LEGACY_PIPELINE_COLUMNS, ASSESSMENT_COLUMN)
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
    if (
        fields.get("ID", "").strip('"') != "ubuntu"
        or fields.get("VERSION_ID", "").strip('"') != "24.04"
    ):
        raise BootstrapError("bootstrap supports Ubuntu 24.04 only")


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
        if isinstance(item, dict):
            lane = item.get("orca_binding") or item.get("id")
            if isinstance(lane, str) and lane:
                lanes.add(lane)
    cards = instance / "state" / "board" / "cards.ndjson"
    if cards.is_file():
        try:
            for raw in cards.read_text(encoding="utf-8").splitlines():
                card = yaml.safe_load(raw)
                lane = card.get("swimlane") if isinstance(card, dict) else None
                if isinstance(lane, str) and lane:
                    lanes.add(lane)
        except (OSError, yaml.YAMLError):
            raise BootstrapError("could not read checkpoint board swimlanes") from None
    return lanes


def _rename_column(api: KanboardClient, column: dict, title: str) -> None:
    """Rename one column, refusing to treat a declined updateColumn as done.

    Kanboard answers this call with a boolean, so a rejected rename returns
    false instead of raising.  Ignoring it would leave the old title on the
    board while the caller reports a current schema.
    """
    if not api.call("updateColumn", column_id=int(column["id"]), title=title):
        raise BootstrapError(f"Kanboard did not rename the Pipeline column to {title}")


def ensure_pipeline_board(instance: Path, *, client: KanboardClient | None = None) -> int:
    """Create the Pipeline board, columns and registry swimlanes without moving cards."""
    try:
        api = client or KanboardClient.for_instance(instance)
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
            # A board that holds cards is never reshaped here: renaming a column in place would
            # silently change what its cards mean, and removing one moves every card it holds to
            # the trash.  Such a board is a migration job for a human, so name both layouts.
            tasks = all_project_cards(api, board_id)
            if tasks:
                hint = ""
                if titles == list(LEGACY_PIPELINE_COLUMNS):
                    hint = "; run `secretary board migrate-assessment` to add it in place"
                raise BootstrapError(
                    "Pipeline board has cards but an incompatible column schema: "
                    f"{', '.join(titles)} (expected: {', '.join(PIPELINE_COLUMNS)}; "
                    f"migratable: {', '.join(LEGACY_PIPELINE_COLUMNS)})" + hint
                )
            for index, title in enumerate(PIPELINE_COLUMNS):
                if index < len(columns) and isinstance(columns[index], dict) and columns[index].get("id"):
                    _rename_column(api, columns[index], title)
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


def _assessment_column_id(columns: list[Any]) -> int:
    """The id of the trailing `Assessment` column an interrupted migration left behind."""
    for column in columns:
        if isinstance(column, dict) and str(column.get("title") or "") == ASSESSMENT_COLUMN:
            try:
                identifier = int(column["id"])
            except (KeyError, TypeError, ValueError):
                break
            if identifier > 0:
                return identifier
    raise BootstrapError(f"Kanboard returned no usable id for the {ASSESSMENT_COLUMN} column")


def _card_placement(api: KanboardClient, board_id: int) -> dict[int, tuple[int, int]]:
    """Where every card sits right now: {task id: (column id, position)}.

    Read before and after the migration so a column insert that silently reshuffles or
    trashes a card is caught here instead of on the board.
    """
    placement: dict[int, tuple[int, int]] = {}
    for card in all_project_cards(api, board_id):
        try:
            identifier = int(card.get("id"))
        except (TypeError, ValueError):
            raise BootstrapError("Kanboard returned a Pipeline card without an id") from None
        placement[identifier] = (
            int(card.get("column_id") or 0), int(card.get("position") or 0),
        )
    return placement


def migrate_assessment_column(instance: Path | None = None, *, client: KanboardClient | None = None) -> dict[str, object]:
    """Add the `Assessment` column to a populated Pipeline board, in place.

    `ensure_pipeline_board` refuses to reshape a board that holds cards, on purpose: a rename
    changes what a column's cards mean and a removal trashes them. This is the one repair that
    is safe on a live board, because it only appends a column and slides it into position.

    Every outcome is retryable. A run that already finished is a no-op; a run whose `addColumn`
    committed but whose answer was lost (or whose reposition then failed) leaves the board on the
    one partial layout below, and the next run finishes that column instead of adding a second one.
    """
    try:
        if client is None and instance is None:
            raise BootstrapError("board migration requires the target instance")
        api = client or KanboardClient.for_instance(instance)
        board = api.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict) or not board.get("id"):
            raise BootstrapError("Pipeline board does not exist")
        board_id = int(board["id"])
        columns = api.call("getColumns", project_id=board_id) or []
        if not isinstance(columns, list):
            raise BootstrapError("Kanboard returned invalid Pipeline columns")
        titles = [str(column.get("title") or "") for column in columns if isinstance(column, dict)]
        if titles == list(PIPELINE_COLUMNS):
            return {
                "ok": True, "action": "board migrate-assessment", "status": "unchanged",
                "board_id": board_id, "columns": titles,
            }
        before = _card_placement(api, board_id)
        if titles == list(LEGACY_PIPELINE_COLUMNS):
            status = "migrated"
            added = api.call("addColumn", project_id=board_id, title=ASSESSMENT_COLUMN)
            if not isinstance(added, int) or added <= 0:
                raise BootstrapError(f"Kanboard did not add the {ASSESSMENT_COLUMN} column")
        elif titles == list(PARTIAL_PIPELINE_COLUMNS):
            # The column is on the board but never reached position 5: an earlier run added it and
            # lost the answer, or the reposition that followed failed. Finish that column rather
            # than adding a second one.
            status = "resumed"
            added = _assessment_column_id(columns)
        else:
            raise BootstrapError(
                "Pipeline board has an unexpected column schema: "
                f"{', '.join(titles)} (migratable: {', '.join(LEGACY_PIPELINE_COLUMNS)}; "
                f"resumable: {', '.join(PARTIAL_PIPELINE_COLUMNS)}; "
                f"expected after migration: {', '.join(PIPELINE_COLUMNS)})"
            )
        if not api.call(
            "changeColumnPosition",
            project_id=board_id,
            column_id=added,
            position=ASSESSMENT_POSITION,
        ):
            raise BootstrapError(
                f"Kanboard did not move {ASSESSMENT_COLUMN} to position {ASSESSMENT_POSITION}"
            )
        current = api.call("getColumns", project_id=board_id) or []
        titles = [
            str(column.get("title") or "") for column in current if isinstance(column, dict)
        ]
        if titles != list(PIPELINE_COLUMNS):
            raise BootstrapError(
                "Pipeline columns after the migration are "
                f"{', '.join(titles)} (expected: {', '.join(PIPELINE_COLUMNS)})"
            )
        after = _card_placement(api, board_id)
        if after != before:
            raise BootstrapError(
                "the migration moved or lost Pipeline cards: "
                f"{len(before)} card(s) before, {len(after)} after"
            )
        return {
            "ok": True, "action": "board migrate-assessment", "status": status,
            "board_id": board_id, "columns": titles, "cards": len(after),
        }
    except TaskError as exc:
        raise BootstrapError(exc.message) from None


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


def _install_platform(*, dry_run: bool, runtime_user: str | None = None) -> None:
    if dry_run:
        return
    needs_docker = shutil.which("docker") is None
    needs_compose = not _docker_compose_available()
    # Bootstrap owns a pinned runtime.  A legacy per-user CLI is suitable for
    # upgrading an existing installation, but it must not turn a new bootstrap
    # into an installation with an unpinned runtime.
    needs_orca = pinned_orca_executable() is None
    if needs_docker or needs_compose or needs_orca:
        if os.geteuid() != 0:
            raise BootstrapError("host prerequisites are absent; rerun bootstrap as root")
        _run(["apt-get", "update"], label="refresh apt")
        # Orca is an Electron AppImage.  These are its explicit runtime
        # dependencies on the one supported host release, not merely FUSE.
        packages = [
            "curl", "fuse", "libnss3", "libgtk-3-0t64", "libgbm1", "libasound2t64",
            "xvfb",
        ]
        if needs_docker:
            packages.append("docker.io")
        if needs_compose:
            packages.append(_compose_package())
        _run(
            ["apt-get", "install", "--yes", *packages],
            label="install Docker and Orca prerequisites",
        )
    _ensure_docker_ready()
    if needs_orca:
        if os.geteuid() != 0:
            raise BootstrapError("Orca is absent; rerun bootstrap as root")
        _install_orca()


def _install_orca() -> None:
    """Extract the AppImage and expose its Node-mode CLI launcher."""
    parent = Path("/opt/secretary")
    image = parent / f"orca-{ORCA_VERSION}.AppImage"
    install_root = parent / "orca"
    parent.mkdir(parents=True, exist_ok=True)
    if install_root.exists():
        raise BootstrapError(f"incomplete Orca installation exists at {install_root}")
    _run(
        ["curl", "--fail", "--location", "--output", str(image), ORCA_APPIMAGE_URL],
        label="download pinned Orca",
        timeout=300,
    )
    image.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix=".orca-extract-", dir=parent) as staging_raw:
        staging = Path(staging_raw)
        _run(
            [str(image), "--appimage-extract"],
            label="extract pinned Orca",
            timeout=300,
            cwd=staging,
        )
        extracted = staging / "squashfs-root"
        cli = extracted / "resources" / "bin" / "orca-ide"
        sandbox = extracted / "chrome-sandbox"
        if not cli.is_file() or not sandbox.is_file():
            raise BootstrapError("pinned Orca AppImage has an unsupported layout")
        _run(["chmod", "-R", "a+rX", str(extracted)], label="set Orca runtime permissions")
        os.chown(sandbox, 0, 0)
        sandbox.chmod(0o4755)
        os.replace(extracted, install_root)
    wrapper = Path("/usr/local/bin/orca")
    wrapper.unlink(missing_ok=True)
    wrapper.symlink_to(install_root / "resources" / "bin" / "orca-ide")


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


def _ensure_docker_ready(*, timeout: int = 60) -> None:
    """Enable Docker and wait until its daemon accepts a client connection."""
    if os.geteuid() != 0:
        raise BootstrapError("Docker must be started by root")
    _run(["systemctl", "enable", "--now", "docker"], label="start Docker")
    deadline = time.monotonic() + timeout
    while True:
        try:
            ready = subprocess.run(
                ["docker", "info"], capture_output=True, text=True, timeout=15,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ready = False
        if ready:
            return
        if time.monotonic() >= deadline:
            raise BootstrapError("Docker daemon did not become ready")
        time.sleep(1)


def _wait_for_kanboard(instance: Path, *, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            KanboardClient.for_instance(instance).call("getVersion")
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
        clone_detail = _clone_or_reuse(args.instance_remote, target, recovery=True, dry_run=args.dry_run)
        try:
            values = read_runtime_env(target, require_ignored=False)
        except RuntimeEnvMissing:
            values = {}
        transport_outcome = ensure_from_runtime_values(
            target, legacy_values=values, runtime_env=runtime, dry_run=args.dry_run,
            allow_default=clone_detail.startswith(("cloned", "would clone")),
        )
        transport = transport_outcome.transport
        if not args.dry_run:
            _mark_bootstrap_checkout(target)
            _set_installation_owner(target, args.installation_user)
            _install_platform(dry_run=False, runtime_user=args.installation_user)
            compose = Path("/opt/secretary/kanboard-compose.yml")
            _compose_file(compose)
            _run(["docker", "compose", "--env-file", str(transport_path(target)), "-f", str(compose), "up", "--detach"], label="start Kanboard", timeout=180)
            _wait_for_kanboard(target)
            ensure_pipeline_board(target, client=KanboardClient.for_instance(target))
        print("secretary bootstrap\nstatus: " + ("preview" if args.dry_run else "ok"))
        return 0
    except (BootstrapError, InstallError, TaskError, OSError, RuntimeError) as exc:
        print(f"secretary bootstrap\nstatus: failed: {exc}")
        return 1
