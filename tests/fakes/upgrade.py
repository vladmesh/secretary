from __future__ import annotations

from secretary.host_apply import HostCommandError


class FakeUnitInstaller:
    """A systemd double that records the calls a reconcile makes."""

    def __init__(self, present: dict[str, bytes] | None = None, active: set[str] | None = None) -> None:
        self.files = dict(present or {})
        self.active = set(active or set())
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def installed(self, name: str) -> bytes | None:
        return self.files.get(name)

    def install(self, unit) -> None:
        if unit.name in self.fail_on:
            raise HostCommandError(f"install {unit.name}: exited 1")
        self.calls.append(("install", unit.name))
        self.files[unit.name] = unit.content

    def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        self.files.pop(name, None)

    def daemon_reload(self) -> None:
        self.calls.append(("daemon-reload", ""))

    def enable(self, name: str) -> None:
        self.calls.append(("enable", name))
        self.active.add(name)

    def disable(self, name: str) -> None:
        self.calls.append(("disable", name))
        self.active.discard(name)

    def restart(self, name: str) -> None:
        self.calls.append(("restart", name))

    def is_active(self, name: str) -> bool:
        return name in self.active


class FakeRegistrar:
    def __init__(self, user: str | None = None) -> None:
        self.user = user
        self.added: list[tuple[str, str]] = []

    def add(self, name: str, repo: str) -> None:
        self.added.append((name, repo))
