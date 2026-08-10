# Interactive head delivery and the session-manager dependency

Status as of 2026-08-10. This is working material for a design decision, not a specification. It
collects what is proven, what has been built, what has been decided and what is still open, so the
next conversation starts from evidence instead of from recollection. Where it states a measurement,
the measurement is cited; where it states an opinion, it says so.

## 1. The problem, stated precisely

Every agent head in this product runs as an interactive TUI in a pane owned by the session manager
(Orca 1.4.163). A head is launched with a command that carries no prompt, so the prompt must be
typed into the pane afterwards. Delivering it is not one event, and treating it as one is the root
of every failure below.

Two independent mechanisms are proven to break it. They are separate defects with separate fixes,
and confusing them has cost the project time.

### 1.1 The transport: a written prompt is not a submitted prompt

`orca terminal send --text <body> --enter` writes the body as raw chunks and then a carriage
return. Codex 0.147.0 covers a large paste with a placeholder, consumes the Enter, and keeps the
body in its composer. The call answers `accepted: true` with a byte count, and the pane is
genuinely idle while holding the composer, so readiness agrees nothing is wrong.

Measured in the controlled A/B (`issue:ac3b55fdac8b330589a1`):

| case | result |
| --- | --- |
| Codex, 6,679 bytes, generic raw send | original Enter plus two resends did not submit; a third did |
| Codex, same body, bracketed-paste framing | submitted on the first Enter |
| Claude, 6,701 bytes, generic raw send | started immediately |

Size is not the whole story: a 843-byte observer wake failed the same way on 2026-08-10. The
boundary is Codex's paste handling, not a byte threshold the product can stay under.

### 1.2 The readiness signal: `tui-idle` can be a false positive

`findCodexReadyPromptIndex` in Orca 1.4.163 scans up to 256 KiB of retained tail and accepts an old
`OpenAI Codex` / `model:` / `directory:` startup banner. That banner stays in the tail, so a pane
reports idle during MCP startup and while visibly `Working` (`issue:033bc902085c6ad13dfb`).

Evidence: synthetic heads differing only by the retained banner — the banner variant reported ready
through 14 working probes while the no-banner variant correctly reported busy; a real Codex head
worked and replied `ACK` through 16 ready probes; a live sprint observer returned satisfied in
under a second from a banner at cursor 1613 with `oldestCursor=0`.

### 1.3 What the two mechanisms do together

A delivery that actually succeeded can be classified as a failure, and in production a failed
launch is followed by `_close_launched_pane`. A healthy working head is therefore destroyed, and
the disappearing pane reads as a spontaneous crash rather than as a cleanup the product initiated.

## 2. Where the evidence lives

- Full investigation: `state/knowledge/investigations/2026-08-10-orca-tui-delivery.md` in the
  instance repository. Two independent investigators (Codex/Sol and Claude/Opus), six experiments,
  both transcripts preserved.
- `issue:13dd4d88df6b33cfb98f` — the original observation: bytes written, prompt unsent. Closed the
  evidence and confirmation seam.
- `issue:ac3b55fdac8b330589a1` — the causal transport seam. Increment 1 of `sprint:915`.
- `issue:033bc902085c6ad13dfb` — do not trust `tui-idle` from a historical banner. Increment 2.
- `issue:06dcf6cb6aacbc38da5f` — one liveness contract for every head. Deliberately out of scope for
  `sprint:915`, and the place a general silence/ping/replacement policy belongs.
- Upstream: `openai/codex#11048` (long paste may need Enter repeatedly), `openai/codex#15355`
  (request for supported local ingress), `stablyai/orca#7466` (closed prompt-loss discussion; it
  does not prove the 1.4.163 generic path is safe).

### Live incidents on 2026-08-10

| time (UTC) | what happened |
| --- | --- |
| 21:36 | sprint:915 observer wake, 843-byte payload, `payload_left_in_composer`, 4 wakes / 4 failures |
| 21:58 | `secretary-1408` blocked by watchdog at 379s idle while its head was in fact working |
| 22:09–22:18 | `codegen-orchestrator-1165` reviewer launch retried 10× over a 12,664-byte prompt, every one `payload-left-in-composer` |
| 22:45 | same card, `review_delivery_failures=24`, worker continuation now failing `turn-observed-but-unconfirmed` at `stage=turn_observed`, replacement worker launched |

Both products were blocked at the same time by the same defect. This is not a secretary-only
problem; `codegen-orchestrator` survived longer only because its worker and observer are Claude
heads, and it stopped at its first Codex head, the reviewer.

## 3. What is already built

### 3.1 The delivery contract (already on main)

`triggered_agents/runtime/tui_delivery.py` is the single path every role uses. It treats delivery as
four separately observed things: payload written, Enter taken, turn observed, caller's criterion
acknowledged. Its central invariant, and the reason a portable protocol is even conceivable:

> The screen is read for evidence, never for content.

Results travel out of band. `secretary check broad` writes a durable receipt under `state/checks/`
keyed by HEAD object id plus a digest of the tracked diff and untracked files, so a scrolled pane
can no longer be mistaken for a suite that never ran, and `RunResult` is the one model every
recorded result is derived from.

### 3.2 The transport (branch `fix/agent-prompt-transport`, not yet merged)

| commit | change |
| --- | --- |
| `96b2543` | `agent_prompt_transport`: bracketed-paste body for Codex, submission as a separate write, both under one per-terminal lock (thread lock plus advisory file lock); ESC and remaining C0 rejected before any write; body bounded at 64 KiB because the public ingress carries it as one argv element and Linux caps a single argument at 128 KiB; body-write-accepted, submit-written and turn-confirmed recorded as three separate facts; receipt persisted on the record for the worker as it already was for the reviewer |
| `2896a3c` | CR normalised to LF before the control-byte policy: a reviewer prompt embeds `task["description"]` verbatim and an HTML textarea submits CRLF, so a card edited in the board's web form would otherwise be a permanent rejection on every retry |
| `a5a306c` | `pane_host`: the three operations delivery needs (`send`, `read`, `wait_idle`) stated as a protocol with `OrcaPaneHost` behind it; the delivery path builds no argument vectors of its own |

Broad suite on `a5a306c`: 2,356 tests, `OK (skipped=8)`, receipt `usable`.

## 4. What the 2026-08-10 canary proved

The branch's own transport was used to deliver a 4,698-byte review prompt to a live Codex head.

- **The transport works.** One framed body, one submit, one turn. The head began reviewing.
- **The confirmation boundary still called it a failure** — `payload-left-in-composer`,
  `resends=2` — and sent two additional Enters to a head that was already working.

Two causes, both belonging to §1.2 rather than to the transport:

1. `_record_probe` marks the payload as left in the composer when the composer fingerprint merely
   *differs* from the pre-send fingerprint. A working Codex head repaints its status line in that
   region, so the fingerprint changes for reasons unrelated to the payload.
2. The turn is accepted on `readiness == BUSY`, or on cursor movement *and* the payload not being
   in the composer. Readiness answered `ready` throughout — the banner false positive — and the
   second disjunct was blocked by cause 1.

**Therefore: Increment 1 alone does not unblock the pipeline.** Merging the transport by itself
would leave `codegen-orchestrator-1165` retrying exactly as it does now, because the production
path closes the pane after this error. Increment 2 is part of the same fix, not a follow-up.

## 5. The larger question: how much of this is Orca

### 5.1 What the dependency actually is

Orca appears in 69 Python files across four distinct subsystems:

1. **Terminal** — `create`, `send`, `read`, `wait --for tui-idle`, `split`, `stop`, `close`. The
   load-bearing one: workers, reviewers (a split in the same worktree) and observers all live here.
2. **Worktree and repo registration** — `host.py` carries "orca repos" as a first-class resource
   kind alongside projects and units, and the workspace path is decided by the binding's
   `orca_binding`, not by the Secretary project id.
3. **Orchestration** — `dispatch`, `task-create`, `check`, `send`, `reply`.
4. **Computer use / browser** — the `colab-run`, `remote-browser` and `google-session-transfer`
   skills. No tmux-shaped replacement exists for this at all.

`orca automations run` was already replaced by the product's own dispatcher
(`triggered_agents/runtime/dispatch.py`), so peeling a subsystem off has precedent.

`docs/ARCHITECTURE.md` already records the boundary honestly: "Launch and cleanup still depend on
Orca's specific API; a target session protocol is a roadmap milestone." Milestone 5 of
`docs/ROADMAP.md` states the goal and leaves the choice open: keep the current session manager, move
to an alternative, fork it, or build a minimal in-house backend.

### 5.2 The argument against a straight replacement

Most of the pain is not Orca-specific. Screen-scraping a TUI to decide whether an agent finished a
turn is fragile wherever it runs: `tmux capture-pane` loses scrolled content the same way, and
`--for tui-idle` would have to be reimplemented as a local quiescence heuristic — the same
heuristic, without another project's bug fixes. Swapping session managers moves this failure class;
it does not remove it.

What removes it is a head that reports through a channel other than the screen. The product has
already moved every *result* out of band (§3.1); what remains on the screen is liveness and
delivery, which is exactly what §1 is about.

### 5.3 The forcing function

The subscription plan is expected to drop `-p` (headless print mode). Two things follow, and they
point in opposite directions from the obvious reading:

- Headless does not disappear; it moves from subscription to API key, i.e. from free to metered.
  The choice becomes a cost policy, not a capability wall.
- Therefore the useful property is being able to choose **per head**: long, expensive worker and
  reviewer heads stay interactive on the subscription; short mechanical heads (curator, retro,
  steward, health ticks) can run headless with a real exit code and none of §1's failure modes.

Today `0b23707` made every head TUI-only. For roughly half of them the TUI buys nothing except the
absence of an alternative in the code.

## 6. Decisions taken

1. **Delivery is a staged contract, not a boolean.** `accepted`/`bytesWritten` is never reported as
   completed delivery. Already on main.
2. **The pane is a liveness and delivery channel only.** Results are durable artefacts read by
   reference. Already on main; this is the invariant that makes everything else portable.
3. **Framing is a protocol, not string concatenation.** One module owns the wire form; roles cannot
   assemble their own. Branch `fix/agent-prompt-transport`.
4. **Raising the Enter retry count is not an accepted fix.** It trades a lost prompt for a
   double-submitted one.
5. **Line endings are normalised; every other control byte is rejected.** Rewriting is confined to
   the one control that carries no meaning, so a rejection can never become a permanent failure for
   ordinary board content.
6. **The delivery path builds no session-manager argument vectors.** `PaneHost` has one
   implementation today; the point is that a second one is a class, not a search.
7. **The seam stops at delivery, deliberately.** Worktree registration, pane creation and teardown
   remain Orca-specific, and the documentation says so rather than implying more portability than
   exists.
8. **`sprint:915` was closed on 2026-08-10 and Increment 1 taken by hand.** The pipeline cannot
   repair its own prompt delivery using prompt delivery; the card that fixes the host is the one
   card that should not be self-hosted. Everything else stays in the pipeline.

## 7. Open holes

Each of these is unresolved. The note after each is what would settle it, not a plan.

1. **Increment 2 is not written.** Bounded reconciliation for contradictory post-send evidence:
   30–60 seconds, no Enter/prompt/ping during it, finite outcomes (confirmed turn, prompt still in
   composer, dead process, `ambiguous-delivery-timeout`), at most one per launch, and a failed head
   closed only by exact retained identity with the product recorded as cleanup initiator. The
   canary in §4 is its acceptance test: that delivery must be classified as success.
2. **Composer classification is wrong, not merely imprecise.** "Fingerprint changed" cannot mean
   "payload still there" while a working head repaints the same region. Needs a positive signal —
   provider status in the current frame, or a paste-placeholder classification — rather than a
   difference.
3. **`turn-observed-but-unconfirmed` is a distinct third failure**, seen on 1165 at 22:45. A turn is
   observed and the caller's criterion still never fires. Not yet diagnosed. It may be the same
   readiness defect on a different branch of the same function, or a criterion that outlives its
   window.
4. **The 0.5s body/submit settling interval is unverified.** The code comments claim it matches
   Orca's private `sendTerminalAgentPrompt` helper. Nobody has checked that against installed
   1.4.163. An unverifiable claim in a comment is worse than no claim.
5. **`_prompt_adapter` defaults to `codex`** when neither the run snapshot nor the head profile
   names an adapter. Safe for the historical TUI-only path; unexamined for a Claude head whose
   record predates the snapshot.
6. **Bracketed-paste bytes travel through `--text` as an argv element.** Nothing has verified what,
   if anything, re-interprets or escapes them between the CLI and the pty.
7. **Whether interactive heads are needed at all for mechanical roles.** §5.3. Deciding this is
   worth more than any single fix in §1, and it is a product decision, not an engineering one.
8. **Session-manager choice remains open** (Milestone 5). The honest way to decide it is a second
   `PaneHost` implementation used to run the same pipeline, so the bug classes can be attributed by
   measurement rather than argument.
9. **Computer use has no migration story.** It is a separate product surface inside Orca and must be
   costed separately from any terminal decision.
10. **Orphaned panes are not accounted for.** A pane with preview `'B'` sat in the 1165 worktree for
    nine minutes with no owner. Small, but it is the shape of the cleanup-attribution problem in
    §1.3.

## 8. Reading order for someone new to this

1. `state/knowledge/investigations/2026-08-10-orca-tui-delivery.md` — the measurements.
2. `triggered_agents/runtime/tui_delivery.py` module docstring — what delivery means here.
3. `docs/ARCHITECTURE.md`, the paragraph beginning "Orca is the current session manager" — where the
   boundary currently sits.
4. `docs/ROADMAP.md` Milestone 5 — what the boundary is meant to become.
5. This document's §4 and §7 — what is known to be still broken.
