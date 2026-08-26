#!/usr/bin/env python3
"""Audit and sync role-owned skills into shell-owned skill directories.

Two registries, layered. The product manifest is the portable one, read from whichever checkout
the caller names, because an upgrade can materialize a host from a checkout other than the one
running this module. An installation may own a second manifest at
``<instance>/skills/manifest.toml`` naming skills that belong to this host alone. A skill is
always read from the tree beside the manifest that declared it, so the two never have to agree
about where sources live.

A skill may also ship one command: an executable ``<skill>.sh`` beside its ``SKILL.md``, linked
into the operator's ``bin`` directory under the skill's own name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secretary.onboarding import DEFAULT_INSTANCE
from triggered_agents.runtime.paths import configured_product_root

ROOT = Path(__file__).resolve().parents[2]
# The manifest of the checkout this module was imported from. It is what the product ships and what
# tests about the shipped canon read, and it is deliberately *not* the fallback any caller lands on:
# the registry a host is audited or synced against belongs to the configured product checkout, which
# is a different path exactly when an alternate checkout is running the command.
MANIFEST = ROOT / "skills" / "manifest.toml"
# Where an installation keeps its own manifest, relative to the instance directory.
INSTANCE_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Where a product checkout keeps its manifest, relative to the checkout root. The same spelling as
# the instance overlay by coincidence, not by contract: the two are named separately so one can
# move without dragging the other.
PRODUCT_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Points the registry at another product manifest, and with it at another `roles/` tree beside that
# file. The delivery check below runs inside the dispatcher tick, so a test needs a registry it can
# own without writing into the shells of the live installation.
MANIFEST_ENV = "SECRETARY_ROLE_SKILLS_MANIFEST"
INSTANCE_ENV = "SECRETARY_INSTANCE"
# Where a skill's command lands. The default is the operator's own bin directory; a test needs a
# directory it can own without writing into the PATH of the live installation.
BIN_DIR_ENV = "SECRETARY_BIN_DIR"

PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"

# A role and a skill are directory names, and both halves of the registry join them onto a root:
# onto a `roles/` tree to read a skill and onto a shell root to write one. Anything with a
# separator or a parent reference in it would move that join somewhere the operator did not name,
# so a name is one plain path component or the manifest is refused.
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class RegistryError(ValueError):
    """A manifest that cannot be read or does not have the shape a registry needs."""


def _absolute(value: Path | str) -> Path:
    """A path that means the same thing from anywhere.

    A symlink resolves its own text against the directory it sits in, not against the working
    directory of whoever ran the sync, so a relative ``--instance`` would materialize a command
    pointing at a path below ``bin``. Symlinks in the path itself are left alone.
    """
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def manifest_path(product_manifest: Path | str | None = None) -> Path:
    """The product manifest: the one the caller named, the one under test, or the configured one.

    A named manifest wins over the environment because the caller that names one is installing a
    particular checkout. With neither, the answer is the configured product checkout —
    ``TA_SECRETARY_REPO``, else ``~/secretary`` — and never the checkout containing this file, since a
    candidate checkout is a normal place to run ``role-skills`` from.
    """
    if product_manifest is not None:
        return _absolute(product_manifest)
    raw = os.environ.get(MANIFEST_ENV)
    if raw:
        return _absolute(raw)
    return product_manifest_path(configured_product_root())


def product_manifest_path(product_root: Path | str) -> Path:
    """The manifest of a named product checkout, which is not always the running one."""
    return _absolute(product_root) / PRODUCT_MANIFEST_RELATIVE


def instance_dir(value: Path | str) -> Path:
    """The private repo root. An instance is named by its directory or by its instance.yaml."""
    path = _absolute(value)
    return path.parent if path.suffix in (".yaml", ".yml") else path


def configured_instance_path() -> Path:
    return _absolute(os.environ.get(INSTANCE_ENV) or DEFAULT_INSTANCE)


def _expand_home(value: Path | str, home: Path | str | None) -> Path:
    """A manifest path with ``~`` read as the installation's home, not the caller's.

    Which home that is belongs to the installation being materialized: an upgrade repairing another
    account's installation would otherwise deliver every skill under the invoking process's home
    while the units it renders name the owner's. With no home named, the process's own is right.
    """
    if home is None:
        return Path(os.path.expanduser(str(value)))
    text = str(value)
    if text == "~":
        return Path(home)
    if text.startswith("~/"):
        return Path(home) / text[2:]
    # ``~other`` names a different account outright; the caller does not get to redirect that.
    return Path(os.path.expanduser(text))


def bin_dir(home: Path | str | None = None) -> Path:
    """The directory on ``PATH`` that a skill's command is linked into."""
    raw = os.environ.get(BIN_DIR_ENV)
    if raw:
        return _absolute(raw)
    return _absolute(Path(home) / "bin") if home is not None else Path.home() / "bin"


def instance_manifest_path(instance_path: Path | str | None = None) -> Path:
    """Where this installation's own manifest would be, whether or not it exists."""
    base = instance_path if instance_path is not None else configured_instance_path()
    return instance_dir(base) / INSTANCE_MANIFEST_RELATIVE


@dataclass(frozen=True)
class ManifestSource:
    """One manifest file and the ``roles/`` tree that belongs to it."""

    origin: str
    path: Path

    @property
    def roles_root(self) -> Path:
        return self.path.parent / "roles"


@dataclass(frozen=True)
class ExpectedSkill:
    target: str
    shell: str
    role: str
    skill: str
    source: Path
    dest: Path
    origin: str = PRODUCT_ORIGIN
    manifest: Path = MANIFEST


@dataclass(frozen=True)
class ExpectedCommand:
    """The one executable a skill ships, and the link that makes it runnable by name."""

    name: str
    role: str
    skill: str
    source: Path
    dest: Path
    origin: str
    manifest: Path


@dataclass(frozen=True)
class SkillRegistry:
    """The product manifest with an optional instance manifest layered over it."""

    sources: tuple[ManifestSource, ...]
    # role -> ordered list of (skill name, the source that declared it)
    roles: dict[str, list[tuple[str, ManifestSource]]] = field(default_factory=dict)
    # target name -> (target table, the source that declared it)
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = field(default_factory=dict)

    def describe_sources(self) -> list[dict[str, str]]:
        return [{"origin": source.origin, "path": str(source.path)} for source in self.sources]

    @property
    def owned_roots(self) -> tuple[Path, ...]:
        """Every ``roles/`` tree this registry reads skills from."""
        return tuple(source.roles_root for source in self.sources)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """One manifest file, parsed. Defaults to the product manifest."""
    target = path or manifest_path()
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{target}: not valid TOML: {exc}") from None
    except (OSError, UnicodeError) as exc:
        raise RegistryError(f"{target}: could not be read: {exc}") from None


def manifest_sources(
    instance_path: Path | str | None = None,
    *,
    product_manifest: Path | str | None = None,
) -> list[ManifestSource]:
    """The manifests that make up this installation's registry, product first.

    A missing instance manifest is not an error: a portable installation has nothing to layer.
    Something at that path that is not a readable file is the opposite case and refuses.
    """
    sources = [ManifestSource(PRODUCT_ORIGIN, manifest_path(product_manifest))]
    overlay = instance_manifest_path(instance_path)
    if overlay.is_file():
        sources.append(ManifestSource(INSTANCE_ORIGIN, overlay))
    elif overlay.is_symlink() or overlay.exists():
        raise RegistryError(f"{overlay}: exists but is not a readable manifest file")
    return sources


def _table(data: dict[str, Any], key: str, source: ManifestSource) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError(f"{source.path}: [{key}] must be a table")
    return value


def _string(table: dict[str, Any], key: str, where: str, source: ManifestSource) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{source.path}: {where}.{key} must be a non-empty string")
    return value


def _string_list(table: dict[str, Any], key: str, where: str, source: ManifestSource) -> list[str]:
    value = table.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise RegistryError(f"{source.path}: {where}.{key} must be a list of non-empty strings")
    return value


def _identifier(value: str, where: str, source: ManifestSource) -> str:
    """One path component, or a refusal naming the manifest that has to be edited."""
    if not IDENTIFIER.fullmatch(value):
        raise RegistryError(
            f"{source.path}: {where} must be a single directory name matching "
            f"{IDENTIFIER.pattern}, got {value!r}"
        )
    return value


def _within(root: Path, path: Path) -> bool:
    root_abs = _absolute(root)
    path_abs = _absolute(path)
    return path_abs == root_abs or root_abs in path_abs.parents


def load_registry(
    instance_path: Path | str | None = None,
    *,
    product_manifest: Path | str | None = None,
) -> SkillRegistry:
    """Read every manifest and layer them in order.

    Roles accumulate: an instance adds skills to a product role rather than replacing the role. A
    target is replaced whole by a later manifest, because a target is one shell root.
    """
    sources = manifest_sources(instance_path, product_manifest=product_manifest)
    roles: dict[str, list[tuple[str, ManifestSource]]] = {}
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = {}
    for source in sources:
        data = load_manifest(source.path)
        if not isinstance(data, dict):
            raise RegistryError(f"{source.path}: manifest must be a table")
        for role_name, role in _table(data, "roles", source).items():
            if not isinstance(role, dict):
                raise RegistryError(f"{source.path}: [roles.{role_name}] must be a table")
            _identifier(role_name, f"[roles.{role_name}] role name", source)
            declared = roles.setdefault(role_name, [])
            seen = {skill for skill, _ in declared}
            for skill in _string_list(role, "skills", f"roles.{role_name}", source):
                _identifier(skill, f"roles.{role_name}.skills entry", source)
                if skill not in seen:
                    declared.append((skill, source))
                    seen.add(skill)
        for target_name, target in _table(data, "targets", source).items():
            if not isinstance(target, dict):
                raise RegistryError(f"{source.path}: [targets.{target_name}] must be a table")
            where = f"targets.{target_name}"
            _string(target, "shell", where, source)
            _string(target, "root", where, source)
            _string_list(target, "roles", where, source)
            targets[target_name] = (target, source)

    for target_name, (target, source) in sorted(targets.items()):
        for role_name in target["roles"]:
            if role_name not in roles:
                raise RegistryError(
                    f"{source.path}: targets.{target_name}.roles names the unknown role {role_name!r}"
                )
    return SkillRegistry(sources=tuple(sources), roles=roles, targets=targets)


def find_overlapping_target_roots(
    registry: SkillRegistry, home: Path | str | None = None
) -> list[dict[str, str]]:
    """Reject nested roots for one shell: recursive discovery mixes their namespaces."""
    errors: list[dict[str, str]] = []
    items = [
        (name, target["shell"], _expand_home(target["root"], home).resolve())
        for name, (target, _) in sorted(registry.targets.items())
    ]
    for index, (left_name, left_shell, left_root) in enumerate(items):
        for right_name, right_shell, right_root in items[index + 1 :]:
            if left_shell != right_shell or left_root == right_root:
                continue
            if left_root in right_root.parents or right_root in left_root.parents:
                errors.append(
                    {
                        "shell": left_shell,
                        "left_target": left_name,
                        "left_root": str(left_root),
                        "right_target": right_name,
                        "right_root": str(right_root),
                    }
                )
    return errors


def iter_expected(registry: SkillRegistry, home: Path | str | None = None) -> list[ExpectedSkill]:
    """Every skill the registry expects in a shell, with both ends of the copy checked."""
    expected: list[ExpectedSkill] = []
    for target_name, (target, source_of_target) in sorted(registry.targets.items()):
        root = _expand_home(target["root"], home)
        for role_name in target["roles"]:
            for skill, source in registry.roles.get(role_name, []):
                item = ExpectedSkill(
                    target=target_name,
                    shell=target["shell"],
                    role=role_name,
                    skill=skill,
                    source=source.roles_root / role_name / skill,
                    dest=root / skill,
                    origin=source.origin,
                    manifest=source.path,
                )
                if not _within(source.roles_root, item.source):
                    raise RegistryError(
                        f"{source.path}: {role_name}/{skill} resolves to {item.source}, outside "
                        f"{source.roles_root}"
                    )
                if not _within(root, item.dest):
                    raise RegistryError(
                        f"{source_of_target.path}: targets.{target_name} would write "
                        f"{item.dest}, outside {root}"
                    )
                expected.append(item)
    return expected


def find_conflicting_destinations(expected: list[ExpectedSkill]) -> list[dict[str, str]]:
    """Two different sources copied into one directory: the second silently buries the first.

    Skill directories are flat under a shell root, so a product skill and an installation skill of
    the same name land on the same path. The pair is reported instead of resolved.
    """
    first_by_dest: dict[Path, ExpectedSkill] = {}
    conflicts: list[dict[str, str]] = []
    for item in expected:
        first = first_by_dest.setdefault(item.dest, item)
        if first is item or first.source == item.source:
            continue
        conflicts.append(
            {
                "dest": str(item.dest),
                "shell": item.shell,
                "left_target": first.target,
                "left_skill": f"{first.role}/{first.skill}",
                "left_source": str(first.source),
                "left_manifest": str(first.manifest),
                "right_target": item.target,
                "right_skill": f"{item.role}/{item.skill}",
                "right_source": str(item.source),
                "right_manifest": str(item.manifest),
            }
        )
    return conflicts


def command_script(role: str, skill: str, source: ManifestSource) -> Path | None:
    """The one command a skill may ship: ``<skill>.sh`` beside its ``SKILL.md``.

    Discovered from the tree rather than declared in the manifest, so a skill carries its entry point
    with it when it moves out of the product and into an installation.
    """
    script = source.roles_root / role / skill / f"{skill}.sh"
    return script if script.is_file() else None


def iter_expected_commands(registry: SkillRegistry, home: Path | str | None = None) -> list[ExpectedCommand]:
    """Every command the registry ships, keyed by the name the operator types.

    A command belongs to the skill, not to a shell: however many targets a skill is copied into, the
    link points at the canonical script. Two skills of the same name would want the same link, and
    that is refused here, before an audit reads the filesystem or a sync writes to it.
    """
    root = bin_dir(home)
    by_name: dict[str, ExpectedCommand] = {}
    for role in sorted(registry.roles):
        for skill, source in registry.roles[role]:
            script = command_script(role, skill, source)
            if script is None:
                continue
            command = ExpectedCommand(
                name=skill,
                role=role,
                skill=skill,
                source=script,
                dest=root / skill,
                origin=source.origin,
                manifest=source.path,
            )
            clash = by_name.get(skill)
            if clash is not None:
                raise RegistryError(
                    f"two skills ship the command {command.dest}: {clash.role}/{clash.skill} "
                    f"from {clash.manifest} and {command.role}/{command.skill} from "
                    f"{command.manifest}"
                )
            by_name[skill] = command
    return [by_name[name] for name in sorted(by_name)]


def _link_target(link: Path) -> Path:
    """What a symlink points at, as a path that means the same from anywhere."""
    raw = Path(os.readlink(link))
    return raw if raw.is_absolute() else _absolute(link.parent / raw)


def _entry_point_is_owned(link_target: Path, owned_roots: tuple[Path, ...]) -> bool:
    """Whether a link we did not write is one of ours to repoint, or somebody else's.

    Ownership is a location this registry reads skills from, not a path that looks like one. That the
    target no longer exists says nothing either way, so the tree decides and existence is not
    consulted. Everything else stays somebody else's, however much its path resembles a skill source.
    """
    return any(link_target == root or root in link_target.parents for root in owned_roots)


def _entry_point_state(command: ExpectedCommand, owned_roots: tuple[Path, ...]) -> dict[str, str]:
    """What sync would do with one entry point, decided before anything is written."""
    base = {
        "command": command.name,
        "role": command.role,
        "skill": command.skill,
        "source": str(command.source),
        "dest": str(command.dest),
        "origin": command.origin,
        "manifest": str(command.manifest),
    }
    if command.dest.is_symlink():
        link_target = _link_target(command.dest)
        if link_target == command.source:
            return base | {"status": "ok", "reason": ""}
        if _entry_point_is_owned(link_target, owned_roots):
            return base | {
                "status": "stale",
                "reason": f"points at {link_target}, which is no longer where the skill lives",
            }
        return base | {
            "status": "conflict",
            "reason": f"{command.dest} is a link to {link_target}, which this registry does not own",
        }
    if command.dest.exists():
        return base | {
            "status": "conflict",
            "reason": f"{command.dest} is an existing file",
        }
    return base | {"status": "missing", "reason": f"{command.dest} does not exist"}


def _write_entry_point(command: ExpectedCommand, state: dict[str, str]) -> None:
    """Point the link at the source and make what it points at executable.

    An entry point that is already right is left exactly as it is. A symlink has no mode of its own,
    so the exec bit has to be on the script itself.
    """
    if state["status"] != "ok":
        command.dest.parent.mkdir(parents=True, exist_ok=True)
        if state["status"] == "stale":
            command.dest.unlink()
        command.dest.symlink_to(command.source)
    mode = command.source.stat().st_mode
    wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if mode != wanted:
        command.source.chmod(wanted)


def _named_manifests(
    instance_path: Path | str | None,
    product_manifest: Path | str | None = None,
) -> str:
    """Every manifest a refusal was decided from — an overlay can be the file at fault."""
    product = manifest_path(product_manifest)
    overlay = instance_manifest_path(instance_path)
    return f"{product} + {overlay}" if overlay.is_file() else str(product)


def skill_delivery(
    role: str,
    skill: str,
    shell: str,
    *,
    instance_path: Path | str | None = None,
) -> dict[str, Any]:
    """Whether one role skill is materialized in one shell, and where it is expected.

    A head is launched into a shell, not into this repository. Both `delivered` and `reason` are
    filled for a manifest that cannot be read at all, because an unreadable registry is not evidence
    that the skill is there.
    """
    result: dict[str, Any] = {
        "role": role,
        "skill": skill,
        "shell": shell,
        "manifest": str(manifest_path()),
        "manifests": [],
        "delivered": False,
        "paths": [],
        "reason": "",
    }
    try:
        registry = load_registry(instance_path)
        result["manifests"] = registry.describe_sources()
        expected = [
            item
            for item in iter_expected(registry)
            if item.role == role and item.skill == skill and item.shell == shell
        ]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = f"skill registry {_named_manifests(instance_path)} could not be read: {exc}"
        return result
    if not expected:
        result["reason"] = f"no {shell} target in {_named_manifests(instance_path)} carries the {role} role"
        return result
    result["paths"] = [str(item.dest / "SKILL.md") for item in expected]
    missing = [path for path in result["paths"] if not Path(path).is_file()]
    if missing:
        result["reason"] = (
            f"{role}/{skill} is not in the {shell} skill directory ({', '.join(missing)}); "
            "run `secretary role-skills sync`"
        )
        return result
    result["delivered"] = True
    return result


def audit(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
    product_manifest: Path | str | None = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    registry = load_registry(instance_path, product_manifest=product_manifest)
    config_errors = find_overlapping_target_roots(registry, home)
    expected = iter_expected(registry, home)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    destination_conflicts = find_conflicting_destinations(expected)

    missing: list[dict[str, str]] = []
    drift: list[dict[str, str]] = []
    source_missing: list[dict[str, str]] = []

    for item in expected:
        source_skill = item.source / "SKILL.md"
        dest_skill = item.dest / "SKILL.md"
        base = {
            "target": item.target,
            "shell": item.shell,
            "role": item.role,
            "skill": item.skill,
            "source": str(item.source),
            "dest": str(item.dest),
            "origin": item.origin,
            "manifest": str(item.manifest),
        }
        if not source_skill.is_file():
            source_missing.append(base)
            continue
        if not dest_skill.is_file():
            missing.append(base)
            continue
        source_hash = _sha256(source_skill)
        dest_hash = _sha256(dest_skill)
        if source_hash != dest_hash:
            drift.append(base | {"source_hash": source_hash, "dest_hash": dest_hash})

    by_target: dict[str, dict[str, int | str]] = {}
    for item in expected:
        target = by_target.setdefault(
            item.target,
            {"shell": item.shell, "expected": 0, "missing": 0, "drift": 0, "source_missing": 0},
        )
        target["expected"] = int(target["expected"]) + 1
    for bucket_name, bucket in (("missing", missing), ("drift", drift), ("source_missing", source_missing)):
        for item in bucket:
            by_target[item["target"]][bucket_name] = int(by_target[item["target"]][bucket_name]) + 1

    # A filtered audit is about named shell targets; commands do not belong to one.
    entry_points = (
        []
        if target_filter
        else [
            _entry_point_state(command, registry.owned_roots)
            for command in iter_expected_commands(registry, home)
        ]
    )
    entry_point_problems = [item for item in entry_points if item["status"] != "ok"]

    ok = not missing and not drift and not source_missing and not config_errors
    ok = ok and not entry_point_problems and not destination_conflicts
    return {
        "ok": ok,
        "manifest": str(manifest_path(product_manifest)),
        "manifests": registry.describe_sources(),
        "targets": by_target,
        "missing": missing,
        "drift": drift,
        "source_missing": source_missing,
        "entry_points": entry_point_problems,
        "config_errors": config_errors,
        "destination_conflicts": destination_conflicts,
    }


def unmaterializable(
    registry: SkillRegistry,
    home: Path | str | None = None,
    target_filter: set[str] | None = None,
) -> list[str]:
    """Every reason this registry cannot be delivered as written, decided without writing.

    Nothing here reads a destination's contents, so the answer is the same before and after a sync.
    `sync` asks the same question, so the two cannot come to different conclusions.
    """
    problems = [
        f"overlapping skill target roots: {error}" for error in find_overlapping_target_roots(registry, home)
    ]
    expected = iter_expected(registry, home)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    problems += [
        f"two skills claim the skill directory {conflict['dest']}: "
        f"{conflict['left_skill']} from {conflict['left_manifest']} and "
        f"{conflict['right_skill']} from {conflict['right_manifest']}"
        for conflict in find_conflicting_destinations(expected)
    ]
    problems += [
        f"missing canonical skill: {item.source}/SKILL.md (declared by {item.manifest})"
        for item in expected
        if not (item.source / "SKILL.md").is_file()
    ]
    commands = [] if target_filter else iter_expected_commands(registry, home)
    problems += [
        f"command entry point {state['command']} from {state['manifest']} "
        f"cannot be materialized: {state['reason']}"
        for state in (_entry_point_state(command, registry.owned_roots) for command in commands)
        if state["status"] == "conflict"
    ]
    return problems


def sync(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
    product_manifest: Path | str | None = None,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Deliver every expected skill and entry point, or refuse before writing anything.

    A registry that is half applied is worse than one that was not applied at all, because the next
    audit cannot tell the two apart.
    """
    registry = load_registry(instance_path, product_manifest=product_manifest)
    problems = unmaterializable(registry, home, target_filter)
    if problems:
        raise RegistryError(problems[0])
    expected = iter_expected(registry, home)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]
    commands = [] if target_filter else iter_expected_commands(registry, home)
    states = [_entry_point_state(command, registry.owned_roots) for command in commands]

    copied: list[dict[str, str]] = []
    for item in expected:
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(item.source, item.dest, dirs_exist_ok=True)
        copied.append(
            {
                "target": item.target,
                "shell": item.shell,
                "role": item.role,
                "skill": item.skill,
                "dest": str(item.dest),
                "origin": item.origin,
            }
        )

    linked: list[dict[str, str]] = []
    for command, state in zip(commands, states):
        _write_entry_point(command, state)
        linked.append(
            {
                "command": command.name,
                "role": command.role,
                "skill": command.skill,
                "dest": str(command.dest),
                "source": str(command.source),
                "origin": command.origin,
                "manifest": str(command.manifest),
                "was": state["status"],
            }
        )
    return {
        "ok": True,
        "copied": copied,
        "linked": linked,
        "after": audit(
            target_filter,
            instance_path=instance_path,
            product_manifest=product_manifest,
            home=home,
        ),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"role skills: {'ok' if result['ok'] else 'drift'}", ""]
    if result.get("manifests"):
        for source in result["manifests"]:
            lines.append(f"- manifest ({source['origin']}): {source['path']}")
        lines.append("")
    for target, stats in sorted(result["targets"].items()):
        lines.append(
            f"- {target} ({stats['shell']}): expected={stats['expected']}, "
            f"missing={stats['missing']}, drift={stats['drift']}, source_missing={stats['source_missing']}"
        )
    for title, key in (("Missing", "missing"), ("Drift", "drift"), ("Source missing", "source_missing")):
        if result[key]:
            lines.extend(["", f"{title}:"])
            for item in result[key]:
                # The operator reading this has two manifests open; the finding has to say which
                # one to edit, not only which layer it came from.
                lines.append(
                    f"- {item['target']} {item['role']}/{item['skill']} "
                    f"[{item.get('origin', PRODUCT_ORIGIN)} {item.get('manifest', manifest_path())}]"
                    f" -> {item['dest']}"
                )
    if result.get("entry_points"):
        lines.extend(["", "Command entry points:"])
        for item in result["entry_points"]:
            lines.append(
                f"- {item['command']} [{item['origin']} {item['manifest']}] "
                f"{item['status']}: {item['reason']}"
            )
    if result.get("destination_conflicts"):
        lines.extend(["", "Destination conflicts:"])
        for item in result["destination_conflicts"]:
            lines.append(
                f"- {item['dest']}: {item['left_skill']} from {item['left_manifest']} "
                f"and {item['right_skill']} from {item['right_manifest']}"
            )
    if result["config_errors"]:
        lines.extend(["", "Configuration errors:"])
        for item in result["config_errors"]:
            lines.append(
                f"- {item['shell']}: nested target roots {item['left_target']}={item['left_root']} "
                f"and {item['right_target']}={item['right_root']}"
            )
    return "\n".join(lines)


def parse_targets(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {part.strip() for part in value.split(",") if part.strip()}


def run_role_skills(args) -> int:
    targets = parse_targets(args.targets)
    instance = getattr(args, "instance", None)
    product_root = getattr(args, "product_root", None)
    product = product_manifest_path(product_root) if product_root else None
    if args.role_skills_command == "audit":
        try:
            result = audit(targets, instance_path=instance, product_manifest=product)
        except (OSError, ValueError) as exc:
            print(f"secretary role-skills audit: {exc}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
        return 1 if args.check and not result["ok"] else 0
    try:
        result = sync(targets, instance_path=instance, product_manifest=product)
    except (OSError, ValueError) as exc:
        print(f"secretary role-skills sync: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result["after"]))
    return 0


def _add_common_arguments(parser, name: str) -> None:
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--targets", help="comma-separated target names from skills/manifest.toml")
    parser.add_argument(
        "--instance",
        default=os.environ.get(INSTANCE_ENV, DEFAULT_INSTANCE),
        help="instance dir or instance.yaml whose skills/manifest.toml is layered over the product "
        f"manifest (default: {INSTANCE_ENV} or {DEFAULT_INSTANCE})",
    )
    parser.add_argument(
        "--product-root",
        help="product checkout whose skills/manifest.toml is the product layer "
        f"(default: {MANIFEST_ENV}, else the configured product checkout)",
    )
    if name == "audit":
        parser.add_argument("--check", action="store_true", help="exit 1 when missing or drift exists")


def add_role_skills_subcommands(subparsers) -> None:
    """Expose the audit as a health interface and the sync as a materializer."""
    command = subparsers.add_parser(
        "role-skills", help="audit or sync role-owned skills into shell skill directories"
    )
    commands = command.add_subparsers(dest="role_skills_command", required=True)
    for name in ("audit", "sync"):
        sub = commands.add_parser(name)
        _add_common_arguments(sub, name)
        sub.set_defaults(handler=run_role_skills, check=False)
    command.set_defaults(handler=run_role_skills)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role_skills_command", required=True)
    for name in ("audit", "sync"):
        p = sub.add_parser(name)
        _add_common_arguments(p, name)
    args = parser.parse_args(argv)
    args.check = getattr(args, "check", False)
    return run_role_skills(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
