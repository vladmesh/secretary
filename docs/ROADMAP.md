# Roadmap

Roadmap описывает последовательность продуктовых состояний, а не очередь карточек. Единственный
активный backlog проекта находится на Pipeline board и читается через `secretary task`. Переход
карточки в Ready означает включение в выбранный спринт.

## Текущий baseline

На рабочем VPS `secretary` уже владеет production dispatcher, memory service и Git-backed
checkpoint. Task lifecycle, worker/reviewer loop, memory journal, onboarding, host planning и
checkpoint recovery primitives доказаны кодом и эксплуатацией. Checkpoint проходит validation,
коммитится на production-тике и отправляется ff-only с RPO до 30 минут; publish изменений instance
repo, checkpoint writer и pusher сериализованы одним writer lock.

Нормализованные board/runs и канон memory facts находятся в `secretary-instance/state/`. Локальный
`secretary-data` хранит mutable/derived runtime state и пересоздаваемый memory index, отдельным
recovery-репозиторием не является.

Восстановимое хранилище секретов (`secretary secret init/set/import`, `secretary-instance/secrets/`)
держит installation credentials в том же instance-репозитории рядом с board и memory: канон
значений зашифрован, восстанавливается одной recovery phrase и уезжает тем же push. Секреты стали
частью recovery-контракта наравне с board, memory и operational configuration.

Фоновые роли материализуются из продуктового канона: packaged units и `automation.toml` управляют
curator, steward и retro, их timers и Orca automations без ручного копирования generated files.
Live instance содержит переносимый desired config для materializer. Archive backup, offsite и
backup timer выведены из основного recovery-контракта; archive остался только ручным optional cold
archive.

Продукт восстанавливает installation user, config/state и local data plane из private remote одной
поддержанной последовательностью `install` / `recover`. Flow принимает host-only credentials,
пересобирает board, memory index, role worktrees и применяет materializer на чистом target.
Bundled package transport Kanboard/Orca и полный adopt существующего live host остаются открытыми
частями Milestone 1.

## Milestone 1. Автоматическая новая установка

### Outcome

Одна bootstrap-команда устанавливает appliance на поддерживаемый чистый VPS. Installer создаёт
выбранного dedicated OS user, private instance repository и локальный data plane, ставит Kanboard,
Orca, memory service, dispatcher, фоновые роли и расписания. Ни одна голова не устанавливается
автоматически.

### Пользовательский путь

```text
install secretary
  -> choose installation user and private remote
  -> fill credentials/.env
  -> apply
  -> status
```

### Уже реализовано

- Product materializer идемпотентно планирует и применяет packaged services/timers включённых
  компонентов.
- Skills, units и Orca automations фоновых ролей выводятся из product root и `automation.toml`;
  повторный прогон сохраняет стабильные automation id и unit names.
- Существующие role worktrees синхронизируются, но создание отсутствующих worktrees остаётся частью
  незавершённого clean-host flow.
- `doctor` и verify materializer'а показывают отсутствующие либо drifted host resources.

Milestone остаётся открытым до появления поддержанной bootstrap-команды и clean-host E2E без
заранее подготовленных checkout'ов, board и Orca state.

### Acceptance gate

- Поддержанный Ubuntu 24.04 host не содержит заранее подготовленных `/home/dev`, checkout'ов,
  board или Orca state.
- Все host paths и resource names выводятся из instance и обнаруженного host context.
- Installer ставит и настраивает bundled Kanboard и Orca без заранее подготовленного runtime.
- Memory, dispatcher, curator, steward, retro и schedules поднимаются materializer'ом без ручного
  копирования units и редактирования generated files.
- Повторный apply идемпотентен, а существующий installation user вызывает явный adopt/recover gate.
- Установка без голов является валидным и наблюдаемым состоянием.

### Decision gates

- Точный package/install transport и поддерживаемая версия Orca.
- Минимальные CPU, RAM и disk requirements для production memory profile.

## Milestone 2. Git-backed recovery

### Outcome

Приватный instance repository используется как автоматический durable checkpoint конфигурации и
нормализованного состояния. Recovery contract не требует отдельного S3 bucket или backup host.
Archive backup, offsite и backup timer выведены из основного пути; archive остаётся ручным
optional cold archive, а не вторым равноправным контрактом.

### Пользовательский путь

```text
install secretary
  -> recover from private remote
  -> enter recovery phrase
  -> rebuild derived state
  -> status
```

### Уже реализовано

- Checkpoint содержит переносимый config/state canon, валидируется до commit и пишется только при
  изменениях.
- Push идёт ff-only раз в 30 минут. Failure и настоящая remote divergence не останавливают работу,
  но остаются fail-closed для checkpoint и видны через `status`/`doctor` вместе с lag.
- Параллельные feature publish и checkpoint commits сериализованы. Ожидаемое interleaving
  восстанавливается автоматически, а чужая remote history остаётся ручной divergence.
- Derived state исключён из checkpoint; archive/offsite больше не участвуют в основном UX и
  doctor gates.

Поддержанный recover-from-private-remote flow и destructive-loss parity на чистом втором target
реализованы. Milestone 2 закрыт; дальнейшие package и live-adopt задачи принадлежат Milestone 1.

### Acceptance gate

- Checkpoint содержит instance config, persona, project/head registry, policies, board export,
  memory facts и необходимый run/audit state.
- Vector index, терминалы, worktrees, generated units и host-local caches не считаются каноном.
- Snapshot проходит validation до commit и push; remote divergence и push failure остаются
  fail-closed и видны в status вместе с checkpoint lag.
- Потеря исходного VPS не мешает восстановить доску, память, operational configuration и
  статические секреты установки из remote.
- После подтверждённой parity основной UX и документация больше не требуют archive bundle/offsite
  transport.

### Зафиксированные решения

- RPO составляет не более 30 минут; checkpoint push использует отдельное 30-минутное окно поверх
  production tick.
- Cold archive для raw transcripts и artifacts допустим только как ручная опция, без timer,
  offsite transport и влияния на recovery readiness.

## Milestone 3. Подключение голов и explainable routing

### Outcome

Пользователь добавляет головы после bootstrap. Система обнаруживает установленный CLI или предлагает
установку, проводит внешний auth flow, проверяет capabilities и создаёт runnable profiles.
Маршрутизация выбирает голову и account без участия нейронной модели.

### Пользовательский путь

```text
secretary head add
  -> discover or install runtime
  -> authenticate account
  -> create account pool and profile
  -> probe
```

### Acceptance gate

- Модель различает agent runtime, account, account pool и head profile; runtime не приравнивается к
  model provider.
- Карточка выбирает `light`, `standard` или `deep`, но может явно задать model или head.
- Router сначала применяет overrides и hard availability constraints, затем учитывает мощность,
  quota/reset state и предпочтение другого model family для review.
- Account со статусом `unknown` доступен оптимистично; quota/auth/transient failures переводят его
  в объяснимое circuit-breaker состояние.
- Каждый run хранит resolved runtime, model, account и decision trace.

### Decision gates

- Источники quota telemetry для каждого runtime.
- Политики автоматической ротации accounts после накопления эксплуатационных данных.

## Milestone 4. Ежедневный control plane

### Outcome

Оператор управляет проектами, настройками и runtime через единый продуктовый интерфейс, не собирая
низкоуровневые команды вручную. CLI остаётся первым интерфейсом; board и live terminal view
покрывают работу и наблюдение.

### Пользовательский путь

```text
add project
  -> scan
  -> propose adapter
  -> provision
  -> gate
  -> smoke card
```

### Acceptance gate

- Верхнеуровневый project workflow сворачивает текущие add/provision/gate стадии в resumable flow.
- `secretary status` объединяет services, schedules, heads, quota state, projects, cards, memory и
  checkpoint freshness; `doctor` остаётся строгой проверкой инвариантов.
- Install, start, stop, logs, upgrade и uninstall доступны через product CLI.
- Schedules и их единственный owner задаются централизованно и применяются идемпотентно.
- Настройки меняются через валидируемые операции, даже пока каноном остаются файлы instance repo.

### Decision gates

- Когда нужен отдельный web control plane.
- Когда Git-backed config стоит заменить control-plane database.

## Milestone 5. Протокольные runtime boundaries

### Outcome

Зависимости от Kanboard, Orca и конкретных CLI локализованы за проверяемыми контрактами. Это не
публичный plugin API, а возможность заменить backend без переписывания task и agent lifecycle.

### Acceptance gate

- Board adapter реализует normalized task model, transitions, audit, export и import contract.
- Session protocol создаёт и перечисляет durable sessions, запускает process, стримит output,
  принимает input, сообщает exit state, завершает process tree и reconciles orphaned state.
- Head adapters реализуют discover/install/probe/launch/delivery/observe без смешивания с task
  routing.
- У каждого контракта есть backend-independent contract suite.
- Отказ Orca UI не разрушает task state и recovery semantics.

### Decision gates

- Оставить Orca, перейти на существующую альтернативу, поддерживать fork или строить минимальный
  собственный session backend.
- Появилась ли реальная потребность во втором board backend и публичном extension API.

## Milestone 6. Публичный open-source release

### Outcome

Новый пользователь может установить поддержанный release, пройти основной путь и понять границы
без знания внутренней истории проекта.

### Acceptance gate

- Есть versioned package, release notes, compatibility matrix, schema/data migrations и rollback.
- Clean-VM E2E проходит install, head onboarding, project add, worker/reviewer task, Git checkpoint
  и recovery на втором target.
- Документированы trusted single-user security boundary, credential scopes и agent host access.
- Лицензия, contribution path, issue templates и минимальные deployment requirements опубликованы.
- Пример не содержит private paths, accounts, projects или исторических repositories автора.

### Decision gates

- Какие telemetry можно собирать локально и только opt-in.
- Как результаты продукта связываются с публичным профилем и консалтингом.

## Поздние направления

После основного delivery path можно добавлять Telegram и голос как новые entry channels, remote
control plane для телефона, richer model-quality metrics и дополнительные deployment profiles.
Командная работа, мультитенантный SaaS и собственная модель биллинга не входят в текущий roadmap.
