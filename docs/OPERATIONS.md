# Эксплуатация

## Текущее состояние

Code absorption подтверждён side-by-side. Product включает memory runtime, compatibility runtime,
automation helpers и generic skills; instance владеет persona и memory settings. Это не cutover:
живые `memory-mcp.service`, `ta-*`, production dispatcher и доска продолжают работать в legacy
контуре.

Нельзя выводить обратное из наличия package, systemd templates или успешного теста. Установка
новых units, остановка старых owners, изменение board state и удаление checkout'ов требуют
отдельной операторской процедуры.

## Установка и проверка кода

```bash
python3 -m pip install .
python3 -m pip install '.[memory]'
python3 -m unittest
```

Первый вариант ставит CLI, второй добавляет memory runtime. Эти команды проверяют checkout и не
устанавливают unit, не запускают daemon и не меняют owner live services. Для проверки установки
сначала использовать `doctor`, `reconcile plan`, `memory verify` и `backup verify` по контракту из
[PROTOCOLS.md](PROTOCOLS.md).

## Data plane

```bash
python3 -m secretary data init --instance INSTANCE
python3 -m secretary data export --instance INSTANCE [--copy-transcripts]
python3 -m secretary data raw-kanboard-dump --instance INSTANCE \
  [--container cp-kanboard] [--source-path /var/www/app/data]
```

`data init` создаёт layout и manifest для указанного instance, включая локальный Git journal
memory facts. `data export` пишет нормализованные board, memory, run и transcript exports; без
`--copy-transcripts` для transcripts сохраняется inventory. `raw-kanboard-dump` делает отдельный
timestamped raw dump в data plane через `docker cp`, не пишет в live container и не использует
Kanboard API.

## Backup и offsite

```bash
python3 -m secretary backup create --instance INSTANCE --kind both
python3 -m secretary backup verify ARCHIVE.tar.age [--age-identity FILE] [--strict]
scripts/pull-backups-offsite.sh SSH_TARGET REMOTE_DATA_DIR LOCAL_BACKUP_DIR
```

`create` поддерживает `--kind core`, `full` и `both`; recipient берётся из instance или задаётся
`--age-recipient`. `verify` принимает archive как позиционный аргумент, а не `--instance`; code
`0` означает успешную проверку, `1` — findings или warnings с `--strict`, `2` — archive недоступен
для проверки. После успешного создания retention оставляет только последний core и удаляет full
archives старше 48 часов.

Offsite script запускается на внешней машине. Он переносит все доступные `*.tar.age` из
`REMOTE_DATA_DIR/backups` через `rsync` с fallback на `scp`, не удаляет local copies и только после
успеха атомарно обновляет `last_fetch` на host. Если в instance задан
`offsite.backup_pull_max_age_days`, `doctor` считает отсутствующий marker warning, а stale marker
finding.

Backup должен быть проверяемым без тяжёлой embedding-модели. После restore сначала проверяются
facts journal и export, затем отдельно выполняется `memory reindex`; SQLite/vector index
производен и не является backup canon.

## Units

Актуальные templates и их условия установки находятся в
[packaging/systemd/README.md](../packaging/systemd/README.md). Они не устанавливаются
автоматически. В частности, templates описывают будущий production layout и требуют review через
renderer/reconcile до любого cutover.

## Cutover gate

Перед сменой owner нужны свежий encrypted backup, `doctor`, memory и backup smoke, ограниченная
pilot-card, наблюдение rollback и только затем отдельное решение о production ownership. Полный
production embedding rebuild на target с 1.9 GiB RAM не был доказан; это ограничение стенда, а не
разрешение пропустить будущую проверку на подходящем host.

## Fact audit

22 пути фактов, которые есть только в старом журнале `panelmem-kb`, проверены без импорта. Это
история проектов, заметки о host access, заменённое runtime-поведение или открытые исследования.
В документацию вошли только подтверждённые кодом product-инварианты: schema validation, journal
facts, атомарная публикация derived index, совместимость model/dimension и read-only host planning.
