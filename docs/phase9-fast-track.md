# Phase 9 fast track: два активных репозитория

## Цель

Перейти к установке секретаря с двумя активными репозиториями:

- публичный `secretary` содержит продуктовый код, runtime, CLI, MCP, тесты и документацию;
- приватный `secretary-instance` содержит persona, project bindings, policies и настройки конкретной
  установки;
- изменяемые данные остаются вне git в `secretary-data`.

Полная сохранность старых карточек, логов и project-specific памяти не является гейтом. Нужны
разумные предосторожности, рабочий текущий контур и возможность продолжать разработку без старых
meta-checkout.

## Принятые ограничения

- Cutover делается на текущем сервере поверх существующих Kanboard и `secretary-data`.
- Идеальный off-host restore и абсолютная parity позиций Kanboard не блокируют Phase 9.
- Query embeddings остаются локальными. Перенос их во внешний API не рассматривается.
- Полный embedding rebuild не запускается при обычном backup.
- Старые remotes можно оставить архивными, но их checkout, services и timers не должны быть нужны
  живой установке.

## Выполненная уборка

Перед консолидацией удалены `project_inspect`, `inspect_notebooks` и `agent-kanban`: локальные
checkout, Orca registrations, bindings, активные записи реестра, project caches и 22 memory facts.
Уникальные Inspect-документы сохранены в приватном `vladmesh/inspect-project-archive`. После уборки
journal, export и memory index содержат по 233 факта, `memory verify` и `doctor --offline` зелёные.

## Последовательность

### 1. Отвязать backup от embedding rebuild

Обычный backup сохраняет git-журнал facts и текстовый export. `index.sqlite` считается производным
cache: его можно сохранить как необязательную оптимизацию, но отсутствие или устаревание индекса не
делает архив невалидным. Backup и его verify не загружают embedding-модель и не запускают reindex.

Полный rebuild должен выполняться через уже запущенный memory daemon, чтобы не держать вторую копию
`intfloat/multilingual-e5-large` в отдельном процессе. Отдельный offline-процесс допустим только при
остановленном daemon.

Гейт: штатный backup не меняет RSS memory service, не создаёт процесс `reindex.py` и проходит verify
по каноническим данным без требования свежего vector index.

### 2. Добавить инкрементальное обновление memory index

Индекс хранит для каждого факта стабильный id и hash текста вместе с embedding и версией модели.
При изменении журнала:

- новый или изменённый hash получает новый document embedding;
- неизменённая строка переиспользуется;
- удалённый fact удаляется из metadata и vector tables без пересчёта остальных;
- полный rebuild нужен только при смене модели, схемы или повреждении базы.

Query embedding продолжает считаться локальной горячей моделью. OpenRouter для этого пути не нужен.

Гейт: add, update и delete одного факта дают зелёный `memory verify`, не запускают полный rebuild и
не создают вторую копию модели.

### 3. Поглотить `memory-mcp` в `secretary`

Перенести MCP server, sqlite-vec schema, incremental update, rebuild entry point, service packaging и
тесты в `secretary`. Модель, dimension, port и installation-specific пути задаются
`secretary-instance`. Канон остаётся в `secretary-data/memory`.

Сохранить публичный `memory_search` contract для существующих клиентов. После cutover отдельный
checkout `memory-mcp` не нужен для install, runtime, backup или restore.

Гейт: memory service устанавливается из `secretary`, отвечает на реальный `memory_search`, переживает
restart и проходит add/update/delete smoke без checkout `~/memory-mcp`.

### 4. Поглотить живой runtime `triggered-agents`

Переносить только используемый контур: dispatcher loop, task protocol adapters, запуск worker и
reviewer, transitions, retry и необходимые steward/curator hooks. Не переносить историю repo,
устаревшие compatibility-модули и отключённые эксперименты.

Актуальные persona и installation-specific настройки из `control-panel` переходят в
`secretary-instance`; продуктовые docs, skills и contracts переходят в `secretary`. Исторические
документы можно оставить в архивных remotes.

Гейт: новый dispatcher один владеет pipeline, одна карточка проходит worker и reviewer, старый
`triggered-agents` dispatcher выключен.

### 5. In-place cutover и удаление старых checkout

Перед переключением создать один encrypted backup и проверить, что архив читается. Затем:

1. остановить старые dispatcher, curator и timers;
2. установить новый runtime из `secretary` и применить `secretary-instance`;
3. использовать существующие Kanboard и `secretary-data`, без restore;
4. проверить memory search и провести одну pilot-card;
5. оставить старые checkout выключенными на короткое rollback-окно;
6. удалить `memory-mcp`, `triggered-agents`, `control-panel` и `panelmem-kb` с хоста.

Гейт: `doctor` зелёный, backup зелёный, memory search работает, pilot-card завершена, удаление старых
checkout не меняет результат.

## Что сознательно не блокирует cutover

- восстановление каждого Done task, комментария, transcript и лога;
- абсолютные числовые позиции карточек после импорта в чистую Kanboard;
- зелёный restore на отдельном headless VPS с Orca GUI;
- raw Kanboard dump как обязательный переносимый канон;
- внешний embedding API и оптимизация его стоимости.

## Ближайшая нарезка

Не создавать весь хвост заранее. Ближайшие карточки:

1. backup без reindex и rebuild через daemon;
2. incremental memory add/update/delete;
3. перенос memory runtime в `secretary`;
4. после memory cutover уточнить фактический остаток `triggered-agents` и нарезать его перенос;
5. последней карточкой сделать in-place cutover с pilot и decommission report.

Если фактическая реализация меняет границы, обновлять этот документ, а не поддерживать параллельный
план в `control-panel`.

## Состояние 2026-07-17

Кодовые шаги 2–4 выполнены напрямую, без pipeline. Memory runtime и automation helpers находятся
в `secretary`, incremental add/update/delete покрыт тестами, persona/config обновлены в
`secretary-instance`. Живые сервисы и доска не переключались по явному решению vladmesh.

Шаг 1 не требует отдельной кодовой карточки: текущий backup не запускает reindex и валидирует
канонические данные независимо от свежести производного индекса. Оставшийся шаг Phase 9 — только
контролируемый in-place cutover и последующий decommission. Фактические проверки и граница
cutover записаны в `docs/phase9-absorption.md`.
