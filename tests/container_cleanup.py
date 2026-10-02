"""Ownership-scoped disposal of PostgreSQL test containers and Compose resources."""

from __future__ import annotations

import json
import os
import subprocess

from ummanu.runtime.container_labels import PRODUCTION_BOARD_LABEL, TEST_BOARD_LABEL


def _docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _inspect(kind: str, name: str) -> dict:
    try:
        data = json.loads(_docker(kind, "inspect", name))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("ambiguous inspection")
        return data[0]
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid {kind} inspection for {name}: {exc}") from exc


def remove_test_container(container_id: str, *, project: str | None = None) -> None:
    """Fail closed if the exact container no longer has this process's test marker."""
    if not container_id or len(container_id.splitlines()) != 1:
        raise RuntimeError("missing or ambiguous test container ID")
    payload = _inspect("container", container_id)
    labels = payload.get("Config", {}).get("Labels") or {}
    if payload.get("Id") != container_id or not isinstance(labels, dict):
        raise RuntimeError(f"test container identity mismatch: {container_id}")
    if labels.get(TEST_BOARD_LABEL) != str(os.getpid()) or PRODUCTION_BOARD_LABEL in labels:
        raise RuntimeError(f"test container ownership mismatch: {container_id}")
    if project is not None and (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "postgres"
    ):
        raise RuntimeError(f"test Compose identity mismatch: {container_id}")
    _docker("rm", "-f", container_id)


def cleanup_test_project(project: str, *, container_expected: bool = True) -> None:
    """Find the one test service by project, verify it, then remove its own resources."""
    ids = _docker("ps", "--all", "--quiet", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}").splitlines()
    if len(ids) > 1:
        raise RuntimeError(f"ambiguous test Compose containers for {project}: {ids}")
    if not ids and container_expected:
        raise RuntimeError(f"missing test Compose container for {project}")
    # A setup attempt that never left an owned container has no authority over a same-named
    # volume or network left by an earlier attempt.
    if not ids:
        return
    if ids:
        remove_test_container(ids[0], project=project)
    for kind, name, discriminator in (
        ("network", f"{project}_default", "com.docker.compose.network"),
        ("volume", f"{project}_board-db", "com.docker.compose.volume"),
    ):
        try:
            payload = _inspect(kind, name)
        except RuntimeError as exc:
            if "No such" in str(exc) or "not found" in str(exc):
                continue
            raise
        labels = payload.get("Labels") or {}
        expected = "default" if kind == "network" else "board-db"
        if payload.get("Name") != name or not isinstance(labels, dict) or (
            labels.get("com.docker.compose.project") != project
            or labels.get(discriminator) != expected
        ):
            raise RuntimeError(f"test Compose {kind} ownership mismatch: {name}")
        _docker(kind, "rm", name)
