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

Первый инкремент доступен как `secretary reconcile plan --instance PATH`; по умолчанию
он читает тот же live inventory, что и `doctor`. `--host-fixture DIR` заменяет live
inventory детерминированным fixture для тестов и offline-проверок. `--offline` нельзя
совмещать с fixture и он не строит plan без inventory. Для offline plan нужен только
`--host-fixture`. Plan не пишет manifest и не меняет host.
Каждая строка плана содержит logical id, kind и
host name. Managed manifest хранит те же поля, canonical desired spec и fingerprint
применённого resource. Для service spec включает role/model, для Orca — explicit
binding name/repo path. При совпадении имени без записи с тем же logical id план выводит
`conflict`; только exact managed record может дать `update` или `delete`.

План fail-closed: heads требуют `host.unit_prefix`, enabled binding требует
явный `orca_binding`. Это plan validation, а не hard schema migration, поэтому
`doctor --offline` остаётся совместимым с instance до явного переноса binding.
При смене host name того же logical id план показывает новый `create` и `delete`
ранее managed старого имени.

До diff plan validates unique logical id и unique pair kind/name. Два desired
ресурса не могут претендовать на один systemd unit или Orca binding.

Exit-коды plan: `0` для прочитанного inventory без conflicts, `1` для conflicts,
`2` для невалидного input или недоступного kind inventory. При недоступности plan
показывает причину для каждого недоступного kind и не строит diff против пустого host.

Существующий desired Orca binding принимается во владение только по одному logical id
через `secretary reconcile adopt`. Команда сверяет имя и нормализованный repo path с
live Orca registry. Без `--yes` она только показывает canonical record и fingerprint;
с `--yes` атомарно обновляет managed manifest. Orca registry, systemd и worktrees при
этом не меняются. Corrupt, duplicate, drifted или symlinked manifest блокирует запись.
Rollback до будущего apply — восстановить предыдущую копию manifest.

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
