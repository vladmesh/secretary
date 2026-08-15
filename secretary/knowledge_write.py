"""Writer for long recoverable documents in `state/knowledge`.

Contract: docs/ARCHITECTURE.md, "Knowledge planes". Knowledge holds the long
reasoning behind a decision: brainstorms, decision logs, incident write-ups.
It is plain tracked markdown, so a document written here rides to the remote
with the rest of the checkpoint and survives a move to another machine.

The writer exists so the role keeping a document does not have to reach for raw
`git`. A bare `git commit` in the instance repo races the tick writer, which
commits `state/board` and `state/runs` in the same repo every minute. This
writer owns `state/knowledge` alone, takes the same `state_repo_lock` the other
writers take, and never runs `git add -A`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from secretary import state_repo
from secretary._fsutil import write_text_atomic as _write_text_atomic
from secretary.state_repo import KNOWLEDGE_PATHSPEC
from triggered_agents.runtime.redact import redact


class KnowledgeError(RuntimeError):
    """A knowledge write did not happen."""


class KnowledgeValidationError(KnowledgeError):
    """The document or its path is not something this writer accepts."""


@dataclass(frozen=True)
class KnowledgeWriteResult:
    document: str
    path: Path
    commit: str
    actor: str
    changed: bool


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
    actor = _clean_actor(actor)
    relative = _clean_document(document)
    body = _document_text(text=text, source_file=source_file)
    # Knowledge leaves the host with the rest of the checkpoint, and a brainstorm
    # describes infrastructure by its nature, so it passes the same secret gate
    # the tick and memory writers apply.
    if redact(body) != body:
        raise KnowledgeValidationError(f"secret detected in state/knowledge/{relative}")

    instance_dir = state_repo.require_repo(instance_dir)
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


def list_knowledge_documents(instance_dir: Path) -> tuple[str, ...]:
    """Every markdown document currently under `state/knowledge`."""
    root = state_repo.knowledge_dir(instance_dir)
    if not root.is_dir():
        return ()
    return tuple(
        sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file())
    )


def _clean_actor(actor: str) -> str:
    value = actor.strip()
    if not value:
        raise KnowledgeValidationError("actor is required")
    return value


def _clean_document(document: str) -> PurePosixPath:
    value = document.strip()
    if not value:
        raise KnowledgeValidationError("document path is required")
    if value.startswith("/") or value.startswith("~"):
        raise KnowledgeValidationError("document path must be relative to state/knowledge")
    value = value.removeprefix("state/knowledge/").rstrip("/")
    if not value.endswith(".md"):
        raise KnowledgeValidationError("knowledge document must be a .md file")
    relative = PurePosixPath(value)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    for part in relative.parts:
        if part in {".", ".."} or any(char not in allowed for char in part):
            raise KnowledgeValidationError(f"document path contains unsupported part: {part}")
    return relative


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
    return "\n".join(
        [
            f"knowledge: {document}",
            "",
            f"Principal: {actor}",
            f"Document: {document}",
        ]
    ) + "\n"
