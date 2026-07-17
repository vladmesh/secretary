"""Render the one-time TASK.md handed to a pipeline worker."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import heads, naming, task_protocol, worker


_COMMENT_MARKER_RE = re.compile(r"^\[([^\]]+)\]\n?(.*)\Z", re.DOTALL)
_OPERATOR_MARKERS = {"po", "secretary", "steward", "steward:blocked-done"}


def _format_comment_ts(ts) -> str:
    """A comment's `date_creation` (unix seconds, from Kanboard) as a readable UTC stamp."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(ts) if ts else "?"


def _metadata(card: dict, base: str) -> list[str]:
    worker_head = card.get("effective_head") or card.get("head") or heads.DEFAULT_PROFILE
    review_head = card.get("effective_review_head") or card.get("review_head") or worker.REVIEWER_HEAD
    lines = [
        "## Метаданные",
        "",
        f"- тип: {card.get('task_type') or '?'}",
        f"- голова worker: {worker_head}",
        f"- голова reviewer: {review_head}",
        f"- слаг: {naming.card_slug(card)}",
        f"- база: {base}",
    ]
    if card.get("blocked_by"):
        lines.append(f"- blocked_by: {card['blocked_by']}")
    lines.append("")
    return lines


def _history(comments: list[dict]) -> list[str]:
    if not comments:
        return []
    lines = ["## История", ""]
    for c in comments:
        lines.append(f"### {_format_comment_ts(c.get('ts'))}")
        lines.append("")
        lines.append((c.get("text") or "").strip())
        lines.append("")
    return lines


def _operator_context(comments: list[dict], limit: int = 5) -> list[str]:
    picked = []
    for c in comments:
        text = (c.get("text") or "").strip()
        m = _COMMENT_MARKER_RE.match(text)
        if not m:
            continue
        marker = m.group(1)
        if marker not in _OPERATOR_MARKERS:
            continue
        body = m.group(2).strip()
        if not body:
            continue
        picked.append((_format_comment_ts(c.get("ts")), marker, body))
    if not picked:
        return []
    lines = ["## Операторский контекст", ""]
    for ts, marker, body in picked[-limit:]:
        lines.append(f"### {ts} [{marker}]")
        lines.append("")
        lines.append(body)
        lines.append("")
    return lines


def render(card: dict, view: dict, base: str) -> str:
    """Build TASK.md from the current card view and resolved base branch."""
    ref = card["reference"]
    branch = naming.worker_branch(ref)
    comments = view.get("comments") or []
    is_contrib = worker.is_contrib(card.get("project") or "")
    legacy = task_protocol.use_legacy_path()
    writer = task_protocol.writer()
    report_command = f"`{task_protocol.command('report', ref)} --kind done|blocked --body-file <файл>`"
    if is_contrib:
        done_clause = (
            f"Контриб-проект (форк): PR в этом пайплайне не открывается — ветку в форк для "
            f"upstream-автора готовит человек. Done для тебя: код закоммичен туда, локальные тесты "
            f"зелёные, ветка запушена в `origin` (твой форк). В коммитах никаких упоминаний AI "
            f"и Co-Authored-By, стиль — как в git log репо."
        )
        report_clause = (
            f"Отчёт по каждому acceptance criterion (сделано/нет и как проверял) — через {writer}: "
            f"{report_command}. Вместо ссылки на PR отчёт done обязан нести ветку и "
            f"head sha пуша, ровно этими протокольными строками в теле:\n"
            f"```\nbranch: {branch}\nhead: <sha HEAD после пуша>\n```\n"
            f"Несогласие со спекой — `--kind blocked` с обоснованием. Карточку сам не двигаешь. "
            f"TASK.md в репо не коммить."
        )
        history_tail = "origin: начни с `git fetch`, продолжай существующую ветку, не пересоздавай её."
    else:
        done_clause = (
            f"Done для тебя: код закоммичен туда, локальные тесты зелёные, ветка запушена, PR "
            f"открыт через `gh` (base — `{base}`). В коммитах и PR никаких упоминаний "
            f"AI и Co-Authored-By, стиль — как в git log репо."
        )
        report_clause = (
            f"Отчёт по каждому acceptance criterion (сделано/нет и как проверял, плюс ссылка на PR) "
            f"— через {writer}: {report_command}. Несогласие со спекой — "
            f"`--kind blocked` с обоснованием. Карточку сам не двигаешь. TASK.md в репо не коммить."
        )
        history_tail = ("origin, PR может быть уже открыт: начни с `git fetch`, продолжай "
                        "существующие ветку/PR, не пересоздавай их.")
    lines = [
        f"# Задача {ref} ({card.get('project', '?')})",
        "",
        f"Роль на доске — worker. Воркспейс уже стоит на ветке `{branch}` (её завели при подъёме "
        f"воркспейса) — ветку создавать или переименовывать не нужно, коммить прямо в неё. "
        f"{done_clause}",
        "",
        report_clause,
        "",
    ]
    if comments:
        lines += [
            f"У карточки ниже есть история — она уже была в работе раньше (возврат из Blocked, "
            f"умершая голова или похожий случай). Ветка `{branch}` может уже существовать на "
            f"{history_tail}",
            "",
        ]
    lines += [
        f"Всегда (независимо от истории): force-push запрещён; пушь только в репозиторий своего "
        f"проекта и только в свою ветку `{branch}`.",
        "",
        "Пауза пайплайна (`drain` или `freeze`) это админ-состояние всей очереди, не поломка "
        "твоей карточки. Не репорти `blocked` только из-за паузы; после `resume` продолжай ту же "
        "карточку в этом же воркспейсе.",
        "",
    ]
    if is_contrib:
        lines += [
            f"Контриб-проект (форк): пуш только в `origin` (твой форк) — `upstream` (репо "
            f"автора) не трогать, туда не пушить и не мержить.",
            "",
        ]
    lines += _metadata(card, base)
    lines += [
        "## Worker write protocol",
        "",
        f"Комментарии: `{task_protocol.command('comment', ref)} --body-file <файл>`.",
    ]
    if not legacy:
        lines += [
            "Совместимый Phase 5 bridge временно оставляет Kanboard credentials в окружении CLI. "
            "Это не техническая граница least-privilege; broker и identity isolation остаются следующими фазами.",
        ]
    lines += [""]
    lines += [naming.memory_block("worker", card.get("project") or "?"), ""]
    lines += ["## Спека", "", view.get("description") or "(описание карточки пустое)", ""]
    lines += _operator_context(comments)
    lines += _history(comments)
    return "\n".join(lines).rstrip("\n") + "\n"
