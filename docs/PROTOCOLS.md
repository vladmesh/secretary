# Протоколы secretary

`--instance` принимает каталог instance или прямой путь к `instance.yaml`. Instance задаёт
конфигурацию установки, а `secretary-data` хранит изменяемое состояние. Команды ниже не являются
инструкцией по cutover: текущие live owners остаются в legacy-контуре.

## Проверка и host ownership

```bash
python3 -m secretary doctor --instance INSTANCE
python3 -m secretary doctor --offline --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE --host-fixture DIR
```

`doctor` всегда read-only. Обычный запуск проверяет config, data и live inventory; `--offline`
оставляет только config/data, а `--host-fixture` заменяет live inventory детерминированным fixture.
Не сочетать fixture с `--offline`: offline не запускает inventory. Exit code: `0` означает
завершённую проверку без findings, `1` — findings или warnings с `--strict`, `2` — невалидный
input либо недоступный inventory. Без `--strict` warnings сами по себе остаются зелёными.

```bash
python3 -m secretary reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -m secretary reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` читает desired state и inventory, ничего не применяет и не пишет manifest.
`--offline` для него намеренно отклоняется: plan требует inventory. Code `0` означает читаемый
plan без conflict, `1` — conflict, `2` — невалидный input или недоступный inventory. Совпавшее имя
host resource не даёт ownership: чужой или неописанный resource остаётся conflict.

`reconcile adopt` касается только одного существующего desired Orca registration. Он сверяет имя
и нормализованный путь repo с binding, показывает fingerprint и без `--yes` остаётся preview.
Подтверждённый запуск атомарно добавляет запись в `host-managed.json`; он не меняет Orca, systemd
или worktree. Unit resources этим путём не adopt'ятся.

## Задачи и pilot dispatcher

Публичный путь к доске — `secretary task`. Карточка содержит `ref`, project, type, state,
dependency, claim, routing, workspace, retry и audit metadata:

```text
ideas → ready → in_progress → validate → done
                         └────────────→ blocked
```

```bash
python3 -m secretary task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-extra --codex-mode exec
```

`create` принимает `--description` или `--body-file`, `--ref`, `--blocked-by`, workspace и routing
fields. Создавать Ready может не каждая роль: worker, reviewer и retro могут создавать только
Ideas. `--codex-mode` принимает только `exec` или `tui` и только для известного worker profile с
adapter `codex`; без флага способ запуска берётся из профиля головы.

Все pilot subcommands dispatcher требуют один точный `--pilot-ref`; broad scan Ready не является
pilot protocol. Последовательность — `preflight`, `pause-old`, `start-new-pilot`, `tick`/`observe`,
затем при отдельном решении `commit-cutover` либо `rollback`. `preflight`, `pause-old`,
`start-new-pilot`, `tick` и commit проверяют freeze legacy dispatcher: нужен hard pause, который
не может auto-resume; drain недостаточен. `rollback` требует непустой `--reason-file`, останавливает
только новые worker handles и сохраняет board card, claim, comments, PR и review state для legacy
continuation.

## Память

Facts лежат flat в `memory/facts/global/<slug>.md` или
`memory/facts/<project-dir>/<slug>.md`. Один факт — один дистиллированный markdown record.
Куратор остаётся writer-ролью, а другие агенты читают через `memory_search`, `memory_get` и
`memory_list`.

```bash
python3 -m secretary memory import --instance INSTANCE [--from SOURCE]
python3 -m secretary memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -m secretary memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -m secretary memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -m secretary memory reindex --instance INSTANCE
```

`import` synchronizes a readable source into the local facts journal. Writer operations require an
actor and complete through the journal protocol; direct edits минуют audit trail. `reindex` rebuilds
only the derived index from the local journal. It does not change facts and must not overlap another
index writer; model and dimension come from the instance host configuration.

Backup, data-plane and unit contracts are in [OPERATIONS.md](OPERATIONS.md).
