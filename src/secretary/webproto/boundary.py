"""The one place this layer's error contract is kept.

`secretary.webproto` promises its callers one thing about failure: what leaves an operation is a
typed protocol code -- `not_found`, `validation`, `owner_conflict`, `backend_unavailable` -- and
never the implementation vocabulary of whatever durable thing it happened to read. The whole point
of that promise is that a transport can hold *one* table from code to its own words and *one*
containment branch: `secretary.web` maps codes to HTTP statuses in `secretary.web.statuses` and
catches `ReadError` once, `secretary web-run` maps them to exit statuses, and a future Telegram
head will do the same.

Until this module existed the promise was kept a call at a time -- `RunLifecycle` translated
`RunStoreError` around its own `save` and `settle`, and every other path was expected to remember.
`OperationLayer.run_list()` did not: `RunStore.for_ref()` raised `RunStoreError` straight through
it, past a transport that catches only `ReadError`, and a single unreadable run record took a whole
card page down instead of marking one section unavailable. That is not a defect of `run_list`. A
contract enforced once per call site is enforced nowhere: the next path to the store or the journal
forgets in exactly the same way, and it is found in review rather than by a test.

So it is enforced here, structurally. A layer that inherits :class:`ProtocolBoundary` has every
public method it defines wrapped at class-creation time, and a new operation added tomorrow is
guarded by the act of being public -- there is no list to keep in step and nothing to remember.

**What is translated, and what deliberately is not.** :data:`IMPLEMENTATION_FAILURES` is the
vocabulary of this layer's durable sources: the run store's own error, and the operating system's
and JSON's, which every file this layer reads speaks. Reaching a caller, each of those means the
same thing -- a source of this installation could not be read -- and that is exactly
`backend_unavailable`. Nothing else is caught. A `TypeError` or an `AttributeError` out of an
operation is a defect of this layer, and dressing it as an unavailable backend would hide it from
the reader best placed to fix it; it is left to travel as what it is.

`OSError` is in the set on purpose, and it covers one case beyond a file: a head backend that
cannot bring a supervisor up raises it out of `run_start`. That is what `RuntimeUnavailable` was
written for -- "the product runtime could not raise, reach or record a head" -- and a caller seeing
the backend's own exception instead was the same leak as `run_list`'s, from a different source.
Closing the run the failed bring-up opened is unaffected: that happens inside the operation, below
this boundary, and is pinned by
`tests.test_web_run_protocol.StartTests.test_a_bring_up_that_fails_closes_the_run_it_opened`.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Callable, TypeVar

from secretary.webproto.errors import ReadError, RuntimeUnavailable
from secretary.webproto.store_io import RunStoreError

#: The durable sources' own vocabularies. `RunStoreError` is what every store of this layer
#: speaks -- read or written, run store or sprint request index -- because every write goes through
#: :func:`secretary.webproto.store_io.write_document`, which is where the atomic writer's own
#: `RuntimeError` stops; `OSError` and `json.JSONDecodeError` are what any file under `<data>/`
#: speaks when it cannot be read or does not parse. Every one of them, reaching a caller, means "a
#: source refused", never "you asked wrongly", so every one of them becomes `backend_unavailable`.
#:
#: `RuntimeError` is deliberately *not* here, and that is exactly why `store_io` exists: adding it
#: would make a `TypeError`'s bare cousin -- a defect of this layer -- indistinguishable from a full
#: disk, so the disk is translated at the seam instead of the vocabulary being widened here.
IMPLEMENTATION_FAILURES: tuple[type[BaseException], ...] = (RunStoreError, OSError, json.JSONDecodeError)

#: Set on a wrapped operation, so a test can tell a guarded operation from an unguarded one without
#: calling it, and so wrapping twice is a no-op.
GUARDED = "__webproto_guarded__"

_Function = TypeVar("_Function", bound=Callable[..., Any])


def guard(function: _Function) -> _Function:
    """`function`, with this layer's failure contract around it.

    A `ReadError` is already the contract and passes through untouched -- including the code the
    operation chose, which this module never second-guesses. An implementation failure becomes
    `RuntimeUnavailable`, chained to what raised it so a traceback still names the file.
    """
    if getattr(function, GUARDED, False):
        return function

    @functools.wraps(function)
    def operation(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except ReadError:
            raise
        except IMPLEMENTATION_FAILURES as exc:
            raise RuntimeUnavailable(
                f"{function.__name__!r} could not be answered: {exc}"
            ) from exc

    setattr(operation, GUARDED, True)
    return operation  # type: ignore[return-value]


def operations(cls: type) -> tuple[str, ...]:
    """The public operations of a boundary class, in definition order.

    The same predicate the wrapping uses, published so a test can enumerate what it has to cover
    rather than keep its own list of what exists.
    """
    return tuple(
        name
        for name, attribute in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(attribute)
    )


class ProtocolBoundary:
    """A layer whose public methods are protocol operations, guarded by being public.

    Subclassing is the whole mechanism: `__init_subclass__` runs once, at class creation, and
    replaces every public function defined in the body with its guarded form. Private helpers are
    untouched -- they are inside the boundary, and a `RunStoreError` there is a fact the layer may
    still want to catch and act on, as `RunLifecycle` does.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in operations(cls):
            setattr(cls, name, guard(vars(cls)[name]))
