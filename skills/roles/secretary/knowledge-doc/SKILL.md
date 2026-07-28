---
name: knowledge-doc
description: Keep a long recoverable document (a brainstorm, a decision log, an incident write-up) in `state/knowledge` of the private instance repository through `secretary knowledge write`, with no raw Git commands. Triggers are "write up this brainstorm", "keep a sprint log", "write up the incident", "save this reasoning", and any edit of a file under the instance repository's knowledge directory.
---

# Knowledge — the instance's long documents

The instance repository's `state/knowledge/` is the place for long reasoning and context: brainstorms,
decision logs, incident write-ups. The documents travel to the private remote with the rest of the
checkpoint, so they survive a move to a new machine.

## What goes where

- Executable work (cards, specs, states) goes on the Pipeline board, through the `spec-card` skill.
- The short current conclusion that must reach a head's context goes into curated memory
  (`state/memory/facts`), written by the curator.
- The long reasoning and the context the conclusion came from goes here.

Knowledge is not indexed, does not appear in `memory_search` and is never loaded into a head's context
wholesale. A document is read on purpose when the history of a question is needed.

## How to write

```bash
python3 -m secretary knowledge write --instance <instance dir> --actor secretary \
  --path decisions/2026-07-25-sprint-1.md --file /tmp/sprint-1.md
python3 -m secretary knowledge list --instance <instance dir>
```

**Do not run raw `git add` or `git commit` in the instance repository.** The dispatcher's tick writer
commits `state/board` and `state/runs` into the same repository every minute, and a manual commit races
it. `knowledge write` takes the shared writer lock and commits only `state/knowledge`.

- `--path` is relative, inside `state/knowledge`, and must end in `.md`. Choose the section by document
  type: `brainstorms/`, `decisions/`, `incidents/`.
- `--file` is the full new text of the document: the command replaces the file wholesale rather than
  appending. When editing a document, keep a working copy (for example in `/tmp`), edit that and write it
  again.
- The format is free; no frontmatter or metadata is required.
- Writing the same content a second time returns `changed: false` and makes no commit.
- A document containing a secret is rejected (a validation error, exit code 2) and never reaches disk.
  Remove the token from the text rather than working around the check.
