# Git-centric recovery

Единый recovery contract `secretary`. Приватный Git-репозиторий установки служит durable
checkpoint конфигурации и переносимого состояния. Переезд на новую машину требует установки
продукта, доступа к этому репозиторию и ручного повторного ввода не хранимых в нём credentials.
Отдельный bundle/S3 не является обязательной частью основного пути.

Writer, memory flatten, remote push и цельный recover-from-remote flow реализованы. Archive backup,
offsite-перенос и backup-таймер выведены из основного пути (см. [Отношение к cold archive](#отношение-к-cold-archive));
git-checkpoint является единственным recovery contract, архив остаётся только ручным optional cold
archive.

## Топология

```text
secretary            публичный репозиторий-шаблон: продукт, CLI, runtime, схемы, generic skills
<приватный репо>     один приватный репозиторий на пользователя: config + state/
host runtime         локальный runtime, пересобираемый из checkpoint; не канон
```

Приватный репозиторий (нынешний `secretary-instance`) поглощает нормализованный data-plane.
Отдельный git-канон для `secretary-data` не заводится. Один remote, один HEAD, один RPO.

## Source of truth

Live board backend остаётся оперативным store. Remote Git HEAD является последним подтверждённым
recovery checkpoint. Между коммитами live-состояние опережает checkpoint на величину RPO. Это
ожидаемое расхождение, а не рассинхрон.

## Состав checkpoint

Канон, нормализованный минимум для восстановления работы:

- instance config: `instance.yaml`, `persona/`, `projects/`, `adapters/`, `heads/`, `policies/`;
- board export: `state/board/cards.ndjson`, `state/board/sprints.ndjson`, `state/board/events.ndjson`,
  `state/board/export.json`;
- run/audit: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`;
- knowledge documents: `state/knowledge/**` (свободный markdown, см.
  [Архитектура](ARCHITECTURE.md#плоскости-знания)).

Вне канона (derived или тяжёлое сырьё, пересоздаётся или в optional cold archive):

- raw Kanboard dumps (`board/kanboard-raw-*`);
- vector index (`memory/index.sqlite`), derived exports (`memory/export.ndjson`);
- transcripts, artifacts, backups;
- терминалы, worktrees, generated host state (systemd units из `packaging/systemd/` и Orca-
  автоматизации фоновых ролей curator/retro/steward). Каноном для них служит продукт, а не
  checkpoint: юниты компилируются из templates в `packaging/systemd/` для installation user и layout,
  а расписание/диспетчеризация автоматизаций —
  из `triggered_agents/agents/<role>/automation.toml`. `secretary reconcile apply` / `secretary
  upgrade` пере-материализуют их идемпотентно на провижининге и recovery (match Orca-автоматизации
  по `name`, edit in place, id/юнит стабильны), поэтому в checkpoint они не входят.

Board export держим только в `cards.ndjson` и `sprints.ndjson` (построчный diff). Дубли
`cards.json` и `sprints.json` в checkpoint не входят.

Карточки Pipeline и сущности спринтов лежат в checkpoint отдельными наборами: спринты живут на
своей доске и в `pipeline export` не попадают, поэтому writer читает их своим проходом, а не
выводит из карточек с `sprint_ref`. Запись спринта несёт ref, цель, Definition of Done,
репозитории, статус, бюджет по типам событий, текущую карточку, resume, все записи к сущности и
audit-метаданные источника. Производные значения (итог бюджета, пороги установки, свежесть resume)
в запись не входят: они пересчитываются из неё и конфигурации.

## Layout

```text
<приватный репо>/
  instance.yaml, persona/, projects/, adapters/, heads/, policies/   config, коммитит оператор
  state/                                                             state, коммитит авто-писатель
    board/   cards.ndjson, sprints.ndjson, events.ndjson, export.json
    runs/    runs.ndjson, claims.json, watermarks.json, export.json
    memory/facts/**
    knowledge/**   брейнштормы, журналы решений, разборы инцидентов
  secrets/                                                           хранилище секретов, см. ниже
    catalog.yaml, installation-key.json, values/<id>.enc.json
```

`secrets/installation.key` — сырой installation key, `0600`, вне git (`.gitignore`), в checkpoint
не входит. Хранилище описано подробнее в разделе «Секреты» ниже и в
[Протоколах](PROTOCOLS.md#секреты).

Memory facts хранятся плоско в едином репозитории. Вложенный git-журнал `memory/facts` убирается;
writer памяти коммитит `propose/commit/supersede` в общий репозиторий. Единая история, один HEAD.

Канон для всех производных (`memory/export.ndjson`, `memory/index.sqlite`, memory-компонент
архива) — только `state/memory/facts`. Seed-источник `panelmem-kb` годится единственно для
инстанса без фактов, поэтому `instance_dir` у `export_all`/`export_memory`/
`export_memory_snapshot`/`rebuild_memory_index` обязателен: забытый аргумент падает, а не уводит
экспорт на чужую память.

## Каденция и RPO

- commit раз в тик диспетчера (60с), on-change: если хэш нормализованного `state/` не менялся,
  тик коммит пропускает;
- push на remote раз в 30 минут;
- durable RPO при потере машины = 30 минут. Локальные коммиты дают мелкую гранулярность истории
  и быстрый локальный откат, но машину не переживают.

## Writer

В репозиторий пишут четверо, каждый своим pathspec:

- tick-writer: `state/board`, `state/runs`, в конце тика диспетчера под `tick_lock`;
- memory-writer: `state/memory`, по факту `propose/commit/supersede` (`curator memory-write`);
- knowledge-writer: `state/knowledge`, по факту `secretary knowledge write`;
- secret-writer: `secrets/` (плюс `.gitignore` при первом `init`), на `secret init/set/import/remove`.
  `secret list` и `secret materialize` в этот writer не входят: `list` только читает каталог, а
  `materialize` пишет env-файлы вне `secrets/` и коммита не делает (подробности —
  [Протоколы](PROTOCOLS.md#секреты)).

Pathspec'ы не пересекаются, поэтому чужое недописанное дерево ни один из них не подхватит.
`git add -A` не использует никто: ручные незакоммиченные правки конфига остаются нетронутыми.
Индекс git не рассчитан на параллельную запись, поэтому все писатели берут общий
`state_repo_lock` на время staging+commit. Memory-, knowledge- и secret-writer коммитят сразу, не
дожидаясь тика: запись попадает в HEAD, и 30-минутный push уносит её вместе с остальным checkpoint.

Миграция с вложенного журнала одноразовая и идемпотентная: дерево `memory/facts` сначала
переносится и коммитится в `state/memory/facts`, вложенный `.git` удаляется только после этого.
Падение между шагами оставляет факты в обоих местах, следующий запуск это опознаёт и дочищает.
Расхождение копий не разрешается автоматически, миграция останавливается и зовёт оператора.

## Валидационный гейт

Перед коммитом снапшот проходит fail-closed проверку. При провале любого пункта тик пропускает
checkpoint, пишет причину в `status`, ретраит на следующем тике. Рваный снапшот в историю не
попадает.

- task audit сведён, нет pending board-мутации;
- writer регенерирует `cards.ndjson` и `sprints.ndjson` из живых досок; оба счётчика в
  `export.json` совпадают с числом строк;
- memory staging (`memory/.staging`) пуст;
- секрет-скан `state/` чист. `state/` уходит на remote, это единственное место возможной утечки
  секрета (вставленный токен в карточке или логе). Опора на `redact.py`. Memory- и
  knowledge-writer гоняют тот же скан по своему тексту перед коммитом: их путь в тик-гейт не
  заходит.

## Failure и divergence

Push failure (сеть, GitHub, auth недоступны в окно пуша): fail-closed на checkpoint, не на работе.
Локальные коммиты продолжаются, диспетчер работает, растущий checkpoint lag виден в `status` и
`doctor`. Следующий 30-минутный push ретраит.

Remote divergence (на remote есть коммиты, которых нет локально): авто-писатель только
fast-forward. Force-push и перезапись истории запрещены. При non-ff push останавливается, `status`
поднимает алярм `remote diverged`, разбор выполняет оператор (pull/rebase/merge).

## Секреты

sops/age не используется. Host `runtime.env` имеет права `0600`, не входит в checkpoint и
содержит только машинно-сгенерированные `KANBOARD_URL`, `KANBOARD_API_USER` и
`KANBOARD_API_TOKEN`. Доступ к GitHub и интерактивные логины голов остаются в password
manager оператора и не копируются на хост продуктом.

Хранилище секретов (`secretary/secret_store.py`, `secretary-instance/secrets/`) — отдельный,
восстановимый канон поверх того же instance-репо: каталог метаданных и версионированные envelope
трекаются git-ом рядом с board и memory и уезжают тем же push. Единственное, что репо
никогда не содержит — сырой installation key (`secrets/installation.key`, gitignored, `0600`) и
recovery phrase, которая генерируется один раз при `secret init`, печатается оператору и нигде не
хранится продуктом. Восстановление на чистом хосте описано ниже, в разделе «Fresh install и
recovery»: с фразой ключ пересобирается и значения возвращаются побайтово; без фразы
recover печатает отчёт locked/missing и ничего не пишет; потеря фразы означает перевыпуск
секретов заново, а не потерю остальной установки.

Security boundary: доверенный single-user host. Board и memory эндпоинты слушают loopback.
Внешние токены (GitHub, providers) прикрыты host access control, `.gitignore` и секрет-сканом
`state/`, а не at-rest шифрованием. Приватный ключ на том же хосте, что и данные, от компрометации
хоста at-rest крипто не защищает, а защищала она только вынесенные копии в git и offsite, которые
контракт убирает.

Installation key принадлежит installation user (тому же пользователю, что владеет хостом и
инсталляцией), а не отдельной, более узкой роли. Централизация секретов в одном хранилище не
сузила границу доверия и не расширила её: любой процесс, который раньше мог прочитать
`runtime.env`, и сегодня может прочитать installation key и открыть тем же ключом всё, что в
хранилище. Продукт не обещает worker isolation: broker, grants и раздельный доступ voркеров к
подмножеству секретов — вне контракта этой карточки и не реализованы. Формулировка "хранилище
секретов" не означает, что секреты защищены от кого-то на этом же хосте сильнее, чем были в
`runtime.env` — она означает только то, что они теперь наблюдаемы, версионированы и
восстановимы без ручного набора значений.

## Observability

`status` и `doctor` показывают checkpoint freshness: время и хэш последнего коммита, время
последнего успешного push, checkpoint lag в минутах и коммитах, причину блокировки гейта,
состояние `remote diverged`.

`status --json` несёт отдельную секцию `secret_store`: инициализировано ли хранилище, число
секретов, время последнего изменения каталога, есть ли пригодный installation key и сводку по
целям материализации — без единого значения, ключа или recovery phrase в любом виде. `doctor`
даёт finding, когда каталог и values разошлись, ключ отсутствует или непригоден при непустом
каталоге, либо права на ключе шире `0600`; здоровое и полностью отсутствующее хранилище findings
не дают.

## Fresh install и recovery

Сначала установить продукт с memory extra. `secretary bootstrap` на Ubuntu 24.04 ставит pinned
Kanboard и Orca, генерирует `runtime.env` и создаёт Pipeline. `secretary install` их не ставит и
fail-closed проверяет оба runtime до изменения live state.

На чистом хосте сначала bootstrap создаёт checkout, локальный `runtime.env` с mode `0600` и
Pipeline без ручного ввода Kanboard credentials:

```bash
python3 -m pip install '.[memory]'
sudo secretary bootstrap \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir INSTANCE \
  --installation-user dev

sudo secretary install \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir INSTANCE \
  --installation-user dev
```

`runtime.env` остаётся gitignored обычным файлом с mode `0600` и содержит как минимум
`KANBOARD_URL`, `KANBOARD_API_USER`, `KANBOARD_API_TOKEN`. Bootstrap генерирует его на fresh
install и не печатает значения и не добавляет файл в Git.

`recover` выполняет одну поддержанную последовательность:

1. Открывает хранилище секретов (если оно инициализировано в этом instance-репо) раньше, чем
   читает `runtime.env`: с `--recovery-phrase-file` / `--recovery-phrase-stdin` (или интерактивным
   prompt на TTY, если ключа ещё нет на диске) installation key пересобирается и материализует
   значения в файлы, которые называет каталог, включая `runtime.env`, если в нём материализован
   какой-то секрет. Без фразы этот шаг ничего не пишет и репортит locked/missing, а `runtime.env`
   остаётся тем, что уже на диске. Раздел «Секреты» ниже разбирает это подробнее.
2. Проверяет remote/checkout, credentials, доступность Kanboard и установленный Orca. Если
   `runtime.env` не появился на шаге 1 и хранилища нет вовсе, это по-прежнему ручной ввод оператора.
3. Материализует `state/board` и `state/runs` из checkpoint в новый local data plane. Из
   `cards.ndjson` и `sprints.ndjson` строятся производные `cards.json` и `sprints.json`; счётчики
   проверяются до live writes.
4. Идемпотентно импортирует доску и пересобирает `memory/export.ndjson` и `memory/index.sqlite` из
   `state/memory/facts`. Импорт доски восстанавливает и сущности спринтов: если экспорт их несёт,
   `restore-board` создаёт board `Secretary sprints` и возвращает каждую сущность целиком — цель,
   Definition of Done, репозитории, статус, бюджет, текущую карточку, resume, записи и
   audit-метаданные источника. Заводить спринт заново после recovery не нужно. Восстановленная
   сущность лежит на новой строке Kanboard, поэтому её собственные даты описывают восстановление, а
   даты источника читаются в `audit.source`.
5. Клонирует отсутствующие project checkouts по `remote` из registry и создаёт
   не-секретные `AGENTS.md` и `config.toml` в managed CODEX_HOME. OAuth остаётся ручным.
6. Запускает тот же materializer, что `secretary upgrade`: пересоздаёт role worktrees, ставит units,
   регистрирует Orca resources и применяет automations.
7. Проверяет restore status. Головы подключаются после bootstrap отдельно (Milestone 3).

Parity сверяется отдельно для карточек и для спринтов, и обе сверки fail-closed: расхождение
оставляет recovery незавершённым (`board restore parity failed` или `sprint restore parity failed`
в `doctor`), а не молча зачитывает восстановление успешным. Живой backend со сущностью спринта,
которой нет в экспорте, restore не перезаписывает, а останавливается.

Повторный `secretary recover` безопасен: checkout fast-forward-only, board restore сверяет parity,
memory index строится заново, materializer на втором проходе не имеет изменений. Восстановленный
инстанс сам остаётся восстановимым: его audit-записи о restore попадают в следующий checkpoint, и
recovery в очередной пустой backend пишет свои события под новым namespace, а не зачитывает чужие
как уже применённые. Namespace живёт в `restore-state.json`, поэтому retry одного recovery остаётся
идемпотентным.

Checkpoint, снятый до того, как сущности спринтов вошли в export, восстанавливается по-прежнему:
`sprints.ndjson` в нём нет, это читается как установка без спринтов, и `doctor` у уже завершённого
восстановления такого инстанса остаётся зелёным. Терминалы,
worktrees, vector index, generated units и host caches из remote не копируются. S3 и отдельный
backup host не требуются.

`secretary recover --dry-run` проверяет checkout, credentials, runtime prerequisites и целостность
checkpoint, затем печатает шаги как `would-change`. Preview не пишет local data plane, не меняет
доску, не запускает memory reindex и host materializer.

Fresh mode не принимает существующего installation user или checkout. Он отказывает с явным
выбором `--recover` для этой же установки либо отдельного adopt workflow для живого хоста. Recover
не перезаписывает dirty checkout, другой remote, произвольный непустой data target или unowned host
resource. Полный adopt существующего live host в этот flow не входит.

## Отношение к cold archive

git-checkpoint является единственным recovery contract. Ручной cold archive сохраняется для
сырья и совместимости, без timer, offsite transport и doctor gate. Scheduled archive backup,
offsite-перенос и archive-age проверка из основного пути выведены и в продукте отсутствуют.

## Acceptance gate

- В product docs описан один основной recovery contract: публичный product repo, приватный
  Git-backed instance/state repo, локальный runtime из checkpoint.
- Зафиксирован состав checkpoint и исключения derived state.
- Определены cadence, атомарная validation/commit/push, поведение при push failure и remote
  divergence, наблюдаемый RPO/lag.
- Описаны пути fresh install и recovery из приватного remote без обязательного S3.
- Ручные archives обозначены optional cold storage, не вторым recovery contract.

## Out of scope

- Bundled package transport и установка Kanboard/Orca на голый host.
- Перенос конфигурации в control-plane database.
- Автоматизация provider credentials, `.env` и авторизации голов.
- Обязательный S3 transport, полный архив transcripts/artifacts, публичный plugin API.
