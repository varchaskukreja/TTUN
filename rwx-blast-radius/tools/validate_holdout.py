#!/usr/bin/env python3
"""Developer-only holdout runner. Results stay outside submission artifacts.

Copies the pinned git tree, mutates only isolated copies, executes the full
catalog and compares actual failures with production/experimental selection.
No outcome is written into a selector or baseline dependency map.
"""
from __future__ import annotations
import argparse
import ast
import difflib
import io
import json
import os
from pathlib import Path
import platform
import random
import signal
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'

# Pytest loads this file as a plugin in a fresh child process.
_catalog = json.loads(Path(os.environ['HOLDOUT_CATALOG']).read_text())['tests'] if os.environ.get('HOLDOUT_CATALOG') else []
_requested = {test['id'] for test in _catalog}
if os.environ.get('HOLDOUT_SELECT'):
    _requested &= set(json.loads(os.environ['HOLDOUT_SELECT']))
_outcomes, _details, _collected, _finished = {}, {}, set(), set()
_collection_errors = []


def pytest_collection_modifyitems(session, config, items):
    items[:] = [item for item in items if item.nodeid in _requested]
    _collected.update(item.nodeid for item in items)


def pytest_collectreport(report):
    if report.failed:
        filename = report.nodeid.split('::')[0]
        _collection_errors.append({'node': report.nodeid, 'error': str(report.longrepr)[:4000]})
        for node in _requested:
            if node.split('::')[0] == filename:
                _outcomes[node] = 'failed'
                _finished.add(node)


def pytest_runtest_logreport(report):
    if report.nodeid not in _requested:
        return
    if report.failed:
        _outcomes[report.nodeid] = 'failed'
        _details.setdefault(report.nodeid, []).append({'phase': report.when, 'subtest': type(report).__name__ == 'SubtestReport', 'error': str(report.longrepr)[:4000]})
    elif report.when == 'call' and _outcomes.get(report.nodeid) != 'failed':
        _outcomes[report.nodeid] = report.outcome
    elif report.skipped and _outcomes.get(report.nodeid) != 'failed':
        _outcomes[report.nodeid] = 'skipped'
    if report.when == 'teardown' or report.skipped:
        _finished.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get('HOLDOUT_REPORT'):
        return
    result = {'requested': sorted(_requested), 'outcomes': _outcomes, 'details': _details, 'collected': sorted(_collected),
              'finished': sorted(_finished), 'unresolved': sorted(_requested - _finished),
              'collection_errors': _collection_errors, 'pytest_exitcode': int(exitstatus),
              'complete': not (_requested - _finished) and int(exitstatus) in (0, 1, 2),
              'python': sys.version, 'platform': platform.platform()}
    Path(os.environ['HOLDOUT_REPORT']).write_text(json.dumps(result, indent=2, sort_keys=True))


def candidates(source, excluded, prefixes):
    result = []
    for path in sorted((source / 'sqlglot').rglob('*.py')):
        relative = path.relative_to(source).as_posix()
        if relative in excluded or not any(relative.startswith(prefix) for prefix in prefixes):
            continue
        text = path.read_text()
        tree = ast.parse(text)
        def visit(node, classes=(), in_function=False):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return
            if isinstance(node, ast.ClassDef):
                classes = (*classes, node.name)
                for statement in node.body:
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) else []
                    value = getattr(statement, 'value', None)
                    if targets and all(isinstance(t, ast.Name) for t in targets) and isinstance(value, ast.Constant) and type(value.value) is bool:
                        result.append({'path': relative, 'class': '.'.join(classes), 'attribute': targets[0].id,
                                       'line': value.lineno, 'column': value.col_offset, 'end_column': value.end_col_offset,
                                       'old': value.value, 'new': not value.value})
            for child in ast.iter_child_nodes(node):
                visit(child, classes)
        visit(tree)
    return sorted(result, key=lambda entry: (entry['path'], entry['line'], entry['attribute']))


def generate(source, sample, directory, seed):
    from patches import parse_patch
    excluded = set()
    if sample:
        for patch in sample.glob('*.patch'):
            excluded.update(change['path'] for change in parse_patch(patch.read_text()))
    rng = random.Random(seed)
    groups = [('dialect_boolean', ['sqlglot/dialects/']),
              ('parser_generator_boolean', ['sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/parser.py', 'sqlglot/generator.py'])]
    plan = []
    for name, prefixes in groups:
        pool = candidates(source, excluded, prefixes)
        if not pool:
            raise ValueError(f'No fresh AST candidate for {name}')
        entry = dict(rng.choice(pool), name=name, candidate_count=len(pool))
        text = (source / entry['path']).read_text()
        lines = text.splitlines(keepends=True)
        # AST columns are UTF-8 byte offsets, even though these literals are ASCII.
        original = lines[entry['line'] - 1].encode()
        lines[entry['line'] - 1] = (original[:entry['column']] + str(entry['new']).encode() + original[entry['end_column']:]).decode()
        updated = ''.join(lines)
        compile(updated, entry['path'], 'exec')
        patch = f'diff --git a/{entry["path"]} b/{entry["path"]}\n' + ''.join(difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True), fromfile='a/' + entry['path'], tofile='b/' + entry['path']))
        patch_path = directory / f'{name}.patch'
        patch_path.write_text(patch)
        entry['patch'] = str(patch_path)
        plan.append(entry)
    return plan


def isolated_tree(source, destination):
    if destination.exists():
        raise ValueError(f'Refusing to overwrite existing isolated tree: {destination}')
    destination.mkdir(parents=True)
    archive = subprocess.check_output(['git', '-C', str(source), 'archive', REVISION])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(destination, filter='data')


def run_suite(tree, catalog, directory, timeout, nodes=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith(('BLAST_', 'HOLDOUT_')) and key != 'PYTEST_ADDOPTS'}
    env.update(HOLDOUT_CATALOG=str(catalog), HOLDOUT_REPORT=str(directory / 'pytest.json'),
               PYTHONPATH=str(Path(__file__).parent) + os.pathsep + str(tree),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1',
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''))
    if nodes is not None:
        env['HOLDOUT_SELECT'] = json.dumps(nodes)
    command = [sys.executable, '-m', 'pytest', *(nodes if nodes is not None else ['tests']), '-q', '--tb=short', '--continue-on-collection-errors', '-p', 'validate_holdout', '-p', 'pytest_timeout', '--timeout=120', '--timeout-method=signal', '-p', 'no:cacheprovider']
    started = time.monotonic()
    with (directory / 'pytest.log').open('w') as log:
        process = subprocess.Popen(command, cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    report_path = directory / 'pytest.json'
    report = json.loads(report_path.read_text()) if report_path.exists() else {'complete': False, 'outcomes': {}, 'unresolved': [test['id'] for test in _catalog]}
    report.update(wall_seconds=round(time.monotonic() - started, 3), process_exitcode=process.returncode, process_timeout=timed_out)
    report['complete'] = report.get('complete', False) and not timed_out
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def selections(source, tests, mapping, patch, mode):
    from selector import Selector
    result = {}
    if mode in ('production', 'both'):
        chosen = Selector(tests, mapping, source).select(patch)
        result['production'] = {'selected_numbers': sorted(chosen)}
    if mode in ('class_guard', 'both'):
        from experiment_classes import ImportGuard
        guard = ImportGuard(source, tests, budget=60)
        chosen = Selector(tests, mapping, source, class_guard=guard).select(patch)
        result['class_guard'] = {'selected_numbers': sorted(chosen), 'guard_calls': guard.calls}
    return result


def recheck_existing(output, catalog, nodes, timeout, reason):
    """Preserve full-run evidence and append explicit targeted environment checks."""
    plan = json.loads((output / 'plan.json').read_text())['mutations']
    summary = json.loads((output / 'summary.json').read_text())
    original_summary = json.loads(json.dumps(summary))
    tests = json.loads(catalog.read_text())['tests']
    if not set(nodes) <= {test['id'] for test in tests}:
        raise ValueError('Rechecks must use catalog test IDs')
    failures = {}
    rechecks = {}
    for name in ['reference', *(item['name'] for item in plan)]:
        directory = output / name
        recheck_dir = directory / 'environment-recheck'
        if recheck_dir.exists():
            raise ValueError(f'Refusing to replace previous recheck evidence: {recheck_dir}')
        recheck_dir.mkdir()
        report = run_suite(directory / 'source', catalog, recheck_dir, timeout, nodes=nodes)
        if not report['complete'] or set(report['outcomes']) != set(nodes):
            raise RuntimeError(f'Incomplete targeted recheck: {name}')
        previous = json.loads((directory / 'pytest.json').read_text())
        outcomes = dict(previous['outcomes'])
        outcomes.update(report['outcomes'])
        reconciled = json.loads(json.dumps(previous))
        reconciled['outcomes'] = outcomes
        reconciled['environment_recheck'] = {'original_report': str(directory / 'pytest.json'), 'targeted_report': str(recheck_dir / 'pytest.json'), 'reason': reason}
        for node in nodes:
            if report['outcomes'][node] != 'failed':
                reconciled.get('details', {}).pop(node, None)
        (directory / 'pytest-rechecked.json').write_text(json.dumps(reconciled, indent=2, sort_keys=True))
        failures[name] = {node for node, status in outcomes.items() if status == 'failed'}
        rechecks[name] = {'report': str(recheck_dir / 'pytest.json'), 'outcomes': report['outcomes'], 'wall_seconds': report['wall_seconds']}
    baseline = failures['reference']
    summary['baseline_failed'] = sorted(baseline)
    by_name = {item['name']: item for item in plan}
    for item in summary['mutations']:
        failing = failures[item['name']]
        item['all_failures'] = sorted(failing)
        item['new_failures'] = sorted(failing - baseline)
        for kind, selection in item['selections'].items():
            numbers = set(by_name[item['name']]['selections'][kind]['selected_numbers'])
            selected = {test['id'] for test in tests if test['number'] in numbers}
            selection['missed_all'] = sorted(failing - selected)
            selection['missed_new'] = sorted(failing - baseline - selected)
    summary['environment_rechecks'] = {'reason': reason, 'tests': nodes, 'cases': rechecks,
                                       'original_baseline_failed': original_summary['baseline_failed'],
                                       'original_summary': str(output / 'summary.json'),
                                       'verification': 'Full-suite results plus targeted environment rechecks; original reports retained.'}
    target = output / 'summary-rechecked.json'
    target.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2), flush=True)


def main():
    sys.path.insert(0, str(ROOT))
    from selector import read_json
    from patches import parse_patch, apply_file
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--map', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--patch', action='append', default=[], type=Path)
    parser.add_argument('--sample-patches', type=Path, default=ROOT / 'sample/commits')
    parser.add_argument('--seed', type=int, default=2026)
    parser.add_argument('--selection', choices=['production', 'class_guard', 'both'], default='both')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--reference-report', type=Path, help='Reuse an explicit complete full-catalog baseline report; no tests are inferred from mutation labels')
    parser.add_argument('--recheck-test', action='append', default=[])
    parser.add_argument('--recheck-reason', default='Explicit targeted rerun in corrected runner environment')
    args = parser.parse_args()
    source, catalog, output = args.source.resolve(), args.catalog.resolve(), args.output.resolve()
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != REVISION or subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True):
        raise SystemExit('Source must be the clean pinned revision')
    output.mkdir(parents=True, exist_ok=True)
    if args.recheck_test:
        recheck_existing(output, catalog, args.recheck_test, args.timeout, args.recheck_reason)
        return
    tests = read_json(catalog)['tests']
    mapping = read_json(args.map)
    plan_path = output / 'plan.json'
    if args.patch:
        plan = [{'name': patch.stem, 'patch': str(patch.resolve())} for patch in args.patch]
    elif plan_path.exists():
        plan = json.loads(plan_path.read_text())['mutations']
    else:
        plan = generate(source, args.sample_patches, output, args.seed)
    for item in plan:
        item['selections'] = selections(source, tests, mapping, Path(item['patch']).read_text(), args.selection)
    plan_path.write_text(json.dumps({'revision': REVISION, 'seed': args.seed, 'mutations': plan}, indent=2, sort_keys=True))
    print(json.dumps([dict({key: value for key, value in item.items() if key != 'selections'}, selections={kind: {'count': len(selection['selected_numbers']), 'guard_calls': selection.get('guard_calls')} for kind, selection in item['selections'].items()}) for item in plan], indent=2), flush=True)
    if args.plan_only:
        return
    # Sequential execution avoids overlapping million-node parser stress tests.
    if args.reference_report:
        reference = json.loads(args.reference_report.read_text())
        if not reference.get('complete') or set(reference.get('finished', [])) != {test['id'] for test in tests}:
            raise ValueError('Reused reference must be complete for the entire catalog')
        (output / 'reference-report.json').write_text(json.dumps(reference, indent=2, sort_keys=True))
    else:
        reference_dir = output / 'reference'
        isolated_tree(source, reference_dir / 'source')
        reference = run_suite(reference_dir / 'source', catalog, reference_dir, args.timeout)
    baseline_failed = {node for node, outcome in reference['outcomes'].items() if outcome == 'failed'}
    summary = {'revision': REVISION, 'reused_reference_report': str(args.reference_report.resolve()) if args.reference_report else None, 'reference_complete': reference['complete'], 'baseline_failed': sorted(baseline_failed), 'mutations': []}
    print(f'Reference: complete={reference["complete"]}, failures={len(baseline_failed)}, seconds={reference["wall_seconds"]}', flush=True)
    for item in plan:
        directory = output / item['name']
        tree = directory / 'source'
        isolated_tree(source, tree)
        for change in parse_patch(Path(item['patch']).read_text()):
            target = (tree / change['path']).resolve()
            if not target.is_relative_to(tree) or not target.is_file():
                raise ValueError('Holdout patches must modify existing files inside isolated tree')
            updated, _ = apply_file(target.read_text(), change)
            target.write_text(updated)
        actual = run_suite(tree, catalog, directory, args.timeout)
        failing = {node for node, outcome in actual['outcomes'].items() if outcome == 'failed'}
        result = {'name': item['name'], 'complete': actual['complete'] and reference['complete'], 'all_failures': sorted(failing), 'new_failures': sorted(failing - baseline_failed), 'wall_seconds': actual['wall_seconds'], 'selections': {}}
        for kind, selected in item['selections'].items():
            selected_nodes = {test['id'] for test in tests if test['number'] in selected['selected_numbers']}
            result['selections'][kind] = {'selected_count': len(selected_nodes), 'missed_all': sorted(failing - selected_nodes), 'missed_new': sorted(failing - baseline_failed - selected_nodes), 'guard_calls': selected.get('guard_calls')}
        summary['mutations'].append(result)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True))
        print(json.dumps(result), flush=True)
    if subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True):
        raise RuntimeError('Original pinned source changed during validation')

if __name__ == '__main__':
    main()
