# Review secretary-1162

# Goal

`tests.test_secret_recover.NoStoreCase.test_an_installation_without_a_store_keeps_the_hand_written_file`
падает на любом хосте, где не установлена Orca, — в том числе на раннере GitHub Actions. Из-за этого
`main` красный, механический гейт красный на любой ветке, и пайплайн не может довести ни одну карточку
до ревью. Сделать этот тест независимым от того, что установлено на хосте.

# Почему это хотфикс, а не обычная карточка

Красный `main` ломает проверку результата: гейт `pipeline/*` красный независимо от содержания ветки.
Карточка `secretary-1161` уже встала в Blocked ровно по этой причине (`report:blocked` 2026-08-06T09:01:43Z),
её ветка чистая на `e930a3f`. Пока это не починено, спринт не может проверить свой Definition of Done.

# Доказательство, что это пред-существующее и средовое

- CI run 31085881940, `main` @ `c1e0e8a`: `Ran 2031 tests`, `FAILED (failures=1)` — это падение.
- CI run 31087114704, `pipeline/secretary-1161` @ `e930a3f0945a`: то же самое единственное падение.
  Диф той ветки — 25 строк только в `tests/test_dispatcher_contracts.py`.
- Полный прогон на хосте установки зелёный (2031 tests OK), потому что там `orca` есть в PATH.

# Механика падения

Указатели, не копипаста; читай текущее дерево.

- `secretary/installation.py:check_prerequisites` сначала проверяет `shutil.which("orca")` и падает
  `InstallError("Orca is not installed; ...")`, затем пробует `orca --version`, и только потом
  доходит до `TaskReader(KanboardClient(...)).list()`, чей отказ даёт
  `InstallError("Kanboard prerequisite failed: ...")`.
- `tests/test_secret_recover.py:NoStoreCase` (проверка около строки 351) ждёт в выводе
  `Kanboard prerequisite failed`, но патчит только `secretary.installation._ensure_installation_user`.
  Orca-прекондишен он не контролирует, поэтому на раннере до Kanboard-ветки исполнение не доходит:
  вывод обрывается на `failed install: Orca is not installed; install a supported Orca runtime before
  secretary recovery`, и `assertIn` падает. `assertEqual(code, 1)` при этом проходит в обоих случаях,
  что и маскировало связь с хостом.

Это тот же класс дефекта, что и в `secretary-1161`: юнит-тест завязан на состояние хоста. Здесь —
на наличие бинаря в PATH.

# Маршрут

Чинится на стороне теста: он должен сам определять исход Orca-прекондишена (так же, как уже подменяет
`_ensure_installation_user`), чтобы детерминированно доходить до проверяемой Kanboard-ветки на любом
хосте — и там, где Orca есть, и там, где её нет. Продуктовый контракт `check_prerequisites` и порядок
проверок в нём не меняются: порядок правильный, Orca-прекондишен обязан быть первым.

# Acceptance criteria

1. `python3 -m unittest tests.test_secret_recover` зелёный на хосте, где `orca` отсутствует в PATH,
   и на хосте, где присутствует. Обе ситуации проверены явно (например, прогоном с урезанным PATH),
   и результат проверки приведён в отчёте.
2. Тест по-прежнему проверяет содержательное: что установка без секрет-стора не трогает написанный
   руками файл, и что до Kanboard-прекондишена дело доходит. Не ослаблять до `assertEqual(code, 1)`
   и не удалять утверждения про `skipped   runtime-env` и `skipped   secret-store`.
3. `secretary/installation.py` не изменён: порядок прекондишенов и текст обеих ошибок прежние.
4. `python3 -m unittest` полностью зелёный, и CI на ветке карточки зелёный — 0 падений, не «столько же,
   сколько на main».

# Вне рамок

Менять `check_prerequisites`, порядок прекондишенов и тексты `InstallError`. Трогать секрет-стор,
продакшен-конфигурацию, юниты, карточку `secretary-1161` и её ветку `pipeline/secretary-1161`.
Чинить любые другие тесты, завязанные на хост, — если найдёшь такие, перечисли их в отчёте, но не трогай.


## Mechanical gate attestation

- validated_sha: a04c9a346dd689d20894549f545a44090fcd0137
- base_sha: c1e0e8add9f283cfc65ae27c8a311c4df1ddee79
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31088264069/job/92572812378)
- completed_at: 2026-08-06T09:16:39+00:00
- command_or_check_set_digest: e05e08ff6e9e51da3be176a7b5215dfddd2f768f01036631e8a3c9ab7be723ca

Independently inspect the diff, acceptance criteria and invariants. The attested broad
check above already passed on this exact SHA: do not rerun that broad command or suite on
the same SHA unless you record a concrete `rerun_reason`. A focused reproduction is allowed
for a new blocker, an uncovered external behaviour, or a security/data-loss high-risk need.
Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.

A red verdict must list every blocker you have found in this round. Prefix each with a
stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.
Do not hold blockers back for a later round and do not widen the scope on the next one.

For every RED blocker, state the concrete reachable scenario, the violated acceptance
criterion or operational invariant, material assumptions, whether this branch introduced
the defect or it was pre-existing, and whether the repair appears local or would change
architecture, a compatibility promise, a product contract, or a trust boundary. Report
evidence; do not silently widen the supported boundary or decide sprint scope.

When a change depends on how an external backend behaves, a passing fixture is not
evidence: it can encode the same wrong assumption as the code under review. Say which
real behaviour you verified and how. If no end-to-end check against the real backend
was possible, write plainly that it was not done and which assumption stays unverified.

Post exactly one review verdict through the secretary task protocol:
Write the body to /tmp/secretary-verdict-secretary-1162-4.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1162 --role reviewer --kind green --request-id dispatcher-attempt-20260806T091023Z-d1b37ee17428-review-green-secretary-1162-4 --body-file /tmp/secretary-verdict-secretary-1162-4.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1162 --role reviewer --kind red --request-id dispatcher-attempt-20260806T091023Z-d1b37ee17428-review-red-secretary-1162-4 --body-file /tmp/secretary-verdict-secretary-1162-4.md
