from pathlib import Path

path = Path(__file__).with_name("_astra_a11_close_patch.py")
text = path.read_text(encoding="utf-8")
replacements = {
    '"\\n    def _close_targets(\\n"': '"\\n    def _close_targets"',
    '"    def _close_targets(\\n"': '"    def _close_targets"',
    '"\\n    def _refuse_restated_decisions(\\n"': '"\\n    def _refuse_restated_decisions"',
    '"\\n    def _check_closeout_is_writable(\\n"': '"\\n    def _check_closeout_is_writable"',
    '"    def _check_staged_closeout(\\n"': '"    def _check_staged_closeout"',
    '"\\n    def _refuse_restated_closeout(\\n"': '"\\n    def _refuse_restated_closeout"',
    '"    def _check_close_decisions_are_writable(\\n"': '"    def _check_close_decisions_are_writable"',
    '"\\n    def _run_close(\\n"': '"\\n    def _run_close"',
    '"    def _run_close(\\n"': '"    def _run_close"',
    '"\\n    def _close_result(\\n"': '"\\n    def _close_result"',
    '"    def _close_result(\\n"': '"    def _close_result"',
}
for old, new in replacements.items():
    if old not in text:
        raise RuntimeError(f"patch marker missing: {old}")
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
