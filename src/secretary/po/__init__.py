"""The product owner head: its permanent workspace on the host."""

# Names the PO session in the environment of each of its turns (`PoRunner.session_environment`), so
# `sprint create` inside a turn records the session that opened the sprint without the head passing it.
PO_SESSION_ENV = "SECRETARY_PO_SESSION"
