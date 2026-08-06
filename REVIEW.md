# Review secretary-1164

# Goal

Недоступный гейт перестаёт быть красным. Транспортный сбой при обращении к бэкенду гейта оставляет
карточку ждать и повторяется, а в Blocked уводит только исчерпание попыток — с причиной, называющей
транспорт.

# Источник

`issue:f2977ff8b6175b7202b0`. Наблюдалось на канарейке `sprint:1200`, 2026-08-06 10:55:47, карточка
`secretary-1161`:

```
merge gate failed: gate gh api failed:
  Get "https://api.github.com/repos/vladmesh/secretary/commits/d9b1ca7.../check-runs":
  net/http: TLS handshake timeout
```

Ревью было зелёным, обязательный чек на точном SHA успешным (CI run 31094285685), работа принята —
и карточка ушла в Blocked из-за моргнувшей сети. Ни одной повторной попытки. Вытащил её наблюдатель:
сам сходил в GitHub API, подтвердил чек на точном SHA и решил release, явно отказавшись
перезапускать широкий прогон. Полагаться на сообразительность головы здесь нельзя.

# Контекст

Указатели, не копипаста; читай текущее дерево.

- Вопрос не был задан и ответа нет. Трактовать отсутствие ответа как отрицательный ответ неверно:
  это ровно та же ошибка, что `unknown`-статус ресурса, который считался пригодным к запуску
  (починено хотфиксом `b8c425a`), и та же, что занятая пана, читаемая как провал запуска
  (соседняя карточка этого спринта).
- Ищи в `secretary/`, где диспетчер спрашивает бэкенд гейта и где `gate gh api failed` становится
  решением по карточке. Путей может быть несколько — предрелизная расписка и мердж; закрыть надо
  все, где спрашивается бэкенд.
- Полученный ответ с проваленным чеком — это красный гейт, и он продолжает работать как сейчас.
  Различие проводится по тому, получен ли ответ, а не по тому, понравился ли он.
- Форма отложенной повторной попытки уже есть в контуре — посмотри, как это сделано у наблюдателя,
  и не изобретай третью.

# Acceptance criteria

1. Транспортный сбой (таймаут, TLS, DNS, обрыв соединения, 5xx от самого бэкенда) не меняет
   состояние карточки. Попытка повторяется на следующем тике.
2. Число попыток ограничено. После исчерпания карточка уходит в Blocked, и причина называет
   транспорт и последнюю ошибку, а не «gate failed».
3. Каждая повторная попытка видна в выводе тика.
4. Красный гейт — то есть полученный ответ с проваленным обязательным чеком — работает как прежде:
   карточка возвращается на доработку, и это покрыто существующими тестами, которые остаются
   зелёными без правок.
5. Регрессия на оба случая, транспорт и красный ответ, на каждом пути, где диспетчер спрашивает
   бэкенд гейта.
6. `python3 -m unittest` зелёный целиком.

# Вне рамок

Менять формат расписки гейта и правило «релиз принимается только по расписке на точном SHA».
Трогать сам механизм мерджа.


## Mechanical gate attestation

- validated_sha: 0aa68325fb737b15421a678b73e26cd0d6629a1b
- base_sha: 8c9fdce580f962f2bc2add95a5e640ef1a4e7548
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31100867040/job/92613833784)
- completed_at: 2026-08-06T12:20:57+00:00
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
Write the body to /tmp/secretary-verdict-secretary-1164-2.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1164 --role reviewer --kind green --request-id dispatcher-attempt-20260806T120508Z-ee9a9e4e2d1b-review-green-secretary-1164-2 --body-file /tmp/secretary-verdict-secretary-1164-2.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1164 --role reviewer --kind red --request-id dispatcher-attempt-20260806T120508Z-ee9a9e4e2d1b-review-red-secretary-1164-2 --body-file /tmp/secretary-verdict-secretary-1164-2.md
