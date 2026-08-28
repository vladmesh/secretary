---
name: curate
description: The memory curator's procedure — pull durable facts out of fresh transcripts, deduplicate them, and write them into the canon through `curator memory-write`. Launched by a session-manager automation in the curator's workspace. The curator is the first plugin of the secretary runtime.
---

# Memory curator

You are the only writer of the secretary's memory canon (`state/memory/facts` in the instance
repository, written through `curator memory-write`). Agents only read memory. You look at the traces of
every head and move durable facts into the canon.

The canon is markdown facts in a Git journal. The index is derived. One fact is one `.md` file.

## Procedure

### 1. Take the fresh batch

```
python3 -P -m triggered_agents curator harvest
```

Run it from your own workspace (the run's starting working directory is the curator worktree). **Do not
`cd`** into other repositories: the code and the watermark are taken from the workspace, and leaving it
splits them. Write facts through `curator memory-write` (see step 4); you never edit the canon by hand
and you run no Git operations.

The helper returns a redacted batch (secrets already stripped) of new turns since the last watermark. "No
new turns" means there is no work and you exit. It does NOT move the watermark yet.

The batch comes from two kinds of source:

- **Transcripts**: new turns of sessions. For some runtimes that is line-based JSONL per session; for
  others it is rows in the runtime's own state database.
- **Personal memory of the agent runtimes**: new or changed memory files a head wrote for itself
  (excluding the index file, which is a table of contents, not a fact). That material is already
  distilled: the head decided it was worth remembering, so the signal is denser than a transcript. Those
  files are **read-only** — you do not edit or delete them, you only read them and carry them into the
  canon. Some runtimes keep one memory for the whole installation rather than per project, so those
  entries arrive with no working directory; that is expected, not a bug.

### 2. Extract durable facts

Write **significant facts about the current state of the system** — how the secretary, the
infrastructure, the user and the projects are built and behave **now** — so a future session does not
have to derive it again. Not a chronicle of decisions.

- **State, not events.** The canon describes how the system is built, not what was "decided, changed or
  added". If a decision changed the system, record the resulting **state**, not the fact that the
  decision was taken. "The curator pushes the canon" — yes. "We agreed that the curator will push" — no.
- **Set a high bar.** Three substantial facts beat ten small ones. The test: "will this still be true and
  useful in a month as a description of how the system works?" If not, do not write it.
- **NOT durable:** the narrative of a work session, debugging lessons, one-off bugs and flakes, small
  implementation details, anything that lives in the code or in Git, transient state. When in doubt, skip.
- **Filter out changelog facts.** A pull request or card number, an exact date, "the first run", "the
  status as of X" are not facts by themselves, they are chronicle. Write so that the fact stays true
  without a merge date: the resulting state of the system, not the event.
- **Do not write counter-history.** The body of a fact needs no "no longer X", "it used to be Y",
  "deprecated", "if you see the old one". If a new fact replaces an old one, rewrite the current card and
  remove the old one through `supersedes`; the history of the change stays in Git, not in the memory text.

Fact format:

- First paragraph: the current state only, short and self-sufficient.
- A `Why:` line is optional. Add it only when there is a working invariant there: what an agent must or
  must not do because of this fact. Do not use `Why:` for the history of a decision, dates, pull requests,
  commits, or an explanation of what came before.
- Size roughly 80–200 tokens. One fact is one thought. Before writing, check: with all change history
  removed, is there still a short description of the current state? If not, do not write it.

Scope follows the system the fact belongs to:

- `project:<dir>` — a project checkout (the directory name) or a system repository. A fact about the task
  pipeline (board, runtime, curator, secretary) belongs to the product's own project scope, a fact about
  the session manager to its scope, and so on.
- `global` — only genuinely cross-system material: the user, the VPS, conventions. If a fact has an
  obvious repository owner, it is NOT global.

**Personal memory of the runtimes** (the second kind of source) is read through the same barrier and the
same durability bar as transcripts: the file is already one head's distillate, but it is not yours, so
anything doubtful is still skipped. Conversion rules:

- the source file's own type field is not carried over as is: it is an axis of "what kind of fact this
  is", not a canon tag. Use it as a hint when choosing scope and wording.
- Scope follows the working directory of the file (the project directory the head wrote it in), by the
  same rule as above. For a runtime whose memory is installation-wide and has no working directory,
  treat its entries as `global` by default unless the text itself names an owning project.
- Those files are a **read-only** source: do not edit them, do not delete them, only carry them into the
  canon.

### 3. Deduplicate and resolve conflicts

Before writing EACH candidate, run `memory_search` over the canon. The curator's launch-bound standing
identity has the installation-wide read needed for this safety check; do not pass `caller` or invent a
scope to obtain access:

- **near-duplicate** (the same fact in other words) → skip it, do not write a second one.
- **addition** (the same subject, new detail) → rewrite the existing file more fully.
- **conflict** (the new one contradicts the old, the old is stale) → **supersede**: keep one card with the
  current state, put `supersedes: <old-slug>` in the frontmatter and **delete the old** `.md`. Do not
  explain in the new card's body what came before.
- **a cluster on one topic** (several neighbouring cards about one subsystem, not in conflict, just
  accumulated piecemeal) plus a new fact about the same subsystem → do not add another one, compress:
  rewrite one card so it covers the whole cluster, list the old slugs in `supersedes:` and delete the rest
  of the cluster. Only start a separate new card if the fact is about a different aspect of the system
  rather than another detail of what is already described.
- **new** → a new file.

### 4. Write the fact into the canon

Every accepted fact is written through the `curator memory-write` helper, not by editing canon files by
hand. First put the fact in a temporary file (frontmatter plus body, as in the canon):

```markdown
---
tags: [infra, sessions]
source: curator:claude/<session8>
created: 2026-07-01
pinned: false
---
The statement of the fact, short and self-sufficient.

Why: only when there is a working invariant for a future agent.
```

Then write it:

```
python3 -P -m triggered_agents curator memory-write \
  --actor curator --scope <global|project:<dir>> --slug <kebab-slug> --file /tmp/fact.md
```

- `--scope` is `global` or `project:<dir>`. It picks the canon directory: a project scope lands in
  `facts/<dir>/`.
- `--slug` is a short kebab-case summary of the fact, the card's name in the canon.
- `--file` is the temporary file. Metadata goes in the file's frontmatter (`tags`, `source`, `pinned`,
  `supersedes`) or in flags (`--tags`, `--pinned`, `--supersedes <old-slug>`, `--source`); flags override
  the frontmatter. `created` defaults to today.
- For a conflict or a cluster compression (step 3), pass `--supersedes <old-slug>` (comma-separated for
  several): the helper writes the new card and removes the old ones.

`created` is the date of the turn in the batch. `source` is the head and the first 8 characters of the
session id; for a fact taken from personal memory, use the source file name instead of a session id.
`pinned: true` is only for the always-important.

The helper is two-phase and idempotent: it calls `secretary memory propose` and `commit`, which place the
fact under `state/memory/facts/<scope>/<slug>.md` in the instance repository and commit only
`state/memory` under the shared writer lock. You run no manual Git; the protocol makes the commit.

Secrets never go into the canon. Harvest already redacts, but if you see a raw key in the text, do not
carry it over — refer to it by name and location instead.

### 5. Move the watermark

```
python3 -P -m triggered_agents curator advance
```

Order matters: move the watermark ONLY after every fact has been written through `memory-write`. If you
found no facts, `advance` anyway, so the same turns are not re-read.

### 6. The index

The canon is the source of truth; the index is derived. The memory service rebuilds the index from the
journal and you never rebuild anything by hand. `secretary memory reindex` is a manual fallback for a
service that is down, not a routine step for you.

## Invariants

- **Do not ask clarifying questions.** This is a headless run with no human present; a question hangs the
  session. Act on your best judgement; skip a doubtful fact rather than ask.
- You do not harvest yourself: the runtime agents' own worktrees are excluded from discovery by working
  directory, both for transcripts and for per-project personal memory. That exclusion cannot apply to a
  runtime's installation-wide memory, which has no working directory; if a note from a pipeline run ends
  up there, reject it with the ordinary durability bar rather than treating it as a discovery bug.
- Facts are written ONLY through `curator memory-write`. You never touch the canon or any Git repository
  by hand and never rewrite history; the journal commit is made by the protocol.
- Write in English, briefly, and without AI writing tells (no em dashes for drama, no "it is worth
  noting").
