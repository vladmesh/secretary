#!/usr/bin/env python3
"""Audit and sync role-owned skills into shell-owned skill directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "skills" / "manifest.toml"
ROLES_ROOT = ROOT / "skills" / "roles"


@dataclass(frozen=True)
class ExpectedSkill:
    target: str
    shell: str
    role: str
    skill: str
    source: Path
    dest: Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict[str, Any]:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))


def find_overlapping_target_roots(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Reject nested roots for one shell: recursive discovery mixes their namespaces."""
    targets = manifest.get("targets", {})
    errors: list[dict[str, str]] = []
    items = [
        (name, target["shell"], Path(target["root"]).expanduser().resolve())
        for name, target in sorted(targets.items())
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


def iter_expected(manifest: dict[str, Any]) -> list[ExpectedSkill]:
    roles = manifest.get("roles", {})
    targets = manifest.get("targets", {})
    expected: list[ExpectedSkill] = []
    for target_name, target in sorted(targets.items()):
        root = Path(target["root"]).expanduser()
        for role_name in target["roles"]:
            role = roles[role_name]
            for skill in role["skills"]:
                expected.append(
                    ExpectedSkill(
                        target=target_name,
                        shell=target["shell"],
                        role=role_name,
                        skill=skill,
                        source=ROLES_ROOT / role_name / skill,
                        dest=root / skill,
                    )
                )
    return expected


def audit(target_filter: set[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    config_errors = find_overlapping_target_roots(manifest)
    expected = iter_expected(manifest)
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
        "manifest": str(MANIFEST),
        "targets": by_target,
        "missing": missing,
        "drift": drift,
        "source_missing": source_missing,
        "config_errors": config_errors,
    }


def sync(target_filter: set[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest()
    config_errors = find_overlapping_target_roots(manifest)
    if config_errors:
        raise ValueError(f"overlapping skill target roots: {config_errors}")
    expected = iter_expected(manifest)
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
            }
        )
    return {"ok": True, "copied": copied, "after": audit(target_filter)}


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"role skills: {'ok' if result['ok'] else 'drift'}", ""]
    for target, stats in sorted(result["targets"].items()):
        lines.append(
            f"- {target} ({stats['shell']}): expected={stats['expected']}, "
            f"missing={stats['missing']}, drift={stats['drift']}, source_missing={stats['source_missing']}"
        )
    for title, key in (("Missing", "missing"), ("Drift", "drift"), ("Source missing", "source_missing")):
        if result[key]:
            lines.extend(["", f"{title}:"])
            for item in result[key]:
                lines.append(f"- {item['target']} {item['role']}/{item['skill']} -> {item['dest']}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("audit", "sync"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--targets", help="comma-separated target names from skills/manifest.toml")
        if name == "audit":
            p.add_argument("--check", action="store_true", help="exit 1 when missing or drift exists")
    args = parser.parse_args(argv)
    targets = parse_targets(args.targets)

    if args.cmd == "audit":
        result = audit(targets)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
        return 1 if args.check and not result["ok"] else 0

    result = sync(targets)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result["after"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
