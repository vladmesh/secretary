"""The document a head is given, and the one short line that points it there.

An interactive head's input channel is a keyboard, and everything the product has lost on it was
large: a ~12 KiB reviewer prompt pasted into a Codex composer that kept the text and consumed the
Enter. Short lines never failed. So the input channel carries no content at all: the task lives in
a file, and the pane receives a bounded line naming that file's absolute path.

Two properties make that reliable by construction, and both are owned here:

  * **the nudge is bounded and single-line whatever the document says.** Prompt text cannot reach
    the terminal through it, because the only thing derived from the document is its path;
  * **the document lives outside every git worktree.** A workspace's identity is its tracked diff
    plus its untracked files, and receipts hash exactly that. A prompt written into the checkout
    would move that identity, so a caller names the worktree the document describes and a document
    inside it is refused rather than quietly written.

The document is durable on purpose: it outlives the head as the run's own record of what that head
was asked to do, which is why it is written where run artifacts are kept.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

# A ceiling in bytes, because bytes are what the terminal receives. 256 is far below any length
# that has ever been mishandled and far above any path this product produces, so a nudge that does
# not fit is a caller with a pathological path rather than a limit worth tuning.
NUDGE_MAX_BYTES = 256
# What the delivery evidence calls this mode. Telemetry records the mode, the nudge's size and the
# document's path; the document's text is not delivery telemetry and is never in it.
NUDGE_FILE_MODE = "nudge-file"
_NUDGE_TEMPLATE = "Read the file {path} and carry out the task written there."
_DOCUMENT_MODE = 0o600
_DOCUMENT_DIR_MODE = 0o700


class PromptDocumentError(RuntimeError):
    """A document or a nudge that would not hold the guarantees above; the message says which."""


def nudge_for(path: str | Path, note: str = "") -> str:
    """The one line a pane receives for a head that has a document waiting.

    Absolute, because the head's own working directory is not something the sender knows. ASCII,
    because the encoding a terminal will apply to the line is not something delivery can prove.
    Control bytes are refused outright: a single newline in a path would turn one nudge into two
    lines and a stray Enter.

    `note` is for the caller whose pointer has to discriminate as well as point. It travels through
    here so there is still exactly one place where the four guarantees are made, and so the ceiling
    is checked over the line as it will actually be delivered. A line that does not fit is refused
    whole: a truncated note would be a discriminator silently cut to length.
    """
    location = str(path)
    if not os.path.isabs(location):
        raise PromptDocumentError(
            f"a nudge names its document by absolute path, and {location!r} is not one"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in location):
        raise PromptDocumentError("a document path carrying control bytes cannot be nudged at")
    try:
        location.encode("ascii")
    except UnicodeEncodeError:
        raise PromptDocumentError(
            f"a nudge carries an ASCII document path, and {location!r} is not one"
        ) from None
    nudge = _NUDGE_TEMPLATE.format(path=location)
    if note:
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in note):
            raise PromptDocumentError("a nudge note carrying control bytes cannot be delivered")
        nudge = f"{nudge} {note}"
    size = len(nudge.encode("utf-8"))
    if size > NUDGE_MAX_BYTES:
        raise PromptDocumentError(
            f"the nudge for {location} is {size} bytes, over the {NUDGE_MAX_BYTES}-byte ceiling"
        )
    return nudge


def write_prompt_document(
    path: str | Path, text: str, *, outside: str | Path | None = None
) -> Path:
    """Put one head's task where it can read it, and where nothing else has to account for it.

    `outside` is the worktree this document describes; a path inside it is a programming error caught
    here rather than a receipt digest that moved for no reason.

    The write is atomic and the file is private (0600), as is its directory. A retry that asks for a
    document it already wrote keeps the file's content and mtime, so a reader can still take the
    timestamp as "when this head was last given a task". Its mode is still made to hold.
    """
    document = Path(path)
    if not document.is_absolute():
        raise PromptDocumentError(
            f"a prompt document is written by absolute path, and {document} is not one"
        )
    if outside is not None:
        _refuse_inside(document, Path(outside))
    directory = document.parent
    try:
        directory.mkdir(mode=_DOCUMENT_DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise PromptDocumentError(f"prompt document directory {directory} is unusable: {exc}") from None
    if _already_holds(document, text):
        _make_private(document)
        return document
    _replace_atomically(document, text)
    return document


def _make_private(document: Path) -> None:
    """Hold the 0600 promise for a document this call did not write.

    A document that is already correct is never rewritten, and its mode is then whatever left it.
    Fixing the mode rather than rewriting the file keeps the content and the mtime as they were.
    """
    try:
        if stat.S_IMODE(document.stat().st_mode) != _DOCUMENT_MODE:
            os.chmod(document, _DOCUMENT_MODE)
    except OSError as exc:
        raise PromptDocumentError(
            f"prompt document {document} could not be made private: {exc}"
        ) from None


def _encoded(text: str) -> bytes:
    """The document's bytes, or a refusal. Same policy as the transport's own body check: text
    that cannot be encoded is a caller's bug, not something to write in a lossy form and hand to a
    head as its task."""
    try:
        return str(text).encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise PromptDocumentError(f"prompt document text is not encodable as UTF-8: {exc}") from None


def _refuse_inside(document: Path, worktree: Path) -> None:
    """Refuse a document that would land inside the checkout it is about.

    Both sides are resolved through their symlinks first: a workspaces root reached by one name and a
    document written under another would otherwise compare as unrelated paths.
    """
    resolved = Path(os.path.realpath(document))
    tree = Path(os.path.realpath(worktree))
    if resolved == tree or tree in resolved.parents:
        raise PromptDocumentError(
            f"a prompt document may not live inside the worktree it describes: "
            f"{resolved} is under {tree}"
        )


def _already_holds(document: Path, text: str) -> bool:
    try:
        return document.read_bytes() == _encoded(text)
    except OSError:
        return False


def _replace_atomically(document: Path, text: str) -> None:
    """Write the document's bytes, unmodified, and swap it into place in one step.

    Binary mode, so what the head opens is what the caller rendered: a prompt that arrived from the
    board's web form carries CRLF, and text mode would rewrite those line endings on the way in and
    translate them back out again.
    """
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{document.name}.", suffix=".tmp", dir=document.parent
        )
        temp_path = Path(temp_name)
        os.chmod(temp_path, _DOCUMENT_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_encoded(text))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, document)
        temp_path = None
    except OSError as exc:
        raise PromptDocumentError(f"prompt document {document} could not be written: {exc}") from None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
