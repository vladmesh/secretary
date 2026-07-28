#!/usr/bin/env python3
"""Audit and sync role-owned skills into shell-owned skill directories.

Two registries, layered. The product manifest is the portable one: the roles and skills every
installation gets. An installation may own a second manifest at ``<instance>/skills/manifest.toml``
naming skills that belong to this host alone, and its ``roles/`` tree sits beside it in the private
instance repository. A skill is always read from the tree beside the manifest that declared it, so
the two never have to agree about where sources live, and an installation without an overlay is a
complete, supported installation.

A manifest may also declare command entry points: a skill that ships an executable helper gets a
link in a directory on ``PATH``, so the operator can run what the skill documents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secretary.onboarding import DEFAULT_INSTANCE


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "skills" / "manifest.toml"
ROLES_ROOT = ROOT / "skills" / "roles"
# Where an installation keeps its own manifest, relative to the instance directory.
INSTANCE_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Points the registry at another product manifest, and with it at another `roles/` tree beside that
# file. The delivery check below runs inside the dispatcher tick, so a test needs a registry it can
# own without writing into the shells of the live installation.
MANIFEST_ENV = "SECRETARY_ROLE_SKILLS_MANIFEST"
INSTANCE_ENV = "SECRETARY_INSTANCE"

PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"


class RegistryError(ValueError):
    """A manifest that cannot be read or does not have the shape a registry needs.

    Carries the offending file in its message: the operator has two manifests and the only useful
    error is the one that says which of them to open.
    """


def manifest_path() -> Path:
    raw = os.environ.get(MANIFEST_ENV)
    return Path(raw).expanduser() if raw else MANIFEST


def roles_root() -> Path:
    """The canonical product skill sources, always beside the manifest that names them."""
    return manifest_path().parent / "roles"


def instance_dir(value: Path | str) -> Path:
    """The private repo root. An instance is named by its directory or by its instance.yaml."""
    path = Path(value).expanduser()
    return path.parent if path.suffix in (".yaml", ".yml") else path


def configured_instance_path() -> Path:
    return Path(os.environ.get(INSTANCE_ENV) or DEFAULT_INSTANCE).expanduser()


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
    """One executable entry point a skill ships, and the link that makes it runnable."""

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
    # command name -> (command table, the source that declared it)
    commands: dict[str, tuple[dict[str, Any], ManifestSource]] = field(default_factory=dict)

    def describe_sources(self) -> list[dict[str, str]]:
        return [{"origin": source.origin, "path": str(source.path)} for source in self.sources]

    def declaring_source(self, role: str, skill: str) -> ManifestSource | None:
        for declared, source in self.roles.get(role, []):
            if declared == skill:
                return source
        return None


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


def manifest_sources(instance_path: Path | str | None = None) -> list[ManifestSource]:
    """The manifests that make up this installation's registry, product first.

    A missing instance manifest is not an error and not a warning: a portable installation simply
    has nothing to layer.
    """
    sources = [ManifestSource(PRODUCT_ORIGIN, manifest_path())]
    overlay = instance_manifest_path(instance_path)
    if overlay.is_file():
        sources.append(ManifestSource(INSTANCE_ORIGIN, overlay))
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


def _relative_source(table: dict[str, Any], where: str, source: ManifestSource) -> Path:
    raw = _string(table, "source", where, source)
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise RegistryError(
            f"{source.path}: {where}.source must be a path inside the skill directory, got {raw!r}"
        )
    return path


def load_registry(instance_path: Path | str | None = None) -> SkillRegistry:
    """Read every manifest and layer them in order.

    Roles accumulate: an instance adds skills to a product role rather than replacing the role, so
    an upgrade that ships a new product skill still delivers it. A target is replaced whole by a
    later manifest, because a target is one shell root and merging two of them means nothing.
    """
    sources = manifest_sources(instance_path)
    roles: dict[str, list[tuple[str, ManifestSource]]] = {}
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = {}
    commands: dict[str, tuple[dict[str, Any], ManifestSource]] = {}
    for source in sources:
        data = load_manifest(source.path)
        if not isinstance(data, dict):
            raise RegistryError(f"{source.path}: manifest must be a table")
        for role_name, role in _table(data, "roles", source).items():
            if not isinstance(role, dict):
                raise RegistryError(f"{source.path}: [roles.{role_name}] must be a table")
            declared = roles.setdefault(role_name, [])
            seen = {skill for skill, _ in declared}
            for skill in _string_list(role, "skills", f"roles.{role_name}", source):
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
        for command_name, command in _table(data, "commands", source).items():
            if not isinstance(command, dict):
                raise RegistryError(f"{source.path}: [commands.{command_name}] must be a table")
            where = f"commands.{command_name}"
            _string(command, "role", where, source)
            _string(command, "skill", where, source)
            _string(command, "dest", where, source)
            _relative_source(command, where, source)
            commands[command_name] = (command, source)

    for target_name, (target, source) in sorted(targets.items()):
        for role_name in target["roles"]:
            if role_name not in roles:
                raise RegistryError(
                    f"{source.path}: targets.{target_name}.roles names the unknown role "
                    f"{role_name!r}"
                )
    return SkillRegistry(
        sources=tuple(sources), roles=roles, targets=targets, commands=commands
    )


def find_overlapping_target_roots(registry: SkillRegistry) -> list[dict[str, str]]:
    """Reject nested roots for one shell: recursive discovery mixes their namespaces."""
    errors: list[dict[str, str]] = []
    items = [
        (name, target["shell"], Path(target["root"]).expanduser().resolve())
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


def iter_expected(registry: SkillRegistry) -> list[ExpectedSkill]:
    expected: list[ExpectedSkill] = []
    for target_name, (target, _) in sorted(registry.targets.items()):
        root = Path(target["root"]).expanduser()
        for role_name in target["roles"]:
            for skill, source in registry.roles.get(role_name, []):
                expected.append(
                    ExpectedSkill(
                        target=target_name,
                        shell=target["shell"],
                        role=role_name,
                        skill=skill,
                        source=source.roles_root / role_name / skill,
                        dest=root / skill,
                        origin=source.origin,
                        manifest=source.path,
                    )
                )
    return expected


def iter_expected_commands(registry: SkillRegistry) -> list[ExpectedCommand]:
    """Entry points resolve against the manifest that declared the skill, not the command.

    An installation may put a command on a product skill; the helper it runs still lives with the
    skill, so that is where the link has to point.
    """
    expected: list[ExpectedCommand] = []
    for name, (command, source) in sorted(registry.commands.items()):
        role, skill = command["role"], command["skill"]
        declaring = registry.declaring_source(role, skill)
        if declaring is None:
            raise RegistryError(
                f"{source.path}: commands.{name} names {role}/{skill}, which no manifest declares"
            )
        expected.append(
            ExpectedCommand(
                name=name,
                role=role,
                skill=skill,
                source=declaring.roles_root / role / skill / Path(command["source"]),
                dest=Path(command["dest"]).expanduser(),
                origin=source.origin,
                manifest=source.path,
            )
        )
    return expected


def _entry_point_is_owned(link_target: Path, command: ExpectedCommand) -> bool:
    """Whether a link we did not just write is still one of ours, dangling or not.

    A skill source has a fixed shape below its manifest — ``roles/<role>/<skill>/<file>`` — and the
    move of a skill from the product tree into an installation only changes what is above that.
    A link with that shape is a stale entry point to repoint; anything else is somebody else's file.
    """
    tail = (command.role, command.skill, *Path(command.source.name).parts)
    parts = link_target.parts
    if len(parts) < len(tail) + 1:
        return False
    return tuple(parts[-len(tail) :]) == tail and parts[-len(tail) - 1] == "roles"


def _entry_point_state(command: ExpectedCommand) -> dict[str, str]:
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
    if not command.source.is_file():
        return base | {
            "status": "source_missing",
            "reason": f"{command.source} does not exist",
        }
    if command.dest.is_symlink():
        link_target = Path(os.readlink(command.dest))
        if link_target == command.source:
            return base | {"status": "ok", "reason": ""}
        if _entry_point_is_owned(link_target, command):
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

    A symlink has no mode of its own, so the exec bit has to be on the helper itself or the
    materialized command is not a command.
    """
    command.dest.parent.mkdir(parents=True, exist_ok=True)
    if state["status"] in ("stale", "ok"):
        command.dest.unlink()
    command.dest.symlink_to(command.source)
    mode = command.source.stat().st_mode
    wanted = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if mode != wanted:
        command.source.chmod(wanted)


def _named_manifests(instance_path: Path | str | None) -> str:
    """Every manifest a refusal was decided from — an overlay can be the file at fault."""
    product = manifest_path()
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

    A head is launched into a shell, not into this repository: the canonical skill being present
    here says nothing about the head being able to open it. `delivered` is what a caller acts on,
    `reason` is what it shows when it refuses; both are filled for a manifest that cannot be read
    at all, because an unreadable registry is not evidence that the skill is there.
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
        result["reason"] = (
            f"skill registry {_named_manifests(instance_path)} could not be read: {exc}"
        )
        return result
    if not expected:
        result["reason"] = (
            f"no {shell} target in {_named_manifests(instance_path)} carries the {role} role"
        )
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
) -> dict[str, Any]:
    registry = load_registry(instance_path)
    config_errors = find_overlapping_target_roots(registry)
    expected = iter_expected(registry)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]

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

    entry_points = [_entry_point_state(command) for command in iter_expected_commands(registry)]
    # A filtered audit is about named shell targets; commands do not belong to one.
    entry_point_problems = [] if target_filter else [
        item for item in entry_points if item["status"] != "ok"
    ]

    ok = not missing and not drift and not source_missing and not config_errors
    ok = ok and not entry_point_problems
    return {
        "ok": ok,
        "manifest": str(manifest_path()),
        "manifests": registry.describe_sources(),
        "targets": by_target,
        "missing": missing,
        "drift": drift,
        "source_missing": source_missing,
        "entry_points": entry_point_problems,
        "config_errors": config_errors,
    }


def sync(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
) -> dict[str, Any]:
    """Deliver every expected skill and entry point, or refuse before writing anything.

    Everything that can be decided from the manifests and the filesystem is decided first: a
    registry that is half applied is worse than one that was not applied at all, because the next
    audit cannot tell the two apart.
    """
    registry = load_registry(instance_path)
    config_errors = find_overlapping_target_roots(registry)
    if config_errors:
        raise RegistryError(f"overlapping skill target roots: {config_errors}")
    expected = iter_expected(registry)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]

    for item in expected:
        if not (item.source / "SKILL.md").is_file():
            raise FileNotFoundError(
                f"missing canonical skill: {item.source}/SKILL.md "
                f"(declared by {item.manifest})"
            )

    commands = [] if target_filter else iter_expected_commands(registry)
    states = [_entry_point_state(command) for command in commands]
    for state in states:
        if state["status"] in ("conflict", "source_missing"):
            raise RegistryError(
                f"command entry point {state['command']} declared by {state['manifest']} "
                f"cannot be materialized: {state['reason']}"
            )

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
                "was": state["status"],
            }
        )
    return {
        "ok": True,
        "copied": copied,
        "linked": linked,
        "after": audit(target_filter, instance_path=instance_path),
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
                lines.append(
                    f"- {item['target']} {item['role']}/{item['skill']} "
                    f"[{item.get('origin', PRODUCT_ORIGIN)}] -> {item['dest']}"
                )
    if result.get("entry_points"):
        lines.extend(["", "Command entry points:"])
        for item in result["entry_points"]:
            lines.append(
                f"- {item['command']} [{item['origin']}] {item['status']}: {item['reason']}"
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
    if args.role_skills_command == "audit":
        try:
            result = audit(targets, instance_path=instance)
        except (OSError, ValueError) as exc:
            print(f"secretary role-skills audit: {exc}")
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
        return 1 if args.check and not result["ok"] else 0
    try:
        result = sync(targets, instance_path=instance)
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
