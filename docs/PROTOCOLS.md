# Протоколы secretary

`--instance` принимает каталог instance или прямой путь к `instance.yaml`. Instance задаёт
конфигурацию установки и хранит переносимый checkpoint в `state/`; `secretary-data` хранит
локальное mutable/derived runtime state.

## Проверка и host ownership

```bash
python3 -m secretary doctor --instance INSTANCE
python3 -m secretary doctor --offline --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE --host-fixture DIR
```

`doctor` всегда read-only. Обычный запуск проверяет config, data и live inventory; `--offline`
оставляет только config/data, а `--host-fixture` заменяет live inventory детерминированным fixture.
Fixture нельзя сочетать с `--offline`. Exit code `0` означает завершённую проверку без findings,
`1` означает findings или warnings с `--strict`, `2` означает невалидный input либо недоступный
inventory. Без `--strict` warnings сами по себе остаются зелёными.

Live parity выводится из того же desired state, что и `reconcile`: каждый project checkout
сверяется по нормализованному полному пути из binding, включая путь вне `projects_root`; сам
`projects_root` нужен для поиска неуправляемых checkout. Недоступный или не нормализуемый expected
checkout делает inventory projects unavailable и даёт code `2`, а не `missing-on-host`. Проверяются unit-файлы, Orca registration
и требуемое enabled/active состояние долгоживущих service и timer. Отсутствующий ресурс или
нездоровый required runtime state даёт finding и code `1`; oneshot service может быть inactive.
`foreign_units` исключены из managed parity и не считаются conflict. В fixture полный путь checkout
задаётся строкой в `projects.txt`; старый каталог `projects/<name>` означает checkout именно под
корнем fixture, а не binding с таким же basename.

```bash
python3 -m secretary reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -m secretary reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` читает desired state и inventory, ничего не применяет и не пишет manifest.
`--offline` намеренно отклоняется. Code `0` означает plan без conflict, `1` означает conflict,
`2` означает невалидный input или недоступный inventory.

`reconcile adopt` касается одного существующего desired Orca registration. Он сверяет имя и
нормализованный repo path, показывает fingerprint и без `--yes` остаётся preview. Подтверждённый
запуск атомарно добавляет managed record, не меняя Orca, systemd или worktree. Unit resources этим
путём не adopt'ятся.

## Задачи

Публичный путь к доске проходит через `secretary task`. Карточка содержит `ref`, project, type,
state, dependency, claim, routing, workspace, retry и audit metadata:

```text
ideas → ready → in_progress → validate → done
                         └────────────→ blocked
```

```bash
python3 -m secretary task list --project PROJECT
python3 -m secretary task show --ref PROJECT-N
python3 -m secretary task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-extra --codex-mode exec
python3 -m secretary task archive --role po --ref PROJECT-N \
  --reason-file REASON.md --request-id REQUEST_ID
python3 -m secretary task edit --role po --ref PROJECT-N \
  --body-file SPEC.md --head codex-terra --review-head claude-opus
```

`create` принимает `--description` или `--body-file`, dependency, workspace и routing fields.
Worker, reviewer и retro могут создавать только Ideas; PO выбирает Ready. `--codex-mode` допустим
только для worker profile с adapter `codex`. Без override launch mode берётся из head profile.

`archive` закрывает карточку в backend и убирает её из обычных active list/export без удаления
истории Kanboard. Операция только для PO, требует непустую причину, пишет append-only audit и
поддерживает идемпотентный retry/reconcile через `--request-id`. Архивировать можно только карточку
без live work: In progress/Validate и карточки с активным claim отклоняются. Закрытая карточка из
Done остаётся выполненной зависимостью; закрытая карточка из другой колонки не считается Done и не
разблокирует `blocked_by`.

`edit` заменяет спеку карточки на месте: `--title`, `--description`/`--body-file` (полный новый
текст, не дифф), `--head`, `--review-head`. Только для PO и только в Ideas/Ready/Blocked: у
активной карточки воркер работает со снапшотом TASK.md, поэтому правка на лету идёт через
preempt/requeue, а не через тихую подмену. Audit event `edited` хранит старый и новый digest;
полный текст прошлых версий восстанавливается из git-истории `state/board/cards.ndjson` в
checkpoint. Комменты остаются диалогом попытки, спека живёт только в description.

Все write-команды проходят role guards и transition checks. Mutation сначала получает
append-only pending audit event, затем сверяется с live board и только после этого считается
committed. Unresolved pending write блокирует согласованный export и recovery checkpoint до
`reconcile-audit`.

`report --kind done` перед любой записью проверяет `git status --porcelain` воркспейса воркера
(CWD процесса) и отказывает с `uncommitted`, если там есть незакоммиченные изменения: воркер
чинит это в своей же сессии, а не узнаёт постфактум из blocked. Untracked runtime tail
(`secretary-data/`) не считается за грязь, `--kind blocked` не гейтится (WIP допустим), а
пост-фактум чек диспетчера остаётся как defense-in-depth.

Диспетчер также запоминает SHA, отклонённый механическим гейтом или красным ревью в текущей
попытке. `done` на том же SHA не идёт в Validate: первый такой отчёт возвращает воркера к
доработке в том же воркспейсе с требованием нового коммита. Второй переводит карточку в Blocked,
чтобы не крутить бесконечный rework-цикл. Если код осознанно не меняется, например дефект в тесте
или самом гейте, воркер сообщает `report --kind blocked` с разбором вместо повторного `done`.

Audit trail всегда пишется в data dir установки: `--data-dir`, иначе `SECRETARY_DATA_DIR`, иначе
`data_dir` из instance config (`--instance` / `SECRETARY_INSTANCE`). Относительный `data_dir`
резолвится от instance file, не от CWD, поэтому вызов из воркспейса чужого проекта не оставляет
там `secretary-data/`. Если data dir не резолвится, команда падает с usage error вместо записи
рядом с процессом.

### Routing-телеметрия попыток

Карточка не хранит историю роутинга: `resolved_review_head` стирается при уходе из Validate, а весь
routing-блок сбрасывается при возврате в Ready. Поэтому «кто был воркером и кто ревьюером на попытке
N» живёт только в append-only журнале, событиями `kind: "routing"`. Пишет их диспетчер, backend при
этом не трогается: у события нет mutation, только запись в `events.ndjson` через обычный
pending/commit путь, идемпотентная по `request_id`.

Попытка (round) — это один подъём воркера плюс заработанное им ревью. Claim открывает попытку 1,
каждый bounce на доработку (red-вердикт, red gate) открывает следующую; respawn, resume после паузы
и перезапуск после отклонённого SHA остаются в своей попытке. Возврат в Ready и повторный claim
добавляют попытку, а не затирают предыдущую: номер берётся из журнала, а не из dispatcher state,
поэтому переживает и потерю record, и restore.

```json
{"kind": "routing", "ref": "PROJECT-N", "payload": {
  "attempt": 2, "attempt_id": "...", "phase": "verdict", "outcome": "red",
  "heads": [{"role": "worker", "head": "codex", "head_source": "card",
             "adapter": "codex", "model": "gpt-5.6-terra", "model_source": "profile",
             "effort": "default", "codex_mode": "exec",
             "resource": "openai-sub", "account": "openai-subscription"}]}}
```

`phase` — `worker` (подъём воркера), `review` (подъём ревьюера), `verdict` (исход попытки, несёт обе
головы), так что пары «воркер-ревьюер» группируются по исходу без join. `outcome` вердикта — `green`
или `red` от ревьюера; bounce механического гейта закрывает попытку своим значением
(`gate_red`, `merge-gate_red`, `review-freeze_red`), чтобы возврат на доработку не приписывался тому,
кто её ревьюил. Если ревьюер уже выдал green, а merge-гейт вернул карточку, в журнале остаются оба
события: `attempts()` показывает последний исход попытки, сам green из журнала не исчезает.

Фактическая голова совпадает с запрошенной. Решение принимается один раз, при claim, из override
карточки или `role_defaults`, и механизма подмены в момент запуска нет: ни health-проверок, ни
fallback-цепочек в диспетчере не существует, а `resolve`/fallback вынесены в отдельную карточку.
Поэтому запись несёт одну голову на роль и `head_source` — откуда взялся её id: `card` (override
карточки), `role_default`, `record` (голова, зафиксированная в dispatcher record карточки,
заклеймленной раньше).

Имя профиля не является историческим ключом: `codex`, `codex-terra`, `codex-high` и `codex-extra` —
одна модель с разным effort, `claude-default` вообще не пинит модель, профили перепиниваются. Поэтому
каждая голова несёт конфигурацию запуска целиком, снятую в момент подъёма и больше не перечитываемую
из `heads.toml`. Снимок делает сам bring-up: `CommandHostRuntime._launch` отдаёт
`LaunchedHead(handle, head, run)`, `prepare_worker`, `restart_worker` и `start_review` пробрасывают
это наверх, диспетчер пишет `run` в журнал как есть. Перечитывание реестра остаётся только для
adopted-карточки, чей подъём случился в прошлой жизни диспетчера.

`model_source` говорит, откуда взялась модель, и `model` пустой только тогда, когда источник это
прямо называет. Профиль без `model` (`claude-default`) запускается как `claude` без `--model`, и
модель выбирает сам CLI; в момент подъёма читаются те же источники и в том же порядке, что у CLI:
`managed_settings`, `profile` (то есть `--model`), `env:ANTHROPIC_MODEL`, `project_settings_local`,
`project_settings`, `user_settings`. Если модель не запинена нигде, значение остаётся пустым под
источником `cli_default` — «выбрано рантаймом», а не молчаливый пропуск. Конструктор `HeadRun`
отвергает пустую модель под любым другим источником.

Каждый подъём внутри попытки пишет своё событие: respawn после молчания, перезапуск после паузы,
rework. `request_id` включает дайджест конфигурации, так что повторный подъём той же головы
коммитится один раз, а подъём на другой adapter/model/effort/resource добавляет второе событие и
заменяет активную голову попытки — вердикт всегда несёт ту голову, которая его заработала.
`attempts()` отдаёт все подъёмы попытки в `worker_runs` / `reviewer_runs`, а `worker` / `reviewer` —
те, что относятся к вердикту.

Читающая сторона — `secretary.routing_journal.attempts(events, ref)`: последовательность попыток
завершённой карточки с головами и исходом. События попадают в recovery checkpoint вместе с остальным
`events.ndjson` и восстанавливаются при materialize.


## Production dispatcher

Production runtime запускается одним tick или постоянным loop:

```bash
python3 -m secretary dispatcher production-tick --instance INSTANCE
python3 -m secretary dispatcher production-observe --instance INSTANCE
python3 -m secretary dispatcher production-run --instance INSTANCE
```

Systemd timer использует one-shot `production-tick`. Runtime обрабатывает только поддержанные
task transitions, сохраняет claim/review state и сверяет live board перед recovery. Production
owner записан в dispatcher state; несовпадение owner, dirty workspace, missing report или
неразрешённый audit state останавливают переход вместо silent fallback.

Старые pilot/cutover subcommands остаются compatibility recovery surface текущей версии, но не
являются путём установки нового instance.

## Пауза

Пауза общая для пайплайна и живёт поверх продуктового диспетчера:

```bash
python3 -m secretary pause drain|freeze --instance INSTANCE --reason "почему"
python3 -m secretary resume --instance INSTANCE
python3 -m secretary pause-status --instance INSTANCE
```

Контракт режимов: `drain` останавливает claim новых карточек и диспатч фоновых ролей, но уже
идущие карточки доезжают цикл; `freeze` дополнительно останавливает живые головы воркера и
ревьюера (`stop`, никогда `teardown`) и замораживает тик целиком — ничего не продвигается и ни
один вотчдог не срабатывает на голову, остановленную намеренно. `resume` поднимает остановленные
головы в тех же воркспейсах, отдаёт карточку с уже поставленным отчётом ближайшему тику и
перезапускает окна вотчдогов.

Флаг: `<data_dir>/dispatcher/pause.json`, читается каждым `production-tick`. Зеркало для фоновых
ролей — легаси `state/pipeline/pause.json`; его пишет и снимает та же команда. Легаси-вход
`triggered_agents pipeline pause|resume` отказывает и указывает на продуктовую команду.

Оператор при freeze может исключить свой собственный воркспейс (`--exclude-workspace`): этим
пользуется `secretary backup create`, который замораживает пайплайн из воркера.

Freeze, поставленный автоматикой из allowlist `TA_HARD_PAUSE_AUTO_RESUME_ACTORS`, истекает через
`TA_HARD_PAUSE_AUTO_RESUME_TTL_S` (по умолчанию 45 минут): тик проверяет это до freeze-skip и
снимает паузу продуктовым `resume` под тем же tick lock. Freeze от человека держится до явного
`resume`. Замороженный тик карточки не двигает, но checkpoint пишет и пушит.

## Подключение проекта

Текущий низкоуровневый onboarding состоит из стадий:

```bash
python3 -m secretary project add ...
python3 -m secretary project provision-start ...
python3 -m secretary project provision-apply ...
python3 -m secretary project gate ...
```

Identity проекта задаётся один раз top-level binding. Scanner и provision готовят изменения, но не
включают binding. Enable разрешён только через успешный gate, привязанный к проверенным revision,
provision run и write-set. Верхнеуровневый resumable workflow остаётся milestone Roadmap.

## Память

Facts лежат flat в `memory/facts/global/<slug>.md` или
`memory/facts/<project-dir>/<slug>.md`. Один факт является одним дистиллированным markdown record.
Куратор остаётся writer-ролью, остальные агенты читают через `memory_search`, `memory_get` и
`memory_list`.

```bash
python3 -m secretary memory verify --instance INSTANCE
python3 -m secretary memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -m secretary memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -m secretary memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -m secretary memory reindex --instance INSTANCE
```

Writer operations требуют actor и проходят journal protocol; прямые edits минуют audit trail.
`reindex` меняет только derived index и не должен пересекаться с другим index writer. Model и
dimension берутся из instance configuration.

## Knowledge

Длинные восстановимые документы (брейнштормы, журналы решений, разборы инцидентов) лежат в
`state/knowledge/<раздел>/<документ>.md`. Разделение с curated memory и Pipeline описано в
[Архитектуре](ARCHITECTURE.md#плоскости-знания).

```bash
python3 -m secretary knowledge write --instance INSTANCE --actor ACTOR \
  --path decisions/2026-07-25-sprint-1.md --file DOC.md
python3 -m secretary knowledge list --instance INSTANCE
```

`write` перезаписывает документ целиком и коммитит только `state/knowledge` под общим writer lock,
поэтому ручной `git commit` в instance-репозитории не нужен и с тиковым писателем не гоняется.
Документ с секретом отклоняется с кодом 2, и на диск ничего не попадает. Повторная запись того же
содержимого возвращает `changed: false` и нового коммита не делает.

Data-plane, archive restore и unit runbooks находятся в [Operations](OPERATIONS.md).
