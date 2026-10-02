#!/usr/bin/env python3
"""One-shot rewrite of the product name from `secretary` to `ummanu` (docs/RENAME.md §T6 row 4).

Run once from the repository root on a clean tree: `python3 scripts/rename_to_ummanu.py`. It rewrites
the content of every tracked text file and then moves every tracked path that carries the name, both
through the same ordered rule table. Spans matched by the skip table are left exactly as they are, and
the files of class T are not rewritten at all (`transition/` still moves with the package, byte for
byte). `--dry-run` prints the counts and the moves and writes nothing.

Class T (docs/RENAME.md §T5): this file keeps both names on purpose and may be deleted in the
packaging sprint.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tokenize
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Ordered rewrite rules, applied one after another to every text span the skip table leaves alone.
#: The role rules (secretary-1931) come before the generic case rules so the report counts them on
#: their own; each is also what the generic rule would produce.
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("env TA_SECRETARY_REPO", re.compile(r"\bTA_SECRETARY_REPO\b"), "UMMANU_REPO"),
    ("role [roles.secretary]", re.compile(r"\[roles\.secretary\]"), "[roles.ummanu]"),
    (
        "role skill secretary/<skill>",
        re.compile(
            r'"secretary/(?=(?:open-sprint|grilling|knowledge-doc|grill-me|thermo-nuclear-code-quality-review)")'
        ),
        '"ummanu/',
    ),
    ("role skills dir", re.compile(r"\bskills/roles/secretary\b"), "skills/roles/ummanu"),
    (
        "role target claude-secretary-global",
        re.compile(r"\bclaude-secretary-global\b"),
        "claude-ummanu-global",
    ),
    ("role target codex-orca-secretary", re.compile(r"\bcodex-orca-secretary\b"), "codex-orca-ummanu"),
    ("role worktrees", re.compile(r"\borca/workspaces/secretary\b"), "orca/workspaces/ummanu"),
    ("UPPER SECRETARY", re.compile(r"SECRETARY"), "UMMANU"),
    ("Title Secretary", re.compile(r"Secretary"), "Ummanu"),
    ("lower secretary", re.compile(r"secretary"), "ummanu"),
)

#: Spans that keep the old name: exactly the four allowlist classes of §T5 (H, I, R in text; T are
#: whole files, below). One alternation, so overlapping spans resolve leftmost-longest per position.
SKIPS: tuple[tuple[str, str], ...] = (
    ("H", r"~/\.hermes/skills/secretary(?:-roles)?\b"),
    ("H", r"\bhermes-secretary(?:-roles)?\b"),
    # `secretary-instance-maintenance` is a product unit, not the instance repository.
    ("I", r"\bsecretary-instance\b(?!-maintenance)"),
    # A card ref; `secretary-0.1.0.dist-info` is a version, not a ref.
    ("R", r"\bsecretary-\d+\b(?!\.\d)"),
    ("R", r"\bsecretary_1727_[0-9a-f]+\.jsonl\.gz\b"),
    # Names of the class T files and of the transition's own command, wherever they are referenced.
    ("T", r"\btransition-from-secretary\.sh\b|\btest_transition_from_secretary\b|\bfrom-secretary\b"),
)
SKIP_PATTERN = re.compile(
    "|".join(f"(?P<{cls}{index}>{pattern})" for index, (cls, pattern) in enumerate(SKIPS))
)

#: Class T: files whose content is never rewritten. `src/secretary/transition/` moves with the package.
T_FILES = (
    "docs/RENAME.md",
    "scripts/rename_to_ummanu.py",
    "scripts/transition-from-secretary.sh",
    "tests/test_transition_from_secretary.py",
    "tests/test_old_name_guard.py",
)
T_DIRS = ("src/secretary/transition/",)
#: Class T paths that keep their own name.
T_KEEP_PATH = (
    "scripts/transition-from-secretary.sh",
    "tests/test_transition_from_secretary.py",
    "docs/RENAME.md",
)

#: Card refs as test data. A test's cards belong to the product's project and a new card's ref is
#: derived from the project id (`f"{project}-"`), so a ref inside a string of test code follows the
#: project: `"secretary-700"` -> `"ummanu-700"`, the same as the `f"secretary-{n}"` refs the case rules
#: already rewrite. Refs in comments and docstrings cite real cards (R) and stay, and so do the refs of
#: a test that replays a recorded historical record (R).
TEST_DATA_REF = re.compile(r"\bsecretary-(\d+)\b(?!\.\d)")
TEST_DATA_RECORDS = (
    "tests/test_dispatcher_gate_receipt.py",  # replays fixtures/gate_attestation_1883.json
    "tests/test_codex_provider_event_ingress.py",  # a golden fingerprint recorded from the deployed producer
)


def is_test_code(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py") and path not in TEST_DATA_RECORDS


def test_data_refs(text: str, counts: Counter[str]) -> str:
    """`text` with the card refs in its non-docstring string literals moved to the new family."""
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    literal = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    skipped = {tokenize.NL, tokenize.COMMENT}
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.type not in literal or not TEST_DATA_REF.search(token.string):
            continue
        before = next((t for t in reversed(tokens[:index]) if t.type not in skipped), None)
        after = next((t for t in tokens[index + 1 :] if t.type not in skipped), None)
        docstring = (
            before is None or before.type in {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
        ) and (after is not None and after.type in {tokenize.NEWLINE, tokenize.ENDMARKER})
        if docstring:
            counts["skip R (docstring ref)"] += len(TEST_DATA_REF.findall(token.string))
            continue
        spans.append((starts[token.start[0] - 1] + token.start[1], starts[token.end[0] - 1] + token.end[1]))
    for start, end in reversed(spans):
        segment, hits = TEST_DATA_REF.subn(r"ummanu-\1", text[start:end])
        counts["test data ref secretary-N"] += hits
        text = text[:start] + segment + text[end:]
    return text


def rewrite(text: str, counts: Counter[str]) -> str:
    """`text` with every rule applied outside the skip spans; tallies rules and skips into `counts`."""
    out: list[str] = []
    cursor = 0
    for match in SKIP_PATTERN.finditer(text):
        out.append(_apply(text[cursor : match.start()], counts))
        out.append(match.group(0))
        counts[f"skip {match.lastgroup[0]}"] += 1
        cursor = match.end()
    out.append(_apply(text[cursor:], counts))
    return "".join(out)


def _apply(segment: str, counts: Counter[str]) -> str:
    for name, pattern, replacement in RULES:
        segment, hits = pattern.subn(replacement, segment)
        counts[name] += hits
    return segment


def is_t_content(path: str) -> bool:
    return path in T_FILES or path.startswith(T_DIRS)


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [name for name in out.decode().split("\0") if name]


def text_of(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    content_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    rewritten = 0
    moves: list[tuple[str, str]] = []
    for name in tracked():
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        if not is_t_content(name):
            text = text_of(path)
            if text is not None:
                new = rewrite(
                    test_data_refs(text, content_counts) if is_test_code(name) else text, content_counts
                )
                if new != text:
                    rewritten += 1
                    if not args.dry_run:
                        path.write_text(new, encoding="utf-8")
        if name in T_KEEP_PATH:
            continue
        target = rewrite(name, path_counts)
        if target != name:
            moves.append((name, target))

    if not args.dry_run:
        for source, target in moves:
            (ROOT / target).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "mv", source, target], cwd=ROOT, check=True)

    print(f"content: {rewritten} files rewritten")
    for key, value in sorted(content_counts.items()):
        print(f"  {key}: {value}")
    print(f"paths: {len(moves)} moved")
    for key, value in sorted(path_counts.items()):
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
