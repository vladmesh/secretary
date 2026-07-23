# Destructive recovery drill

Сценарий: оператор открывает свежую сессию Claude Code вне Orca, даёт ей этот файл,
сносит сервер под ноль и восстанавливает установку из приватного instance remote парой
команд. Карточки, память и проекты возвращаются; derived state пересобирается.

Статус: drill НЕ готов к прогону на проде, пока открыты secretary-679 (bootstrap
Kanboard/Orca), secretary-680 (project checkouts + CODEX_HOME) и secretary-681
(runtime.env cleanup). Шаги, закрываемые этими карточками, помечены. Первый прогон
только на одноразовом хосте (LXC-контейнер или дешёвый VPS), прод после зелёного
одноразового прогона.

## Что оператор держит вне сервера

Единственные вещи, которые не восстанавливаются из remote и должны жить в password
manager оператора:

1. Доступ к GitHub: возможность добавить deploy key или выдать PAT для
   `secretary-instance` (private) и project-репозиториев.
2. Логины голов: ChatGPT-аккаунт для `codex login`, Claude-аккаунт для `claude`,
   GitHub-аккаунт для `gh auth login`.
3. Креды хостинг-панели (не на сервере; после secretary-681 их в runtime.env нет).

Всё остальное либо в приватном remote, либо генерится машиной.

## Перед вайпом (на живом хосте)

1. Пайплайн пуст: `secretary task list --state in_progress --state validate` возвращает
   `[]`. Незапушенная ветка in-flight воркера погибнет вместе с хостом.
2. Checkpoint свежий и запушен: в `secretary doctor --instance ~/secretary-instance`
   lag 0 commits / push свежий. RPO контракта 30 минут; перед вайпом дождаться или
   форсировать push производственным тиком.
3. Instance-репо чистый: `git -C ~/secretary-instance status` без незакоммиченного.
4. Первый прогон: снять ручной cold archive (`secretary backup create`) и утащить
   архив с хоста. Belt and suspenders, контрактом не требуется.
5. Решить судьбу `~/.claude` оператора (память ассистента, глобальный CLAUDE.md,
   скиллы): вне продуктового контракта, при вайпе теряется. Либо принять потерю,
   либо забрать tar перед вайпом.

## Вайп

Переустановка ОС из панели хостинга (поддержанный Ubuntu/Debian) либо снос всего
содержимого. После этого на хосте нет ни пользователя dev, ни чекаутов, ни Kanboard,
ни Orca, ни systemd-юнитов.

## Восстановление

Порядок команд для свежей сессии. После recover остаётся ручной только подготовка
project checkouts и managed CODEX_HOME до закрытия secretary-680.

1. Базовый хост: ssh-доступ root/sudo, `apt install git python3.12 python3.12-venv`.
   Опционально Claude Code для оператора.
2. GitHub-доступ: `ssh-keygen`, добавить deploy key к `secretary-instance` и
   project-репозиториям (или PAT в git credential store). Ручной шаг, автоматизации
   не будет: это и есть человеческий секрет.
3. Продукт: клонировать secretary и поставить CLI с memory extra:

   ```bash
   git clone https://github.com/vladmesh/secretary.git ~/secretary
   cd ~/secretary && python3 -m pip install '.[memory]'
   ```

4. Kanboard + Orca: bootstrap ставит pinned Kanboard в Docker и pinned Orca AppImage,
   генерирует API token в `runtime.env` и создаёт Pipeline с колонками и
   swimlanes из project registry:

   ```bash
   sudo secretary bootstrap \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance --installation-user dev
   ```
5. Install:

   ```bash
   secretary install \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance \
     --installation-user dev
   ```

6. runtime.env создаёт bootstrap. Человек не вводит `KANBOARD_*`.
7. Recover:

   ```bash
   secretary recover \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance \
     --installation-user dev
   ```

   Возвращает board, runs, memory facts + index, юниты, role worktrees, automations.
   До secretary-680 руками: клонировать project checkouts из registry и собрать
   managed CODEX_HOME. После 680 это делает recover.
8. Головы: `codex login`, `claude` (логин), `gh auth login`. Интерактивные внешние
   шаги, остаются ручными по контракту (Milestone 3 сделает их управляемыми, не
   автоматическими).

## Проверка parity

1. `secretary doctor --instance ~/secretary-instance` зелёный, checkpoint lag 0.
2. Счётчики совпадают с pre-wipe: `secretary task list | jq length`, число memory
   facts, число runs.
3. Смоук памяти: `memory_search` через mcp возвращает знакомые факты.
4. Смоук пайплайна end-to-end: тестовая карточка через
   `secretary task create --state ready` доходит до Done воркером и ревьюером.
5. Diff production-тика: журнал `secretary-dispatcher-production` показывает
   `status: ok`, checkpoint push проходит на тот же remote.

## Осознанные потери

Не восстанавливаются и не должны: Orca-терминалы и worktrees попыток, vector index
(пересобирается), journald-логи, сырые transcripts (если не снят cold archive),
`~/.claude` оператора (если не забран руками), незапушенные ветки воркеров.
