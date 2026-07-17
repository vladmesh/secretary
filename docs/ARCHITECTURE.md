# Архитектура secretary

`secretary` является субстратом для нескольких взаимозаменяемых agent heads. Он владеет task и
memory protocols, dispatcher lifecycle, installation contracts и восстановлением. Нативные CLI
провайдеров выполняют работу поверх этого контура.

На текущей production-установке dispatcher, memory daemon, backup runtime и фоновые роли работают
из `secretary`. Legacy runtime repositories и units удалены.

## Граница хранилищ

```text
secretary                 продукт: CLI, runtime, схемы, тесты, generic skills
secretary-instance        приватная конфигурация одной установки
secretary-data            изменяемые board, memory, runs, transcripts и backups
```

Product repository не хранит bindings реальных проектов, credentials, карточки или host-local
state. Instance содержит persona, project bindings, adapters, policies, head profiles и encrypted
configuration material. Структурированный реестр проектов живёт только в
`secretary-instance/projects/`.

`secretary-data` хранит mutable runtime state. Memory facts внутри него ведутся Git-журналом;
остальные компоненты экспортируются в нормализованном виде. SQLite/vector index, worktrees,
терминалы и generated host resources считаются производными.

Целевой Git-backed recovery checkpoint описан в [Roadmap](ROADMAP.md). Пока он не реализован,
instance repo и archive/data restore остаются раздельными действующими контурами.

## Runtime flow

```text
operator / automation
          │
          ▼
  secretary task protocol ────────> Kanboard
          │                            ▲
          ▼                            │
 production dispatcher ──> head adapter ──> Orca session ──> native agent CLI
          │
          └──── run/audit state ──────┘

agent heads ── memory_search ──> MCP/index <── facts journal <── curator
```

Kanboard является текущим live task store. Все поддержанные записи проходят через
`secretary task`, который применяет role guards, transitions и append-only audit. Dispatcher
разрешает routing, создаёт worker/reviewer lifecycle и сверяет board, workspace, report и review
state перед переходами.

Orca является текущим session manager и live terminal UI. Launch и cleanup ещё зависят от его
конкретного API; целевой session protocol остаётся roadmap milestone. Head-specific render и
delivery локализованы в adapters, но текущий public contract ещё не является стабильным plugin API.

## Memory plane

Facts лежат как markdown records в `secretary-data/memory/facts`. Куратор является writer-ролью и
пишет через `secretary memory propose/commit/supersede`; другие головы читают через MCP.
`export.ndjson` и SQLite/vector index восстанавливаются из журнала. Только один index writer может
публиковать производное состояние одновременно.

Embedding model загружается локально. В production-проверке startup достигал примерно 1.9 GiB RSS;
отдельный target с 1.9 GiB общей RAM не смог выполнить live rebuild. Точный поддерживаемый minimum
ещё должен быть определён clean-host тестами.

## Ownership и безопасность

- Текущий security profile предполагает одного доверенного владельца VPS. Агенты ещё не изолированы
  как недоверенные tenants.
- `doctor` читает config, data и host inventory, но не меняет host.
- `reconcile plan` строит desired state. Имя или prefix не дают ownership без managed manifest или
  secretary-owned marker.
- Secrets принадлежат instance/runtime environment и не попадают в product docs, facts, exports или
  diagnostics.
- Task audit и pending writes fail-closed: незавершённая board mutation блокирует согласованный
  export и recovery checkpoint.

Командные контракты находятся в [Protocols](PROTOCOLS.md), действующие runbooks в
[Operations](OPERATIONS.md), продуктовая цель в [Vision](VISION.md). Исполняемая очередь работы
находится только на Pipeline board.
