"""The one place a protocol code becomes an HTTP status.

The layer below answers callers that are not HTTP, so its failures are typed codes
(:mod:`secretary.webproto.errors`) and never status numbers. Somewhere that has to become 404, and
the whole of criterion 1 is that "somewhere" is one table rather than a number written at each
`except` site. A status scattered across handlers drifts: the same `not_found` becomes 404 on one
route and 400 on another, and a client then has to know which route it asked.

So handlers here raise nothing and return no numbers. They call the layer, let a
:class:`~secretary.webproto.errors.ReadError` out, and this maps it.

| code | status | why |
| --- | --- | --- |
| `not_found` | 404 | the board or the run store answered, and holds nothing under that name |
| `validation` | 400 | the request itself is wrong: a missing field, a cursor this layer did not issue |
| `owner_conflict` | 409 | the request is well formed and refused on the state of the world |
| `backend_unavailable` | 503 | a source this request needs could not be reached at all |

An unmapped code is 500 and not a guess: a code this transport has never heard of is a defect of
this transport, and answering it with a plausible-looking 400 would hide the defect behind a status
a client treats as its own fault.
"""

from __future__ import annotations

#: Every protocol code this transport knows, and the status it is answered with. Complete with
#: respect to `secretary.webproto.errors`; a test fails if the layer grows a code that is missing.
HTTP_STATUS_BY_CODE: dict[str, int] = {
    "not_found": 404,
    "validation": 400,
    "owner_conflict": 409,
    "backend_unavailable": 503,
}

#: What a code with no entry above is answered with. See the module docstring: this is a bug
#: report, not a fallback that quietly works.
UNMAPPED_CODE_STATUS = 500


def status_for(code: str) -> int:
    """The HTTP status for one protocol code."""
    return HTTP_STATUS_BY_CODE.get(str(code or ""), UNMAPPED_CODE_STATUS)
