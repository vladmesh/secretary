"""Production dispatcher orchestration.

This package is additive: existing flat dispatcher modules keep their writers until
their dedicated migration cards (see docs/ARCHITECTURE.md, "Source layout and module
boundaries"). New dispatcher-adjacent vocabulary belongs here instead of widening the
flat package root again.
"""
