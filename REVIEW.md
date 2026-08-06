# Review secretary-1165

# Goal

Мёртвый ресурс головы уводит карточку на живую семью. Пока живых семей нет — честный claim-skip с
записью в тик, а не запуск голов в пустоту и не молчаливое ожидание.

# Источник

`issue:bd7dc1bdadb194f7308b`, вторая половина. Первая половина уже раскатана хотфиксом `b8c425a`:
исчерпанная квота теперь классифицируется как `exhausted`, а не `unknown`, и `launch_allowed` её не
пропускает. Эта карточка про то, что делать дальше.

Наблюдалось на канарейке `sprint:1200`, 2026-08-06 09:42–09:44: подписка openai-sub кончилась
посреди спринта, диспетчер заклеймил `secretary-1161`, голова умерла мгновенно, вотчдог сделал
респавн, второй столл увёл карточку в Blocked. Два запуска и раунд в никуда. Карточку пришлось
вручную переводить на claude-профиль.

# Контекст

Указатели, не копипаста; читай текущее дерево.

- `triggered_agents/agents/pipeline/health.py:resolve_head` уже умеет обходить цепочку фолбэка в
  ширину и возвращать `None` — это и есть claim-skip. Механизм есть; у продуктовых профилей просто
  пустые цепочки.
- Пустые `fallback` у продуктовых профилей в `heads.toml` — осознанное решение от 2026-07-04, и
  причина записана прямо там: ночью 4 июля фолбэк на дешёвый gemini-flash сжёг три попытки воркера,
  ни разу не дойдя до отчёта. Решение принималось против головы заведомо слабее, а не против
  равноценной головы другой семьи. Эта карточка его пересматривает — но не отменяет: качество
  головы в цепочке остаётся требованием.
- Правило «воркер и ревьюер — не одна и та же голова» должно пережить любой перевод. Сейчас, при
  исчерпанном openai-sub, оно и так ослаблено до разницы в усилии (см. `role_defaults` в
  `heads.toml` и записанную там причину) — не ослабляй его дальше.
- Канон голов живёт в `secretary-instance/heads/heads.toml` и материализуется через
  `secretary upgrade`. Если карточка меняет форму цепочек, а не только код — правка канона это
  часть работы, но раскатка на живую установку в неё не входит.

# Acceptance criteria

1. Красный или исчерпанный ресурс предпочитаемой головы приводит к запуску на живой семье
   сопоставимого класса, а не к claim-skip, если такая голова есть.
2. Перевод сохраняет правило «воркер и ревьюер — разные головы». Если единственная живая семья
   даёт для обоих одну и ту же голову, это не перевод, а отказ: claim-skip с причиной.
3. Если живых семей не осталось — claim-skip, карточка ждёт в Ready, и тик пишет, какой ресурс
   мёртв и почему. Ни одного запуска головы в такой ресурс.
4. Слабая голова не подставляется молча: цепочка фолбэка задаётся в каноне явно, и выбор
   не-предпочитаемой головы записывается на карточку так, чтобы ревью и наблюдатель видели, кто
   на самом деле делал работу.
5. Регрессия: ресурс красный, ресурс исчерпан, обе семьи мертвы, перевод ломающий правило разных
   голов. Существующие тесты `resolve_head` остаются зелёными.
6. `python3 -m unittest` зелёный целиком.

# Вне рамок

Раскатывать изменённый канон на живую установку. Трогать классификацию проб — она уже сделана
хотфиксом. Выпиливание exec-режима у codex.


## Mechanical gate attestation

- validated_sha: f7e7818fb3f19e027c8840d01a0027fce4716fbb
- base_sha: a61689c6c67cf5c9a38553318f7fa44045aba8ff
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31109244959/job/92642389345)
- completed_at: 2026-08-06T14:09:33+00:00
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
Write the body to /tmp/secretary-verdict-secretary-1165-3.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1165 --role reviewer --kind green --request-id dispatcher-attempt-20260806T133627Z-de63149988b5-review-green-secretary-1165-3 --body-file /tmp/secretary-verdict-secretary-1165-3.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1165 --role reviewer --kind red --request-id dispatcher-attempt-20260806T133627Z-de63149988b5-review-red-secretary-1165-3 --body-file /tmp/secretary-verdict-secretary-1165-3.md
