---
name: start-sprint
description: "Start a new product sprint: gather live context, grill the unresolved product forks if needed, fix the goal and Definition of Done in a separate sprint document, clear Ready in the affected repositories and set the single active-sprint pointer. Use on explicit requests like 'start a sprint', 'new sprint', 'plan a sprint', `$start-sprint`."
---

# Start Sprint

Create the contract for one autonomously executable sprint. Do not create cards and do not start work:
`$run-sprint` does that.

## The canon

- One active sprint for the whole product.
- Documents live under the sprints directory in the instance repository's knowledge tree.
- A status pointer names the active document.
- A sprint may touch the product repository, the instance repository and any related repository.
- A sprint has no phases, task pool or one-item-per-card mapping. Cards are cut one at a time during
  execution.
- The board holds execution. The sprint document holds the goal, the decisions and the causal history.

Write knowledge only through `secretary knowledge write`, to keep the writer lock, the secret scan and the
Git audit. Do not commit the instance repository by hand.

## 1. Check the state

1. Call `memory_search` with `caller=secretary`: the goal, previous decisions, the last sprint.
2. Read:
   - the sprint status pointer, if it exists;
   - the most recent sprint document;
   - the current vision, roadmap and backlog of the affected repositories;
   - the related Ideas and the results of previous cards.
3. If the status pointer names an unclosed sprint, do not create a new one. Offer to continue it through
   `$run-sprint`, or to cancel it explicitly by a separate decision from the user.

Do not reconstruct state from memory when the live sprint document says otherwise.

## 2. Agree the contract

First derive what can be proved from the code and the documents. For the remaining forks use grilling: one
question at a time, with your recommended answer, waiting for the reply.

Grilling is unnecessary if the user has already explicitly agreed everything needed:

- one sentence of product goal;
- a checkable Definition of Done;
- the affected repositories;
- scope hints, as optional pointers;
- what is out of scope;
- the product decision gates;
- a proportionate validation plan.

Check that the goal is a product increment rather than a list of fixes. Every scope hint must plausibly
serve the goal. If the goal conflicts with the product vision, stop and raise the contradiction.

## 3. Create the sprint document

Path: `sprints/YYYY-MM-DD-<slug>.md`, with a slug of 2 to 4 kebab-case words.

```markdown
---
title: <title>
status: active
created: YYYY-MM-DD
repositories:
  - <repo>
---

# Sprint: <title>

## Goal

<One sentence about the end product state.>

## Definition of Done

- <A checkable result>

## Context

<Why now; links to the vision, roadmap, brainstorm and previous sprint.>

## Scope Hints

- <An optional direction, neither a task nor a promised card>

## Out of Scope

- <A boundary>

## Stop Conditions

- The goal is provably unreachable.
- A required decision changes the vision, a product contract, the security boundary, ownership, a durable
  data contract, a mandatory external dependency or the Definition of Done.
- Access is missing.
- An external action is required that could cut off the current session.

## Validation Plan

<The minimal sufficient real check through a product interface.>

## Decisions

## Execution Log

## Hotfixes

## Deferred

## Results
```

After the start, the goal, Definition of Done, out of scope and stop conditions are an immutable contract.
`$run-sprint` may change the path but may not weaken the contract.

## 4. Clear Ready

For each affected repository:

1. Read Ready, In progress and Validate through the task protocol.
2. Do not interrupt an active card automatically:
   - if it directly serves the goal, record it as a first step already under way;
   - otherwise wait for it to finish before opening the sprint.
3. Return every other Ready card to Ideas, with a comment that its admission is withdrawn for the duration
   of the atomic sprint.
4. Do not promote Ideas. They are input material; `$run-sprint` creates fresh cards.
5. Do not touch other products' cards.

If a board write did not complete, do not set the active pointer.

## 5. Set the pointer

Write the sprint status pointer through the knowledge writer:

```markdown
# Sprint Status

**Active:** `<title>` (`YYYY-MM-DD-<slug>.md`)
**State:** ready
**Updated:** <RFC3339>
**Current card:** none
**Next:** invoke `$run-sprint`
```

Write the sprint document first, then the status pointer. After writing, re-read both documents and check
the task audit. Fix a partial write before reporting.

## 6. Report back

Give the path to the document, the goal, the Definition of Done, the repositories, which Ready cards were
returned to Ideas, and that the next step is `$run-sprint`. Do not create a card "just in case".
