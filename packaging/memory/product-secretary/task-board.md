---
id: task-board
title: Tasks and the Pipeline board
---

Secretary uses the Pipeline board as the source of truth for work. An execution card has a project, a `code` or `research` type, a full specification, acceptance criteria, dependencies, and a durable history. A card reference is normally `PROJECT-N`.

The ordinary flow is `Issues → Ready → In progress → Validate → Assessment → Done`; a card can also become `Blocked`. `Issues` is for proposals awaiting PO triage. Execution work starts in `Ready`, not in `Issues`.

One card is one bounded outcome. Put the complete current specification in the card description, split larger work into dependent cards, and keep dependencies explicit with `blocked_by`. Do not treat chat, a local note, or memory as a replacement for the card specification or board state.

Use `secretary task list` and `secretary task show` to read the current board. Creation, editing, claims, and state moves are role-guarded protocol operations; agents should not bypass them by writing to the board backend directly.
