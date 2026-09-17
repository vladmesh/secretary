"""Writer for long recoverable documents in `state/knowledge`.

Contract: docs/ARCHITECTURE.md, "Knowledge planes". Knowledge holds the long
reasoning behind a decision: brainstorms, decision logs, incident write-ups.
It is plain tracked markdown, so a document written here rides to the remote
with the rest of the checkpoint and survives a move to another machine.

The writer exists so the role keeping a document does not have to reach for raw
`git`. A bare `git commit` in the instance repo races the tick writer, which
commits `state/board` and `state/runs` in the same repo on its five-minute
periodic cadence. This
writer owns `state/knowledge` alone, takes the same `state_repo_lock` the other
writers take, and never runs `git add -A`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from secretary import state_repo
from secretary._fsutil import write_text_atomic as _write_text_atomic
from secretary.state_repo import KNOWLEDGE_PATHSPEC
from triggered_agents.runtime.redact import redact


class KnowledgeError(RuntimeError):
    """A knowledge write did not happen."""


class KnowledgeValidationError(KnowledgeError):
    """The document or its path is not something this writer accepts.

    `reason` is a short fixed word a caller can branch on (`path`, `source_missing`, `source_empty`,
    `special_file`, `secret`, `size_cap`); `""` where no caller needs one.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


# The whole of one directory write, in bytes. A report directory holds a write-up and the scripts and
# data behind it, not a dataset; anything larger belongs outside the instance repository.
KNOWLEDGE_DIRECTORY_CAP_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class KnowledgeDocument:
    """One knowledge write that has passed every check this writer makes before it writes.

    The product of :func:`check_knowledge_document`, and what :func:`write_knowledge_document`
    writes: the cleaned actor, the path relative to `state/knowledge`, the body, and the instance
    repository the write lands in.
    """

    actor: str
    document: PurePosixPath
    text: str
    instance_dir: Path


@dataclass(frozen=True)
class KnowledgeWriteResult:
    document: str
    path: Path
    commit: str
    actor: str
    changed: bool


def check_knowledge_document(
    instance_dir: Path,
    *,
    document: str,
    actor: str,
    text: str | None = None,
    source_file: Path | None = None,
) -> KnowledgeDocument:
    """Everything :func:`write_knowledge_document` refuses, asked without writing anything.

    The preflight half of the one writer, split out rather than restated: a caller that has to
    know *before* it starts an operation whether the document at the end of it can be written --
    a sprint close, which must refuse before its first board write rather than halfway through --
    asks this, and the rules it is answered by are the write's own.
    """
    actor = _clean_actor(actor)
    relative = _clean_document(document)
    body = _document_text(text=text, source_file=source_file)
    # Knowledge leaves the host with the rest of the checkpoint, and a brainstorm
    # describes infrastructure by its nature, so it passes the same secret gate
    # the tick and memory writers apply.
    if redact(body) != body:
        raise KnowledgeValidationError(f"secret detected in state/knowledge/{relative}")
    return KnowledgeDocument(actor, relative, body, state_repo.require_repo(instance_dir))


def write_knowledge_document(
    instance_dir: Path,
    *,
    document: str,
    actor: str,
    text: str | None = None,
    source_file: Path | None = None,
    message: str | None = None,
) -> KnowledgeWriteResult:
    """Write one document under `state/knowledge` and commit it.

    `changed=False` means the document on disk already had this content; the
    commit is then the current HEAD and nothing was added to the history.
    """
    checked = check_knowledge_document(
        instance_dir, document=document, actor=actor, text=text, source_file=source_file
    )
    actor, relative, body = checked.actor, checked.document, checked.text
    instance_dir = checked.instance_dir
    target = state_repo.knowledge_dir(instance_dir) / relative
    with state_repo.state_repo_lock(instance_dir):
        try:
            _write_text_atomic(target, body)
        except RuntimeError as exc:
            raise KnowledgeError(f"could not write state/knowledge/{relative}: {exc}") from None
        commit = state_repo.commit(
            instance_dir,
            KNOWLEDGE_PATHSPEC,
            message or _commit_message(str(relative), actor),
        )
        if commit is None:
            head = state_repo.head(instance_dir) or ""
            return KnowledgeWriteResult(
                document=str(relative),
                path=target,
                commit=head,
                actor=actor,
                changed=False,
            )
    return KnowledgeWriteResult(
        document=str(relative),
        path=target,
        commit=commit,
        actor=actor,
        changed=True,
    )


@dataclass(frozen=True)
class KnowledgeDirectory:
    """One directory write that has passed every check :func:`write_knowledge_directory` makes.

    `files` holds each regular file of the source, by its path relative to the source, with its bytes.
    """

    actor: str
    directory: PurePosixPath
    files: tuple[tuple[PurePosixPath, bytes], ...]
    instance_dir: Path


def check_knowledge_directory(
    instance_dir: Path,
    *,
    directory: str,
    actor: str,
    source_dir: Path,
) -> KnowledgeDirectory:
    """Everything :func:`write_knowledge_directory` refuses, asked without writing anything.

    The source must be a real, non-empty directory of regular files and subdirectories: a symlink or a
    special file anywhere in it is refused, as is a `.git` entry, a total size over
    `KNOWLEDGE_DIRECTORY_CAP_BYTES`, and a text file the secret gate would change. A binary file (one
    that is not UTF-8 or holds a NUL byte) is copied unchanged and is not secret-scanned.
    """
    actor = _clean_actor(actor)
    relative = _clean_directory(directory)
    files = _directory_files(Path(str(source_dir)).expanduser())
    for name, data in files:
        text = _text_or_none(data)
        if text is not None and redact(text) != text:
            raise KnowledgeValidationError(
                f"secret detected in {name} (target state/knowledge/{relative}/{name})", "secret"
            )
    return KnowledgeDirectory(actor, relative, files, state_repo.require_repo(instance_dir))


def write_knowledge_directory(
    instance_dir: Path,
    *,
    directory: str,
    actor: str,
    source_dir: Path,
    message: str | None = None,
) -> KnowledgeWriteResult:
    """Replace one directory under `state/knowledge` with the source directory and commit it.

    Under one `state_repo_lock` the target's whole contents become the source's (a file the source no
    longer has disappears from the target), and only that directory's pathspec is committed.
    `changed=False` means the committed directory already had this content. If the commit fails the
    previous contents are put back.
    """
    checked = check_knowledge_directory(instance_dir, directory=directory, actor=actor, source_dir=source_dir)
    instance_dir = checked.instance_dir
    target = state_repo.knowledge_dir(instance_dir) / checked.directory
    pathspec = (str(state_repo.KNOWLEDGE_RELATIVE / checked.directory),)
    with state_repo.state_repo_lock(instance_dir):
        if target.exists() and (target.is_symlink() or not target.is_dir()):
            raise KnowledgeValidationError(
                f"state/knowledge/{checked.directory} exists and is not a directory", "path"
            )
        previous = _replace_directory(target, checked.files)
        try:
            commit = state_repo.commit(
                instance_dir,
                pathspec,
                message or _commit_message(f"{checked.directory}/", checked.actor),
            )
        except BaseException:
            _restore_directory(target, previous)
            # The original failure is the one to report; the index is only put back when git answers.
            with contextlib.suppress(Exception):
                state_repo.git(instance_dir, ["add", "--", *pathspec], label="restore staged state")
            raise
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        if commit is None:
            return KnowledgeWriteResult(
                document=f"{checked.directory}/",
                path=target,
                commit=state_repo.head(instance_dir) or "",
                actor=checked.actor,
                changed=False,
            )
    return KnowledgeWriteResult(
        document=f"{checked.directory}/",
        path=target,
        commit=commit,
        actor=checked.actor,
        changed=True,
    )


def list_knowledge_documents(instance_dir: Path) -> tuple[str, ...]:
    """Every markdown document currently under `state/knowledge`."""
    root = state_repo.knowledge_dir(instance_dir)
    if not root.is_dir():
        return ()
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file()))


def _clean_actor(actor: str) -> str:
    value = actor.strip()
    if not value:
        raise KnowledgeValidationError("actor is required")
    return value


_PATH_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _clean_document(document: str) -> PurePosixPath:
    value = document.strip()
    if not value:
        raise KnowledgeValidationError("document path is required")
    if value.startswith("/") or value.startswith("~"):
        raise KnowledgeValidationError("document path must be relative to state/knowledge")
    value = value.removeprefix("state/knowledge/").rstrip("/")
    if not value.endswith(".md"):
        raise KnowledgeValidationError("knowledge document must be a .md file")
    return _clean_parts(value, "document")


def _clean_directory(directory: str) -> PurePosixPath:
    value = directory.strip()
    if not value:
        raise KnowledgeValidationError("directory path is required", "path")
    if value.startswith(("/", "~")):
        raise KnowledgeValidationError("directory path must be relative to state/knowledge", "path")
    value = value.removeprefix("state/knowledge/").rstrip("/")
    if not value or value == "state/knowledge":
        raise KnowledgeValidationError("directory path must name a directory below state/knowledge", "path")
    return _clean_parts(value, "directory")


def _clean_parts(value: str, what: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    for part in value.split("/"):
        if part in {"", ".", ".."} or any(char not in _PATH_CHARACTERS for char in part):
            raise KnowledgeValidationError(f"{what} path contains unsupported part: {part}", "path")
    return relative


def _directory_files(source: Path) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Every regular file below `source`, sorted, after the structural and size refusals."""
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError:
        raise KnowledgeValidationError(f"source directory not found: {source}", "source_missing") from None
    except OSError as exc:
        raise KnowledgeError(f"could not read {source}: {exc}") from None
    if stat.S_ISLNK(mode):
        raise KnowledgeValidationError(f"source directory is a symlink: {source}", "special_file")
    if not stat.S_ISDIR(mode):
        raise KnowledgeValidationError(f"source is not a directory: {source}", "source_missing")
    found: list[tuple[PurePosixPath, Path, int]] = []
    total = 0
    for root, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted([*dirnames, *filenames]):
            path = Path(root) / name
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if name == ".git":
                raise KnowledgeValidationError(f"source holds a .git entry: {relative}", "special_file")
            try:
                entry = path.lstat()
            except OSError as exc:
                raise KnowledgeError(f"could not read {path}: {exc}") from None
            if stat.S_ISLNK(entry.st_mode):
                raise KnowledgeValidationError(f"source holds a symlink: {relative}", "special_file")
            if stat.S_ISDIR(entry.st_mode):
                continue
            if not stat.S_ISREG(entry.st_mode):
                raise KnowledgeValidationError(f"source holds a special file: {relative}", "special_file")
            total += entry.st_size
            if total > KNOWLEDGE_DIRECTORY_CAP_BYTES:
                raise KnowledgeValidationError(
                    f"source directory is over the {KNOWLEDGE_DIRECTORY_CAP_BYTES // (1024 * 1024)} MiB cap: "
                    f"{source}",
                    "size_cap",
                )
            found.append((relative, path, entry.st_size))
    if not found:
        raise KnowledgeValidationError(f"source directory holds no files: {source}", "source_empty")
    files: list[tuple[PurePosixPath, bytes]] = []
    for relative, path, _size in found:
        try:
            files.append((relative, path.read_bytes()))
        except OSError as exc:
            raise KnowledgeError(f"could not read {path}: {exc}") from None
    if sum(len(data) for _name, data in files) > KNOWLEDGE_DIRECTORY_CAP_BYTES:
        raise KnowledgeValidationError(
            f"source directory is over the {KNOWLEDGE_DIRECTORY_CAP_BYTES // (1024 * 1024)} MiB cap: {source}",
            "size_cap",
        )
    return tuple(files)


def _text_or_none(data: bytes) -> str | None:
    """The file as text when it is UTF-8 without NUL bytes; None for a binary file."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _replace_directory(target: Path, files: tuple[tuple[PurePosixPath, bytes], ...]) -> Path | None:
    """Swap `target` for a directory holding `files`; the previous directory is returned, moved aside."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".new", dir=target.parent))
    except OSError as exc:
        raise KnowledgeError(f"could not write {target}: {exc}") from None
    previous: Path | None = None
    try:
        for name, data in files:
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if target.exists():
            previous = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".old", dir=target.parent))
            os.rmdir(previous)
            os.replace(target, previous)
        os.replace(staging, target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if previous is not None and not target.exists():
            _restore_directory(target, previous)
        raise KnowledgeError(f"could not write {target}: {exc}") from None
    return previous


def _restore_directory(target: Path, previous: Path | None) -> None:
    shutil.rmtree(target, ignore_errors=True)
    if previous is not None:
        with contextlib.suppress(OSError):
            os.replace(previous, target)


def _document_text(*, text: str | None, source_file: Path | None) -> str:
    if (text is None) == (source_file is None):
        raise KnowledgeValidationError("pass exactly one of text or source_file")
    if text is None:
        source_file = Path(str(source_file)).expanduser()
        try:
            text = source_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KnowledgeValidationError(f"document file not found: {source_file}") from None
        except OSError as exc:
            raise KnowledgeError(f"could not read {source_file}: {exc}") from None
        except UnicodeError as exc:
            raise KnowledgeValidationError(f"could not decode {source_file}: {exc}") from None
    if not text.strip():
        raise KnowledgeValidationError("document is empty")
    return text if text.endswith("\n") else text + "\n"


def _commit_message(document: str, actor: str) -> str:
    return (
        "\n".join(
            [
                f"knowledge: {document}",
                "",
                f"Principal: {actor}",
                f"Document: {document}",
            ]
        )
        + "\n"
    )
