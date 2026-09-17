from __future__ import annotations

from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]

    manifest = root / "tests/ci-shards.txt"
    text = manifest.read_text()
    marker = "unit tests/test_a12_source_bridge.py\n"
    if marker not in text:
        raise RuntimeError("temporary A12 source bridge is not registered")
    manifest.write_text(text.replace(marker, "", 1))

    workflow = root / ".github/workflows/ci.yml"
    text = workflow.read_text()
    start = text.index("  a12_source_patch:\n")
    end = text.index("  test_suites:\n", start)
    workflow.write_text(text[:start] + text[end:])

    for relative in (
        "tests/test_a12_source_bridge.py",
        "scripts/a12_patch_sources.py",
        "scripts/a12_cleanup.py",
    ):
        (root / relative).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
