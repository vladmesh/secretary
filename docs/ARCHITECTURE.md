# Архитектура secretary

`secretary` является субстратом для нескольких взаимозаменяемых agent heads. Он владеет task и
memory protocols, dispatcher lifecycle, installation contracts и восстановлением. Нативные CLI
провайдеров выполняют работу поверх этого контура.

На текущей production-установке dispatcher, memory daemon и фоновые роли работают из `secretary`.
Legacy runtime repositories и units удалены.

## Граница хранилищ

```text
secretary                 продукт: CLI, runtime, схемы, тесты, generic skills
secretary-instance        приватные config и переносимый recovery checkpoint одной установки
secretary-data            локальный mutable/derived runtime data plane
```

Product repository не хранит bindings реальных проектов, credentials, карточки или host-local
state. Instance содержит persona, project bindings, adapters, policies и head profiles.
Восстановимое хранилище секретов (`secretary-instance/secrets/`) держит каталог метаданных и
sealed values в git рядом с board и memory; единственное, что репо никогда не содержит — сырой
installation key и recovery phrase, см. [Recovery](RECOVERY.md), раздел «Секреты». Host
`runtime.env` остаётся отдельным файлом с правами `0600`, gitignored и вне checkpoint или archive
payload; миграция его текущего содержимого в хранилище — отдельный шаг с участием оператора.
Структурированный реестр проектов живёт только в `secretary-instance/projects/`.

`secretary-instance/state/` хранит нормализованный recovery-канон: board, runs, memory facts и
knowledge-документы.
`secretary-data` остаётся локальным рабочим data plane для task audit, dispatcher state, derived
exports/index, search log, raw dumps, transcripts и artifacts. SQLite/vector index, worktrees,
терминалы и generated host resources считаются производными и в checkpoint не входят.

Git-backed recovery checkpoint описан в [Recovery](RECOVERY.md). Ручной cold archive остаётся
необязательным инструментом для сырья и совместимости, но не участвует в recovery readiness.

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

Facts лежат как markdown records в `secretary-instance/state/memory/facts`. Куратор является
writer-ролью и пишет через `secretary memory propose/commit/supersede`; протокол коммитит только
`state/memory` под общим instance-repo writer lock. Другие головы читают через MCP. `export.ndjson`
и SQLite/vector index в `secretary-data/memory/` восстанавливаются из канона. Только один index
writer может публиковать производное состояние одновременно.

Embedding model загружается локально. В production-проверке startup достигал примерно 1.9 GiB RSS;
отдельный target с 1.9 GiB общей RAM не смог выполнить live rebuild. Точный поддерживаемый minimum
ещё должен быть определён clean-host тестами.

## Плоскости знания

Знание разложено на три плоскости, и вопрос «куда это писать» решается по длине и назначению
записи. Pipeline board хранит исполняемую работу: карточки, спеки, статусы. Curated memory
(`state/memory/facts`) хранит короткий актуальный вывод, который голова должна получить в контекст
через `memory_search`. Knowledge (`state/knowledge`) хранит длинное рассуждение и контекст, из
которого вывод получился: брейнштормы, журналы решений, разборы инцидентов.

Knowledge не индексируется, не попадает в `memory_search` и целиком в контекст голов не грузится.
Документ читают адресно, когда нужна история вопроса. Формат свободный: это обычный tracked
markdown, никакого frontmatter или метаданных писатель не требует. Пишут через
`secretary knowledge write`; писатель владеет только `state/knowledge`, берёт общий instance-repo
writer lock и проверяет документ на секреты, поэтому ручной `git commit` в knowledge не нужен и
гонки с тиковым писателем не создаёт.

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
