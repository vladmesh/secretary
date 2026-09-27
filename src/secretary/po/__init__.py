"""The product owner head: its permanent workspace on the host."""

# Names the PO session in the environment of each of its turns (`PoRunner.session_environment`), so
# `sprint create` inside a turn records the session that opened the sprint without the head passing it.
PO_SESSION_ENV = "SECRETARY_PO_SESSION"
# Names the request id of the input the turn answers, beside the session (secretary-1792), so `task
# create --role po` inside a turn records where a delegated card came from (`board/po_origin.py`).
# Unset for a turn whose input carried no request id.
PO_REQUEST_ENV = "SECRETARY_PO_REQUEST"
