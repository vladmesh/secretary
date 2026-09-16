from pathlib import Path

path = Path(__file__).with_name("a08_bootstrap.py")
text = path.read_text(encoding="utf-8")
old = '''    if count != 1:\n        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:80]!r}")\n    write(path, content.replace(old, new, 1))\n'''
new = '''    if count < 1:\n        raise RuntimeError(f"{path}: expected a match, found {count}: {old[:80]!r}")\n    write(path, content.replace(old, new, 1))\n'''
if text.count(old) != 1:
    raise RuntimeError("bootstrap replace_once guard changed unexpectedly")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
