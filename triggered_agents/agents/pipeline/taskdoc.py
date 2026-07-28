"""Render the one-time TASK.md handed to a pipeline worker."""
from __future__ import annotations

from . import card_comments, heads, naming, task_protocol, worker


def _metadata(card: dict, base: str) -> list[str]:
    worker_head = card.get("effective_head") or card.get("head") or heads.DEFAULT_PROFILE
    review_head = card.get("effective_review_head") or card.get("review_head") or worker.REVIEWER_HEAD
    lines = [
        "## Metadata",
        "",
        f"- type: {card.get('task_type') or '?'}",
        f"- worker head: {worker_head}",
        f"- reviewer head: {review_head}",
        f"- slug: {naming.card_slug(card)}",
        f"- base: {base}",
    ]
    if card.get("blocked_by"):
        lines.append(f"- blocked_by: {card['blocked_by']}")
    lines.append("")
    return lines


def _history(comments: list[dict]) -> list[str]:
    if not comments:
        return []
    lines = ["## History", ""]
    for c in comments:
        lines.append(f"### {card_comments.format_ts(c.get('ts'))}")
        lines.append("")
        lines.append((c.get("text") or "").strip())
        lines.append("")
    return lines


def _operator_context(comments: list[dict], limit: int = 5) -> list[str]:
    picked = []
    for c in comments:
        marker, body = card_comments.split_marker((c.get("text") or "").strip())
        if marker not in card_comments.OPERATOR_MARKERS:
            continue
        if not body:
            continue
        picked.append((card_comments.format_ts(c.get("ts")), marker, body))
    if not picked:
        return []
    lines = ["## Operator context", ""]
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
    report_command = f"`{task_protocol.command('report', ref)} --kind done|blocked --body-file <file>`"
    if is_contrib:
        done_clause = (
            f"Contrib project (a fork): no PR is opened in this pipeline — a human prepares the "
            f"branch in the fork for the upstream author. Done for you means: the code is committed "
            f"there, the local tests are green, and the branch is pushed to `origin` (your fork). No "
            f"mention of AI and no Co-Authored-By trailers in the commits; match the style of the "
            f"repository's git log."
        )
        report_clause = (
            f"Report on each acceptance criterion (met or not, and how you checked) through "
            f"{writer}: {report_command}. Instead of a PR link, a done report must carry the branch "
            f"and the head sha of the push, in exactly these protocol lines in the body:\n"
            f"```\nbranch: {branch}\nhead: <sha of HEAD after the push>\n```\n"
            f"If you disagree with the spec, use `--kind blocked` with your reasoning. You do not "
            f"move the card yourself. Do not commit TASK.md into the repository."
        )
        history_tail = ("origin: start with `git fetch`, continue the existing branch, do not "
                        "recreate it.")
    else:
        done_clause = (
            f"Done for you means: the code is committed there, the local tests are green, the branch "
            f"is pushed, and a PR is open through `gh` (base `{base}`). No mention of AI and no "
            f"Co-Authored-By trailers in the commits or the PR; match the style of the repository's "
            f"git log."
        )
        report_clause = (
            f"Report on each acceptance criterion (met or not, how you checked, plus the PR link) "
            f"through {writer}: {report_command}. If you disagree with the spec, use "
            f"`--kind blocked` with your reasoning. You do not move the card yourself. Do not commit "
            f"TASK.md into the repository."
        )
        history_tail = ("origin, and the PR may already be open: start with `git fetch`, continue "
                        "the existing branch and PR, do not recreate them.")
    lines = [
        f"# Task {ref} ({card.get('project', '?')})",
        "",
        f"Your board role is worker. The workspace already sits on branch `{branch}` (it was created "
        f"when the workspace came up), so there is no branch to create or rename: commit straight "
        f"into it. {done_clause}",
        "",
        report_clause,
        "",
    ]
    if comments:
        lines += [
            f"The card below has history: it has been worked on before (a return from Blocked, a "
            f"dead head or a similar case). Branch `{branch}` may already exist on "
            f"{history_tail}",
            "",
        ]
    lines += [
        f"Always, regardless of history: force-push is forbidden; push only to your own project's "
        f"repository and only to your own branch `{branch}`.",
        "",
        "A pipeline pause (`drain` or `freeze`) is an administrative state of the whole queue, not a "
        "breakage of your card. Do not report `blocked` merely because of a pause; after `resume`, "
        "continue the same card in the same workspace.",
        "",
    ]
    if is_contrib:
        lines += [
            f"Contrib project (a fork): push only to `origin` (your fork). Do not touch `upstream` "
            f"(the author's repository), do not push to it and do not merge into it.",
            "",
        ]
    lines += _metadata(card, base)
    lines += [
        "## Worker write protocol",
        "",
        f"Comments: `{task_protocol.command('comment', ref)} --body-file <file>`.",
    ]
    if not legacy:
        lines += [
            "A compatibility bridge still leaves board credentials in the CLI environment. That is "
            "not a technical least-privilege boundary; a broker and identity isolation remain future "
            "phases.",
        ]
    lines += [""]
    lines += [naming.memory_block("worker", card.get("project") or "?"), ""]
    lines += ["## Spec", "", view.get("description") or "(the card description is empty)", ""]
    lines += _operator_context(comments)
    lines += _history(comments)
    return "\n".join(lines).rstrip("\n") + "\n"
