"""One-shot, AST-checked extraction against secretary main 1770764 (not product code)."""
from __future__ import annotations

import ast
import copy
import inspect
import re
import textwrap
from pathlib import Path

ROOT = Path.cwd()
path = ROOT / 'src/secretary/dispatcher.py'
source = path.read_text()
tree = ast.parse(source)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DispatcherRuntime')
names = {
    '_begin_red_transition': 'begin_red_transition',
    '_complete_red_transition': 'complete_red_transition',
    '_deliver_red_continuation': '_deliver_red_continuation',
    '_observe_retained_continuation_progress': '_observe_retained_continuation_progress',
    '_block_unadmitted_continuation_liveness': '_block_unadmitted_continuation_liveness',
    '_continuation_recovery_window': '_continuation_recovery_window',
    '_advance_no_progress_continuation': '_advance_no_progress_continuation',
    '_finish_retained_worker_resume': '_finish_retained_worker_resume',
    '_restart_red_worker': '_restart_red_worker',
    '_record_worker_continuation': '_record_worker_continuation',
}
free_names = {'_retained_worker_busy_deferred', '_retained_worker_recovery_window', '_continuation_no_progress_evidence'}
methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
assert len(methods) == len(names), 'Unexpected base: extraction already applied or source changed'
lines = source.splitlines(keepends=True)


def relocate_calls(text):
    text = re.sub(r'\bself\b', 'runtime', text)
    for old, new in names.items():
        text = text.replace(f'runtime.{old}(', f'{new}(runtime, ')
    return text


class ExpectedMove(ast.NodeTransformer):
    """Check every original statement survives, changing only ownership and runtime binding."""
    def visit_Name(self, node):
        if node.id == 'self':
            node.id = 'runtime'
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id == 'runtime' and node.func.attr in names:
                node.func = ast.Name(id=names[node.func.attr], ctx=ast.Load())
                node.args.insert(0, ast.Name(id='runtime', ctx=ast.Load()))
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        node.name = names[node.name]
        assert node.args.args[0].arg == 'self'
        node.args.args[0].arg = 'runtime'
        node.args.args[0].annotation = ast.Name(id='Any', ctx=ast.Load())
        return node


parts = []
for node in methods:
    text = textwrap.dedent(''.join(lines[node.lineno - 1:node.end_lineno]))
    text = relocate_calls(text)
    text = text.replace(f'def {node.name}(', f'def {names[node.name]}(', 1)
    text = text.replace('    runtime,', '    runtime: Any,', 1)
    generated = ast.parse(text).body[0]
    expected = ExpectedMove().visit(copy.deepcopy(node))
    for function in (generated, expected):
        if ast.get_docstring(function) is not None:
            function.body[0].value.value = inspect.cleandoc(function.body[0].value.value)
    assert ast.dump(generated) == ast.dump(expected), f'Behavior changed in {node.name}'
    parts.append(text.rstrip())

advance = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_advance_worker')
advance_text = ''.join(lines[advance.lineno - 1:advance.end_lineno])
start = advance_text.index('        continuation = record.worker_continuation\n')
end = advance_text.index('        reported = _handle_worker_report(', start)
recovery = advance_text[start:end]
recovery_body = relocate_calls(textwrap.dedent(recovery))
recovery_text = '''def recover_worker_continuation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    marker: str | None,
) -> dict[str, Any] | None:
    """Recover delivery after report lookup, before report consumption or shared wait policy.

    A terminal marker can prove delivery. Red-transition replay has a separate entry point
    because it must run before looking up a report from the newly reserved generation.
    """
    ref = task["ref"]
''' + textwrap.indent(recovery_body, '    ') + '    return None\n'
expected_branches = ExpectedMove().visit(ast.parse(textwrap.dedent(recovery))).body
actual_branches = ast.parse(recovery_text).body[0].body[2:-1]
assert ast.dump(ast.Module(body=actual_branches, type_ignores=[])) == ast.dump(ast.Module(body=expected_branches, type_ignores=[]))
parts.insert(0, recovery_text.rstrip())
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in free_names:
        parts.append(''.join(lines[node.lineno - 1:node.end_lineno]).rstrip())

body = '\n\n\n'.join(parts) + '\n'
used = {n.id for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
imports = []
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)) and not (isinstance(node, ast.ImportFrom) and node.module == '__future__'):
        selected = [alias for alias in node.names if (alias.asname or alias.name.split('.')[0]) in used]
        if selected:
            item = copy.deepcopy(node)
            item.names = selected
            imports.append(ast.unparse(item))
header = '''"""Retained/red worker continuation and its bounded delivery recovery.

This module owns the durable red intent, board transition, continuation delivery and
confirmed-stop replacement handoff. Models remain in worker_lifecycle; launches remain in
worker_launch. Gate/review decisions and shared wait/vitality policy retain their owners.
The runtime is a collaborator, not an implementation facade, as in worker_report.
"""

from __future__ import annotations

'''
new_module = ROOT / 'src/secretary/dispatch/worker_continuation.py'
new_module.write_text(header + '\n'.join(imports) + '\n\n\n' + body)

ranges = [(n.lineno - 1, n.end_lineno) for n in methods]
ranges += [(n.lineno - 1, n.end_lineno) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in free_names]
for start_line, end_line in sorted(ranges, reverse=True):
    del lines[start_line:end_line]
new_source = ''.join(lines)
new_source = new_source.replace(recovery, '''        recovered = _recover_worker_continuation(
            self, task, record, records, payload, attempt_id, marker=marker
        )
        if recovered is not None:
            return recovered
''', 1)
for old in ('_begin_red_transition', '_complete_red_transition'):
    new_source = new_source.replace(f'self.{old}(', f'{old}(self, ')
new_tree = ast.parse(new_source)
remaining = {n.id for n in ast.walk(new_tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
new_lines = new_source.splitlines(keepends=True)
for node in reversed(new_tree.body):
    if isinstance(node, (ast.Import, ast.ImportFrom)) and not (isinstance(node, ast.ImportFrom) and node.module == '__future__'):
        text = ''.join(new_lines[node.lineno - 1:node.end_lineno])
        selected = [alias for alias in node.names if (alias.asname or alias.name.split('.')[0]) not in used or (alias.asname or alias.name.split('.')[0]) in remaining]
        if len(selected) != len(node.names) and 'noqa' not in text:
            item = copy.deepcopy(node)
            item.names = selected
            new_lines[node.lineno - 1:node.end_lineno] = [ast.unparse(item) + '\n'] if selected else []
new_source = ''.join(new_lines)
new_source = re.sub(r'\n{4,}', '\n\n\n', new_source)
new_source = new_source.replace('from secretary.dispatch.launch import REVIEW_ROLE, STAGE_RESPAWN, WORKER_ROLE, BringUpFailure', 'from secretary.dispatch.launch import (\n    REVIEW_ROLE,\n    STAGE_RESPAWN,\n    WORKER_ROLE,\n    BringUpFailure,\n)')
anchor = 'from secretary.dispatch.worker_launch import ('
assert anchor in new_source
new_source = new_source.replace(anchor, '''from secretary.dispatch.worker_continuation import (
    begin_red_transition as _begin_red_transition,
    complete_red_transition as _complete_red_transition,
    recover_worker_continuation as _recover_worker_continuation,
)
''' + anchor, 1)
path.write_text(new_source)

for filename in ('tests/test_dispatcher.py', 'tests/test_dispatcher_launch_intent.py'):
    p = ROOT / filename
    text = p.read_text()
    text = text.replace('from secretary import dispatcher as ', 'from secretary.dispatch import worker_continuation as dispatcher_worker_continuation\nfrom secretary import dispatcher as ', 1)
    for name in ('_deliver_red_continuation', '_finish_retained_worker_resume'):
        text = text.replace(f'mock.patch.object(self.runtime, "{name}"', f'mock.patch.object(dispatcher_worker_continuation, "{name}"')
    p.write_text(text)
p = ROOT / 'tests/test_dispatcher_contracts.py'
text = p.read_text().replace('from secretary.dispatch import worker_launch as dispatcher_worker_launch', 'from secretary.dispatch import worker_continuation as dispatcher_worker_continuation\nfrom secretary.dispatch import worker_launch as dispatcher_worker_launch', 1)
text = text.replace('    dispatcher_worker_launch,\n', '    dispatcher_worker_launch,\n    dispatcher_worker_continuation,\n', 1)
p.write_text(text)
p = ROOT / 'tests/test_architecture.py'
text = p.read_text().replace('advance_source.index("if continuation.delivery_pending:")', 'advance_source.index("_recover_worker_continuation(")').replace('advance_source.index("if continuation.delivery_confirmed:")', 'advance_source.index("_recover_worker_continuation(")')
p.write_text(text)
p = ROOT / 'pyproject.toml'
p.write_text(p.read_text().replace('    "src/secretary/dispatch/worker_report.py",', '    "src/secretary/dispatch/worker_report.py",\n    "src/secretary/dispatch/worker_continuation.py",', 1))
p = ROOT / 'tests/ci-shards.txt'
p.write_text(p.read_text().replace('unit tests/test_dispatcher_worker_report.py', 'unit tests/test_dispatcher_worker_report.py\nunit tests/test_dispatcher_worker_continuation.py', 1))
p = ROOT / 'docs/ARCHITECTURE.md'
text = p.read_text()
anchor = 'Dependency rules:\n'
paragraph = '''The dispatcher is being split by lifecycle ownership. `dispatch.worker_continuation` owns
retained/red continuation: durable rework intent, replayable board move, delivery and its bounded
provider-progress recovery, and the confirmed-stop handoff to `dispatch.worker_launch`. Durable
models stay in `dispatch.worker_lifecycle`. In `_advance_worker`, red-transition replay runs before
report lookup; delivery recovery runs after `worker_report_marker` but before `handle_worker_report`.
This ordering lets a report prove a prior delivery without resending its prompt. Gate/review decisions,
Assessment parking, and shared wait/watchdog/vitality policy are outside this boundary. There are no
implementation callbacks into the dispatcher for the extracted continuation methods.

'''
assert anchor in text
p.write_text(text.replace(anchor, paragraph + anchor, 1))
test_source = ROOT / '.workbench/test_dispatcher_worker_continuation.py'
if test_source.exists():
    (ROOT / 'tests/test_dispatcher_worker_continuation.py').write_text(test_source.read_text())
print('Verified AST-equivalent relocation of', len(methods), 'methods, delivery-recovery branches and', len(free_names), 'helpers')
print('New continuation module:', len(new_module.read_text().splitlines()), 'lines')
print('Dispatcher runtime:', len(new_source.splitlines()), 'lines')
