# Host contract for Phase 7

Решения grill-сессии 2026-07-14 для перехода dispatcher и `reconcile`.

## Desired state and ownership

Desired host state вычисляется из instance config, heads, policies, projects и runtime
components. Ручные списки `host.units` и `host.orca_repos` не являются вторым
каноном. Instance задаёт границы владения и входные параметры, а применённый результат
`reconcile` записывает в managed manifest в data.

`reconcile` создаёт, обновляет или удаляет ресурс только если его подтверждает managed
manifest или проверяемая managed-метка. Префикс имени нужен для поиска конфликтов, но
сам по себе не даёт права менять ресурс. Чужие units, Orca bindings, worktrees и
интерактивные сессии остаются вне владения secretary.

Ни generic multi-instance, ни lease/fencing, ни отдельный `instance_id` в этот
контур не входят. Старый dispatcher переносится pilot-фильтром: pause, cutover,
проверка pilot-карточки и ручной rollback при неуспехе.

## Doctor

`secretary doctor` всегда read-only и по умолчанию проверяет config, data и живой
host. У команды нет `--dry-run`. `--offline` отключает только host-проверку; он
нужен для fixture и изолированных проверок config/data.

Exit-коды:

- `0`: проверка завершилась без findings; warnings допустимы;
- `1`: проверка завершилась, но есть findings; `--strict` также превращает warnings
  в `1`;
- `2`: проверка не завершилась, например из-за невалидного config или недоступного
  inventory.

Недекларированная поверхность остаётся warning: отсутствие ресурса вне границы
владения и ожидаемые миграционные артефакты не делают doctor красным без `--strict`.

## Project names

Project `id` остаётся стабильным логическим идентификатором. Путь к репозиторию и
имя Orca binding задаются явно в binding, поэтому дефисы и подчёркивания не
преобразуются неявно.

До появления Phase 7 живой checkout instance обновляется вручную. После появления
соответствующего контура это обязанность `secretary upgrade`.
