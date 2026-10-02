"""The old product name stays only where docs/RENAME.md §T5 allows it.

Every tracked path and every tracked text file is checked for `secretary` in any case. A match
passes only when a row of `ALLOWLIST` covers it: the Hermes agent (H), the instance repository (I),
historical records (R) or the transition's own files (T). There is one table and no per-file
exemption outside those four classes, so a new mention of the old name fails here with its file,
line and text.

Class T: this file names the old name on purpose.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OLD_NAME = re.compile(r"(?i)secretary")

#: Any tracked path.
ANYWHERE = ("*",)

#: The allowlist: (class, what the old name may appear in, the paths where it may). A pattern of
#: `None` allows the whole file, content and path alike. `fnmatch` patterns, so `*` crosses `/`.
ALLOWLIST: tuple[tuple[str, re.Pattern[str] | None, tuple[str, ...]], ...] = (
    ("H", re.compile(r"~/\.hermes/skills/secretary(?:-roles)?\b"), ("skills/manifest.toml",)),
    ("H", re.compile(r"\bhermes-secretary(?:-roles)?\b"), ("skills/manifest.toml",)),
    # `secretary-instance-maintenance` is a product unit, not the instance repository.
    ("I", re.compile(r"\bsecretary-instance\b(?!-maintenance)"), ANYWHERE),
    # A card ref; `secretary-0.1.0.dist-info` is a version, not a ref.
    ("R", re.compile(r"\bsecretary-\d+\b(?!\.\d)"), ANYWHERE),
    ("R", re.compile(r"\bsecretary_1727_[0-9a-f]+\.jsonl\.gz\b"), ANYWHERE),
    # A quoted historical record, replayed against its immutable audit digest byte for byte.
    ("R", None, ("tests/fixtures/gate_attestation_1883.json",)),
    # The names of the class T files and of the transition's own command, wherever they are referenced.
    (
        "T",
        re.compile(
            r"\btransition-from-secretary\.sh\b|\btest_transition_from_secretary\b|\bfrom-secretary\b"
        ),
        ANYWHERE,
    ),
    (
        "T",
        None,
        (
            "src/ummanu/transition/*",
            "scripts/transition-from-secretary.sh",
            "tests/test_transition_from_secretary.py",
            "scripts/rename_to_ummanu.py",
            "docs/RENAME.md",
            "tests/test_old_name_guard.py",
        ),
    ),
)


def _applies(globs: tuple[str, ...], path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


def violations(path: str, text: str | None) -> list[str]:
    """Every match of the old name in `path` and in its `text` (None for a binary file) that no
    allowlist row covers, as `path:line: match in context`."""
    rows = [(cls, pattern) for cls, pattern, globs in ALLOWLIST if _applies(globs, path)]
    if any(pattern is None for _, pattern in rows):
        return []
    patterns = [pattern for _, pattern in rows if pattern is not None]
    found = [f"{path}: path carries '{match}'" for match in _uncovered(path, patterns)]
    if text is None:
        return found
    for number, line in enumerate(text.splitlines(), start=1):
        found += [
            f"{path}:{number}: '{match}' in {line.strip()[:160]!r}" for match in _uncovered(line, patterns)
        ]
    return found


def _uncovered(text: str, patterns: list[re.Pattern[str]]) -> list[str]:
    hits = list(OLD_NAME.finditer(text))
    if not hits:
        return []
    allowed = [match.span() for pattern in patterns for match in pattern.finditer(text)]
    return [
        hit.group(0)
        for hit in hits
        if not any(start <= hit.start() and hit.end() <= end for start, end in allowed)
    ]


def text_of(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def tracked_files() -> list[str]:
    completed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git ls-files failed: {completed.stderr.decode(errors='replace').strip()}")
    return [name for name in completed.stdout.decode().split("\0") if name]


class OldNameGuardTests(unittest.TestCase):
    def test_the_tree_names_the_old_product_only_where_the_allowlist_does(self) -> None:
        found: list[str] = []
        for name in tracked_files():
            path = ROOT / name
            if path.is_symlink() or not path.is_file():
                found += violations(name, None)
                continue
            found += violations(name, text_of(path.read_bytes()))
        self.assertEqual(found, [], "the old product name outside docs/RENAME.md §T5:\n" + "\n".join(found))

    def test_a_stray_old_name_fails_in_every_file_outside_the_whole_file_rows(self) -> None:
        names = tracked_files()
        self.assertGreater(len(names), 100)
        exempt = {name for name in names if violations(name, "secretary") == []}
        self.assertEqual(
            exempt,
            {
                name
                for name in names
                if any(pattern is None and _applies(globs, name) for _, pattern, globs in ALLOWLIST)
            },
            "only the whole-file rows (the T files and the one R record) may carry a stray old name",
        )

    def test_a_failure_names_the_file_line_and_match(self) -> None:
        found = violations("docs/OPERATIONS.md", "# Operations\n\nRun `python3 -m Secretary doctor`.\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("docs/OPERATIONS.md:3: 'Secretary' in "), found[0])
        self.assertEqual(
            violations("src/ummanu/secretary_helper.py", ""),
            [
                "src/ummanu/secretary_helper.py: path carries 'secretary'",
            ],
        )
        self.assertEqual(len(violations("tests/test_x.py", "SECRETARY_DATA_DIR = 1\n")), 1)

    def test_each_class_covers_its_own_spelling_only(self) -> None:
        allowed = {
            "skills/manifest.toml": '[targets.hermes-secretary-roles]\nroot = "~/.hermes/skills/secretary"\n',
            "README.md": "The instance repo `vladmesh/secretary-instance`, its card secretary-instance-12.\n",
            "docs/OPERATIONS.md": "Since secretary-1932 (branch pipeline/secretary-1932).\n",
            "tests/test_local_pty_journal_turn.py": 'FIXTURE = "secretary_1727_9c6b884b.jsonl.gz"\n',
            "tests/fixtures/local_pty_journals/secretary_1727_9c6b884b.jsonl.gz": None,
            "src/ummanu/transition/names.py": 'OLD = "secretary"\n',
            "docs/RENAME.md": "secretary → ummanu\n",
            "tests/fixtures/gate_attestation_1883.json": '{"url": "https://github.com/vladmesh/secretary"}\n',
            "tests/ci-shards.txt": "unit tests/test_transition_from_secretary.py\n",
            "docs/PROTOCOLS.md": "Run `ummanu transition from-secretary --plan`.\n",
        }
        for path, text in allowed.items():
            with self.subTest(path=path):
                self.assertEqual(violations(path, text), [])
        refused = {
            # H spellings belong to the skills manifest only.
            "docs/OPERATIONS.md": "root = ~/.hermes/skills/secretary\n",
            # The instance-maintenance unit is a product unit.
            "packaging/systemd/README.md": "secretary-instance-maintenance.timer\n",
            # A version is not a card ref.
            "tests/test_upgrade.py": 'dist = "secretary-0.1.0.dist-info"\n',
            "tests/test_role_skills.py": "[roles.secretary]\n",
            "pyproject.toml": "[tool.secretary]\n",
            "src/ummanu/cli.py": 'prog="secretary"\n',
        }
        for path, text in refused.items():
            with self.subTest(path=path):
                self.assertEqual(len(violations(path, text)), 1, violations(path, text))


if __name__ == "__main__":
    unittest.main()
