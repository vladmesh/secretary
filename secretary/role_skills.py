#!/usr/bin/env python3
"""Audit and sync role-owned skills into shell-owned skill directories.

Two registries, layered. The product manifest is the portable one: the roles and skills every
installation gets. An installation may own a second manifest at ``<instance>/skills/manifest.toml``
naming skills that belong to this host alone — a personal browser bridge, an operator-only tool —
and its ``roles/`` tree sits beside it in the private instance repository. A skill is always read
from the tree beside the manifest that declared it, so the two never have to agree about where
sources live, and an installation without an overlay is a complete, supported installation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from triggered_agents.runtime.paths import configured_instance_path, instance_dir


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "skills" / "manifest.toml"
ROLES_ROOT = ROOT / "skills" / "roles"
# Where an instance keeps its own manifest, relative to the instance directory.
INSTANCE_MANIFEST_RELATIVE = Path("skills") / "manifest.toml"
# Points the registry at another product manifest, and with it at another `roles/` tree beside that
# file. The delivery check below runs inside the dispatcher tick, so a test needs a registry it can
# own without writing into the shells of the live installation.
MANIFEST_ENV = "SECRETARY_ROLE_SKILLS_MANIFEST"

PRODUCT_ORIGIN = "product"
INSTANCE_ORIGIN = "instance"


def manifest_path() -> Path:
    raw = os.environ.get(MANIFEST_ENV)
    return Path(raw).expanduser() if raw else MANIFEST


def roles_root() -> Path:
    """The canonical product skill sources, always beside the manifest that names them."""
    return manifest_path().parent / "roles"


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
class SkillRegistry:
    """The product manifest with an optional instance manifest layered over it."""

    sources: tuple[ManifestSource, ...]
    # role -> ordered list of (skill name, the source that declared it)
    roles: dict[str, list[tuple[str, ManifestSource]]] = field(default_factory=dict)
    # target name -> (target table, the source that declared it)
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = field(default_factory=dict)

    def describe_sources(self) -> list[dict[str, str]]:
        return [{"origin": source.origin, "path": str(source.path)} for source in self.sources]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """One manifest file, parsed. Defaults to the product manifest."""
    return tomllib.loads((path or manifest_path()).read_text(encoding="utf-8"))


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


def load_registry(instance_path: Path | str | None = None) -> SkillRegistry:
    """Read every manifest and layer them in order.

    Roles accumulate: an instance adds skills to a product role rather than replacing the role, so
    an upgrade that ships a new product skill still delivers it. A target is replaced whole by a
    later manifest, because a target is one shell root and merging two of them means nothing.
    """
    sources = manifest_sources(instance_path)
    roles: dict[str, list[tuple[str, ManifestSource]]] = {}
    targets: dict[str, tuple[dict[str, Any], ManifestSource]] = {}
    for source in sources:
        data = load_manifest(source.path)
        for role_name, role in (data.get("roles") or {}).items():
            declared = roles.setdefault(role_name, [])
            seen = {skill for skill, _ in declared}
            for skill in role["skills"]:
                if skill not in seen:
                    declared.append((skill, source))
                    seen.add(skill)
        for target_name, target in (data.get("targets") or {}).items():
            targets[target_name] = (target, source)
    return SkillRegistry(sources=tuple(sources), roles=roles, targets=targets)


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

    ok = not missing and not drift and not source_missing and not config_errors
    return {
        "ok": ok,
        "manifest": str(manifest_path()),
        "manifests": registry.describe_sources(),
        "targets": by_target,
        "missing": missing,
        "drift": drift,
        "source_missing": source_missing,
        "config_errors": config_errors,
    }


def sync(
    target_filter: set[str] | None = None,
    *,
    instance_path: Path | str | None = None,
) -> dict[str, Any]:
    registry = load_registry(instance_path)
    config_errors = find_overlapping_target_roots(registry)
    if config_errors:
        raise ValueError(f"overlapping skill target roots: {config_errors}")
    expected = iter_expected(registry)
    if target_filter:
        expected = [item for item in expected if item.target in target_filter]

    copied: list[dict[str, str]] = []
    for item in expected:
        if not (item.source / "SKILL.md").is_file():
            raise FileNotFoundError(f"missing canonical skill: {item.source}/SKILL.md")
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
    return {"ok": True, "copied": copied, "after": audit(target_filter, instance_path=instance_path)}


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"role skills: {'ok' if result['ok'] else 'drift'}", ""]
    for source in result.get("manifests", []):
        lines.append(f"- manifest ({source['origin']}): {source['path']}")
    if result.get("manifests"):
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
                    f"[{item['origin']}] -> {item['dest']}"
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
        result = audit(targets, instance_path=instance)
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
        default=os.environ.get("SECRETARY_INSTANCE"),
        help="instance directory whose skills/manifest.toml is layered over the product manifest "
        "(default: SECRETARY_INSTANCE or ~/secretary-instance)",
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
