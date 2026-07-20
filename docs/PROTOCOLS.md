# Протоколы secretary

`--instance` принимает каталог instance или прямой путь к `instance.yaml`. Instance задаёт
конфигурацию установки, а `secretary-data` хранит изменяемое состояние.

## Проверка и host ownership

```bash
python3 -m secretary doctor --instance INSTANCE
python3 -m secretary doctor --offline --instance INSTANCE
python3 -m secretary doctor --instance INSTANCE --host-fixture DIR
```

`doctor` всегда read-only. Обычный запуск проверяет config, data и live inventory; `--offline`
оставляет только config/data, а `--host-fixture` заменяет live inventory детерминированным fixture.
Fixture нельзя сочетать с `--offline`. Exit code `0` означает завершённую проверку без findings,
`1` означает findings или warnings с `--strict`, `2` означает невалидный input либо недоступный
inventory. Без `--strict` warnings сами по себе остаются зелёными.

```bash
python3 -m secretary reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -m secretary reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` читает desired state и inventory, ничего не применяет и не пишет manifest.
`--offline` намеренно отклоняется. Code `0` означает plan без conflict, `1` означает conflict,
`2` означает невалидный input или недоступный inventory.

`reconcile adopt` касается одного существующего desired Orca registration. Он сверяет имя и
нормализованный repo path, показывает fingerprint и без `--yes` остаётся preview. Подтверждённый
запуск атомарно добавляет managed record, не меняя Orca, systemd или worktree. Unit resources этим
путём не adopt'ятся.

## Задачи

Публичный путь к доске проходит через `secretary task`. Карточка содержит `ref`, project, type,
state, dependency, claim, routing, workspace, retry и audit metadata:

```text
ideas → ready → in_progress → validate → done
                         └────────────→ blocked
```

```bash
python3 -m secretary task list --project PROJECT
python3 -m secretary task show --ref PROJECT-N
python3 -m secretary task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-extra --codex-mode exec
```

`create` принимает `--description` или `--body-file`, dependency, workspace и routing fields.
Worker, reviewer и retro могут создавать только Ideas; PO выбирает Ready. `--codex-mode` допустим
только для worker profile с adapter `codex`. Без override launch mode берётся из head profile.

Все write-команды проходят role guards и transition checks. Mutation сначала получает
append-only pending audit event, затем сверяется с live board и только после этого считается
committed. Unresolved pending write блокирует согласованный export и backup до `reconcile-audit`.

`report --kind done` перед любой записью проверяет `git status --porcelain` воркспейса воркера
(CWD процесса) и отказывает с `uncommitted`, если там есть незакоммиченные изменения: воркер
чинит это в своей же сессии, а не узнаёт постфактум из blocked. Untracked runtime tail
(`secretary-data/`) не считается за грязь, `--kind blocked` не гейтится (WIP допустим), а
пост-фактум чек диспетчера остаётся как defense-in-depth.

Audit trail всегда пишется в data dir установки: `--data-dir`, иначе `SECRETARY_DATA_DIR`, иначе
`data_dir` из instance config (`--instance` / `SECRETARY_INSTANCE`). Относительный `data_dir`
резолвится от instance file, не от CWD, поэтому вызов из воркспейса чужого проекта не оставляет
там `secretary-data/`. Если data dir не резолвится, команда падает с usage error вместо записи
рядом с процессом.

## Production dispatcher

Production runtime запускается одним tick или постоянным loop:

```bash
python3 -m secretary dispatcher production-tick --instance INSTANCE
python3 -m secretary dispatcher production-observe --instance INSTANCE
python3 -m secretary dispatcher production-run --instance INSTANCE
```

Systemd timer использует one-shot `production-tick`. Runtime обрабатывает только поддержанные
task transitions, сохраняет claim/review state и сверяет live board перед recovery. Production
owner записан в dispatcher state; несовпадение owner, dirty workspace, missing report или
неразрешённый audit state останавливают переход вместо silent fallback.

Старые pilot/cutover subcommands остаются compatibility recovery surface текущей версии, но не
являются путём установки нового instance.

## Подключение проекта

Текущий низкоуровневый onboarding состоит из стадий:

```bash
python3 -m secretary project add ...
python3 -m secretary project provision-start ...
python3 -m secretary project provision-apply ...
python3 -m secretary project gate ...
```

Identity проекта задаётся один раз top-level binding. Scanner и provision готовят изменения, но не
включают binding. Enable разрешён только через успешный gate, привязанный к проверенным revision,
provision run и write-set. Верхнеуровневый resumable workflow остаётся milestone Roadmap.

## Память

Facts лежат flat в `memory/facts/global/<slug>.md` или
`memory/facts/<project-dir>/<slug>.md`. Один факт является одним дистиллированным markdown record.
Куратор остаётся writer-ролью, остальные агенты читают через `memory_search`, `memory_get` и
`memory_list`.

```bash
python3 -m secretary memory verify --instance INSTANCE
python3 -m secretary memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -m secretary memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -m secretary memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -m secretary memory reindex --instance INSTANCE
```

Writer operations требуют actor и проходят journal protocol; прямые edits минуют audit trail.
`reindex` меняет только derived index и не должен пересекаться с другим index writer. Model и
dimension берутся из instance configuration.

Data-plane, archive restore и unit runbooks находятся в [Operations](OPERATIONS.md).
