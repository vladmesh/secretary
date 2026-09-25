from __future__ import annotations

from secretary.host_apply import HostCommandError, UnitProcessIdentity


class FakeUnitInstaller:
    """A systemd double that records the calls a reconcile makes."""

    _next_pid = 10_000

    def __init__(self, present: dict[str, bytes] | None = None, active: set[str] | None = None) -> None:
        self.files = dict(present or {})
        self.active = set(active or set())
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()
        self.identities = {name: self._new_identity() for name in self.active if name.endswith(".service")}

    @classmethod
    def _new_identity(cls) -> UnitProcessIdentity:
        cls._next_pid += 1
        return UnitProcessIdentity(cls._next_pid, cls._next_pid * 10, f"fake-{cls._next_pid:032x}")

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
        if name.endswith(".service"):
            self.identities[name] = self._new_identity()

    def disable(self, name: str) -> None:
        self.calls.append(("disable", name))
        self.active.discard(name)
        self.identities.pop(name, None)

    def restart(self, name: str) -> None:
        # `systemctl restart` starts a stopped unit too, with a new main process.
        self.calls.append(("restart", name))
        self.active.add(name)
        if name.endswith(".service"):
            self.identities[name] = self._new_identity()

    def is_active(self, name: str) -> bool:
        return name in self.active

    def process_identity(self, name: str) -> UnitProcessIdentity | None:
        return self.identities.get(name) if name in self.active else None
