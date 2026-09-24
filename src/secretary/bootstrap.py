"""Bootstrap the host-owned PostgreSQL board store and its Docker prerequisites.

The checkpoint deliberately does not carry these services or their credentials. They are
reproducible host state: this module installs Docker and Compose, provisions the
PostgreSQL board store (`board/provision.py`), migrates it to this build's schema
(`board/migrate.py`) and verifies its role contract. That empty, migrated store is the whole board a fresh installation starts from:
cards come later from `task create` or from install recovery restoring a checkpoint into it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

from secretary import _proc
from secretary._fsutil import write_text_atomic
from secretary.board.migrate import migrate_instance
from secretary.board.provision import provision as provision_board_store
from secretary.board.provision import verify_roles as verify_board_store_roles
from secretary.installation import (
    InstallError,
    _clone_or_reuse,
    _ensure_installation_user,
    _run,
    _set_installation_owner,
)

BOOTSTRAP_STAMP = ".secretary-bootstrap"


class BootstrapError(RuntimeError):
    pass


def _host_supported(os_release: Path = Path("/etc/os-release")) -> None:
    try:
        fields = dict(
            line.split("=", 1) for line in os_release.read_text(encoding="utf-8").splitlines() if "=" in line
        )
    except OSError:
        raise BootstrapError("could not identify the operating system") from None
    if fields.get("ID", "").strip('"') != "ubuntu" or fields.get("VERSION_ID", "").strip('"') != "24.04":
        raise BootstrapError("bootstrap supports Ubuntu 24.04 only")


def _install_platform(*, dry_run: bool, runtime_user: str | None = None) -> None:
    if dry_run:
        return
    needs_docker = shutil.which("docker") is None
    needs_compose = not _docker_compose_available()
    if needs_docker or needs_compose:
        if os.geteuid() != 0:
            raise BootstrapError("host prerequisites are absent; rerun bootstrap as root")
        _run(["apt-get", "update"], label="refresh apt")
        packages: list[str] = []
        if needs_docker:
            packages.append("docker.io")
        if needs_compose:
            packages.append(_compose_package())
        _run(
            ["apt-get", "install", "--yes", *packages],
            label="install Docker prerequisites",
        )
    _ensure_docker_ready()


def _compose_package() -> str:
    """Return the Compose v2 package exposed by this distribution's own apt archive."""
    for package in ("docker-compose-v2", "docker-compose-plugin"):
        try:
            result = _proc.run(["apt-cache", "show", package], timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise BootstrapError("could not inspect apt packages for Docker Compose") from None
        if result.returncode == 0:
            return package
    raise BootstrapError("no Docker Compose v2 package is available from configured apt sources")


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _proc.run(["docker", "compose", "version"], timeout=30).returncode == 0
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
            ready = _proc.run(["docker", "info"], timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ready = False
        if ready:
            return
        if time.monotonic() >= deadline:
            raise BootstrapError("Docker daemon did not become ready")
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
    try:
        if not args.dry_run and os.geteuid() != 0:
            raise BootstrapError("host bootstrap must run as root")
        if not args.dry_run:
            _host_supported()
        # Bootstrap may be safely rerun for an existing dedicated user.
        _ensure_installation_user(args.installation_user, recovery=True, dry_run=args.dry_run)
        _clone_or_reuse(
            args.instance_remote,
            target,
            recovery=True,
            dry_run=args.dry_run,
            installation_user=args.installation_user,
        )
        if not args.dry_run:
            _mark_bootstrap_checkout(target)
            _install_platform(dry_run=False, runtime_user=args.installation_user)
            provision_board_store(target, allow_create=True)
            migrate_instance(target)
            verify_board_store_roles(target)
            # Last, so the handoff covers what provisioning created as root under the instance:
            # `board-store.env` (0600, read by every role and instance-bound CLI) and its
            # `.gitignore` entry. The Compose definition stays root's in /opt/secretary.
            _set_installation_owner(target, args.installation_user)
        print("secretary bootstrap\nstatus: " + ("preview" if args.dry_run else "ok"))
        return 0
    except (BootstrapError, InstallError, OSError, RuntimeError) as exc:
        print(f"secretary bootstrap\nstatus: failed: {exc}")
        return 1
