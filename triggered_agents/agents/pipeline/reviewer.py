"""Layer-3 reviewer prompt — the one-time REVIEW.md handed to the independent review head.

Validate layer 3 (design-task-pipeline.md, «LLM-ревью против спеки»): once the cheap mechanical
layers are green (CI, and the stand for stand projects), the dispatcher spawns a fresh Claude head
that is NOT the card's worker and has no write access to the code. It reads the whole repo and the
full PR (not just the diff) and posts one structured verdict, then the dispatcher acts on it like
it acts on a worker report — including, on green, squash-merging the PR itself (validate.py). A
green verdict is no longer double-checked by a human before merge, so this prompt also has the
reviewer verify whatever live checks the worker's report claims. Safe local checks are re-run in
the review worktree; heavyweight checks that need Docker, a stand or external writes are verified
through the exact green mechanical gate for the current head SHA, its workflow and logs. This is
the class of proof-of-work a human skim used to catch without forcing a read-only reviewer to
repeat side effects that the lower validation layers already own.

This module only builds the text of REVIEW.md. The host side (worktree + head) lives in worker.py,
so the dispatcher keeps talking to a single host boundary. The thermo-nuclear quality lens is not
copied into the source: the skill file is read at build time and embedded verbatim into the prompt
(design decision: «использовать как есть, вчитывать файл в промпт ревьюера, не копировать в код»),
falling back to a load-it-yourself instruction if the file is missing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import card_comments, model, naming, worker

THERMO_SKILL = Path(os.environ.get(
    "TA_THERMO_SKILL",
    str(Path.home() / ".claude/skills/thermo-nuclear-code-quality-review/SKILL.md")))

# Prior rounds of validation on this same card: the reviewer's own verdicts and the dispatcher's
# note when a red one sent the card back for rework. These three are what makes round N aware that
# round N-1 happened at all.
_ROUND_MARKERS = (model.MARKER_REVIEW_GREEN, model.MARKER_REVIEW_RED, model.MARKER_REVIEW_RETURN)

# The history is capped on two axes at once, because it grows on two axes at once: a card collects
# many comments, and single comments (CI log dumps, full worker reports) run to tens of kilobytes.
# A count cap alone still lets one log sheet swallow the prompt; a length cap alone still lets forty
# short comments bury the spec. Caps are per-section since the sections are not equally valuable —
# spec clarifications and prior verdicts are the reason for showing history at all, so they get
# their own budget instead of competing with chatter for the tail of one shared list. Everything is
# a tail: the newest comments are the ones that describe the state under review. `pipeline show`
# stays the escape hatch for anything clipped.
_SPEC_NOTE_LIMIT = 5     # same budget taskdoc gives the worker's operator context
_ROUND_LIMIT = 6         # ~3 red rounds, each a verdict plus the dispatcher's return note
_HISTORY_LIMIT = 10
_COMMENT_CHARS = 2000


def _clip(body: str) -> str:
    if len(body) <= _COMMENT_CHARS:
        return body
    return (body[:_COMMENT_CHARS].rstrip()
            + f"\n\n… обрезано, ещё {len(body) - _COMMENT_CHARS} символов "
              "(полный текст — в `pipeline show`)")


def _parse(comments: list[dict]) -> list[tuple[str, str, str]]:
    """Card comments as (stamp, marker, body), scrubbed and clipped. Scrubbing runs here rather
    than at the section level so no path into the prompt can skip it: a comment body is board text
    that a worker or a CI log may have carried a token into, and REVIEW.md is written to disk in
    the reviewer's workspace."""
    out = []
    for c in comments or []:
        marker, body = card_comments.split_marker(worker.scrub_secrets((c.get("text") or "").strip()))
        if not body:
            continue
        out.append((card_comments.format_ts(c.get("ts")), marker, _clip(body)))
    return out


def _entries(picked: list[tuple[str, str, str]]) -> list[str]:
    lines = []
    for ts, marker, body in picked:
        lines.append(f"### {ts}" + (f" [{marker}]" if marker else ""))
        lines.append("")
        lines.append(body)
        lines.append("")
    return lines


def _spec_notes(parsed: list[tuple[str, str, str]]) -> list[str]:
    """PO/secretary/steward comments, rendered right under the spec. The card description is not
    editable in this pipeline (`pipeline update` has no description flag), so a comment is the only
    way the PO can amend a spec after creation — which makes these strictly newer than the text
    above them, and the reviewer has to be told that rather than left to guess which wins."""
    picked = [p for p in parsed if p[1] in card_comments.OPERATOR_MARKERS]
    if not picked:
        return []
    dropped = len(picked) - _SPEC_NOTE_LIMIT
    lines = [
        "### Уточнения спеки комментариями",
        "",
        "Описание карточки выше после создания не редактируется, поэтому PO уточняет спеку "
        "комментарием. Эти комментарии новее описания: где они расходятся, criterion задаёт "
        "комментарий, а описание считается устаревшим в этой части. Прямые указания ревьюеру "
        "здесь тоже обязательны к исполнению.",
        "",
    ]
    if dropped > 0:
        lines += [f"(последние {_SPEC_NOTE_LIMIT} из {len(picked)}; остальные — в `pipeline show`)", ""]
    return lines + _entries(picked[-_SPEC_NOTE_LIMIT:])


def _rounds(parsed: list[tuple[str, str, str]]) -> list[str]:
    """Verdicts of earlier review rounds on this same card, plus the dispatcher's return notes.

    Without this a red verdict is amnesic by construction: `_review_red` returns the card to In
    progress and the next Validate entry spawns a brand-new head with a brand-new prompt, so round
    N sees the worker's answer to round N-1's finding with no idea a finding existed. On
    codegen_orchestrator-646 that cost a real miss — round 1 flagged a collision risk in a slug
    rule, the worker replaced the rule, and round 2 read a description saying one thing and code
    saying another, then passed it green as if the gap were nothing."""
    picked = [p for p in parsed if p[1] in _ROUND_MARKERS]
    if not picked:
        return []
    dropped = len(picked) - _ROUND_LIMIT
    lines = [
        "## Прошлые раунды ревью этой карточки",
        "",
        "Эта карточка уже проходила ревью — ниже вердикты прошлых раундов и причины возвратов "
        "диспетчера. Это НЕ описание текущего кода: воркер работал уже после них. Для каждой "
        "прошлой находки реши, закрыта она в текущем состоянии или нет. И если текущий код "
        "расходится со спекой именно потому, что так потребовал прошлый раунд, — это не новый "
        "дефект, а расхождение спеки с кодом: зафиксируй его явно в вердикте, не пропусти молча "
        "и не выдавай за находку.",
        "",
    ]
    if dropped > 0:
        lines += [f"(последние {_ROUND_LIMIT} из {len(picked)}; остальные — в `pipeline show`)", ""]
    return lines + _entries(picked[-_ROUND_LIMIT:])


def _history(parsed: list[tuple[str, str, str]]) -> list[str]:
    """Everything else on the card, newest tail first-class: worker reports, CI verdicts, blocked
    notes. The comments already rendered as spec notes or prior rounds are left out so the two
    sections that matter most are not diluted by their own duplicates."""
    shown = card_comments.OPERATOR_MARKERS | set(_ROUND_MARKERS)
    picked = [p for p in parsed if p[1] not in shown]
    if not picked:
        return []
    dropped = len(picked) - _HISTORY_LIMIT
    lines = ["## Остальная история карточки", ""]
    if dropped > 0:
        lines += [f"(последние {_HISTORY_LIMIT} из {len(picked)}; остальные — в `pipeline show`)", ""]
    return lines + _entries(picked[-_HISTORY_LIMIT:])


def _quality_lens() -> str:
    """The thermo-nuclear skill, read from disk and embedded. Never hardcode its content — read
    the current file so the lens tracks the skill, and degrade to a pointer if it is absent."""
    try:
        return (f"Ниже — модуль качества (скилл thermo-nuclear) целиком; применяй его как есть:\n\n"
                f"````\n{THERMO_SKILL.read_text(encoding='utf-8').strip()}\n````")
    except OSError:
        return (f"Модуль качества — скилл thermo-nuclear по пути `{THERMO_SKILL}` "
                f"(не прочитался при сборке промпта). Загрузи его сам и применяй как есть.")


def build_task(card: dict, ref: str, pr: str | None, spec: str, base_branch: str,
              branch: str | None = None, head_sha: str | None = None,
              comments: list[dict] | None = None) -> str:
    """REVIEW.md for the reviewer head: what to review, the three lenses, the blocking semantics,
    and how to emit the verdict + Идеи cards through board-CLI. `spec` is the card description.
    `pr` is the card's PR link — or None for a contrib (fork) card, which has no PR in this
    pipeline by definition (a human opens it against upstream from the pushed branch afterward);
    `branch`/`head_sha` then point at what to review instead (the worker's own report:done
    protocol line, validate._contrib_ref). `comments` is the card's comment list (ops.show_card),
    rendered into the prompt: the reviewer must not depend on choosing to go fetch its own history
    (see _rounds and _spec_notes)."""
    project = card.get("project", "?")
    parsed = _parse(comments or [])
    review_branch = naming.reviewer_branch(ref)
    if pr:
        what_to_read = [
            f"PR карточки: {pr}",
            f"Отчёт воркера (report:done) и вся история карточки вклеены ниже, отдельными "
            f"разделами — читай их там, а не догадывайся сходить за ними. В отчёте — заявленные "
            f"живые проверки (см. ниже). За полным текстом того, что помечено обрезанным: "
            f"`python3 -m triggered_agents pipeline show --ref {ref}`, роль не нужна.",
            f"База проекта: `{base_branch}`.",
            f"Воркспейс уже стоит на состоянии PR — своя ветка `{review_branch}` заведена от головы PR "
            "при подъёме, чекаутить/переключать ветку не нужно. Тебе доступен весь репо и полный PR, "
            "не только дифф:",
            "```",
            f"gh pr diff {pr}       # дифф PR, если нужен именно дифф, а не полный код",
            "```",
            f"Читай нужные файлы репо на состоянии PR, а не только строки диффа. Своя ветка "
            f"`{review_branch}` — только твоя рабочая копия, её (как и любую ветку) не пушить.",
        ]
    else:
        what_to_read = [
            f"Contrib-карточка (форк): PR в этом пайплайне не открывается — ветку в форк для "
            f"upstream-автора готовит человек. Ветка воркера: `{branch}`, голова: `{head_sha}`.",
            f"Отчёт воркера (report:done) и вся история карточки вклеены ниже, отдельными "
            f"разделами — читай их там, а не догадывайся сходить за ними. В отчёте — заявленные "
            f"живые проверки (см. ниже). За полным текстом того, что помечено обрезанным: "
            f"`python3 -m triggered_agents pipeline show --ref {ref}`, роль не нужна.",
            f"База проекта: `{base_branch}`.",
            f"Воркспейс уже стоит на состоянии этой ветки — своя ветка `{review_branch}` заведена от "
            "той же головы при подъёме, чекаутить/переключать ветку не нужно. Тебе доступен весь "
            "репо на этом состоянии, не только дифф:",
            "```",
            "git log --oneline -20     # история ветки, если нужен только список коммитов",
            "```",
            f"Читай нужные файлы репо на этом состоянии, а не только строки диффа. Своя ветка "
            f"`{review_branch}` — только твоя рабочая копия, её (как и любую ветку) не пушить.",
        ]
    lines = [
        f"# Ревью задачи {ref} ({project}) — слой 3 валидации",
        "",
        "Ты — независимая голова-ревьюер. Ты НЕ воркер этой карточки и прав на код нет: не коммить, "
        "не пушь, не меняй PR. Твои единственные артефакты — один вердикт-коммент и, при "
        "необходимости, карточки-идеи. Нижние слои валидации (CI, для стенд-проектов ещё стенд+e2e) "
        "уже зелёные — твоя работа поверх них.",
        "",
        naming.memory_block("reviewer", project),
        "",
        "## Что вычитать",
        "",
        *what_to_read,
        "",
        "## Спека карточки (по ней проверяешь criterion за criterion)",
        "",
        spec or "(описание карточки пустое)",
        "",
        *_spec_notes(parsed),
        *_rounds(parsed),
        "## Три линзы (все обязательны)",
        "",
        "### 1. Спека-комплаенс",
        "По каждому acceptance criterion спеки реши: выполнен РЕАЛЬНО или НА БУМАГЕ (заявлен в "
        "отчёте, но код/тест этого не делает, проверка мокнута насквозь, criterion обойдён). "
        "Для каждого — вердикт реально/на бумаге с обоснованием из кода.",
        "",
        "### 2. Adversarial-охота за багами",
        "Ищи дефекты по классам отказов; ОБЯЗАТЕЛЬНЫ все:",
        "- **Пути ошибок**: что происходит при падении каждой стадии/вызова (исключение проглочено? "
        "частичное состояние? ретрай вечно?).",
        "- **Гонки/конкурентность**: параллельные тики/воркеры/хендлы, общий стейт, лок-файлы, "
        "read-check-write без атомарности.",
        "- **Залипание навсегда без сигнала человеку**: может ли карточка/процесс встать так, что "
        "никто не узнает (нет вотчдога, нет эскалации, бесконечный ретрай, потерянный хендл).",
        "- **Утечка секретов** в комменты/логи/доску (токены, env, ключи — что постится без scrub).",
        "- **Blast radius на соседние системы**: задевает ли изменение чужие контейнеры/ветки/"
        "стейт/доску за рамками своей задачи.",
        "Каждая находка — с конкретным файлом и сценарием поломки (какой вход/состояние → что "
        "ломается).",
        "",
        "### 3. Качество кода (thermo-nuclear)",
        _quality_lens(),
        "",
        *_history(parsed),
        "## Живые проверки из отчёта воркера",
        "",
        "Зелёный вердикт теперь сам достаточен для автомержа — ручной вычитки человека после "
        "него больше нет. Если в отчёте воркера заявлена конкретная живая проверка (смоук, "
        "ручной прогон, curl на эндпоинт, запуск скрипта/команды) — прогони её сам там, где это "
        "безопасно и воспроизводимо прямо в этом воркспейсе (без стенда, без докера, без записи "
        "во внешние сервисы или чужие данные). Для heavyweight-проверки, которую reviewer "
        "сознательно не должен повторять (Docker, стенд, внешняя запись), допускается независимое "
        "механическое evidence: проверь, что точная CI/stand job зелёная на ТЕКУЩЕМ head SHA, "
        "прочитай её workflow/команду и релевантные логи или artifact, и убедись, что job реально "
        "исполняет заявленный путь, а не мок или no-op. Такое evidence считается реальным "
        "выполнением criterion; отсутствие личного Docker-прогона само по себе не блокер. Если "
        "нет ни безопасного rerun, ни подходящего механического evidence текущего SHA — не "
        "догадывайся, фиксируй как «на бумаге» и объясни, чего не хватает.",
        "",
        "## Семантика блокировки (важно — что блокер, а что нет)",
        "",
        "- **Блокеры** (красный вердикт): дифф самого PR подрывает качество кода и есть конкретное "
        "исправимое замечание; ЛИБО файл переваливает за 1000 строк из-за этого PR; ЛИБО баг любого "
        "из классов линзы 2; ЛИБО criterion выполнен только на бумаге.",
        "- **НЕ блокеры**: pre-existing долг, находки в СОСЕДНЕМ коде (не тронутом PR), амбициозные "
        "перестройки за рамками карточки. Их НЕ пихай в красный вердикт — оформляй карточками в "
        "колонку Идеи (см. ниже). Это единственное исключение из «прав на код нет».",
        "",
        "## Как отдать результат",
        "",
        "Один вердикт-коммент через board-CLI. Красный — если есть блокер любой линзы; иначе "
        "зелёный. Тело вердикта:",
        "1. По каждому criterion спеки: реально / на бумаге + почему.",
        "2. По каждой живой проверке из отчёта воркера: прогнал сам (что вышло), ИЛИ подтвердил "
        "механическим evidence текущего SHA (какая job, workflow/команда и результат), ИЛИ не "
        "смог подтвердить (почему) — не пропускай молча.",
        "3. Находки, ранжированные блокер / замечание, КАЖДАЯ с файлом и сценарием поломки.",
        "",
        "```",
        "# красный (есть блокеры), тело обязательно:",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind red --body-file <файл>",
        "# зелёный (блокеров нет):",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind green --body-file <файл>",
        "```",
        "",
        "Не-блокеры (соседний код, долг, идеи за рамками) — карточками в Идеи:",
        "```",
        f"python3 -m triggered_agents pipeline --role reviewer idea --project {project} "
        "--title '<кратко>' --description-file <файл>",
        "```",
        "",
        "Постишь РОВНО ОДИН вердикт. Не двигай карточку и не пиши в код — на красный её вернёт "
        "диспетчер сам.",
    ]
    return "\n".join(lines)
