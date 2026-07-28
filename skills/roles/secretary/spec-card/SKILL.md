---
name: spec-card
description: Turn an agreement with the owner (or an agent's idea) into a spec card on the Pipeline board — the PO role in the task pipeline. Triggers are "file a card", "into the pipeline", "the spec is ready", "yes, go ahead" after brainstorming a task, and any explicit approval to do a specific task.
---

# Spec-card — spec to card

You write task specs onto the Pipeline board, the single source of truth for executable work. The
dispatcher then brings up the workspace and the worker head from the card, so the spec has to be enough
for a fresh session with none of this conversation's context.

The current task protocol is `docs/PROTOCOLS.md` in the product repository.

## Spec template

Write markdown in exactly this structure, into a temporary file:

```markdown
## Goal

<A paragraph: what should exist and why. No "how" — the worker decides the implementation.>

## Context

- <path to a file or directory worth reading>
- <link to a document>
- memory_search: "<query>" — <what it should find>

## Acceptance criteria

- <checkable criterion 1>
- <checkable criterion 2>

## Out of scope

- <what this card deliberately does not do>
```

Context holds pointers only (paths, documents, `memory_search` queries), not pasted content: anything
copied will be stale by the time the workspace comes up. Acceptance criteria are what the worker later
marks done or not done in its report, with an explanation of how it checked, so phrase them checkably,
not as "do it properly".

Operational details do **not** go into the spec text but into the fields of the `create` call: `--type`
(code|research), `--project`, `--blocked-by`, `--head` (a profile from the head registry),
`--review-head`, `--slug`.

## Review head

By default `--review-head` is not passed: Validate takes the global reviewer profile and, after the
mechanical layers, runs the LLM review layer. If the owner explicitly accepts the risk of no independent
LLM read for a specific card, the PO may pass `--review-head none`. That disables only the LLM layer.
Branch integrity, CI (or an explicit no-CI declaration in the manifest), the end-to-end stand and the
other mechanical checks stay mandatory.

`none` is fine for a small, low-risk card, or one that has already been read by hand. Do not use it as an
answer to a red CI run, a hung check, a stand failure, a closed pull request or a temporarily unavailable
reviewer: those states must stay red or Blocked rather than turning green. The mode is cleared by a PO
update with `--review-head ""`, or by setting a concrete reviewer profile.

## Slug

ALWAYS pass `--slug`. It goes into the worker's workspace name (`<reference>-<slug>`) and the terminal tab
title, so the session-manager UI shows what the head is doing. Generate it yourself from the substance of
the task: 2 to 4 lowercase words joined by hyphens, `[a-z0-9-]{1,30}`, with no generic project prefix (the
reference already carries that). Example: a task about tearing down workspaces on Done →
`--slug teardown-done-workspaces`. If `create` refuses, the slug is wrong, not missing: a card without one
does not fail (the pipeline substitutes a fallback from the title), but the UI is then uninformative, so
always pass your own.

## Size

Half a page to a page. If it does not fit, cut it into a chain of cards through `blocked_by`: create the
predecessor, take its reference from the JSON response of `create`, and pass it in `--blocked-by` on the
next card.

## Spec radius

Acceptance criteria must not require changes outside the card project's repository: a worker pushes only
to its own project. Do the cross-repository parts yourself as PO, before or alongside the card, and give a
pointer to them in Context, or split them into a separate card in the right project through `blocked_by`.

## Gate by source

- The task came out of a conversation with the owner and they agreed ("yes, go ahead", or any explicit
  approval) → create it directly in `Ready` (`--column Ready`).
- The idea came from an agent (a retro finding, a research result, a worker's suggestion) → the `Ideas`
  column only (the `create` default). Do not promote it to `Ready` yourself; a person or a separate
  conversation does that.

## Concurrency

Code tasks in one project are strictly sequential: the dispatcher will not let a second code card be
claimed while the first is not Done. The guard already holds that at claim time, but if a new task
explicitly depends on another's result (rather than merely competing for the project), still set
`--blocked-by`.

## Invocation

```bash
spec=$(mktemp)
cat > "$spec" <<'EOF'
## Goal
...

## Context
...

## Acceptance criteria
...

## Out of scope
...
EOF

pipeline --role po create \
  --project example-project --type code --title "Short title" \
  --column Ready --head claude-sonnet --slug "short-slug" --description-file "$spec"
```

For an idea (the `Ideas` column) simply do not pass `--column`. Always pass the spec through
`--description-file`; do not inline markdown into an argument.

If you need per-card no-review:

```bash
pipeline --role po create \
  --project example-project --type code --title "Short title" \
  --column Ready --head claude-sonnet --review-head none \
  --slug "short-slug" --description-file "$spec"
```

To approve a previously filed idea, or to return a corrected Blocked card to work (both are PO
transitions):

```bash
pipeline --role po ready --ref example-project-42
```

To inspect a card:

```bash
pipeline show --ref example-project-42
```

## After creating it

Tell the owner the card's reference and the column it landed in.

## Reference

Do not pass `--ref`: it is generated as `<project>-<id>` from the `create` response. Set your own `--ref`
only when you need it in advance, for example to reference a card before it exists.
