# Product gaps

`secretary` уже содержит основное runtime-ядро: task-протокол, dispatcher, worker/reviewer lifecycle,
curator/steward/retro, memory journal и MCP, backup/restore, onboarding, provisioning, gate, doctor и
host ownership planning. Главный разрыв до продукта находится не в агентской логике, а в delivery
и едином control plane.

Целевой пользовательский путь:

```text
install secretary
  -> create secretary-instance
  -> configure board, credentials and heads
  -> apply host resources
  -> add a project
  -> complete the first card
  -> verify backup and restore
```

Сейчас этот путь требует ручной сборки instance, env, systemd, Orca registrations, board и
automation schedules. Live installation доказала работу компонентов, но не clean-room deployment.

## P0: развёртывание новой установки

### Создание instance

Нужна команда уровня `secretary instance init`, которая создаёт минимальный валидный
`secretary-instance`: `instance.yaml`, persona, heads, policies, projects, adapters, `.gitignore`,
шаблон runtime env и initial Git commit. Текущий `bootstrap --empty` создаёт data plane, но не
полноценную установку.

### Install и apply

`reconcile plan` умеет показать desired host state, а `reconcile adopt` принимает существующий
ресурс под управление. Нет команды, которая применяет план на чистом host. Нужен поддержанный
`install`/`reconcile apply`, создающий venv или package installation, systemd units, timers, Orca
registrations, постоянные agent workspaces, memory service, dispatcher и backup runtime.

### Переносимые host resources

Systemd assets и часть runtime defaults содержат `/home/dev`, конкретные checkout paths, имя
пользователя, Orca workspace layout, `CODEX_HOME` и `cp-kanboard`. Units должны рендериться из
instance config и обнаруженного host context, а не редактироваться оператором.

### Один scheduler owner

Для curator, steward и retro пока существуют два альтернативных scheduler: systemd и Orca
Automations. Desired schedule и owner не хранятся в instance. Нужен instance-контракт с
`owner: systemd|orca`, enabled state, timezone, schedule и missed-run policy. Apply должен
гарантировать одного owner и отключать альтернативный.

### Board provisioning

Продукт предполагает готовый Kanboard с нужным проектом, колонками, swimlanes, metadata и API
credentials. Нужны два поддержанных режима: bundled board и подключение существующего Kanboard с
compatibility check. Provisioning должен быть идемпотентным.

### Credentials и providers

Нет setup flow для Kanboard token, age key, GitHub access, Codex/Claude/OpenRouter и Orca runtime.
Нужны шаблон secret policy, wizard или последовательный CLI, проверка доступа и ясное разделение
обязательных и опциональных providers.

## P1: первый проект и ежедневная эксплуатация

### Единый onboarding workflow

Низкоуровневые `project add`, `provision-start`, `provision-apply` и `project gate` реализуют
надёжный протокол, но требуют знания внутренних стадий. Верхнеуровневый workflow должен провести
пользователя через scan, adapter proposal, provisioning, diff, gate, Orca registration и smoke
card. Низкоуровневые команды остаются для диагностики и recovery.

### Настройка голов

Нужны provider probes, проверка авторизации и готовые минимальные профили: Codex-only,
Claude-only, mixed и API-based. Пользователь должен видеть, какие роли обязательны, какие модели
доступны и почему выбран конкретный fallback.

### Единый status view

Нужен `secretary status`, который объединяет service/timer state, scheduler ownership, provider
health, проекты, активные карточки, backup freshness, memory parity и последние запуски
curator/steward/retro. `doctor` сохраняет роль строгой проверки инвариантов.

### Release и upgrade contract

Нужны публикуемые releases/packages, воспроизводимые dependencies, compatibility matrix, migrations
для instance schema и data layout, `upgrade plan/apply`, rollback и versioned uninstall. Установка
из рабочего checkout не является продуктовым delivery path.

### Service lifecycle

Нужны поддержанные команды install/start/stop/status/logs/uninstall вместо ручного копирования
units и прямого управления `systemctl`.

### Backup lifecycle

Нужны генерация age key, instance policy retention, offsite setup, disk-space checks, last-success
status и периодический restore drill. Service failure должен влиять на общую readiness, даже если
старые архивы ещё валидны.

### Decommission

Нужна безопасная команда, отдельно поддерживающая stop runtime, remove services, remove Orca
registrations, preserve data и full uninstall. Cutover показал, что удаление checkout не удаляет
stale Orca projects и automations автоматически.

## P1: security и platform contract

### Security model

Нужно описать trust boundary, host permissions агентов, credential scopes, sandbox policy, audit
retention и безопасные defaults. Первая версия может честно поддерживать только trusted single-user
host, но это должно быть явным контрактом.

### Orca dependency

Нужно решить, является ли Orca частью appliance или внешней обязательной платформой, затем
зафиксировать install step, поддерживаемые версии, health probe, upgrade order и degraded mode.

### Provider requirements

Документация должна объяснять, какие подписки или API keys нужны, можно ли работать с одним
provider и какие функции пропадают без конкретного adapter.

## P2: зрелость экосистемы

Нужны стабильный extension contract для adapters, heads, roles и skills; локальные opt-in метрики
dispatcher lag, card cycle time, launch failures, backup age и automation success; примеры instance
для основных deployment profiles.

## Acceptance gate

Главная продуктовая проверка должна проходить на чистой VM без `/home/dev`, legacy repositories и
ручных артефактов:

1. Установить released package.
2. Создать новый instance и data plane.
3. Настроить один provider и board.
4. Применить host resources.
5. Добавить тестовый проект.
6. Провести карточку через worker и reviewer до Done.
7. Создать encrypted backup.
8. Восстановить установку на втором чистом target.

Unit и component E2E тесты остаются обязательными, но не заменяют этот сценарий.

## Минимальный продуктовый релиз

Реалистичный первый supported profile:

- Ubuntu/Debian, один доверенный пользователь, одна машина;
- Orca как явная обязательная dependency;
- bundled Kanboard через Docker Compose;
- один `secretary-instance`;
- Codex-only как первый provider profile;
- systemd как единственный scheduler;
- CLI без отдельного UI;
- Git repository для instance и encrypted backup с документированным offsite pull.

Минимальный внешний интерфейс:

```bash
pipx install secretary
secretary instance init ~/secretary-instance
secretary configure
secretary install
secretary project add ~/projects/my-app
secretary status
```

Пять первых productization outcomes:

1. `secretary instance init`.
2. `secretary install` или `secretary reconcile apply`.
3. Renderer без host-specific paths.
4. Scheduler ownership и schedules в instance schema.
5. Clean-VM E2E от установки до первой Done-карточки и restore.

