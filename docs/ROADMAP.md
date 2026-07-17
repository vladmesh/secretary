# Roadmap

Это outcome-level план, не копия доски. Карточки остаются единственным исполняемым источником
работы; при подготовке изменения читать доску через её protocol.

## 1. Завершить controlled cutover

Сделать `secretary` и `secretary-instance` единственными operational repositories только после
свежего backup, зелёных doctor/memory/backup checks, одного pilot-card и rollback window. Убрать
legacy runtime dependencies, writers и host owners после подтверждённого результата, а не в ходе
install или test. Provenance: `secretary-602`, `secretary-613`.

## 2. Укрепить data и recovery boundary

Довести parity raw Kanboard dump и normalized export, безопасную публикацию memory index,
manifest/model/dimension verification и исключение конкурирующих index writers. Отдельно
проверить path/config loading, Git environment isolation и data-entry outcomes. Provenance:
`secretary-535`, `574`, `578`, `584`, `605`, `609`, `610`, `613`; `memory-mcp-323`,
`587`–`593`.

## 3. Укрепить agent и pipeline runtime до decommission

Снизить blast radius credentials, закрыть self-kill и force-push классы, сделать паузу,
watchdog, review retries, health probes, terminal cleanup и recovery наблюдаемыми и
идемпотентными. Сохранять один owner на automation и не превращать feedback в auto-fix.
Provenance: `triggered-agents-246`, `263`, `270`, `286`, `288`, `331`, `340`, `341`, `360`,
`375`, `401`, `405`, `411`, `458`, `463`, `484`, `491`, `546`.

## 4. Свести документацию и delivery roles

Перенести только действующие contracts в product/instance docs, а project-specific history
оставить в её репозиториях. Проверить sync role skills, memory availability в heads, актуальные
model profiles и non-interactive bring-up. Provenance: `triggered-agents-421`, `424`, `453`,
`498`, `565`, `616`; `control-panel-351`; `panelmem-kb-617`.

## Planned: routing models

Automatic family selection и quota-driven routing не входят в текущий protocol. Пока карточка
несёт явный head/review-head, а runtime применяет instance profiles и их fallback. Позже можно
добавить нормализованную complexity, family preference, quota telemetry, объяснимое resolution и
эскалацию после failed review. Это будущая работа, а не описание текущего поведения.

## Отложено

Не строить automatic subscription balancing, container secret broker, broad multi-instance lease
или asynchronous stand-run без измеримого operational need. Security isolation и secrets policy
сначала проходят отдельный design review.
