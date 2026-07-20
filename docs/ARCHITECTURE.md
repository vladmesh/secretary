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
state. Instance содержит persona, project bindings, adapters, policies и head profiles. Секреты
живут только в host `runtime.env` с правами `0600`; файл gitignored в instance repo и не входит в
git checkpoint или archive payload. Структурированный реестр проектов живёт только в
`secretary-instance/projects/`.

`secretary-data` хранит mutable runtime state. Memory facts внутри него ведутся Git-журналом;
остальные компоненты экспортируются в нормализованном виде. SQLite/vector index, worktrees,
терминалы и generated host resources считаются производными.

Git-backed recovery checkpoint описан в [Recovery](RECOVERY.md). Archive/data restore остаётся
переходной страховкой до parity и не является вторым источником secret state.

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

Orca является текущим session manager и live terminal UI. Одна карточка занимает один worktree:
воркер получает свой терминал, ревьюер запускается отдельной split-панелью в том же worktree, и оба
handle хранятся в dispatcher state порознь. Split, а не второй `terminal create`, потому что на
headless-серве созданный терминал приходит фоновой поверхностью и в уже открытом на клиенте
worktree не материализуется. На старте ревью голова воркера гасится, а её коммит запоминается:
merge-gate не принимает green-вердикт, если checkout с тех пор уехал. Launch и cleanup ещё зависят
от конкретного API Orca; целевой session protocol остаётся roadmap milestone. Head-specific render и
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
- Secrets принадлежат host `runtime.env`; в instance git, facts, exports, checkpoint, archives и
  diagnostics они не попадают.
- Task audit и pending writes fail-closed: незавершённая board mutation блокирует согласованный
  export и recovery checkpoint.

Командные контракты находятся в [Protocols](PROTOCOLS.md), действующие runbooks в
[Operations](OPERATIONS.md), продуктовая цель в [Vision](VISION.md). Исполняемая очередь работы
находится только на Pipeline board.
