# Git-centric recovery

Единый recovery contract `secretary`. Приватный Git-репозиторий установки служит durable
checkpoint конфигурации и переносимого состояния. Переезд на новую машину требует установки
продукта, доступа к этому репозиторию и ручного повторного ввода не хранимых в нём credentials.
Отдельный bundle/S3 не является обязательной частью основного пути.

Writer, memory flatten, remote push и цельный recover-from-remote flow реализованы. Archive backup,
offsite-перенос и backup-таймер выведены из основного пути (см. [Переход и parity](#переход-и-parity));
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
- board export: `state/board/cards.ndjson`, `state/board/events.ndjson`, `state/board/export.json`;
- run/audit: `state/runs/runs.ndjson`, `claims.json`, `watermarks.json`, `export.json`;
- memory facts: `state/memory/facts/**`.

Вне канона (derived или тяжёлое сырьё, пересоздаётся или в optional cold archive):

- raw Kanboard dumps (`board/kanboard-raw-*`);
- vector index (`memory/index.sqlite`), derived exports (`memory/export.ndjson`);
- transcripts, artifacts, backups;
- терминалы, worktrees, generated host state (systemd units из `packaging/systemd/` и Orca-
  автоматизации фоновых ролей curator/retro/steward). Каноном для них служит продукт, а не
  checkpoint: юниты берутся из `packaging/systemd/`, а расписание/диспетчеризация автоматизаций —
  из `triggered_agents/agents/<role>/automation.toml`. `secretary reconcile apply` / `secretary
  upgrade` пере-материализуют их идемпотентно на провижининге и recovery (match Orca-автоматизации
  по `name`, edit in place, id/юнит стабильны), поэтому в checkpoint они не входят.

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

sops/age не используется. Host `runtime.env` имеет права `0600`, не входит в checkpoint и
содержит только машинно-сгенерированные `KANBOARD_URL`, `KANBOARD_API_USER` и
`KANBOARD_API_TOKEN`. Доступ к GitHub и интерактивные логины голов остаются в password
manager оператора и не копируются на хост продуктом.

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

Сначала установить продукт с memory extra. `secretary bootstrap` на Ubuntu 24.04 ставит pinned
Kanboard и Orca, генерирует `runtime.env` и создаёт Pipeline. `secretary install` их не ставит и
fail-closed проверяет оба runtime до изменения live state.

На чистом хосте сначала bootstrap создаёт checkout, локальный `runtime.env` с mode `0600` и
Pipeline без ручного ввода Kanboard credentials:

```bash
python3 -m pip install '.[memory]'
sudo secretary bootstrap \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir /home/dev/secretary-instance \
  --installation-user dev

sudo secretary install \
  --instance-remote git@github.com:OWNER/secretary-instance.git \
  --instance-dir /home/dev/secretary-instance \
  --installation-user dev
```

`runtime.env` остаётся gitignored обычным файлом с mode `0600` и содержит как минимум
`KANBOARD_URL`, `KANBOARD_API_USER`, `KANBOARD_API_TOKEN`. Bootstrap генерирует его и не печатает
значения и не добавляет файл в Git.

`recover` выполняет одну поддержанную последовательность:

1. Проверяет remote/checkout, credentials, доступность Kanboard и установленный Orca.
2. Материализует `state/board` и `state/runs` из checkpoint в новый local data plane. Из
   `cards.ndjson` строится производный `cards.json`; счётчики проверяются до live writes.
3. Идемпотентно импортирует доску и пересобирает `memory/export.ndjson` и `memory/index.sqlite` из
   `state/memory/facts`.
4. Клонирует отсутствующие project checkouts по `remote` из registry и создаёт
   не-секретные `AGENTS.md` и `config.toml` в managed CODEX_HOME. OAuth остаётся ручным.
5. Запускает тот же materializer, что `secretary upgrade`: пересоздаёт role worktrees, ставит units,
   регистрирует Orca resources и применяет automations.
5. Проверяет restore status. Головы подключаются после bootstrap отдельно (Milestone 3).

Повторный `secretary recover` безопасен: checkout fast-forward-only, board restore сверяет parity,
memory index строится заново, materializer на втором проходе не имеет изменений. Терминалы,
worktrees, vector index, generated units и host caches из remote не копируются. S3 и отдельный
backup host не требуются.

`secretary recover --dry-run` проверяет checkout, credentials, runtime prerequisites и целостность
checkpoint, затем печатает шаги как `would-change`. Preview не пишет local data plane, не меняет
доску, не запускает memory reindex и host materializer.

Fresh mode не принимает существующего installation user или checkout. Он отказывает с явным
выбором `--recover` для этой же установки либо отдельного adopt workflow для живого хоста. Recover
не перезаписывает dirty checkout, другой remote, произвольный непустой data target или unowned host
resource. Полный adopt существующего live host в этот flow не входит.

## Переход и parity

Parity gate ухода с архива на git закрыт clean-host recovery из приватного репозитория: установка
продукта, clone, ручной `runtime.env`, `secretary recover`, зелёный status, совпадающие счётчики
board/memory/runs. Clean-host тест начинает без checkout, board, index, worktrees и Orca state.

Production cutover выполнен без долгого параллельного периода. Memory facts перенесены в private
instance repo, memory service читает новый канон, а scheduled archive units сняты. Из основного пути
выведены:

1. archive backup create/verify как обязательная часть контракта — остаётся ручным опциональным
   инструментом (`secretary backup create`), не recovery-контрактом;
2. offsite-перенос (`pull-backups-offsite.sh`) и его doctor-гейт — удалены;
3. backup-таймер (`secretary-backup.service`/`.timer`) и archive-age doctor-проверка — удалены.

git-checkpoint является единственным recovery contract. Ручной cold archive сохраняется для сырья
и совместимости, без timer, offsite transport и doctor gate.

## Реализационная нарезка

1. Готово: дизайн-контракт, отказ от sops/age, checkpoint writer, memory flatten, 30-минутный push
   и вывод archive/offsite из основного пути.
2. Готово: fresh install и recovery path
   (`install -> clone -> runtime.env -> rebuild -> status`) для Git-backed Milestone 2.

## Acceptance gate

- В product docs описан один основной recovery contract: публичный product repo, приватный
  Git-backed instance/state repo, локальный runtime из checkpoint.
- Зафиксирован состав checkpoint и исключения derived state.
- Определены cadence, атомарная validation/commit/push, поведение при push failure и remote
  divergence, наблюдаемый RPO/lag.
- Описаны пути fresh install и recovery из приватного remote без обязательного S3.
- Ручные archives обозначены optional cold storage, не вторым recovery contract.
- Есть поэтапная нарезка и проверяемый parity gate для оставшегося clean-host recovery flow.

## Out of scope

- Bundled package transport и установка Kanboard/Orca на голый host.
- Перенос конфигурации в control-plane database.
- Автоматизация provider credentials, `.env` и авторизации голов.
- Обязательный S3 transport, полный архив transcripts/artifacts, публичный plugin API.
