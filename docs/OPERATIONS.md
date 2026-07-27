# Эксплуатация

Документ описывает поведение продукта. Состояние конкретной установки — кто владеет юнитами,
какие компоненты подняты, свежий ли checkpoint — читается из `secretary status` и
`secretary doctor`, а не из этого файла.

## Установка и проверка кода

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m unittest
```

Первый вариант ставит CLI, второй добавляет memory runtime. Bundled package transport Kanboard/Orca
остаётся decision gate первого milestone; готовый runtime применяется через `secretary install` /
`secretary recover` по [Recovery](RECOVERY.md).

Для текущей сводки установки использовать `secretary status --instance <dir>`. Его `--json`
даёт стабильный снимок services/timers, активных попыток, checkpoint, памяти и ресурсов хоста,
без записи состояния. `doctor` отвечает на другой вопрос: какие инварианты нарушены. Он остаётся
строгой проверкой и его `--json` возвращает структурированный список findings. Для изменения
хоста по-прежнему нужен `reconcile plan` и отдельное подтверждённое применение.

## Runtime secrets

Секреты установки живут в восстановимом хранилище (`secretary secret init/set/import`, каталог
`secrets/` приватного репозитория) и материализуются оттуда в env-файлы. `runtime.env` рядом с
`instance.yaml` может быть одной из таких целей: тогда канон значений лежит в хранилище, а файл
является materialized копией. Переведена ли конкретная установка на materialization, показывает
`secretary status --json`, секция `secret_store`, поле `materialize`; продукт делает это не сам,
шаг выполняет оператор командой `secret import`. Сам файл в любом случае `0600`, в `.gitignore`
приватного репозитория и не входит в checkpoint или archive payload. `secretary shell` получает
весь файл для trusted operator-сессии, dispatcher-launched worker/reviewer получают только
allowlisted board credentials и non-secret runtime switches через `secretary.role_env`.
Хранилище не обещает worker isolation:
у него нет своего broker или grants, и installation key открывает все секреты сразу, теми же
правами, что и раньше читали `runtime.env` (см. [Recovery](RECOVERY.md), раздел «Секреты»).

`secretary status --json` показывает состояние хранилища секцией `secret_store` (инициализировано,
число секретов, время последнего изменения каталога, пригодность installation key, сводка целей
материализации), без единого значения. `doctor` даёт finding на рассинхрон каталога и values, на
отсутствующий или непригодный ключ при непустом каталоге и на права ключа шире `0600`.

Instance config не содержит secret materialization inputs. `reconcile` строит host plan из
bindings/config и не расшифровывает secret store.

### Контракт тест-дублей диспетчера

`tests/test_dispatcher_contracts.py` держит `FakeHost`/`FakeCatalog`/`FakeKanboard` в контракте с
`CommandHostRuntime`/`InstanceCatalog`/`KanboardClient`. Набор методов, которые дёргает
`DispatcherRuntime`, вычисляется из исходников (AST), а не ведётся руками, поэтому новый вызов у
реального host автоматически становится требованием к фейку. Формы возвратов сверяются прогоном
реального host в `mode="noop"`.

Вне unit-покрытия остаётся всё, что требует живого стека: сам shell-out в `orca`, `gh` и `git`
внутри `CommandHostRuntime._run*`, отказы Kanboard-транспорта (`TaskError`) в середине board-move
и реальная нумерация позиций карточек. Эти швы проверяет оператор на живом стенде.

## System requirements

Memory runtime загружает локальную embedding model. На production cutover startup занимал около
шести минут и достигал примерно 1.9 GiB RSS. Отдельный target с 1.9 GiB общей RAM не смог завершить
live rebuild. Поддерживаемый minimum ещё не установлен; не считать 2 GiB profile доказанным.

Memory model cache lives at `DATA_DIR/memory/fastembed-cache`, never in `/tmp`. The memory unit
passes this path directly to fastembed, so it survives `systemd-tmpfiles-clean`. `host.memory_threads`
sets the ONNX Runtime inference limit; its default is `1`, because this host has three cores and a
single semantic search otherwise expands across all of them while the dispatcher still ticks every
minute. `secretary doctor --instance INSTANCE` prints the cache path and warns when `data_dir` puts
it below `/tmp` or `/var/tmp`.

To move a live cache without downloading the 2.1 GB model again, stop the service and copy
`/tmp/fastembed_cache` into `DATA_DIR/memory/fastembed-cache`. If a previous restore created
`DATA_DIR/memory/.fastembed-cache`, merge it into that same destination before deleting it. Then
run `secretary reconcile apply --instance INSTANCE` and start `secretary-memory.service`. Verify
the cache path with `secretary doctor --instance INSTANCE` before removing either old cache. The
service must remain stopped during the copy so fastembed cannot create a partial second cache.
Treat this migration as a required deployment step before the first restart with this release.

Orca runtime принадлежит хосту. Secretary не создаёт `secretary-orca.service` и не запускает
`orca serve`: scheduler units имеют только `After=orca-server.service`, без `Wants=` на runtime,
поэтому минутный dispatcher tick не может его перезапустить. `packaging/systemd` не содержит
`secretary-orca.*`, и `reconcile`/`resolve_packaged` больше не проверяют наличие Orca-исполняемого
файла: тот check был мёртвым (никакой packaged unit никогда не имел `component == "orca"`) и удалён
в secretary-756.

`secretary doctor` показывает `orca-server.service` как external, not managed by Secretary, и
отличает отсутствующий сервис от неактивного. На реальном systemd (проверено на 255)
`systemctl is-enabled`/`is-active` для никогда не установленного unit'а не падают и не молчат —
они выходят с ненулевым кодом и печатают в stdout `not-found`/`inactive`. Поэтому `status --json`
видит `host.external_runtime.enabled == "not-found"` как обычное (не-null) значение, а
человекочитаемый `doctor` печатает это состояние как `Orca runtime: absent (external, not managed
by Secretary)`, а не сырой токен `not-found`.

При миграции старый `secretary-orca.service` и его временный drop-in нужно удалить через обычный
systemd change после того, как `orca-server.service` подтверждён active; сам `orca-server.service`
не останавливать и не перезапускать.

## Data plane

```bash
python3 -m secretary data init --instance INSTANCE
python3 -m secretary data export --instance INSTANCE [--copy-transcripts]
python3 -m secretary data raw-kanboard-dump --instance INSTANCE \
  [--container cp-kanboard] [--source-path /var/www/app/data]
```

`data init` создаёт локальный layout и manifest. Канон memory facts находится в
`INSTANCE/state/memory/facts`; в data dir остаются его derived export/index. `data export` пишет
нормализованные board, memory, run и transcript exports; без `--copy-transcripts` сохраняется только
transcript inventory. `raw-kanboard-dump` создаёт timestamped raw dump через `docker cp`, не пишет
в live container и не использует Kanboard API.

## Checkpoint writer

Каждый production-тик под `tick_lock` в конце регенерирует board и runs exports, проверяет
снапшот и коммитит `state/board` и `state/runs` в приватный репозиторий инстанса
(контракт — `docs/RECOVERY.md`). Стейджится только этот pathspec, ручные правки конфига
коммит не затрагивает. Гейт fail-closed: pending task audit, расхождение счётчиков
`export.json` с числом строк или найденный секрет блокируют коммит, причина уходит в
`checkpoint` в state диспетчера, следующий тик ретраит. Без изменений `state/` коммита нет.

Board регенерируется одним `pipeline export`: доска целиком за один вызов, метаданные и
комментарии всех карточек — одним batched JSON-RPC запросом. Экспорт на 200 карточек занимает
около секунды, так что тик остаётся 60-секундным.

Memory writer независимо коммитит `state/memory` при `propose/commit/supersede`. Его pathspec не
пересекается с tick-writer, а общий instance-repo lock сериализует оба writer'а и publish reviewed
изменений instance repo.

## Status and doctor

`secretary status --json --instance INSTANCE` is the read-only operational snapshot. It is safe
to poll: it reports managed services and timers, projects and configured heads, active dispatcher
attempts, their workspace, watchdog pane/progress/respawn state, sprint observer heads, pause state, checkpoint freshness,
memory index state and host disk, memory and load. A live invocation uses the dispatcher's own pane
probe for watchdog liveness; `--offline` deliberately reports that liveness as unprobed.

`secretary doctor --json --instance INSTANCE` evaluates invariants over the same snapshot and
returns structured findings with a non-zero exit status for a broken or unavailable host. Use
`status` to answer what is running now, and `doctor` to decide what needs repair. The default
human-readable `doctor` output remains available for incident work.

Когда включён `host.components.dispatcher-production`, Orca repo
`<data_dir>/dispatcher/observer-root/observers` принадлежит установке. Он появляется лениво при
первом запуске наблюдателя, поэтому fresh installation не получает finding на его отсутствие.
После создания doctor сопоставляет регистрацию с этим путём и считает совпавшее имя по другому пути
чужой регистрацией. `reconcile plan` и `reconcile apply` этот repo не создают, не удаляют и не
записывают в managed manifest.

## Record reconciliation and controlled divergences

Каждый production tick, до того как продвигать активные карточки, сверяет свои записи
(`production-state.json` `records`) с реальным состоянием доски. `_advance_active` смотрит только
на карточки, которые доска сейчас называет `in_progress`/`validate`: запись про карточку, которую
PO увёл из цикла напрямую (в `ideas`, `ready`, `blocked`, `done`, или удалил вовсе), этому циклу
никогда не попадётся на глаза. Реконсиляция закрывает именно этот разрыв: она отдельно проходит по
всем записям, чьей карточки нет среди активных, спрашивает у доски её текущее состояние и, если
карточка действительно вне цикла, убирает запись. Тик сообщает об этом действием
`{"step": "production-reconcile", "action": "record-removed", "ref": ..., "card_state": ...}`.
Реконсиляция трогает только bookkeeping: workspace и terminal, которые вела запись, не
останавливаются и не удаляются — они принадлежат PO ровно так же, как принадлежала карточка, и
разбираться с ними (или воскрешать карточку обратно) остаётся его решением. Если запрос к доске
временно недоступен, запись не трогается до следующего тика: реконсиляция не рискует принять сбой
бэкенда за уход карточки из цикла.

Список активных карточек, по которому реконсиляция решает, чья запись — кандидат на удаление, это
снимок, снятый в начале тика (`in_progress`/`validate` на момент `list()`). Между этим снимком и
собственным обращением реконсиляции к доске PO может успеть вернуть карточку обратно в цикл: снимок
не входит в него, а карточка уже снова активна. Поэтому отсутствие в снимке — это только повод
посмотреть, а не основание удалять: непосредственно перед удалением записи (или закрытием привязанной
к ней divergence) реконсиляция ещё раз спрашивает у доски текущее состояние именно этой карточки и
пропускает её, если оно оказалось `in_progress`/`validate`.

Controlled divergence — сигнал о расхождении между тем, что диспетчер ожидал от доски, и тем, что
она вернула (`active_claim_mismatch`, `claim_live_mismatch` и подобные), записанный в
`controlled_divergences` через `dispatcher_state.record_divergence`. Жизненный цикл:

- **Открытие.** Divergence создаётся с `"status": "open"` в момент обнаружения расхождения, вместе
  с `expected`/`actual`/`details` — тем, что нужно для разбора.
- **Наблюдение.** Пока карточка divergence остаётся в активном цикле (`in_progress`/`validate`),
  запись остаётся открытой: `status --json` и `doctor --json` показывают её в
  `dispatcher.divergences.open`, `doctor` поднимает finding `unresolved controlled divergence`
  (виден и в `--offline`, потому что читается прямо из снапшота состояния, без похода на хост).
- **Закрытие.** Тот же проход реконсиляции, что убирает осиротевшие записи, закрывает и
  divergence: как только её карточка больше не входит в активный цикл — неважно, в каком
  состоянии она оказалась, — divergence получает `"status": "closed"`, `closed_at` и
  `closed_reason`. Divergence, привязанная к terminal/non-active карточке, поэтому не остаётся
  открытой бесконечно: secretary-716 (запись и divergence, пережившие уход карточки в Ideas на
  шесть дней) была ровно этим отсутствием закрывающего правила.

`status --json`/`doctor --json` дают явную, не-null картину: `dispatcher.divergences` —
`open_count`, `total_count` и список открытых с `pilot_ref`/`reason`/`opened_at`;
`dispatcher.reconciliation` — `records_tracked`, `last_tick_finished_at` (штампуется каждым тиком
независимо от версии диспетчера — не доказательство того, что реконсиляция вообще существует в
установленном коде) и `last_reconciled_at` (штампуется только самим проходом реконсиляции; `null`,
пока хост не протикал хотя бы раз на коде с реконсиляцией — честное "неизвестно" вместо заимствования
чужого поля как доказательства).

Отдельно от парности unit-ownership (`host.units`, `doctor` missing/unmanaged-on-host):
`host.external_runtime` — состояние `orca-server.service`, хостового рантайма, которым Secretary не
владеет, но от которого зависит планировщик (`{"name", "enabled", "active"}`, не-null при доступном
хосте). Одноразовые (`Type=oneshot`) unit'ы, запускаемые своим таймером (например,
`secretary-dispatcher-production.service`), не имеют `[Install]`-секции и активны только вокруг
самого запуска — от них не требуется ни `enabled`, ни `active`, но их состояние всё равно
опрашивается и попадает в `host.units`, а не остаётся `null` как для unit'а, который никогда не
проверяли.

## Рождение спринта

Спринт заводит человек через интерактивного секретаря; сам спринт при этом рождается как сущность
на board `Secretary sprints`, а не как документ. Подготовку задаёт ролевой скилл секретаря
`open-sprint` (канон — `skills/roles/secretary/open-sprint/SKILL.md`, в шеллы едет обычным
`secretary role-skills sync`). Он лежит и в claude-, и в codex-цели роли секретаря, поэтому
поведение не зависит от того, какого секретаря открыли.

Скилл ведёт секретаря по подготовке: живой контекст (открытые и закрытые спринты, deferred из их
resume-записей и комментариев, roadmap, Ideas затронутых репозиториев), проверка, что нужные
репозитории не удерживает другой открытый спринт, гриллинг по нерешённым продуктовым развилкам и
формулировка Definition of Done проверяемыми пунктами. Выбор цели остаётся за человеком и не
делегируется.

Сущность создаётся продуктовой командой; роль `po`:

```bash
python3 -m secretary sprint create --role po --actor <actor> \
  --goal "<одно предложение>" --dod-file /tmp/dod.md \
  --repository secretary --repository secretary-instance
python3 -m secretary sprint show --ref sprint:<ID>
python3 -m secretary sprint status --ref sprint:<ID>
```

Дальше спринт руками не ведут: голову-наблюдателя поднимает production tick (см. ниже), общение с
идущим спринтом идёт записями к сущности (`secretary sprint comment`), а статус читается из данных
(`secretary sprint status`, `secretary task list --sprint`). `STATUS.md` для этого контура не
пишется: состояние спринта лежит там же, где карточки.

Сущность спринта входит в checkpoint отдельным набором (`state/board/sprints.ndjson`) и
восстанавливается вместе с карточками: после recovery спринт возвращается со всеми полями и
записями, заводить его заново не нужно. Контракт — [Recovery](RECOVERY.md#состав-checkpoint).

Разделение хранилищ: цель, текст Definition of Done, репозитории, статус, бюджет, текущая карточка
и resume — поля сущности; документ в `secretary-instance/state/knowledge/` держит только «почему»
(контекст момента, выбор цели, отвергнутые альтернативы) и указатель `sprint:<ID>`. Поля сущности
документ не дублирует.

Старый контур остаётся рядом и не переписан: скиллы `start-sprint` и `run-sprint` ведут спринт как
документ в `state/knowledge/sprints/` с указателем `STATUS.md` и исполнением интерактивным
секретарём. Сам Secretary до отдельного инкремента ведётся им, поэтому двойной контур сейчас
сознательный. Один спринт живёт ровно в одном контуре.

## Головы-наблюдатели спринтов

Тот же production tick, тем же проходом реконсиляции, держит по одной голове-наблюдателю на каждый
открытый спринт с board `Secretary sprints`. Наблюдатель не участвует в claim карточек: он не
занимает project slot, не появляется в `records` и на очередь Ready не влияет.

Пока sprint открыт, его repositories принадлежат этой голове как единственному product writer:
observer создаёт только связанные с ним карточки и ведёт их изменениями на доске. Dispatcher
сохраняет штатный цикл уже связанных карточек. Если оператору нужно вмешаться, PO передаёт
`--sprint-override` и непустой `--sprint-override-reason-file` в `secretary task create`, `move`
или `edit`; причина остаётся в durable audit. Отказ `sprint_write_forbidden` называет sprint и
предлагает записать изменение к его сущности. `sprint_guard_unavailable` означает, что live sprint
board не удалось проверить, поэтому запись намеренно не прошла.

Перед запуском production tick сверяет budget audit связанных карточек. При
`sprint_budget.signal` в prompt наблюдателя попадает отметка о достигнутом пороге, а скилл роли
велит на ней пересмотреть вектор и записать пересмотр resume-записью. При
`sprint_budget.hard` sprint становится `stopped`: head штатно останавливается, новые связанные Ready
карточки пропускаются, а активные карточки остаются в обычном цикле. Оператор проверяет это через
`secretary status --json`: `installation.sprints.items` показывает статус, причину hard-остановки,
разбивку бюджета, resume freshness и состояние наблюдателя каждого спринта. Недоступность board видна в
`installation.sprints.error`. Детали одного спринта доступны через `secretary sprint status --ref
sprint:ID`. Переход в stopped остаётся в audit как `budget_hard_stopped` с причиной
`budget_hard_limit`. Продолжить остановленный sprint может только `secretary sprint reopen --role po`.

Решение тика на спринт видно в `actions` под `{"step": "observer-reconcile"}`:

- `observer-launched` — открытый спринт без записи получил голову;
- `observer-live` — голова жива, тик ничего не делал;
- `observer-relaunched` — pid головы мёртв, поднята новая (в записи растёт `launches`);
- `observer-stopped` — спринт закрыт или исчез с доски, голова остановлена, запись снята;
- `observer-stop-failed` — хост отверг остановку, голова считается живой: запись остаётся в
  состоянии `stop-pending` вместе с хэндлом, `observer_stopped` не пишется, следующий тик повторяет
  остановку. Сюда же попадает случай, когда останавливать надо по воркспейсу (хэндла нет), а Orca
  не отдала список терминалов: нечитаемая инвентаризация не считается пустой, иначе живая голова
  осталась бы без записи. Если спринт за это время снова открыли, тик просто видит голову живой;
- `observer-launch-deferred` — запуск отложен (ресурс головы не готов, ролевой скилл не доставлен в
  шелл этой головы, bring-up не удался или
  старый терминал не закрылся перед переподъёмом); спринт остаётся в записи с причиной, следующий
  тик пробует снова. Если bring-up упал уже после создания терминала и закрыть его не удалось,
  запись сохраняет хэндл с флагом `abandoned_handle`: тик не считает такую голову живой, сначала
  повторяет закрытие терминала и только затем поднимает замену;
- `observer-adopted` — на диске нашлось намерение запуска, которое пережило свой тик, и pid
  указанной в нём головы жив: голова принимается как голова этого спринта, счётчик запусков
  доводится до номера её попытки, а второй голову никто не поднимает. Хэндл терминала умер вместе с
  тем тиком, поэтому в `status` у такой записи `handle_known: false`, а остановка идёт по воркспейсу
  наблюдателя;
- `observer-launch-pending` — намерение запуска ещё внутри своего окна ожидания pid: голова могла
  просто не успеть записать pid-файл, поэтому тик её не трогает (ни остановки, ни второго
  `prepare_observer`) и разбирается на следующем проходе;
- `observer-launch-skipped` — идёт `drain`, новых голов не поднимаем. Запись при этом всё равно
  заводится (состояние `deferred`, причина `pipeline is draining`, профиль головы проставлен),
  чтобы открытый спринт был виден снаружи; ни гейт готовности, ни хост при этом не дёргаются, а
  после `resume` ближайший тик поднимает голову из той же записи;
- `sprint-board-unavailable` — доску спринтов не удалось прочитать; ни одна живая голова при этом
  не останавливается.

Профиль головы берётся из `role_defaults.observer` (`heads/heads.yaml`, генерится
`secretary upgrade` из `triggered_agents/agents/pipeline/heads.toml`). Перед запуском отрабатывает
тот же гейт готовности ресурса, что и перед claim карточки, с теми же вердиктами (см. «Готовность
голов»). Голова запускается через `role_env exec --role observer` в собственном воркспейсе
`<workspaces root>/observers/<ref>` с собственным терминалом; промпт `SPRINT.md` рендерится из живой
сущности спринта в момент запуска и ссылается на ролевой скилл по пути.

Воркспейс наблюдателя — зарегистрированный в Orca ворктри, а не просто каталог: без регистрации
`orca terminal create` отвечает `selector_not_found` и запуск уходит в `observer-launch-deferred`.
Режется он не из репозитория проекта, а из отдельного пустого репозитория
`<data_dir>/dispatcher/observer-root/observers`, который диспетчер заводит сам при первом запуске
(`git init` + `orca repo add`); в списке `orca repo list` он виден как `observers`. Ничего настраивать
руками не нужно, и удалять его не надо: он переиспользуется всеми спринтами. Каталог по пути
воркспейса, о котором Orca не знает (остаток от запуска, не дошедшего до Orca), диспетчер удаляет и
создаёт воркспейс заново — в нём лежит только `SPRINT.md`, который следующий запуск всё равно
перезаписывает.

Доверие codex к этому воркспейсу диспетчер выставляет сам: перед запуском он дописывает
`[projects."<путь>"] trust_level = "trusted"` в `config.toml` того `CODEX_HOME`, с которым запускается
голова, — и на воркспейс, и на корень репозитория наблюдателей, потому что спрашивает codex именно
про корень. Руками этот файл править не нужно. Если оператор держит один из этих путей на другом
уровне доверия, запуск уходит в `observer-launch-deferred` с причиной, называющей файл и путь:
решение снимается правкой этой записи, а не повторным тиком.

Остановка головы глушит терминалы воркспейса (`orca terminal stop --worktree`) и снимает регистрацию
(`orca worktree rm --force`), поэтому после закрытия спринта ни терминала, ни ворктри наблюдателя в
Orca не остаётся. Если ворктри уже не зарегистрирован, остановка считается выполненной: это то, что
делает повтор незавершённой остановки конечным.

Bring-up, упавший уже после создания ворктри (например, на `terminal create`), оставляет
регистрацию без головы. Запись это помнит отдельно от живости процесса, поэтому закрытие спринта
всё равно снимает ворктри, а не бросает его в Orca; отказ Orca на этом шаге — обычная неудавшаяся
остановка, запись остаётся в `stop-pending` и следующий тик возвращается к ней.

### Ролевой скилл наблюдателя

Что наблюдатель делает внутри сессии, задаёт скилл роли `observer` — `observe-sprint`. Канон лежит
в продукте (`skills/roles/observer/observe-sprint/SKILL.md`), в шеллы попадает обычным
`secretary role-skills sync` (шаг `role-skills` в `secretary upgrade`) и проверяется тем же
`secretary role-skills audit --check`.

Перед запуском тик проверяет, что скилл лежит в шелле поднимаемой головы. Если нет, голова не
поднимается: тик отдаёт `observer-launch-deferred` с причиной вида

```
observer role skill is not available to this head: observer/observe-sprint is not in the codex
skill directory (<root>/observe-sprint/SKILL.md); run `secretary role-skills sync`
```

Причина лежит в `deferred_reason` записи наблюдателя, поэтому видна в `secretary status --json`
(`.dispatcher.observers`), `secretary sprint status --ref sprint:ID` (`.observer`) и
`secretary dispatcher production-observe`. Лечится доставкой скилла:

```bash
secretary role-skills audit --check
secretary role-skills sync
```

Следующий тик поднимает голову из той же записи. Та же причина печатается, когда для шелла головы
в `skills/manifest.toml` вовсе нет цели с ролью `observer` (например, `role_defaults.observer`
переставили на профиль другого шелла) и когда сам манифест нечитаем.

Живость — тот же pid-heartbeat, что у воркера и ревьюера
(`$SECRETARY_DISPATCHER_BODY_DIR/secretary-observer-pid-<ref>.pid`, по умолчанию под `/tmp`).
Свежезапущенная голова ещё не успела записать pid, поэтому нечитаемый pid-файл считается живым в
течение окна `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` (по умолчанию 180 секунд) и мёртвым после.
Автоматического ремонта зависшей (в отличие от мёртвой) головы нет: такой случай разбирает оператор.

События жизненного цикла (`observer_launched`, `observer_relaunched`, `observer_stopped`,
`observer_launch_deferred`) лежат в общем durable audit-логе (`board/events.ndjson`) с reference
спринта; повтор с тем же `request_id` второго события не создаёт. `request_id` строится из reference,
поколения записи (`generation`) и счётчика запусков, поэтому спринт, вернувшийся на доску после снятия
записи, пишет свои события заново, а не растворяется в дедупликации первого цикла.

Событие стейджится в `board/pending-audit/` до вызова хоста и коммитится после него. Если audit-лог
не пишется, видно это так:

- `observer-launch-deferred` с причиной `observer lifecycle event could not be staged` или
  `observer-stop-failed` с упоминанием staging — хранилище отказало до действия, голову не поднимали
  и терминал не закрывали, следующий тик пробует снова;
- любой outcome с полем `audit: pending` (статус `degraded`) — действие выполнено и записано в
  `production-state.json`, но событие осталось в pending. Дописывает его ремонт:

```bash
secretary task verify-audit --instance INSTANCE     # .pending
secretary task reconcile-audit --instance INSTANCE  # repaired/unresolved
```

Запись наблюдателя фиксируется на диске тем же порядком, что и событие. Намерение запуска (спринт,
поколение, профиль головы, номер попытки, воркспейс и pid-файл будущей головы, состояние
`launching`) пишется в `production-state.json` до вызова хоста, а не в конце тика. Отсюда два
наблюдаемых случая:

- `observer-launch-deferred` с причиной `observer launch intent could not be persisted` — state не
  пишется, головы никто не поднимал; чинить надо диск или права на `dispatcher/production-state.json`,
  следующий тик пробует снова;
- запись в состоянии `launching` с непустым `pending_launch` — тик умер, не успев записать исход
  запуска. Разбирает это ближайший тик сам, по pid-файлу из той же записи. Живой pid даёт
  `observer-adopted`. Pid-файла ещё нет, а окно `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` не
  истекло — `observer-launch-pending`: намерение остаётся как есть, голову никто не закрывает.
  После истечения окна (и при мёртвом pid) тик закрывает терминалы воркспейса, и попытка
  считается исчерпанной, если её событие уже лежит в логе (голова поднималась и умерла — идёт
  `observer-relaunched` со своей строкой в аудите), и просто повторяется тем же номером, если
  событие так и осталось в pending (хост ответить не успел, поднимать было нечего). Руками тут
  делать нечего.

Успешный запуск, чей state-write не прошёл, отдаёт outcome со статусом `degraded` и полем
`state: pending`: голова поднята, намерение на диске, следующий тик её усыновит.

Состояние снаружи, без чтения транскрипта:

```bash
secretary status --json --instance INSTANCE           # .dispatcher.observers
secretary dispatcher production-observe --instance INSTANCE   # .observers
secretary pause-status --instance INSTANCE            # .observers, .stopped_observer
```

Строка наблюдателя отдаёт спринт, профиль головы, состояние (`running` / `launching` / `deferred` /
`stop-pending` / `pause-stop-pending` / `stopped-by-pause` / `pending`), живость pid (`alive`,
`pid_known`), число запусков, воркспейс, флаги `handle_known` и `abandoned_handle`, время и вид
последнего действия и причину отложенного запуска.

## Checkpoint push

Push идёт на том же тике, но по своему окну: раз в 30 минут, только fast-forward, без
force-push. Перед пушем `ls-remote` сверяет тип remote: если тип уже равен локальному HEAD,
пуш не нужен; если он предок HEAD, идёт `git push origin HEAD:refs/heads/<branch>`. Git-вызовы
неинтерактивные (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) и с 60-секундным таймаутом, чтобы
недоступный remote или запрос пароля не держали тик.

Сбой пуша fail-closed на checkpoint, но не на работе: диспетчер продолжает двигать карточки,
локальные коммиты идут, причина и растущий lag видны, следующее окно ретраит.

`remote diverged` — на remote есть коммиты, которых нет локально. Пуш останавливается, алярм
висит в `status` и `doctor`, автоматика ничего не переписывает. Если причина была в interleaving
green publish и checkpoint, следующий dispatcher tick сам сведёт локальный instance checkout, а
checkpoint pusher сразу перепроверит diverged-состояние и погасит алярм fast-forward-only. Ручной
разбор нужен, когда remote содержит историю, которой нет ни в reviewed branch, ни в локальном
checkpoint checkout:

```bash
git -C INSTANCE fetch origin
git -C INSTANCE merge --no-edit FETCH_HEAD   # или rebase, по ситуации
```

После того как remote стал предком локального HEAD, следующий тик пушит сам и алярм гаснет.

Freshness видна в `dispatcher production-observe` (поле `checkpoint`) и в `doctor` блоком
`checkpoint freshness`: последний коммит, последний успешный push, lag в коммитах и минутах,
причина блокировки гейта, состояние `remote diverged`. Lag в минутах — возраст самого старого
непушнутого коммита, то есть реальная величина потери при потере машины. `doctor` поднимает
finding на `remote diverged`, на заблокированный гейт и на lag больше 60 минут (два пропущенных
окна).

## Восстановление

Единственный recovery contract — Git-backed checkpoint из [Recovery](RECOVERY.md). Живое
восстановление идёт из приватного репозитория инстанса, без обязательного S3 transport.

```bash
secretary install --instance-remote REMOTE --instance-dir INSTANCE --installation-user dev
secretary recover --instance-remote REMOTE --instance-dir INSTANCE --installation-user dev \
  --recovery-phrase-file PHRASE_FILE
```

Обе команды открывают хранилище секретов (если оно инициализировано в этом instance-репо) раньше,
чем читают `runtime.env`: с recovery phrase (`--recovery-phrase-file`, `--recovery-phrase-stdin`
или интерактивный prompt на TTY, если ключа ещё нет на диске) installation key пересобирается и
материализует `runtime.env` и другие цели из каталога, включая Kanboard credentials, если они
заведены в хранилище. Только если хранилища в этом instance-репо нет вовсе, `runtime.env` остаётся
ручным файлом оператора, как описано в [Recovery](RECOVERY.md). Без фразы recover не отказывает:
он восстанавливает всё, что не требует credentials, и печатает отчёт locked/missing по секретам,
которые остались недоступны.

Первая команда клонирует remote и останавливается до появления host-only credentials, если
хранилище их не материализовало. Вторая единым идемпотентным flow материализует checkpoint,
восстанавливает board — и карточки Pipeline, и сущности спринтов с их полями, бюджетом, resume и
записями, — пересобирает memory index, role worktrees и host resources, затем проверяет
status. `restore-board` печатает обе величины (`cards`, `sprints`), а расхождение любой из двух
parity-сверок оставляет recovery незавершённым и видно в `doctor`.

Низкоуровневые `bootstrap --empty`, `restore-board`, `memory reindex`, `reconcile apply` и
`restore-reconcile` остаются диагностическими примитивами, а не основным runbook.

## Опциональный cold archive

`backup create`/`backup verify` остаются ручным инструментом на случай выгрузки сырья, не
recovery-контрактом. Автоматического таймера, offsite-переноса и doctor-гейта у него больше нет.

```bash
python3 -m secretary backup create --instance INSTANCE --kind both
python3 -m secretary backup verify ARCHIVE.tar [--strict]
```

`create` пишет обычный tar в `backups/` (`core`, `full`, `both`), без шифрования. `verify`
возвращает `0` при успехе, `1` для findings или strict warnings, `2` для недоступного archive.
Восстановление из такого архива по-прежнему доступно через `secretary restore ARCHIVE.tar` для
совместимости. Архив не является recovery contract и не влияет на `doctor` или readiness.

## Авто-мёрж зелёных карточек

Когда ревьюер ставит `review:green`, production dispatcher сам доводит карточку до `done`, без
ручного мёржа:

1. Push worker-ветки в `origin/main` fast-forward-only (`git push origin BRANCH:main`). Если main
   разошёлся, push отклоняется — dispatcher не форсит и не подчищает конфликт сам.
2. Fast-forward локального чекаута соответствующего проекта на новый `origin/main`. Для проекта
   `secretary` это self-deploy: production dispatcher мёржит и сразу подтягивает изменения в
   собственный checkout, из которого работает.
3. Teardown воркспейса: dispatcher останавливает терминалы worktree (worker, ревьюер и их
   дочерние процессы) и удаляет worktree через `orca worktree rm`.

Для private instance repo publish идёт под тем же writer lock, что и checkpoint. Dispatcher
публикует только reviewed branch и локально известную checkpoint-историю: remote tip должен быть
предком worker-ветки или локального instance checkout. Чужая remote-история остаётся ручным
runbook case, без авто-мёржа в green-карточку. После успешного publish dispatcher мёржит
`origin/main` в локальный checkout instance repo. Поэтому checkpoint-коммит, появившийся между
preflight и publish, сохраняется обычным merge-коммитом вместе с feature commit. Если тик упал
после remote publish, но до локального merge, следующий тик повторяет Done-путь идемпотентно:
push видит уже опубликованный результат, локальный checkout догоняется merge-коммитом, и карточка
завершается без ручного вмешательства.

Teardown выполняется только на этом Done-пути. При `review:red` (rework) воркспейс и его ветка
остаются нетронутыми, чтобы worker мог продолжить в том же worktree.

Kill-switch: `SECRETARY_DISPATCHER_AUTOMERGE=off` отключает push и fast-forward шаги (`Host.complete_green`)
целиком — карточка всё равно уходит в `done`, но branch остаётся неслитым и требует ручного мёржа.
Дефолт — `on` (авто-мёрж включён).

## Пауза пайплайна

Аварийная остановка — одна команда продуктового CLI, два режима:

```bash
python3 -m secretary pause drain  --instance INSTANCE --reason "почему"
python3 -m secretary pause freeze --instance INSTANCE --reason "почему"
python3 -m secretary resume       --instance INSTANCE
python3 -m secretary pause-status --instance INSTANCE
```

`drain` — тик перестаёт клеймить Ready, но карточки, которые уже в работе, доезжают свой цикл:
воркер дописывает, ревьюер судит, зелёный PR мержится. Берётся, когда нужно остановить приток, не
обрывая работу.

`freeze` — то же плюс живые головы воркера, ревьюера и наблюдателей спринтов останавливаются, и тик
после этого не продвигает ничего. Воркспейсы, worktree и незакоммиченная работа не трогаются:
останавливаются только терминалы. Берётся, когда хост нужно освободить прямо сейчас (бэкап,
перезагрузка, разбор аварии).

`resume` снимает паузу, поднимает остановленные freeze'ом головы воркеров и ревьюеров в тех же
воркспейсах и выдаёт вотчдогам ожиданий свежее окно, чтобы длинная пауза не прочиталась потом как
молчание головы. Карточка, чья голова успела отчитаться во время freeze, head'а не получает: её
двигает ближайший тик по уже записанному отчёту. Наблюдателей `resume` сам не поднимает: он снимает
с них пометку паузы (`observers_resumed` в ответе), и ближайший тик поднимает их обычной
реконсиляцией. При `drain` живую голову наблюдателя не трогает никто, и новых не поднимают; спринт,
открытый во время `drain`, получает отложенную запись с причиной и виден во всех сводках.

Если хост отверг остановку наблюдателя при `freeze`, ответ `pause` отдаёт это отдельным
предупреждением со списком спринтов, а запись остаётся в состоянии `pause-stop-pending` с хэндлом.
Реконсиляция под freeze не работает, поэтому остановку повторяет сам замороженный тик: результат
виден в `observer_stops` его ответа. Голова, дожившая до `resume`, повторно не поднимается —
ближайший тик видит её живой (`observer-live`).

Смена режима на весу запрещена: сначала `resume`, потом пауза в другом режиме. Повторная пауза в
том же режиме — no-op.

Флаг лежит в живом data plane, рядом с состоянием того диспетчера, который реально работает:
`<data_dir>/dispatcher/pause.json`. Оттуда его читает каждый прогон продуктового тика. Фоновые
роли (steward/curator/retro) по-прежнему читают легаси-флаг в воркспейсе пайплайна, поэтому пауза
дополнительно пишет туда зеркало, а `resume` его убирает — но только если зеркало поставила сама
пауза. Чужой легаси-флаг не перезаписывается и не удаляется.

Легаси-вход `triggered_agents pipeline pause|resume` больше не пауза: он писал флаг, которого
продуктовый диспетчер не читает. Команда отказывает и называет `secretary pause`.

### Пауза или авария

`pause-status` показывает состояние продуктового диспетчера: режим, кто и когда поставил, путь к
файлу флага, и построчно по карточкам — что с их головами:

- `running` — голова живая;
- `stopped-by-pause` — голову остановила пауза, воркспейс на месте, `resume` поднимет её обратно;
- `not-running` — головы нет и пауза её не останавливала: это либо карточка, до которой ещё не
  дошли, либо настоящий обрыв, и разбирать его надо как обрыв (см. «Вотчдоги ожиданий»).

Тик во время freeze отвечает `skipped` с причиной `pipeline is frozen by pause` и снимком паузы, а
не молчанием, так что «ничего не двигается» в логе всегда отличимо от вставшего диспетчера. Health
probe (`production-tick --probe`) во время freeze тоже отвечает `ok` с этим снимком.

Замороженный тик по-прежнему пишет и пушит checkpoint: freeze останавливает продвижение карточек, а
не durability, иначе долгая пауза оказалась бы дырой в истории снимков и растущим push-лагом ровно
там, где восстановление и понадобится. В ответе такого тика есть `checkpoint` и `checkpoint_push`,
хотя `actions` пустых нет вообще.

### Freeze, который снимается сам

Freeze от автоматики живёт по TTL. Если `actor` паузы входит в
`TA_HARD_PAUSE_AUTO_RESUME_ACTORS` (по умолчанию `pipeline,secretary-backup,secretary,steward,
curator,retro`), то через `TA_HARD_PAUSE_AUTO_RESUME_TTL_S` секунд (по умолчанию 2700) ближайший
тик сам вызовет `resume` — тем же путём, что оператор, с подъёмом голов и свежими окнами вотчдогов.
Без этого `secretary backup create`, убитый до своего `finally`, оставлял бы диспетчер замороженным
навсегда. Freeze от человека (любой другой actor) не истекает никогда: окно обслуживания снимает тот,
кто его поставил. `TA_HARD_PAUSE_AUTO_RESUME_TTL_S=0` выключает авто-resume совсем.

`pause-status` в поле `auto_resume` отвечает, снимется ли пауза сама: `fresh` (снимется, TTL ещё не
вышел), `manual-or-unknown-actor` (не снимется, держит человек), `disabled` (авто-resume выключен).
В ответе тика, который снял паузу по TTL, лежит `auto_resume` с `resumed: true`, возрастом паузы и
списками поднятых голов, так что снятие по TTL не путается ни с ручным resume, ни с обрывом.

## Вотчдоги ожиданий

Диспетчер ждёт голову в двух точках: `waiting-worker-report` (карточка в In progress) и
`waiting-review-verdict` (карточка в Validate). Раньше оба ожидания повторялись каждый тик без
ограничения, и голова, умершая до отчёта, оставляла карточку висеть (secretary-637, secretary-649).

Теперь на каждом тике ожидания диспетчер сверяет сохранённые handle и `leafId` активной попытки с
`orca terminal list`. Orca может выдать другой handle тому же pane, поэтому leafId остаётся
стабильным токеном и для воркера, и для ревьюера. Отсутствующий или disconnected pane сразу запускает тот
же путь, что и stall: один respawn в том же воркспейсе, затем Blocked с сигналом оператору.
Ответ о недоступном runtime не считается смертью головы: тик возвращает
`worker-runtime-unavailable` или `review-runtime-unavailable` и не трогает карточку только из-за
ошибки инвентаря. Такой тик не доказывает прогресс, поэтому обычный потолок ожидания продолжает
работать как fallback и по его истечении запускает обычный respawn/Blocked путь.

Наличие pane само по себе недостаточно. Инвентарь содержит `lastOutputAt`; диспетчер хранит
последний вывод именно сохранённого pane, не всего воркспейса. Если вывод не меняется до потолка,
срабатывает тот же watchdog. Так ловится экран логина и другой живой, но бездействующий head, а
вывод случайного shell в том же worktree не маскирует проблему. Заголовок терминала и
`status:running` по-прежнему не используются: голова переписывает title своей OSC-последовательностью,
а `status:running` может залипнуть на `working` после тихого выхода.

Третий случай (secretary-751, живой инцидент на secretary-731): pane остаётся connected, а Orca всё
равно держит в нём собственный interactive shell воркспейса даже после того, как процесс головы
завершился, — сам этот shell типизирует команду головы построчно и не закрывается вместе с ней.
Возврат к приглашению shell один раз обновляет `lastOutputAt`, поэтому по признакам первых двух
случаев такая голова читается как «вывод был, потом тишина» и ждала бы обычного длинного потолка.
Отличить эти случаи без разбора текста сессии помогает pid, который голова сама пишет перед `exec`:
launch-команда оборачивается в `echo "$$" > <pid-file>; exec <голова>`, `$$` внутри shell — это pid
самого shell, а `exec` заменяет его образ процессом головы без fork, так что записанный pid
остаётся pid'ом головы на всю её жизнь. На каждом тике ожидания диспетчер проверяет этот pid через
`kill(pid, 0)`: если pane connected, а pid из файла уже не отвечает, это тот же путь, что missing
или disconnected pane — один respawn в том же воркспейсе, затем Blocked. Файл лежит вне воркспейса,
как report/verdict body: `SECRETARY_DISPATCHER_BODY_DIR` (дефолт `/tmp`), имя
`secretary-<worker|review>-pid-<ref>.pid`; respawn удаляет его перед новым запуском, чтобы не
прочитать pid мёртвого предшественника раньше, чем новая голова перезапишет файл своим.

Если `kill(pid, 0)` подтверждает, что процесс головы жив, это положительный сигнал ливнеса, а не
просто отсутствие доказательства смерти, — и тик пропускает оба таймаута, короткое окно первого
вывода и длинный потолок бездействия, целиком, независимо от того, сдвигался ли `lastOutputAt`.
Молчание живой головы с подтверждённым pid ничего не доказывает, поэтому respawn или Blocked для
неё запускает только реальный выход процесса (тот же путь, что и missing/disconnected pane выше).
Пока файла ещё нет — свежий запуск ещё не успел выполнить свой `echo`, либо раннер вообще не даёт
этот сигнал, — это не читается ни как смерть головы, ни как подтверждение жизни, и тик продолжает
использовать обычные проверки по `lastOutputAt`: короткое окно первого вывода и длинный потолок.
Единственный такой раннер — сырой оверрайд `SECRETARY_DISPATCHER_WORKER_COMMAND` /
`SECRETARY_DISPATCHER_REVIEW_COMMAND`: он подставляет команду в обход каталога голов и поэтому не
получает обёртку с heartbeat. Для него, как и для старого Orca без `lastOutputAt`, длинный потолок
остаётся единственным fallback — ровно потому, что для этих раннеров нет способа подтвердить
ливнес независимо от вывода pane.

Каждый свежий сигнал прогресса начинает новое окно ожидания. Поэтому потолок означает, сколько
голова молчит, а не сколько длится задача: пишущая вывод голова не получает respawn только из-за
возраста карточки. Если `lastOutputAt` известен, но не сдвинулся дальше времени подъёма головы,
действует отдельное короткое окно в 180 секунд: это ловит экран логина и другую голову, которая
не напечатала ничего после запуска. После первого вывода действует только длинный потолок, потому
что голова имеет право долго думать. У Codex TUI alternate screen может не обновлять
`lastOutputAt`, поэтому для профилей `codex_mode: tui` сигнал дополняется mtime rollout JSONL,
привязанного к worktree, а не к конкретному pane: активность другой Codex-сессии в том же worktree
тоже обновит этот дополнительный сигнал. Это компромисс alternate screen, который проверяет только
метаданные файла, не читает текст сессии. На старом
Orca без `lastOutputAt` короткое окно не применяется, а потолок остаётся fallback. Первое
превышение — один respawn той же головы в том же воркспейсе, второе — карточка в Blocked с
сигналом оператору.

Respawn пишет комментарий на доску, чтобы оператор отличал первое залипание от карточки, у которой
голову уже перезапускали, не дожидаясь финального Blocked.

- `SECRETARY_INITIAL_OUTPUT_STALL_SECONDS` — короткое окно первого вывода, дефолт 180 секунд.
- `SECRETARY_REVIEW_VERDICT_STALL_SECONDS` — потолок ожидания вердикта после первого вывода,
  дефолт 5400 секунд.
- `SECRETARY_WORKER_REPORT_STALL_SECONDS` — потолок ожидания отчёта после первого вывода,
  дефолт 21600 секунд.

Все три переменные читаются в момент проверки; мусор или ноль в значении откатывается на дефолт, чтобы
опечатка в юните не роняла старт диспетчера.

Тело отчёта и вердикта голова пишет в файл вне воркспейса
(`/tmp/secretary-report-<ref>-<round>.md`, `/tmp/secretary-verdict-<ref>-<round>.md`, каталог
переопределяется `SECRETARY_DISPATCHER_BODY_DIR`), а не собирает inline в шелле: codex-рантайм
режет команды с `rm`, а кавычки и backtick в теле ломают вызов. Файл остаётся на месте, подчищать
его голове не нужно, поэтому в имени есть раунд: иначе второй ревьюер подобрал бы тело первого.

Номер раунда (`review_baseline`) входит и в `--request-id` вердикта. `attempt_id` живёт всю
попытку и не меняется на переходе review:red -> rework -> report:done, так что без раунда второй
red в рамках одной попытки выглядит для `TaskWriter` реплеем первого: комментарий не пишется, CLI
всё равно отвечает «вердикт записан», ревьюер выходит, карточка стоит в Validate до вотчдога.

## Units

Актуальные templates и их назначение находятся в
[packaging/systemd/README.md](../packaging/systemd/README.md). Юниты раскатывает
`secretary reconcile apply`; ручная установка больше не нужна и не даёт ownership.

Production dispatcher timer запускает one-shot tick. Memory, curator, steward и retro должны иметь
ровно одного scheduler owner.

## Upgrade

`secretary upgrade --instance <dir>` подтягивает новую версию продукта и пере-материализует
установку под неё. Идемпотентна: повторный запуск на актуальном хосте не делает ничего.

```
secretary upgrade --instance INSTANCE --dry-run   # решить всё, ничего не писать
secretary upgrade --instance INSTANCE
```

Шаги, по порядку; каждый печатает `changed`/`unchanged`/`skipped`/`failed`, первый `failed`
останавливает прогон:

| шаг | что делает |
| --- | --- |
| `pull` | `git fetch` + `merge --ff-only` чекаута продукта. Грязный чекаут — отказ. |
| `dependencies` | переустановка в `.venv`, если в pull двигался манифест зависимостей |
| `role-skills` | `role_skills sync` в shell-овые skill-директории |
| `role-worktrees` | ff worktree ролей (`~/orca/workspaces/secretary/<role>`) на base branch |
| `host` | `reconcile apply`: юниты из `packaging/systemd` + Orca-регистрации |
| `automations` | create/repoint Orca-автоматизаций из `automation.toml` |
| `memory` | рестарт `secretary-memory.service`, если менялся код, зависимости или сам юнит |
| `verify` | повторный dry-run: вторая раскатка обязана быть no-op |

Флаги: `--no-pull` (только пере-материализация), `--base-branch`, `--product-root`, `--json`.

### Ownership и fail-closed

`reconcile apply` пишет только то, что подтверждено `host-managed.json`. Имя под
`host.unit_prefix`, которого нет ни в плане, ни в manifest, — это `conflict`, и любой conflict
отменяет весь прогон до первой записи. Разрешить можно двумя способами:

- юнит действительно наш и совпадает с packaged-файлом байт в байт →
  `secretary reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`;
- имя принадлежит чему-то другому (например dev-only `secretary-supervisor.*`) → перечислить его
  в `host.foreign_units` в instance.yaml.

Юнит, который отличается от packaged-файла, adopt не примет: сначала удалить его руками
(`sudo rm /etc/systemd/system/<name>`) и дать `apply` поставить канон, либо разобраться, почему
хост разошёлся с продуктом.

Компонент, который эта установка сознательно не крутит, выключается в конфиге, а не отсутствием
юнита на хосте:

```yaml
host:
  components:
    curator:
      enabled: false
      reason: "load shedding, secretary-XXX"
```

Выключенный компонент, юнит которого стоит и принадлежит нам, будет остановлен и удалён.

### Health-набор

Детерминированный набор, пригодный как gate перед и после upgrade:

```
secretary doctor --instance <dir>
secretary role-skills audit --check
secretary dispatcher production-tick --instance <dir> --probe
python3 -m unittest
```

`--probe` — это настоящий сухой тик: он берёт тот же singleton-lock, проходит те же mutation
guards, сканирует те же состояния карточек и прогоняет ту же логику решения, но первая же запись
превращается в abort и попадает в отчёт как «что сделал бы следующий тик». Зелёный probe при
сломанном тике невозможен — сломанный тик падает и здесь.

### Готовность голов

Перед новым worker-, reviewer- или observer-запуском диспетчер читает ресурс профиля из `heads/heads.yaml`
и выполняет его `probe`. Вердикты лежат в
`<data_dir>/dispatcher/resource_health.json`; их можно посмотреть без запуска карточки:

```
secretary dispatcher resource-health --instance <dir>
```

Проверка кэшируется на 300 секунд. Это ограничивает расход probe до одного дешёвого вызова на
ресурс за окно, хотя production tick может идти чаще. `ready` разрешает запуск.
`unauthenticated` и `unavailable` оставляют новую карточку в Ready до следующей проверки, без
claim и без занятия project slot. Такой выбор не создаёт цикл claim/отказ и не превращает
временную проблему провайдера в операторскую Blocked-карточку. Для уже взятой карточки повторный
worker launch блокирует её с причиной, сохраняя контекст попытки. Для головы-наблюдателя те же два
вердикта означают отложенный запуск: спринт остаётся открытым, причина видна в записи наблюдателя,
следующий тик пробует снова.

`unknown` означает, что сам probe не удалось надёжно выполнить или классифицировать. Он виден в
снимке, но не запрещает запуск: сбой наблюдения не доказывает отказ ресурса и не может навсегда
остановить очередь.

Если `claude-sub` показывает `unauthenticated`, оператор запускает `/login` в интерактивном
Claude, затем ждёт окончания TTL или проверяет следующий тик. Для `openai-sub` восстановите
ChatGPT-сессию через `codex login` в том же `CODEX_HOME`, который указан у профиля. При
`unavailable` не перезапускайте карточки: проверьте состояние провайдера, дождитесь следующего
TTL и снимите `resource-health`; fallback-маршрутизация сюда не входит.
