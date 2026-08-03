"""The sprint binding a test's observer calls run with.

A head is launched for one sprint and carries that reference in its own environment, and every
write of role `observer` is authenticated against it.  A test that calls the writers in process is
that head, so it has to say which sprint it is; without this the writers refuse it the way they
refuse a head nobody bound.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator
from unittest import mock

from secretary.role_env import OBSERVER_GENERATION_ENV, OBSERVER_SPRINT_ENV

GENERATION = "testgeneration"


def observer_env(sprint: str, generation: str = GENERATION) -> dict[str, str]:
    return {OBSERVER_SPRINT_ENV: sprint, OBSERVER_GENERATION_ENV: generation}


@contextmanager
def as_observer(sprint: str, generation: str = GENERATION) -> Iterator[None]:
    """Run the block as the observer head of `sprint`."""
    with mock.patch.dict(os.environ, observer_env(sprint, generation)):
        yield


@contextmanager
def unbound_observer() -> Iterator[None]:
    """Run the block as a head nobody bound to a sprint."""
    with mock.patch.dict(os.environ, {}):
        for name in (OBSERVER_SPRINT_ENV, OBSERVER_GENERATION_ENV):
            os.environ.pop(name, None)
        yield


def bind_observer(test, sprint: str, generation: str = GENERATION) -> None:
    """Bind a whole test case to one sprint, undone when the test ends."""
    patcher = mock.patch.dict(os.environ, observer_env(sprint, generation))
    patcher.start()
    test.addCleanup(patcher.stop)
