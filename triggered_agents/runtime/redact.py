"""Secret redaction — scrub raw secrets before a transcript reaches the model or canon.

Git is forever; a key that lands in a canon's history is compromised for good. This runs
before extraction (so the model never sees the raw secret) and is the last line before
anything is written. Two layers:

  1. Exact-value scrub: load values from known .env files on disk and redact verbatim
     occurrences. Strongest — catches whatever actually lives in this VPS's secrets.
  2. Pattern scrub: regexes for well-known key shapes (sk-…, AGE-SECRET, Bearer, …),
     a backstop for secrets pasted into a transcript that aren't in any .env we know.
"""
from __future__ import annotations

import re
from pathlib import Path

# .env files whose VALUES are known secrets on this host. Exact matches get scrubbed.
DEFAULT_ENV_FILES = [
    Path.home() / ".hermes" / ".env",
    Path.home() / "secretary-instance" / "runtime.env",
]

# Minimum length for an .env value to be treated as a secret worth scrubbing verbatim
# (short values like "true"/"8077" are config, not secrets, and would over-redact).
MIN_ENV_VALUE_LEN = 12

REDACTED = "«REDACTED»"

# Well-known secret shapes. Ordered longest/most-specific first.
PATTERNS = [
    (re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{50,}"), "age-secret-key"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "anthropic-key"),
    (re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"), "openrouter-key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "openai-project-key"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "openai-key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "github-token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack-token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key-id"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "google-api-key"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"), "bearer-token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "private-key-block"),
]


def _load_env_values(env_files) -> list[str]:
    values = []
    for path in env_files:
        p = Path(path)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if len(val) >= MIN_ENV_VALUE_LEN:
                values.append(val)
    # Longest first so a value that contains another gets scrubbed whole.
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, env_files=None) -> str:
    """Return `text` with known secrets replaced by a labeled placeholder."""
    if not text:
        return text
    for val in _load_env_values(DEFAULT_ENV_FILES if env_files is None else env_files):
        if val in text:
            text = text.replace(val, f"{REDACTED}:env-value")
    for pat, label in PATTERNS:
        text = pat.sub(f"{REDACTED}:{label}", text)
    return text


if __name__ == "__main__":
    sample = (
        "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX and "
        "AGE-SECRET-KEY-1QQPQRSTUVWXYZ0123456789QQPQRSTUVWXYZ0123456789QQPQ "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
    )
    print(redact(sample))
