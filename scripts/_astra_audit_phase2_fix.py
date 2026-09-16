from pathlib import Path

path = Path("astra_audit.md")
text = path.read_text(encoding="utf-8")
old = (
    "A10 is complete via PR #441. A07 is complete across PR #443 and PR #444: product board roles now have one canonical typed vocabulary from the normalized Actor through TaskWriter, CLI choices, transitions, and importer boundaries; the separate legacy runtime registry stays with A19. A08 is complete via PR #445. A09 is complete across PR #442 and PR #446: both task and sprint cross-feature private-normalizer imports are gone. PR #446 landed the first A11 typed read-model slice, and PR #447 completed the bounded admission/reservation/guard-index slice. A11 now continues only with the broader write-path/internal sprint-document flow. The goal remains one typed vocabulary per concept and one parser at each legacy boundary."
)
new = (
    "A10 is complete via PR #441. A07 is complete across PR #443 and PR #444: product board roles now have one canonical typed vocabulary from the normalized Actor through TaskWriter, CLI choices, transitions, and importer boundaries; the separate legacy runtime registry stays with A19. A08 is complete via PR #445. A09 is complete across PR #442 and PR #446: both task and sprint cross-feature private-normalizer imports are gone. A11 is complete across PR #446, PR #447, PR #448, and PR #449: read normalization, admission/guard state, non-close writes, and the close transaction now each have typed domain boundaries while released persistence/public projections remain compatible. The goal remains one typed vocabulary per concept and one parser at each legacy boundary; the next bounded dict-protocol cleanup is A12."
)
if text.count(old) != 1:
    raise RuntimeError(f"expected one stale Phase 2 paragraph, found {text.count(old)}")
text = text.replace(old, new, 1)
if "A11 now continues" in text:
    raise RuntimeError("stale A11 continuation wording remains")
path.write_text(text, encoding="utf-8")
