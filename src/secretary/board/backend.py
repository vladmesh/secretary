"""Which implementation serves the card reader and writer, decided in one named place.

`TaskReader` and `TaskWriter` have two implementations since `secretary-1587`: today's Kanboard
JSON-RPC one, and one over the PostgreSQL board store (`docs/BOARD_STORE.md` §2.2, §6).  Which one
a process uses is **not** inferred from whether `board-store.env` happens to exist — an
installation may hold a fully migrated store and still be served by Kanboard, and that is exactly
the state this card leaves the live installation in.  It is read from one environment name,
`SECRETARY_CARD_BACKEND`, whose absence means `kanboard`.

Three properties the card asks for and this module is where each of them is true:

* **one named place** — `CARD_BACKEND_ENV`, nothing else, and no filesystem probe;
* **decided once per process** — `card_backend()` caches the first answer, so a mid-run change of
  the environment cannot make one command read one backend and write the other;
* **an unknown value refuses** — `BoardBackendError`, naming the value and the two it is not,
  rather than a silent fall back to Kanboard, which would turn a typo into a live-board write.

Reversibility is the point of keeping the switch this thin.  Nothing here migrates, copies or
converts anything, so setting the name back to `kanboard` is the whole of the rollback.
"""

from __future__ import annotations

import os

#: The one named place.  A card backend is chosen here or it is `kanboard`.
CARD_BACKEND_ENV = "SECRETARY_CARD_BACKEND"

KANBOARD = "kanboard"
POSTGRES = "postgres"

#: The closed vocabulary, in the order diagnostics list it.
CARD_BACKENDS = (KANBOARD, POSTGRES)

DEFAULT_CARD_BACKEND = KANBOARD


class BoardBackendError(RuntimeError):
    """The configured card backend is not one this build knows how to serve."""


_decided: str | None = None


def parse_card_backend(value: str | None) -> str:
    """The vocabulary check on its own, without the per-process memory.

    An unset or empty value is the default; anything else is either a member of the vocabulary or
    a refusal.  Whitespace is stripped because an environment file is edited by hand, and case is
    not folded because the two names are literals of the design, not user prose.
    """
    if value is None or not value.strip():
        return DEFAULT_CARD_BACKEND
    name = value.strip()
    if name not in CARD_BACKENDS:
        raise BoardBackendError(
            f"{CARD_BACKEND_ENV} must be one of {', '.join(CARD_BACKENDS)}, not {name!r}"
        )
    return name


def card_backend() -> str:
    """The backend this process serves cards from, decided once and remembered.

    The first call reads the environment; every later call in the same process returns that same
    answer.  A command that read cards from Kanboard cannot therefore write them to PostgreSQL
    because something re-exported the variable between the two halves of its work.
    """
    global _decided
    if _decided is None:
        _decided = parse_card_backend(os.environ.get(CARD_BACKEND_ENV))
    return _decided


def reset_card_backend() -> None:
    """Forget the per-process decision.

    Only a test harness that runs both backends in one interpreter has a reason to call this;
    product code never does, which is what keeps "decided once per process" true where it matters.
    """
    global _decided
    _decided = None


def card_backend_status() -> dict[str, object]:
    """What `secretary status` reports about the switch: the value and where it came from."""
    raw = os.environ.get(CARD_BACKEND_ENV)
    try:
        backend = card_backend()
    except BoardBackendError as exc:
        return {
            "backend": None,
            "source": CARD_BACKEND_ENV,
            "default": DEFAULT_CARD_BACKEND,
            "findings": [str(exc)],
        }
    return {
        "backend": backend,
        "source": CARD_BACKEND_ENV if raw and raw.strip() else "default",
        "default": DEFAULT_CARD_BACKEND,
        "findings": [],
    }


def card_client(instance_dir: object, *, role: str = "app"):
    """The board client this process's switch names, built for one installation.

    This is the single construction path the two implementations share, and the only place the
    switch is *acted on*: `card_backend()` decides, and everything downstream — reader, writer,
    audit owner, host adapter — follows the client that comes back.  Nothing here probes for
    `board-store.env`; a `postgres` switch with no store configuration refuses with the store's
    own reason, which is the diagnosis an operator needs rather than a silent Kanboard fallback.
    """
    from secretary.tasks import KanboardClient

    if card_backend() == KANBOARD:
        return KanboardClient.for_instance(instance_dir)
    from secretary.board.sql_cards import SqlCardClient
    from secretary.board.store import resolve_role

    return SqlCardClient(resolve_role(instance_dir, role), instance_dir)


__all__ = [
    "CARD_BACKENDS",
    "CARD_BACKEND_ENV",
    "DEFAULT_CARD_BACKEND",
    "KANBOARD",
    "POSTGRES",
    "BoardBackendError",
    "card_backend",
    "card_backend_status",
    "card_client",
    "parse_card_backend",
    "reset_card_backend",
]
