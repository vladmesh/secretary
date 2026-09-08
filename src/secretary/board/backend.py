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

import hashlib
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


#: What a caller asks the switch to serve.  Only `CARD` has a second implementation today; the
#: other two are Kanboard boards until their own card builds them, and a `postgres` switch says
#: so rather than handing back a Kanboard client as if the switch had not been read.
CARD = "card"
SPRINT = "sprint"
PRODUCT_ISSUE = "product/issue"

#: Which of those the PostgreSQL implementation answers.  A caller that needs anything else is
#: refused by name, because a silent Kanboard client under a `postgres` switch is the same
#: "decided by default" defect the switch exists to remove.
POSTGRES_SERVES = frozenset({CARD, PRODUCT_ISSUE, SPRINT})


# Every normalized entity shares the integer-addressed board-client vocabulary.  These ranges
# are its one namespace: Cards retain their positive int4 task number, while Sprint, Product and
# Issue keys are stored and indexed beside their string identity.  Sprint reserves two disjoint
# half-ranges so numbered references are reversible without colliding with custom references.
RECORD_KINDS = ("sprint", "product", "issue")
RECORD_KEY_BASES = {"sprint": 2_000_000_000, "product": 3_000_000_000, "issue": 4_000_000_000}
RECORD_KEY_SPAN = 1_000_000_000
SPRINT_NUMBER_KEY_SPAN = RECORD_KEY_SPAN // 2


def record_key(kind: str, identifier: str) -> int:
    """Return the stable SQL transport key for one normalized non-Card identity."""
    if kind not in RECORD_KINDS:
        raise BoardBackendError(f"a board record names one of {', '.join(RECORD_KINDS)}, not {kind!r}")
    text = str(identifier).strip()
    if not text:
        raise BoardBackendError(f"a {kind} key needs an identifier")
    if kind == "sprint" and text.startswith("sprint:") and text[7:].isdigit():
        number = int(text[7:])
        if number >= SPRINT_NUMBER_KEY_SPAN:
            raise BoardBackendError(
                f"a numbered Sprint must be below {SPRINT_NUMBER_KEY_SPAN}, not {number}"
            )
        return RECORD_KEY_BASES[kind] + number
    digest = hashlib.sha256(f"{kind}:{text}".encode()).digest()
    offset = SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else 0
    span = SPRINT_NUMBER_KEY_SPAN if kind == "sprint" else RECORD_KEY_SPAN
    return RECORD_KEY_BASES[kind] + offset + int.from_bytes(digest[:8], "big") % span


def record_key_kind(value: object) -> str | None:
    """Return the normalized non-Card kind for a transport key, or None for a Card key."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for kind in RECORD_KINDS:
        base = RECORD_KEY_BASES[kind]
        if base <= number < base + RECORD_KEY_SPAN:
            return kind
    return None


def board_client(
    instance_dir: object,
    *,
    serves: tuple[str, ...] = (CARD,),
    role: str = "app",
    transport: object | None = None,
):
    """The board client this process's switch names, built for one installation.

    This is the single construction path the two implementations share, and the only place the
    switch is *acted on*: `card_backend()` decides, and everything downstream — reader, writer,
    audit owner, host adapter — follows the client that comes back.  Nothing here probes for
    `board-store.env`; a `postgres` switch with no store configuration refuses with the store's
    own reason, which is the diagnosis an operator needs rather than a silent Kanboard fallback.

    `serves` is what the call site needs from the client it is asking for, and it is the whole of
    why this function takes an argument at all.  A site that reads sprints, or Product/Issue, or —
    like `restore.py` — cards *and* sprints through one client cannot be served by a backend that
    holds only cards, so under `postgres` it is refused by name here instead of being handed a
    Kanboard client that contradicts the switch.  `transport` is for the one caller that has
    already built the Kanboard transport it wants probed; every other caller leaves it unset.
    """
    from secretary.tasks import KanboardClient, TaskError

    # The refusals leave here as `TaskError`, the one vocabulary every command above this
    # function already renders as a named failure with an exit status.  `parse_card_backend`
    # keeps `BoardBackendError` for `card_backend_status`, which reports rather than refuses.
    try:
        backend = card_backend()
    except BoardBackendError as exc:
        raise TaskError("backend_error", str(exc), 1) from None
    if backend == KANBOARD:
        if transport is not None:
            return KanboardClient(transport, instance_dir)
        return KanboardClient.for_instance(instance_dir)
    unknown = tuple(entity for entity in serves if entity not in POSTGRES_SERVES)
    if unknown:
        raise TaskError(
            "backend_error",
            f"{CARD_BACKEND_ENV}={backend} serves {', '.join(sorted(POSTGRES_SERVES))} only; "
            f"{', '.join(unknown)} is a Kanboard board on this build",
            1,
        )
    from secretary.board.sql_cards import SqlCardClient
    from secretary.board.store import BoardStoreError, resolve_role

    try:
        credentials = resolve_role(instance_dir, role)
    except BoardStoreError as exc:
        raise _store_refusal(exc) from None
    return SqlCardClient(credentials, instance_dir)


def card_client(instance_dir: object, *, role: str = "app"):
    """`board_client` for the one entity the PostgreSQL implementation serves."""
    return board_client(instance_dir, serves=(CARD,), role=role)


def _store_refusal(exc: Exception):
    """A store refusal, in the vocabulary `run_task_command` already prints (`TaskError`).

    `BoardStoreError` is a `RuntimeError`, and a `RuntimeError` reaching a CLI handler is a
    traceback.  Every command that reads or writes cards already renders `TaskError` as a named
    refusal with an exit status, so a `board-store.env` that is missing, malformed or tracked by
    git leaves here as `backend_unavailable` carrying the store's own sentence.
    """
    from secretary.tasks import TaskError

    return TaskError("backend_unavailable", f"board store is not usable: {exc}", 1)


ENTITY_KINDS = ("task", "sprint")


def entity_id(kind: str, backend: str, number: int) -> str:
    """Mint `<kind>_<backend>_<n>`: the one identity a normalized row carries.

    The convention has three parts and each is load-bearing.  `kind` says whether the row is a
    card or a sprint, `backend` says which of the two implementations answered, and `number` is
    that backend's own number for the row — Kanboard's task id, or `tasks.task_number` in the
    store.  It is minted here and nowhere else, which is what makes `entity_number` able to read
    every value the product can produce: the two functions are one convention, not two.
    """
    if kind not in ENTITY_KINDS:
        raise BoardBackendError(f"an entity identity names one of {', '.join(ENTITY_KINDS)}, not {kind!r}")
    if backend not in CARD_BACKENDS:
        raise BoardBackendError(
            f"an entity identity names one of {', '.join(CARD_BACKENDS)}, not {backend!r}"
        )
    return f"{kind}_{backend}_{int(number)}"


def entity_number(kind: str, value: object) -> int | None:
    """Read the number back out of an identity `entity_id` minted, for **either** backend.

    `None` means the value is not an identity of this kind, and every caller turns that into its
    own refusal.  The whole vocabulary is tried rather than one literal prefix: a parser that
    knew only `task_kanboard_` answered `None` for every card the PostgreSQL backend produced,
    which is how `report`, `verdict` and `decide` failed there while the reader worked.  A bare
    number is still accepted, because the file journal holds records written before the identity
    carried a backend at all.
    """
    if kind not in ENTITY_KINDS:
        raise BoardBackendError(f"an entity identity names one of {', '.join(ENTITY_KINDS)}, not {kind!r}")
    text = "" if value is None else str(value).strip()
    for backend in CARD_BACKENDS:
        prefix = f"{kind}_{backend}_"
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


__all__ = [
    "CARD",
    "CARD_BACKENDS",
    "CARD_BACKEND_ENV",
    "DEFAULT_CARD_BACKEND",
    "ENTITY_KINDS",
    "KANBOARD",
    "POSTGRES",
    "PRODUCT_ISSUE",
    "RECORD_KEY_BASES",
    "RECORD_KEY_SPAN",
    "RECORD_KINDS",
    "SPRINT",
    "BoardBackendError",
    "board_client",
    "card_backend",
    "card_backend_status",
    "card_client",
    "entity_id",
    "entity_number",
    "parse_card_backend",
    "record_key",
    "record_key_kind",
    "reset_card_backend",
]
