# Review secretary-1168

# Goal

Сохранить устойчивую identity панели из ответа `orca terminal create`/`split` и использовать её для worker, reviewer и observer, чтобы alias create-time handle не мог потерять живую голову или направить lifecycle-операцию в другую пану.

# Context

`CommandHostRuntime._create_terminal` и `_split_pane` в `secretary/dispatcher.py` сейчас возвращают только handle и отбрасывают `paneKey`; `_pane_leaf` затем пытается восстановить leaf через inventory по тому же нестабильному handle. Это затрагивает `_settle_worker_pane`, `prepare_worker`, review launch, `dispatcher_pause_ops`, `dispatcher_launch._adopt_launch_intent` и `dispatcher_observer._launch_observer`. `stop_head` и `_split_anchor` также адресуют worker по create-time handle. Живое измерение и исходный issue `issue:41284657c3e732f04f7f` показали, что inventory может не перечислить этот alias при существующей pane, тогда как `paneKey` содержит тот же `leafId`, что и inventory.

# Selected route

Переиспользовать уже возвращаемый `paneKey`: разобрать из него `leafId` на launch boundary, пронести его в launch result/intent и сразу записать в persistent worker, reviewer и observer records. Для адресации находить текущий inventory handle по сохранённому leaf, а handle и PID оставить fallback для legacy/adopted records без leaf. Новых зависимостей, external protocol или migration не вводить: старые реально сохранённые records могут пережить upgrade и обязаны оставаться управляемыми по существующему handle/PID пути.

# Acceptance criteria

- Worker, reviewer и observer получают непустой leaf напрямую из create/split `paneKey`; test fixtures с create-time handle, отсутствующим в inventory, и совпадающим inventory `leafId` доказывают сохранение identity без повторного поиска по handle.
- Лiveness, точечный stop/close и worker split-review anchor сначала разрешают текущую пану по сохранённому leaf и работают, когда inventory показывает только alias; операция для одной worker/reviewer head не заменяется workspace-wide stop.
- Crash/adoption между launch и state commit сохраняет доступную launch identity в intent, а records, у которых исторически нет paneKey/leaf, продолжают fail-closed управляться существующим handle/PID fallback без второго writer.
- Существующие handle-alias и adopted-head tests остаются зелёными; добавить focused sequence tests для worker, reviewer и observer, включая stop/anchor alias path.
- Не менять режимы Codex, routing policy, single-owner security boundary, Orca или жизненный цикл whole-workspace observer, который изолирован отдельным observer workspace.

# Out of scope

Удаление Codex exec-mode, manual Blocked/stop recovery, red-review continuation, lifecycle telemetry и upgrade recovery canon. Эти изменения идут отдельными cuts после этого общего identity foundation.


## Mechanical gate attestation

- validated_sha: d1ede894f726de8dcfd922f4106acee7d6f6b82d
- base_sha: 2a1151a434c44a4588323553895b3b64d8bd9f1b
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31326153418/job/93276744724)
- completed_at: 2026-08-09T17:22:52+00:00
- command_or_check_set_digest: e05e08ff6e9e51da3be176a7b5215dfddd2f768f01036631e8a3c9ab7be723ca

Independently inspect the diff, acceptance criteria and invariants. The attested broad
check above already passed on this exact SHA: do not rerun that broad command or suite on
the same SHA unless you record a concrete `rerun_reason`. A focused reproduction is allowed
for a new blocker, an uncovered external behaviour, or a security/data-loss high-risk need.
Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.

A red verdict must list every blocker you have found in this round. Prefix each with a
stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.
Do not hold blockers back for a later round and do not widen the scope on the next one.

For every RED blocker, state the concrete reachable scenario, the violated acceptance
criterion or operational invariant, material assumptions, whether this branch introduced
the defect or it was pre-existing, and whether the repair appears local or would change
architecture, a compatibility promise, a product contract, or a trust boundary. Report
evidence; do not silently widen the supported boundary or decide sprint scope.

When a change depends on how an external backend behaves, a passing fixture is not
evidence: it can encode the same wrong assumption as the code under review. Say which
real behaviour you verified and how. If no end-to-end check against the real backend
was possible, write plainly that it was not done and which assumption stays unverified.

Post exactly one review verdict through the secretary task protocol:
Write the body to /tmp/secretary-verdict-secretary-1168-10.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1168 --role reviewer --kind green --request-id dispatcher-attempt-20260809T170434Z-b85b4e55a1b7-review-green-secretary-1168-10 --body-file /tmp/secretary-verdict-secretary-1168-10.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1168 --role reviewer --kind red --request-id dispatcher-attempt-20260809T170434Z-b85b4e55a1b7-review-red-secretary-1168-10 --body-file /tmp/secretary-verdict-secretary-1168-10.md
