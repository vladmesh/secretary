# Global instructions

## Shared memory

Shared memory lives in the `memory` MCP server. Before answering or acting on context that has been
discussed before, call `memory_search`. Do not pass `caller`: Secretary derives read authority from
the launch-bound identity. Pass `scope` only to narrow the scopes already granted to the session.

## Git

Do not add AI co-authorship to commits. Do not rewrite pushed history and do not force-push without
an explicit request from the user.

## Style

Write densely and in the register of the surrounding author. Do not use em dashes, AI-sounding
openers, restatements of the same point, or bold for intonation.
