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
python3 -m secretary backup verify ARCHIVE.tar.age [--age-identity FILE] [--strict]
scripts/pull-backups-offsite.sh SSH_TARGET REMOTE_DATA_DIR LOCAL_BACKUP_DIR
```

`create` поддерживает `core`, `full` и `both`; age recipient берётся из instance или задаётся
явно. `verify` возвращает `0` для успешной проверки, `1` для findings либо strict warnings и `2`
для недоступного archive. Retention оставляет последний core и удаляет full archives старше 48
часов.

Offsite script запускается на внешней машине. Он переносит доступные `*.tar.age` через `rsync` с
fallback на `scp`, не удаляет local copies и после успеха атомарно обновляет `last_fetch` на host.
`doctor` использует configured max age для warning/finding.

Этот archive path остаётся действующей страховкой, пока Git-backed checkpoint не достиг recovery
parity. Целевой основной contract описан в Roadmap и не требует обязательного S3 transport.

## Archive restore

```bash
python3 -m secretary restore ARCHIVE.tar.age --instance INSTANCE \
  --age-identity IDENTITY [--dry-run]
python3 -m secretary restore-board --instance INSTANCE
python3 -m secretary memory reindex --instance INSTANCE
python3 -m secretary reconcile plan --instance INSTANCE
python3 -m secretary restore-reconcile --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE
```

Restore публикует data root только после успешной проверки и extraction в staging. Board import,
memory reindex и host reconcile являются отдельными handoff стадиями. До их завершения `doctor`
сохраняет findings. Vector index является derived state и не входит в backup canon.

## Units

Актуальные templates и их назначение находятся в
[packaging/systemd/README.md](../packaging/systemd/README.md). Live units были установлены вручную
из этих assets. Любое изменение должно сначала пройти render/reconcile review и
`systemd-analyze verify`; копирование template само по себе не передаёт ownership.

Production dispatcher timer запускает one-shot tick. Memory, backup, curator, steward и retro
должны иметь ровно одного scheduler owner. Автоматический apply и централизованный schedule
contract остаются roadmap work.
