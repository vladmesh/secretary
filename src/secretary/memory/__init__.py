"""Bounded memory-plane helpers."""

# The embedding model the memory service loads when its environment names none. It lives here, not
# in `memory_service`, so `secretary upgrade` can bind a running service to it without importing the
# service's embedding stack.
DEFAULT_MODEL = "intfloat/multilingual-e5-large"
