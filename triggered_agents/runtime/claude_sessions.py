"""Where Claude Code keeps one workspace's transcripts, spelled the way Claude Code spells it.

This is a foreign product's on-disk convention, and the only thing that makes a rule about it true
is the catalogue Claude Code actually writes. It writes one directory per project under
`~/.claude/projects`, named after the absolute workspace path with **every** character that is not
a letter or a digit replaced by `-` — separators and underscores alike.

Reading that name as `path.replace('/', '-')` is what blinded the worker bring-up on 2026-08-11:
for `/home/dev/orca/workspaces/codegen_orchestrator/...` the glob looked into a directory that has
never existed, the durable user-turn criterion could therefore never fire, and six live heads were
closed twelve seconds after each took its prompt. Secretary's own workspaces carry no underscore,
which is why the same code confirmed secretary cards and only ever failed on another product.

`tests/test_dispatcher_tui.py` holds that rule against the real `~/.claude/projects` on the host,
because a unit test that mocks the directory it is asserting about proves nothing about a contract
somebody else owns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

__all__ = ["claude_project_dir_name", "claude_session_paths"]

# Every non-alphanumeric character, not just the separator. Verified against the 58 project
# directories on this host: none of them contains an underscore, a dot or any other punctuation,
# although the workspace paths they were named after do.
_NON_PROJECT_CHAR_RE = re.compile(r"[^A-Za-z0-9]")


def claude_project_dir_name(workspace: str) -> str:
    """The `~/.claude/projects` directory name Claude Code gives this workspace."""
    return _NON_PROJECT_CHAR_RE.sub("-", str(Path(workspace).resolve(strict=False)))


def claude_session_paths(workspace: str, *, root: Path) -> Iterator[Path]:
    """Yield this workspace's Claude session logs without scanning the other projects."""
    try:
        yield from (root / claude_project_dir_name(workspace)).glob("*.jsonl")
    except OSError:
        return
