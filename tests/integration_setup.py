"""Narrow CI-only signals for required integration-test setup.

These helpers are for a test or fixture boundary that cannot exercise its
declared integration contract.  They are not product assertions and the CI
evidence runner recognizes only ``RequiredIntegrationSetup`` by type.
"""

from __future__ import annotations

from collections.abc import Callable


class RequiredIntegrationSetup(RuntimeError):
    """A required disposable integration dependency is unavailable."""


def require_integration_setup(dependency: object | None, reason: str) -> object:
    """Return a required dependency or give CI a typed setup failure."""
    if dependency is None:
        raise RequiredIntegrationSetup(reason)
    return dependency


def require_disposable_board_fixture(factory: Callable[[], object] | None) -> object:
    """Create the required disposable board fixture without reaching a live board."""
    if factory is None:
        raise RequiredIntegrationSetup("required disposable-board fixture is unavailable")
    try:
        return factory()
    except RequiredIntegrationSetup:
        raise
    except Exception as exc:
        raise RequiredIntegrationSetup("required disposable-board fixture is unavailable") from exc
