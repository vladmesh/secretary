"""Secret redaction — scrub raw secrets before a transcript reaches the model or canon.

Git is forever; a key that lands in a canon's history is compromised for good. This runs
before extraction (so the model never sees the raw secret) and is the last line before
anything is written. Two layers:

  1. Exact-value scrub: load values from known .env files on disk and redact verbatim
     occurrences. Strongest — catches whatever actually lives in this VPS's secrets.
  2. Pattern scrub: regexes for well-known key shapes (sk-…, AGE-SECRET, Bearer, …),
     a backstop for secrets pasted into a transcript that aren't in any .env we know.

`scrub_secrets` adds a third, wider layer on top of those two for text that goes onto a board
card: secret-looking KEY=value assignments and long token-shaped blobs.
"""
from __future__ import annotations

import re
from pathlib import Path

from triggered_agents.runtime.role_env import is_sensitive_env_name

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
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github-pat"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "slack-token"),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"), "slack-webhook"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key-id"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "google-api-key"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"), "bearer-token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "private-key-block"),
]

# `runtime.env` is an environment *configuration* file, not a list of secret
# values.  In particular a local board URL is deliberately long enough to have
# tripped the old length-only rule.  Treating every long value as a secret made
# mentioning KANBOARD_URL on a card stop the checkpoint and, worse, made a
# normal config value look like leaked credential material.
#
# Names remain the primary signal for exact-value redaction.  A URL with user
# info is the exception: it can carry a password even when its variable is
# named DATABASE_URL or KANBOARD_URL, so its value is protected too.  Pattern
# redaction below remains the backstop for credentials that arrive outside the
# selected runtime file.
_URL_WITH_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s@]+@")


def looks_like_credential(value: str) -> bool:
    """Whether plaintext itself has a known credential or credential-URL shape."""
    return bool(_URL_WITH_USERINFO_RE.match(value)) or any(
        pattern.search(value) for pattern, _label in PATTERNS
    )


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
            name, _, val = line.partition("=")
            name = name.strip()
            val = val.strip().strip('"').strip("'")
            if len(val) >= MIN_ENV_VALUE_LEN and (
                is_sensitive_env_name(name) or _URL_WITH_USERINFO_RE.match(val)
            ):
                values.append(val)
    # Longest first so a value that contains another gets scrubbed whole.
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, env_files=None, secret_values=None) -> str:
    """Return `text` with known secrets replaced by a labeled placeholder."""
    if not text:
        return text
    files = [*DEFAULT_ENV_FILES, *(env_files or ())]
    values = [*(_load_env_values(files)), *(secret_values or ())]
    for val in sorted({str(value) for value in values if len(str(value)) >= MIN_ENV_VALUE_LEN}, key=len, reverse=True):
        if val in text:
            text = text.replace(val, f"{REDACTED}:env-value")
    for pat, label in PATTERNS:
        text = pat.sub(f"{REDACTED}:{label}", text)
    return text


# Board comments are a card's public journal, so error texts and captured logs get scrubbed
# before posting. `redact` above catches known .env values and token shapes; on top of that:
# KEY=value assignments whose name smells like a secret, and long base64/hex-ish blobs
# (no `/`, so filesystem paths survive).
# Keep a marker emitted by an upstream scrubber verbatim.  TaskWriter applies
# this final board-boundary scrub even when a dispatcher already scrubbed its
# diagnostic, and replacing one safe marker with another only makes audit
# evidence noisier.
_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)"
    r"\s*=\s*(?!<redacted>|«REDACTED»)(\S+)"
)
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+=_-]{40,}\b")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_git_sha(blob: str) -> bool:
    """A git sha — full (40 hex) or abbreviated (7-40 hex) — is plain hex, no `+`/`=`/mixed-case
    entropy a real token would carry. Masking it turns a commit reference in a CI-failure comment
    into noise for no security gain."""
    return bool(_HEX_RE.match(blob))


def scrub_secrets(text: str, env_files=None, secret_values=None) -> str:
    """Mask secret-looking material in `text` before it reaches a board comment. `_BLOB_RE` casts
    a wide net over long alnum runs, so a git sha or any other hex-shaped identifier is spared —
    only the rest (base64/token-looking blobs) gets masked."""
    if not text:
        return text
    text = redact(text, env_files=env_files, secret_values=secret_values)
    text = _ASSIGN_RE.sub(rf"\1={REDACTED}", text)
    return _BLOB_RE.sub(lambda m: m.group(0) if _is_git_sha(m.group(0)) else f"{REDACTED}:blob", text)


if __name__ == "__main__":
    sample = (
        "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX and "
        "AGE-SECRET-KEY-1QQPQRSTUVWXYZ0123456789QQPQRSTUVWXYZ0123456789QQPQ "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
    )
    print(redact(sample))
