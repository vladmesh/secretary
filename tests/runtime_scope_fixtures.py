"""Disposable packaged host state for runtime ownership consumer regressions."""

from pathlib import Path

from ummanu.host import build_plan, load_packaged_units, manifest_text


def host_fixture(root: Path, data: Path, scope: str = ""):
    packaging = root / "packaging" / "systemd"
    packaging.mkdir(parents=True)
    for component in ("example", "memory", "dispatcher-production"):
        (packaging / f"ummanu-{component}.service").write_text(
            "[Unit]\nDescription=Disposable fixture\n[Service]\nType=oneshot\nExecStart=/bin/true\n")
    for component in ("example", "dispatcher-production"):
        (packaging / f"ummanu-{component}.timer").write_text(
            f"[Timer]\nOnCalendar=hourly\nUnit=ummanu-{component}.service\n"
            "[Install]\nWantedBy=timers.target\n")
    instance = {"version": 1, "name": "disposable", "data_dir": str(data),
                "host": {"unit_prefix": "ummanu-"}}
    packaged = load_packaged_units(packaging, "ummanu-")
    desired = build_plan(instance, [], packaged=packaged)
    fixture = root / "host"
    fixture.mkdir()
    (fixture / "units.txt").write_text("\n".join([r.name for r in desired] + ([scope] if scope else [])))
    (fixture / "unit-states.txt").write_text("\n".join(f"{r.name} enabled active" for r in desired))
    (data / "host-managed.json").write_text(manifest_text(desired))
    return instance, packaged, desired, fixture
