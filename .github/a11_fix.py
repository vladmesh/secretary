from __future__ import annotations

from pathlib import Path


root = Path(__file__).resolve().parents[1]
path = root / "src/secretary/sprints.py"
text = path.read_text(encoding="utf-8")

old = "        _refuse_open_sprint(candidate, others, limit=self._open_sprint_limit())\n"
new = """        _refuse_open_sprint(
            SprintAdmission.from_document(candidate),
            [SprintAdmission.from_document(sprint) for sprint in others],
            limit=self._open_sprint_limit(),
        )
"""

if old not in text:
    raise RuntimeError("sprint conflict call-site anchor moved")

path.write_text(text.replace(old, new, 1), encoding="utf-8")
