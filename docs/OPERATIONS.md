# Эксплуатация

## Текущее состояние

Live ownership принадлежит `secretary`: production dispatcher, memory daemon, backup runtime и
systemd timers работают из product checkout. `secretary-instance` хранит installation config,
`secretary-data` хранит mutable data. Legacy checkouts и units удалены.

Живой `doctor`, memory parity, task read/write и systemd backup были проверены после cutover.
Исторический журнал процедуры доступен в Git history и не является действующим runbook.

## Установка и проверка кода

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m unittest
```

Первый вариант ставит CLI, второй добавляет memory runtime. Эти команды проверяют checkout и пока
не устанавливают unit, не создают Kanboard/Orca и не применяют host resources. Поддержанный
автоматический installer является первым milestone [Roadmap](ROADMAP.md).

Для проверки действующей установки использовать `doctor`, `reconcile plan`, `memory verify` и
`backup verify` по контракту из [Protocols](PROTOCOLS.md).

## Runtime secrets

Секреты установки живут только в host `runtime.env` рядом с `instance.yaml`. Файл должен быть
`0600`, находится в `.gitignore` instance-репозитория и не входит в checkpoint или archive
payload. `secretary shell` получает весь файл для trusted operator-сессии, dispatcher-launched
worker/reviewer получают только allowlisted board credentials и non-secret runtime switches через
`secretary.role_env`.

Instance config не содержит secret materialization inputs. `reconcile` строит host plan из
bindings/config и не расшифровывает secret store.

### Контракт тест-дублей диспетчера

`tests/test_dispatcher_contracts.py` держит `FakeHost`/`FakeCatalog`/`FakeKanboard` в контракте с
`CommandHostRuntime`/`InstanceCatalog`/`KanboardClient`. Набор методов, которые дёргает
`DispatcherRuntime`, вычисляется из исходников (AST), а не ведётся руками, поэтому новый вызов у
реального host автоматически становится требованием к фейку. Формы возвратов сверяются прогоном
реального host в `mode="noop"`.

Вне unit-покрытия остаётся всё, что требует живого стека: сам shell-out в `orca`, `gh` и `git`
внутри `CommandHostRuntime._run*`, отказы Kanboard-транспорта (`TaskError`) в середине board-move
и реальная нумерация позиций карточек. Эти швы проверяет оператор на живом стенде.

## System requirements

Memory runtime загружает локальную embedding model. На production cutover startup занимал около
шести минут и достигал примерно 1.9 GiB RSS. Отдельный target с 1.9 GiB общей RAM не смог завершить
live rebuild. Поддерживаемый minimum ещё не установлен; не считать 2 GiB profile доказанным.

## Data plane

```bash
python3 -m secretary data init --instance INSTANCE
python3 -m secretary data export --instance INSTANCE [--copy-transcripts]
python3 -m secretary data raw-kanboard-dump --instance INSTANCE \
  [--container cp-kanboard] [--source-path /var/www/app/data]
```

`data init` создаёт layout и manifest, включая локальный Git journal memory facts. `data export`
пишет нормализованные board, memory, run и transcript exports; без `--copy-transcripts` сохраняется
только transcript inventory. `raw-kanboard-dump` создаёт timestamped raw dump через `docker cp`,
не пишет в live container и не использует Kanboard API.

## Текущий archive backup

```bash
python3 -m secretary backup create --instance INSTANCE --kind both
python3 -m secretary backup verify ARCHIVE.tar [--strict]
scripts/pull-backups-offsite.sh SSH_TARGET REMOTE_DATA_DIR LOCAL_BACKUP_DIR
```

`create` поддерживает `core`, `full` и `both` и пишет обычный tar archive в `backups/`; archive
encryption не применяется. `verify` возвращает `0` для успешной проверки, `1` для findings либо
strict warnings и `2` для недоступного archive. Retention оставляет последний core и удаляет full
archives старше 48 часов.

Offsite script запускается на внешней машине. Он переносит доступные `*.tar` через `rsync` с
fallback на `scp`, не удаляет local copies и после успеха атомарно обновляет `last_fetch` на host.
`doctor` использует configured max age для warning/finding.

Этот archive path остаётся переходной страховкой, пока Git-backed checkpoint из
[Recovery](RECOVERY.md) не достиг recovery parity. Основной контракт не требует обязательного S3
transport и не переносит host `runtime.env`.

## Archive restore

```bash
python3 -m secretary restore ARCHIVE.tar --instance INSTANCE [--dry-run]
python3 -m secretary restore-board --instance INSTANCE
python3 -m secretary memory reindex --instance INSTANCE
python3 -m secretary reconcile plan --instance INSTANCE
python3 -m secretary restore-reconcile --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE
```

Restore публикует data root только после успешной проверки и extraction в staging. Board import,
memory reindex и host reconcile являются отдельными handoff стадиями. До их завершения `doctor`
сохраняет findings. Vector index является derived state и не входит в backup canon.

## Авто-мёрж зелёных карточек

Когда ревьюер ставит `review:green`, production dispatcher сам доводит карточку до `done`, без
ручного мёржа:

1. Push worker-ветки в `origin/main` fast-forward-only (`git push origin BRANCH:main`). Если main
   разошёлся, push отклоняется — dispatcher не форсит и не подчищает конфликт сам.
2. Fast-forward локального чекаута соответствующего проекта на новый `origin/main`. Для проекта
   `secretary` это self-deploy: production dispatcher мёржит и сразу подтягивает изменения в
   собственный checkout, из которого работает.
3. Teardown воркспейса: dispatcher останавливает терминалы worktree (worker, ревьюер и их
   дочерние процессы) и удаляет worktree через `orca worktree rm`.

Teardown выполняется только на этом Done-пути. При `review:red` (rework) воркспейс и его ветка
остаются нетронутыми, чтобы worker мог продолжить в том же worktree.

Kill-switch: `SECRETARY_DISPATCHER_AUTOMERGE=off` отключает push и fast-forward шаги (`Host.complete_green`)
целиком — карточка всё равно уходит в `done`, но branch остаётся неслитым и требует ручного мёржа.
Дефолт — `on` (авто-мёрж включён).

## Вотчдоги ожиданий

Диспетчер ждёт голову в двух точках: `waiting-worker-report` (карточка в In progress) и
`waiting-review-verdict` (карточка в Validate). Раньше оба ожидания повторялись каждый тик без
ограничения, и голова, умершая до отчёта, оставляла карточку висеть (secretary-637, secretary-649).

Теперь каждое ожидание ограничено потолком по времени. Первое превышение — один respawn той же
головы в том же воркспейсе, второе — карточка в Blocked с сигналом оператору.

Потолок — единственный сигнал, лайвнесс-проб нет намеренно. Заголовок терминала голова
перезаписывает своей OSC-последовательностью сразу после старта, а orca `status:running`
залипает на `working` после тихого выхода (637/649/654). Оба сигнала показали бы живую голову
мёртвой и убивали бы здоровые карточки, поэтому потолки заданы с запасом.

- `SECRETARY_REVIEW_VERDICT_STALL_SECONDS` — потолок ожидания вердикта, дефолт 5400.
- `SECRETARY_WORKER_REPORT_STALL_SECONDS` — потолок ожидания отчёта воркера, дефолт 21600.

Тело отчёта и вердикта голова пишет в файл вне воркспейса (`/tmp/secretary-report-<ref>.md`,
`/tmp/secretary-verdict-<ref>.md`, каталог переопределяется `SECRETARY_DISPATCHER_BODY_DIR`), а не
собирает inline в шелле: codex-рантайм режет команды с `rm`, а кавычки и backtick в теле ломают
вызов. Файл остаётся на месте, подчищать его голове не нужно.

## Units

Актуальные templates и их назначение находятся в
[packaging/systemd/README.md](../packaging/systemd/README.md). Юниты раскатывает
`secretary reconcile apply`; ручная установка больше не нужна и не даёт ownership.

Production dispatcher timer запускает one-shot tick. Memory, backup, curator, steward и retro
должны иметь ровно одного scheduler owner.

## Upgrade

`secretary upgrade --instance <dir>` подтягивает новую версию продукта и пере-материализует
установку под неё. Идемпотентна: повторный запуск на актуальном хосте не делает ничего.

```
secretary upgrade --instance /home/dev/secretary-instance --dry-run   # решить всё, ничего не писать
secretary upgrade --instance /home/dev/secretary-instance
```

Шаги, по порядку; каждый печатает `changed`/`unchanged`/`skipped`/`failed`, первый `failed`
останавливает прогон:

| шаг | что делает |
| --- | --- |
| `pull` | `git fetch` + `merge --ff-only` чекаута продукта. Грязный чекаут — отказ. |
| `dependencies` | переустановка в `.venv`, если в pull двигался манифест зависимостей |
| `role-skills` | `role_skills sync` в shell-овые skill-директории |
| `role-worktrees` | ff worktree ролей (`~/orca/workspaces/secretary/<role>`) на base branch |
| `host` | `reconcile apply`: юниты из `packaging/systemd` + Orca-регистрации |
| `automations` | create/repoint Orca-автоматизаций из `automation.toml` |
| `memory` | рестарт `secretary-memory.service`, если менялся код, зависимости или сам юнит |
| `verify` | повторный dry-run: вторая раскатка обязана быть no-op |

Флаги: `--no-pull` (только пере-материализация), `--base-branch`, `--product-root`, `--json`.

### Ownership и fail-closed

`reconcile apply` пишет только то, что подтверждено `host-managed.json`. Имя под
`host.unit_prefix`, которого нет ни в плане, ни в manifest, — это `conflict`, и любой conflict
отменяет весь прогон до первой записи. Разрешить можно двумя способами:

- юнит действительно наш и совпадает с packaged-файлом байт в байт →
  `secretary reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`;
- имя принадлежит чему-то другому (например dev-only `secretary-supervisor.*`) → перечислить его
  в `host.foreign_units` в instance.yaml.

Юнит, который отличается от packaged-файла, adopt не примет: сначала удалить его руками
(`sudo rm /etc/systemd/system/<name>`) и дать `apply` поставить канон, либо разобраться, почему
хост разошёлся с продуктом.

Компонент, который эта установка сознательно не крутит, выключается в конфиге, а не отсутствием
юнита на хосте:

```yaml
host:
  components:
    curator:
      enabled: false
      reason: "load shedding, secretary-XXX"
```

Выключенный компонент, юнит которого стоит и принадлежит нам, будет остановлен и удалён.

### Разовая миграция существующей установки

В desired-состояние юнита теперь входит дайджест packaged-файла, поэтому первый прогон на хосте,
который ставили руками, потребует один раз навести порядок:

1. `secretary upgrade --instance <dir> --dry-run` — посмотреть список conflict-имён.
2. Для каждого юнита, который байт в байт совпадает с `packaging/systemd/`:
   `secretary reconcile adopt --instance <dir> --logical-id systemd:unit:<name> --yes`.
3. Юниты не нашей установки (`secretary-supervisor.*`) — в `host.foreign_units`.
4. Уже записанные в manifest юниты покажут `update` — это refresh fingerprint под новую схему;
   apply перезапишет файл каноном (для совпадающих файлов запись байт-в-байт).
5. Повторить dry-run: он должен быть чистым, после этого запускать upgrade без флага.

### Health-набор

Детерминированный набор, пригодный как gate перед и после upgrade:

```
secretary doctor --instance <dir>
secretary role-skills audit --check
secretary dispatcher production-tick --instance <dir> --probe
python3 -m unittest discover -s tests
```

`--probe` — это настоящий сухой тик: он берёт тот же singleton-lock, проходит те же mutation
guards, сканирует те же состояния карточек и прогоняет ту же логику решения, но первая же запись
превращается в abort и попадает в отчёт как «что сделал бы следующий тик». Зелёный probe при
сломанном тике невозможен — сломанный тик падает и здесь.
