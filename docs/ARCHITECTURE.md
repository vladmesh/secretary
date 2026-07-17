# Архитектура secretary

Статус на 2026-07-17: код продукта собран в `secretary`, а конфигурация установки — в
`secretary-instance`. Это side-by-side состояние. Production-доска, dispatcher, timers и memory
service ещё принадлежат legacy-контуру до отдельного решения оператора.

## Граница трёх хранилищ

```text
secretary                 продукт: CLI, runtime, схемы, тесты, generic skills
secretary-instance        приватная конфигурация одной установки
secretary-data            изменяемые board/memory/runs/transcripts/backups
```

`secretary` не хранит bindings реальных проектов, секреты, карточки, транскрипты и host-local
state. `secretary-instance` хранит persona, bindings, adapters, policies и profiles голов.
`secretary-data` не является одним git-репозиторием: журнал memory facts внутри него ведётся
отдельным локальным Git, остальные артефакты — data plane и backup input.

Структурированный реестр подключённых проектов живёт только в
`secretary-instance/projects/`; его нельзя дублировать в этом документе.

## Потоки

```text
Головы ── task protocol ──> board
  │                          ▲
  │                          │ legacy dispatcher до cutover
  │
  └─ memory_search <── MCP/index <── facts journal <── secretary memory <── curator
```

Memory facts — markdown-файлы в `secretary-data/memory/facts`. `export.ndjson` и SQLite/vector
index производны и восстанавливаются из facts. Куратор остаётся единственным writer-ролью;
головы читают память через MCP. В product code есть memory service и reindex, но установленный
`memory-mcp` service пока исполняется из legacy checkout.

Task protocol нормализует работу с существующей доской. До production cutover legacy dispatcher
остаётся единственным владельцем обычных claims, Validate и watchdog. Product dispatcher может
работать только в явно ограниченном pilot-контуре.

## Ownership и безопасность

- `doctor` читает config, data и host inventory, но не меняет host.
- `reconcile plan` показывает desired state. Изменять ресурс разрешено только при подтверждённом
  managed manifest или managed marker; совпадение имени или префикса не даёт ownership.
- Secrets принадлежат instance и не попадают в product docs, facts, exports или diagnostics.
- Старые `control-panel`, `memory-mcp`, `panelmem-kb` и `triggered-agents` — архивы или
  переходные runtime owners, а не входы нового install/restore contract.

Командные контракты — в [PROTOCOLS.md](PROTOCOLS.md), безопасная эксплуатация и доказанный статус
— в [OPERATIONS.md](OPERATIONS.md), оставшаяся работа — в [ROADMAP.md](ROADMAP.md).
