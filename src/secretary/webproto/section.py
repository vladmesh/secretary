"""The one place a section of a snapshot is assembled, and the one invariant it holds:

    A source that refused, or that was never read, may not delete, shadow or fabricate an answer
    another source already gave. Every section says which source answered it, and an answer is
    attributed to the source that actually produced it.

`sources.py` gave that rule its shape -- every section carries a `Source`, and "there are no running
agents" is never spelled the same way as "the file that would say so could not be read". What it did
not give it was a *place*. Each section assembled itself, so the rule was kept a branch at a time,
and four sites of one document broke it in four different ways across two review rounds: a refusal
ordered above a board answer already in hand; an answer decided by one source and returned under
another's availability; one try block covering two sources, so the failure of the second erased the
first; and an affirmative claim -- "this sprint declared no observer, and none was started" --
manufactured out of a file that only proves it holds no row. Each was repaired locally and the next
one appeared somewhere else, which is what a rule with no enforcement point looks like.

So the rule is enforced here, structurally, and a section is covered by the act of being a section.

**A source is a `Reading`**, made once for the whole document: its key, its `Source`, and the value
it produced. :meth:`SourceSet.value` refuses to hand back the payload of a source that did not
answer, so a section cannot read one by accident.

**A section is decided by :meth:`SourceSet.decide`**, from an ordered list of :class:`Rule`. A rule
names the source it `answers` from and every source it `needs`, receives exactly those sources'
values as its arguments -- and is *not run at all* unless every one of them answered. So the value
of a refused source can never reach a claim: not because a branch remembered to check, but because
the code that would have used it never executes. The first rule that produces wins, and the section
carries the `Source` of the rule's `answers` key -- the source that actually produced the answer,
never the availability of whatever else happened to be read.

**What a refusal may say is declared, not written per branch.** Every section declares its `blank`:
the field values that claim nothing. When no rule can produce, the section is that blank, attributed
to the highest-precedence source among those consulted that refused -- the first missing input in
the chain -- and carrying its reason. A section may narrate that refusal (`reason`, and the
identifiers it is about), but the claim fields are checked against the blank, so a branch that tries
to answer under a source that refused raises :class:`SectionContractError` rather than shipping.

**A section that cannot answer when everything answered is a hole**, and `decide` raises rather than
inventing an attribution for it: with every consulted source available and no rule producing, the
section's rules are not total, which is a defect of this layer and not a fact about the
installation.

**And the document seam.** A section is consumed in exactly two places, and both ask where it came
from. :func:`render` turns the assembled tree into JSON: it refuses a plain mapping carrying a
`source` of this shape, *and* it refuses a `Section` this module did not decide. :class:`SectionSet`
closes the same loop on the other side -- every public method of a subclass is wrapped at class
creation and must answer with a section that `decide` or `mark` produced, exactly as
`ProtocolBoundary` wraps every public operation. A section added next month is guarded by being a
public method of the set, and there is no list to keep in step.

The provenance is what makes that true rather than merely stated. Being a `Section` is not the
credential: a builder that returned `Section(read.source("liveness"), {"state": "working"},
"liveness")` under an unavailable liveness source was accepted by both places, for being the right
type, and published the exact claim this module exists to prevent. So a section carries `minted`,
which only :func:`_minted` sets, which only `decide` and `mark` call. **The promise, stated exactly:**
a section built by calling the constructor cannot be returned from a builder and cannot reach a
document. An author who reaches into this module for the private sentinel can still mint one -- that
is forging rather than forgetting, and no seam in this language prevents it; what is prevented is
the section written the ordinary way that claims more than its source gave.

`SectionContractError` is a `RuntimeError` on purpose, and deliberately outside
`boundary.IMPLEMENTATION_FAILURES`: it is a defect of this layer, not a source that refused, and
dressing it as `backend_unavailable` would hide it from the reader best placed to fix it.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from secretary.webproto import sources

#: The four fields `sources.Source.to_json` always writes. `render` uses them to tell a section
#: somebody hand-built from a data mapping that merely happens to have a `source` field of its own.
SOURCE_FIELDS = frozenset({"state", "reason", "observed_at", "data_age_seconds"})

#: What a refusal may still put into words without making a claim: why nothing was established, and
#: which subject the section would have been about. Everything else is checked against the blank.
NARRATION = ("reason",)


class SectionContractError(RuntimeError):
    """A section that claims more than its source gave. A defect of this layer, never a refusal."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One source of one document, read once: what it is called, whether it answered, what it gave.

    `value` is meaningful only when the source answered, and :meth:`SourceSet.value` is the only way
    a rule gets at it, so "unavailable" cannot quietly become an empty list.
    """

    key: str
    source: sources.Source
    value: Any = None

    @property
    def answered(self) -> bool:
        return self.source.state == sources.AVAILABLE


@dataclass(frozen=True, slots=True)
class Rule:
    """One way a section may be answered: from which source, needing which, by what.

    `needs` is every source whose value the rule reads, and it always contains `answers`. The rule
    receives those values, in that order, and runs only when every one of them answered. Returning
    `None` means "this rule does not settle it", and the next rule is tried.
    """

    answers: str
    needs: tuple[str, ...]
    produce: Callable[..., Mapping[str, Any] | None]

    def __post_init__(self) -> None:
        if self.answers not in self.needs:
            raise SectionContractError(
                f"a rule answering from {self.answers!r} must name it among the sources it needs"
            )


def rule(
    answers: str, produce: Callable[..., Mapping[str, Any] | None], *, needs: Sequence[str] = ()
) -> Rule:
    """`Rule`, with `needs` defaulting to the source the rule answers from."""
    return Rule(answers, tuple(needs) if needs else (answers,), produce)


#: Proof that a section was assembled by this module, carried by the section itself.
#:
#: A private object and deliberately not a boolean or a string: a flag is set by anybody who
#: constructs the dataclass, and this is never exported, never written to a document and never
#: reachable from a value a caller passes in. So `minted is _MINTED` answers exactly one question --
#: did :meth:`SourceSet.decide` or :meth:`SourceSet.mark` make this? -- and answers it for an
#: instance somebody built by calling the constructor with three arguments, which is what a section
#: written by hand looks like.
#:
#: What it does *not* claim: an author who deliberately reaches into this module for the sentinel
#: can still mint one. That is forging, not forgetting, and the rule this seam exists to enforce is
#: that a section written the ordinary way cannot claim more than its source gave.
_MINTED = object()


@dataclass(frozen=True, slots=True)
class Section:
    """One section: the source that answered it, named, and the fields it answered with.

    The name is in the document and not only in this object. Two sources that both answered are the
    same four fields -- `available`, no reason, the moment of the read -- so without it a reader
    cannot tell an answer the board established from one the dispatcher established, and "every
    section names the source that answered it" would hold only for the sources that failed.

    `minted` is where this object says where it came from. An instance built by calling this
    constructor carries nothing, is not :attr:`trusted`, and is refused by `guard` and by `render` --
    the two places a section is consumed. Without that, a section written by hand was accepted by
    both merely for being a `Section`, and a builder that returned
    `Section(read.source("liveness"), {"state": "working"}, "liveness")` under an unavailable
    liveness source published exactly the claim this module exists to prevent.
    """

    source: sources.Source
    fields: Mapping[str, Any]
    name: str
    minted: Any = None

    @property
    def trusted(self) -> bool:
        """Whether this section was decided here, rather than assembled by a caller."""
        return self.minted is _MINTED

    def to_json(self) -> dict[str, Any]:
        return render(self)


def _minted(source: sources.Source, fields: Mapping[str, Any], name: str) -> Section:
    """The only place a trusted section is made. `decide` and `mark` are its only callers."""
    return Section(source, fields, name, _MINTED)


class SourceSet:
    """Every source of one document, read once, in the precedence they are consulted in.

    The order is the order a refusal is attributed in: when nothing could answer, the section names
    the first source of this order that was consulted and did not answer, because that is the first
    input the chain was missing.
    """

    __slots__ = ("_order", "_readings")

    def __init__(self, readings: Iterable[Reading]) -> None:
        self._readings: dict[str, Reading] = {}
        for reading in readings:
            self._readings[reading.key] = reading
        self._order: tuple[str, ...] = tuple(self._readings)

    def __contains__(self, key: object) -> bool:
        return key in self._readings

    def reading(self, key: str) -> Reading:
        try:
            return self._readings[key]
        except KeyError:
            raise SectionContractError(f"no source of this document is called {key!r}") from None

    def answered(self, key: str) -> bool:
        return self.reading(key).answered

    def source(self, key: str) -> sources.Source:
        return self.reading(key).source

    def value(self, key: str) -> Any:
        """What this source gave, and an error rather than a payload when it did not answer."""
        reading = self.reading(key)
        if not reading.answered:
            raise SectionContractError(
                f"the {key!r} source did not answer, so it has no value to read: {reading.source.reason}"
            )
        return reading.value

    def mark(self, key: str) -> Section:
        """One source's availability, said for the document as a whole and claiming nothing."""
        return _minted(self.source(key), {}, key)

    def replacing(self, key: str, value: Any) -> SourceSet:
        """This set with one source's value narrowed -- the same reading, for one subject of it.

        A listing reads each source once for the whole document and then answers per sprint. The
        narrowing keeps the source's own availability: what changes is which part of what it gave is
        in front of the sections, never whether it answered.
        """
        self.reading(key)
        return SourceSet(
            Reading(entry.key, entry.source, value if entry.key == key else entry.value)
            for entry in self._readings.values()
        )

    def decide(
        self,
        *rules: Rule,
        blank: Mapping[str, Any],
        narrates: Sequence[str] = NARRATION,
        unresolved: Callable[[Reading], Mapping[str, Any]] | None = None,
    ) -> Section:
        """The section these rules decide, attributed to the source that decided it.

        `blank` is what this section says when nothing could answer: its claim fields, at the values
        that claim nothing. `narrates` names the fields a refusal may still fill -- the reason, and
        the identifiers the section is about -- and every other field is checked against `blank`.
        """
        consulted: list[str] = []
        for one in rules:
            for key in one.needs:
                self.reading(key)
                if key not in consulted:
                    consulted.append(key)
        for one in rules:
            if not all(self.answered(key) for key in one.needs):
                continue
            produced = one.produce(*(self.value(key) for key in one.needs))
            if produced is None:
                continue
            return _minted(self.source(one.answers), self._checked(produced, blank), one.answers)
        refused = next(
            (key for key in self._order if key in consulted and not self.answered(key)), None
        )
        if refused is None:
            raise SectionContractError(
                "every source this section consults answered and no rule settled it: "
                f"the rules over {consulted} are not total"
            )
        reading = self.reading(refused)
        fields = self._checked(unresolved(reading) if unresolved else dict(blank), blank)
        for name, value in blank.items():
            if name not in narrates and fields[name] != value:
                raise SectionContractError(
                    f"{refused!r} did not answer, so this section may not claim {name}={fields[name]!r}"
                )
        return _minted(reading.source, fields, refused)

    @staticmethod
    def _checked(produced: Mapping[str, Any], blank: Mapping[str, Any]) -> dict[str, Any]:
        """The same fields in every branch: a branch that forgets one is a defect, not an omission."""
        if set(produced) != set(blank):
            raise SectionContractError(
                f"a section answered with fields {sorted(produced)} where it declares {sorted(blank)}"
            )
        return dict(produced)


def render(node: Any) -> Any:
    """The assembled document as JSON, and the seam that keeps a section from being hand-built.

    A `Section` becomes its source plus its fields. A plain mapping carrying a `source` of the shape
    `sources.Source` writes is refused: that is a section assembled outside `decide`, which is the
    one thing this module exists to prevent.
    """
    if isinstance(node, Section):
        if not node.trusted:
            raise SectionContractError(
                f"the {node.name!r} section was not decided by this seam, so it may not be published; "
                "a document's sections are built by SourceSet.decide"
            )
        return {
            "source": {**node.source.to_json(), "name": node.name},
            **{key: render(value) for key, value in node.fields.items()},
        }
    if isinstance(node, Mapping):
        carried = node.get("source")
        if isinstance(carried, Mapping) and SOURCE_FIELDS <= set(carried):
            raise SectionContractError(
                f"a section carrying {sorted(node)} was assembled outside this seam; "
                "a document's sections are built by SourceSet.decide"
            )
        return {key: render(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [render(value) for value in node]
    return node


#: Set on a wrapped builder, so a test can tell a guarded section from an unguarded one without
#: calling it, and so wrapping twice is a no-op.
GUARDED = "__webproto_section__"


def guard(function: Callable[..., Any]) -> Callable[..., Any]:
    """`function`, required to answer with a `Section` and nothing else."""
    if getattr(function, GUARDED, False):
        return function

    @functools.wraps(function)
    def builder(*args: Any, **kwargs: Any) -> Any:
        produced = function(*args, **kwargs)
        if not isinstance(produced, Section):
            raise SectionContractError(
                f"{function.__name__!r} is a section and must answer with one, not "
                f"{type(produced).__name__}"
            )
        if not produced.trusted:
            raise SectionContractError(
                f"{function.__name__!r} assembled its section itself; a section is decided by "
                "SourceSet.decide, which is what holds the invariant over it"
            )
        return produced

    setattr(builder, GUARDED, True)
    return builder


def sections(cls: type) -> tuple[str, ...]:
    """The sections a set defines, in definition order. The same predicate the wrapping uses."""
    return tuple(
        name
        for name, attribute in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(attribute)
    )


class SectionSet:
    """A class whose public methods each assemble one section of a document.

    Subclassing is the whole mechanism, exactly as with `boundary.ProtocolBoundary`: every public
    method defined in the body is wrapped at class creation and must answer with a section that
    `SourceSet.decide` or `SourceSet.mark` decided -- not merely with something of that type -- so a
    section added tomorrow is covered by being one. Helpers stay private and are untouched.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name in sections(cls):
            setattr(cls, name, guard(vars(cls)[name]))
