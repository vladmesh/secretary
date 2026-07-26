# Destructive recovery drill

Сценарий: оператор открывает свежую сессию Claude Code вне Orca, даёт ей этот файл,
сносит сервер под ноль и восстанавливает установку из приватного instance remote парой
команд. Карточки, память и проекты возвращаются; derived state пересобирается.

Первый прогон выполняется на одноразовом хосте. Для пересборки
`intfloat/multilingual-e5-large` нужно не менее 4 ГБ RAM либо достаточный swap; на
маленьком 2-гигабайтном VPS memory reindex можно вынести из инфраструктурного smoke,
но перед production wipe его надо проверить на хосте подходящего размера.

## Что оператор держит вне сервера

Единственные вещи, которые не восстанавливаются из remote и должны жить в password
manager оператора:

1. Доступ к GitHub: возможность добавить deploy key или выдать PAT для
   `secretary-instance` (private) и project-репозиториев.
2. Логины голов: ChatGPT-аккаунт для `codex login`, Claude-аккаунт для `claude`,
   GitHub-аккаунт для `gh auth login`.
3. Креды хостинг-панели (не на сервере; после secretary-681 их в runtime.env нет).
4. Recovery phrase хранилища секретов (`secretary secret init`): она нужна только на
   чистом хосте, чтобы пересобрать installation key, и продукт её нигде не хранит
   — ни на диске, ни в git. Потеря фразы не топит установку: она означает
   перевыпуск секретов (`secret set` заново для каждого), а не потерю остального.

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

Переустановка ОС из панели хостинга (поддержанный Ubuntu 24.04) либо снос всего
содержимого. После этого на хосте нет ни пользователя dev, ни чекаутов, ни Kanboard,
ни Orca, ни systemd-юнитов.

## Восстановление

Порядок команд для свежей сессии. Project checkouts и не-секретная часть managed
CODEX_HOME восстанавливаются автоматически.

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

4. Kanboard + Orca: на Ubuntu 24.04 bootstrap ставит pinned Kanboard в Docker,
   запускает и проверяет Docker, ставит pinned Orca AppImage-CLI в
   `/usr/local/bin/orca` (Orca runtime — внешний, secretary его не запускает и не
   держит как systemd unit, см. OPERATIONS.md), генерирует API token в
   `runtime.env` и создаёт Pipeline с колонками и swimlanes из project registry:

   ```bash
   sudo secretary bootstrap \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance --installation-user dev
   ```
5. Install:

   ```bash
   sudo secretary install \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance \
     --installation-user dev
   ```

6. runtime.env создаёт bootstrap. Человек не вводит `KANBOARD_*`.
7. Recover:

   ```bash
   sudo secretary recover \
     --instance-remote git@github.com:vladmesh/secretary-instance.git \
     --instance-dir /home/dev/secretary-instance \
     --installation-user dev
   ```

   Возвращает board, runs, memory facts + index, project checkouts, managed
   CODEX_HOME, юниты, role worktrees и automations. Хранилище секретов (если оно
   инициализировано в этом instance-репо) открывается до проверки `runtime.env`:

   - С фразой (`--recovery-phrase-file` / `--recovery-phrase-stdin`, либо
     интерактивный prompt на TTY, если ключа ещё нет): installation key
     пересобирается из фразы, значения расшифровываются и env-файлы, на которые
     указывает каталог, возвращаются побайтово.
   - Без фразы: recover не отказывает и не выдумывает значения, а печатает отчёт
     locked/missing и ничего не пишет. Пример шагов в `--json`:

     ```json
     {"id": "secret:kanboard.api-token", "status": "locked", "detail": "KANBOARD_API_TOKEN -> runtime-env"}
     {"id": "secret:legacy.note", "status": "missing", "detail": "- -> not materialized"}
     ```

   - Неверная фраза — явная ошибка (`RecoveryPhraseError`), recover не оставляет
     после себя ключа и не трогает то, что уже было на диске.
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

Отдельно: весь стейт Orca-сервера живёт вне продукта, в `~/.config/orca`
(`orca-data.json`, `orchestration.db`, device-токены, e2ee-ключ) и в
`/home/dev/orca/workspaces`. В checkpoint он не входит. После вайпа десктопные
клиенты придётся пэйрить заново, а связку remote-browser с ноутом оператора
поднимать повторно через `orca environment add --name <имя> --pairing-code <код>`.
Recovery этого не делает и не должен: pairing-код выдаёт человек. Планировать как
ручной шаг после шага 8, иначе восстановление зелёное, а браузерный мост мёртв.
