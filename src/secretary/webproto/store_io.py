"""The one place `secretary.webproto` puts a file on disk, and the vocabulary it speaks.

`boundary.py` promises that an operation is guarded by the act of being public: there is no list to
keep in step and nothing to remember. That promise is only true while every failure a durable source
of this layer can raise is *in* :data:`~secretary.webproto.boundary.IMPLEMENTATION_FAILURES`, and
one of them was not. `secretary._fsutil.write_text_atomic` converts its `OSError` into a
`RuntimeError`, and `RuntimeError` is deliberately absent from that tuple: it is what a defect of
this layer travels as, and dressing a `TypeError` up as an unavailable backend would hide it from
the reader best placed to fix it.

So a full disk under any store of this layer escaped as a bare `RuntimeError`, past a transport that
catches `ReadError` and past the operation's own `except RunStoreError`. That was true of the sprint
request index added by secretary-1569 and equally true of `RunStore`, which has written its records
this way since secretary-1562; the second one is not a new defect but the same one, copied.

Repairing it at either call site would leave the promise false for the next store that writes a
file, which is exactly the failure mode `boundary.py` exists to prevent: a contract kept one call
at a time is kept nowhere. So the translation lives here, at the one seam where this layer meets the
filesystem, and every write in the package goes through :func:`write_document`.

Two things this module deliberately does not do. It does not change `write_text_atomic`: a dozen
callers outside this layer -- `broad_check`, `head_registry`, `checkpoint`, `knowledge_write`,
`state_repo`, `backup_verify` -- already catch its `RuntimeError` on purpose, and changing their
vocabulary from here would be somebody else's refactor. And it does not add `RuntimeError` to
`IMPLEMENTATION_FAILURES`, which would make every defect of this layer look like an unavailable
source.
"""

from __future__ import annotations

import os
from pathlib import Path

from secretary._fsutil import write_text_atomic


class RunStoreError(RuntimeError):
    """A durable store of this layer could not be read or written. Never a statement about a run.

    Defined here rather than in :mod:`secretary.webproto.runs`, where it was, because it is the
    vocabulary of *this seam*: what a store raises when the filesystem refused it, whichever store
    that is. `runs` re-exports the name, so every caller that already imports it from there --
    including :data:`~secretary.webproto.boundary.IMPLEMENTATION_FAILURES` -- keeps working, and
    there is still exactly one class.
    """


def write_document(path: Path | str, payload: str) -> None:
    """Write one of this layer's durable documents, atomically, or refuse in the layer's own words.

    The whole point is the `except`. `write_text_atomic` publishes through a temporary file in the
    target's own directory and replaces it, which is the durability every record here needs; what it
    does not do is speak this layer's failure vocabulary, and it is not this card's business to make
    it. So its `RuntimeError` -- and any `OSError` that reaches here around it -- becomes
    `RunStoreError`, which the boundary already turns into `backend_unavailable` for a caller that
    never learned either.
    """
    target = Path(os.fspath(path))
    try:
        write_text_atomic(target, payload)
    except RunStoreError:
        raise
    except (RuntimeError, OSError) as exc:
        raise RunStoreError(f"the record at {target} could not be written: {exc}") from None
