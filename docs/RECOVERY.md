# Git-centric recovery

Единый recovery contract `secretary`. Приватный Git-репозиторий установки служит durable
checkpoint конфигурации и переносимого состояния. Переезд на новую машину требует установки
продукта, доступа к этому репозиторию и ручного повторного ввода не хранимых в нём credentials.
Отдельный bundle/S3 не является обязательной частью основного пути.

Документ задаёт целевой контракт. Реализация writer, installer и recovery workflow остаётся за
отдельными карточками. Текущий процесс разработки пайплайна выводит archive path агрессивно
(см. [Переход и parity](#переход-и-parity)); production-контракт ниже описывает нужный уровень
надёжности, к которому пайплайн приходит.

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
- board export: `state/board/cards.ndjson`, `state/board/events.ndjson`, `state/board/export.json`;
- run/audit: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`.

Вне канона (derived или тяжёлое сырьё, пересоздаётся или в optional cold archive):

- raw Kanboard dumps (`board/kanboard-raw-*`);
- vector index (`memory/index.sqlite`), derived exports (`memory/export.ndjson`);
- transcripts, artifacts, backups;
- терминалы, worktrees, generated host state.

Board export держим только в `cards.ndjson` (построчный diff). Дубль `cards.json` в checkpoint
не входит.

## Layout

```text
<приватный репо>/
  instance.yaml, persona/, projects/, adapters/, heads/, policies/   config, коммитит оператор
  state/                                                             state, коммитит авто-писатель
    board/   cards.ndjson, events.ndjson, export.json
    runs/    runs.ndjson, claims.json, watermarks.json, export.json
    memory/facts/**
```

Memory facts хранятся плоско в едином репозитории. Вложенный git-журнал `memory/facts` убирается;
writer памяти коммитит `propose/commit/supersede` в общий репозиторий. Единая история, один HEAD.

## Каденция и RPO

- commit раз в тик диспетчера (60с), on-change: если хэш нормализованного `state/` не менялся,
  тик коммит пропускает;
- push на remote раз в 30 минут;
- durable RPO при потере машины = 30 минут. Локальные коммиты дают мелкую гранулярность истории
  и быстрый локальный откат, но машину не переживают.

## Writer

В репозиторий пишут двое, каждый своим pathspec:

- tick-writer: `state/board`, `state/runs`, в конце тика диспетчера под `tick_lock`;
- memory-writer: `state/memory`, по факту `propose/commit/supersede` (`curator memory-write`).

Pathspec'ы не пересекаются, поэтому чужое недописанное дерево ни один из них не подхватит.
`git add -A` не использует никто: ручные незакоммиченные правки конфига остаются нетронутыми.
Индекс git не рассчитан на параллельную запись, поэтому обе стороны берут общий `state_repo_lock`
на время staging+commit. Memory-writer коммитит сразу, не дожидаясь тика: факт попадает в HEAD, и
30-минутный push уносит его вместе с остальным checkpoint.

Миграция с вложенного журнала одноразовая и идемпотентная: дерево `memory/facts` сначала
переносится и коммитится в `state/memory/facts`, вложенный `.git` удаляется только после этого.
Падение между шагами оставляет факты в обоих местах, следующий запуск это опознаёт и дочищает.
Расхождение копий не разрешается автоматически, миграция останавливается и зовёт оператора.

## Валидационный гейт

Перед коммитом снапшот проходит fail-closed проверку. При провале любого пункта тик пропускает
checkpoint, пишет причину в `status`, ретраит на следующем тике. Рваный снапшот в историю не
попадает.

- task audit сведён, нет pending board-мутации;
- writer регенерирует `cards.ndjson` из живой доски; счётчики в `export.json` совпадают с числом
  строк;
- memory staging (`memory/.staging`) пуст;
- секрет-скан `state/` чист. `state/` уходит на remote, это единственное место возможной утечки
  секрета (вставленный токен в карточке или логе). Опора на `redact.py`. Memory-writer гоняет тот
  же скан по тексту факта перед своим коммитом: его путь в тик-гейт не заходит.

## Failure и divergence

Push failure (сеть, GitHub, auth недоступны в окно пуша): fail-closed на checkpoint, не на работе.
Локальные коммиты продолжаются, диспетчер работает, растущий checkpoint lag виден в `status` и
`doctor`. Следующий 30-минутный push ретраит.

Remote divergence (на remote есть коммиты, которых нет локально): авто-писатель только
fast-forward. Force-push и перезапись истории запрещены. При non-ff push останавливается, `status`
поднимает алярм `remote diverged`, разбор выполняет оператор (pull/rebase/merge).

## Секреты

sops/age не используется. Секреты живут только в host `runtime.env` (права `0600`, в `.gitignore`),
в checkpoint не входят никогда.

Security boundary: доверенный single-user host. Board и memory эндпоинты слушают loopback.
Внешние токены (GitHub, providers) прикрыты host access control, `.gitignore` и секрет-сканом
`state/`, а не at-rest шифрованием. Приватный ключ на том же хосте, что и данные, от компрометации
хоста at-rest крипто не защищает, а защищала она только вынесенные копии в git и offsite, которые
контракт убирает.

## Observability

`status` и `doctor` показывают checkpoint freshness: время и хэш последнего коммита, время
последнего успешного push, checkpoint lag в минутах и коммитах, причину блокировки гейта,
состояние `remote diverged`.

## Fresh install и recovery

```text
install secretary
  -> clone приватного репозитория
  -> заполнить runtime.env (credentials, вручную)
  -> rebuild derived state (restore-board, memory reindex, reconcile)
  -> status
```

Recovery пересоздаёт live board, vector index и host resources из checkpoint. S3 или отдельный
backup host не требуются. Головы подключаются после bootstrap отдельно.

## Переход и parity

Целевой parity gate, критерий ухода с архива на git: clean-host E2E recovery из приватного
репозитория. Установить продукт, clone, ручной `runtime.env`, `restore-board`, `memory reindex`,
`reconcile`, `doctor` зелёный, счётчики board/memory/runs совпадают с источником.

Порядок в production-контракте:

1. git-checkpoint writer работает параллельно с архивами;
2. parity gate пройден, выводятся archive backup, offsite (`pull-backups-offsite.sh`) и
   backup-таймер; docs перестают называть архив recovery-контрактом;
3. optional cold archive для сырых transcripts и artifacts остаётся будущим решением.

В текущем процессе разработки пайплайна cutover агрессивный: sops/age убираются сразу, архивы
выводятся после одной dry-run проверки restore-from-git, без долгого параллельного периода.

## Реализационная нарезка

1. Дизайн-контракт (этот документ).
2. Выпилить sops/age из install/reconcile/backup path. Секреты остаются только в host
   `runtime.env`.
3. Checkpoint writer: влить `state/` в приватный репозиторий, flatten memory, хук на тике
   (validate, `git add state/`, commit on-change), отдельный 30-минутный push, lag в `status`/`doctor`.
4. Вывести archive/offsite из основного пути после dry-run восстановления из git.
5. Fresh install и recovery path (`install -> clone -> runtime.env -> rebuild -> status`),
   карточки под Milestone 1/2.

## Acceptance gate

- В product docs описан один основной recovery contract: публичный product repo, приватный
  Git-backed instance/state repo, локальный runtime из checkpoint.
- Зафиксирован состав checkpoint и исключения derived state.
- Определены cadence, атомарная validation/commit/push, поведение при push failure и remote
  divergence, наблюдаемый RPO/lag.
- Описаны пути fresh install и recovery из приватного remote без обязательного S3.
- Encrypted archives обозначены переходной страховкой до parity, не вторым равноправным контрактом.
- Есть поэтапная нарезка и проверяемый parity gate для перехода, без реализации миграции в этой
  карточке.

## Out of scope

- Реализация checkpoint writer, installer и recovery workflow.
- Перенос конфигурации в control-plane database.
- Автоматизация provider credentials, `.env` и авторизации голов.
- Обязательный S3 transport, полный архив transcripts/artifacts, публичный plugin API.
