from pathlib import Path

path = Path(__file__).with_name("a08_bootstrap.py")
text = path.read_text(encoding="utf-8")

old_guard = '''    if count != 1:\n        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:80]!r}")\n    write(path, content.replace(old, new, 1))\n'''
new_guard = '''    if count < 1:\n        raise RuntimeError(f"{path}: expected a match, found {count}: {old[:80]!r}")\n    write(path, content.replace(old, new, 1))\n'''
if text.count(old_guard) != 1:
    raise RuntimeError("bootstrap replace_once guard changed unexpectedly")
text = text.replace(old_guard, new_guard, 1)

old_restore_target = '        "    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,\\n    positive_int as _positive_int,\\n",\n'
new_restore_target = (
    '        "    TASK_STATE_BY_COLUMN as _STATE_BY_COLUMN,\\n'
    '    enum_or_default as _enum_or_default,  # noqa: F401 - released private compatibility alias\\n'
    '    positive_int as _positive_int,\\n",\n'
)
if text.count(old_restore_target) != 1:
    raise RuntimeError("bootstrap restore compatibility target changed unexpectedly")
text = text.replace(old_restore_target, new_restore_target, 1)

path.write_text(text, encoding="utf-8")
