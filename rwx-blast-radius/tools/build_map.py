#!/usr/bin/env python3
"""Generate trace maps exclusively from unmodified pinned sqlglot tests.

Run with the environment that has coverage, pytest and sqlglot test dependencies.
"""
import argparse
import ast
import gzip
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import subprocess
import sys

REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'


def export(source, work, catalog, output):
    from coverage import Coverage, CoverageData
    combined = Coverage(data_file=str(work / 'baseline.coverage'))
    combined.load()
    combined.combine(data_paths=[str(work)], strict=False, keep=True)
    combined.save()
    tracked = set(subprocess.check_output(['git', '-C', str(source), 'ls-files'], text=True).splitlines())
    tests = json.loads(catalog.read_text())['tests']
    ids = [t['id'] for t in tests]
    indices = {name: i for i, name in enumerate(ids)}
    context_cache = {}
    def context_indices(contexts):
        result = set()
        for context in contexts:
            if context not in context_cache:
                if context in indices:
                    context_cache[context] = (indices[context],)
                elif context.startswith('@scope|'):
                    _, scope, node = context.split('|', 2)
                    prefix = node.rstrip('/') + ('/' if scope == 'package' else '::')
                    context_cache[context] = tuple(i for name, i in indices.items() if scope == 'session' or name.startswith(prefix))
                else:
                    context_cache[context] = ()
            result.update(context_cache[context])
        return sorted(result)
    data = CoverageData(basename=str(work / 'baseline.coverage'))
    data.read()
    files = {}
    for filename in sorted(data.measured_files()):
        try:
            rel = str(Path(filename).relative_to(source))
        except ValueError:
            continue
        contexts = data.contexts_by_lineno(filename)
        lines = {str(line): context_indices(cs) for line, cs in contexts.items()}
        union = sorted({i for li in lines.values() for i in li})
        entry = {'lines': lines, 'tests': union, 'collection': any('' in cs for cs in contexts.values()), 'collection_lines': sorted(line for line, cs in contexts.items() if '' in cs), 'functions': [], 'classes': []}
        try:
            tree = ast.parse(Path(filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    end = node.end_lineno
                    touched = sorted({i for line in range(start, end + 1) for i in lines.get(str(line), [])})
                    record = {'name': node.name, 'start': start, 'end': end, 'tests': touched}
                    entry['classes' if isinstance(node, ast.ClassDef) else 'functions'].append(record)
        except (SyntaxError, UnicodeError):
            pass
        files[rel] = entry
    reads = json.loads((work / 'reads.json').read_text())
    for child in work.glob('child-reads-*.json'):
        for rel, contexts in json.loads(child.read_text()).items():
            reads.setdefault(rel, []).extend(contexts)
    for rel, contexts in reads.items():
        if rel not in tracked:
            continue
        entry = files.setdefault(rel, {'lines': {}, 'tests': [], 'collection': False, 'functions': [], 'classes': []})
        entry['read_tests'] = context_indices(contexts)
        entry['collection_read'] = '' in contexts
        entry['tests'] = sorted(set(entry['tests']) | set(entry['read_tests']))
    class_files = list(work.glob('child-classes-*.json'))
    if (work / 'classes.json').exists():
        class_files.append(work / 'classes.json')
    class_contexts = {}
    for path in class_files:
        for rel, classes in json.loads(path.read_text()).items():
            for name, contexts in classes.items():
                class_contexts.setdefault(rel, {}).setdefault(name, set()).update(contexts)
    for rel, classes in class_contexts.items():
        if rel in files:
            files[rel]['class_consumers'] = {name: context_indices(cs) for name, cs in classes.items()}
            files[rel]['class_collection'] = sorted(name for name, cs in classes.items() if '' in cs)
    results = json.loads((work / 'results.json').read_text())
    missing = sorted(set(json.loads((work / 'missing_tests.json').read_text())) | (set(ids) - set(results)))
    payload = {'revision': REVISION, 'test_ids': ids, 'files': files,
               'missing_tests': missing, 'baseline_outcomes': results,
               'environment': {'python': sys.version, 'platform': platform.platform(), 'machine': platform.machine(), 'packages': {name: importlib.metadata.version(name) for name in ('coverage', 'pytest', 'duckdb', 'numpy', 'pandas')}}}
    if (work / 'call_outcomes.json').exists():
        payload['call_outcomes'] = json.loads((work / 'call_outcomes.json').read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    encoded = gzip.compress(raw, mtime=0) if output.suffix == '.gz' else raw
    temporary = output.with_name(output.name + '.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(output)
    print(f'{len(files)} mapped files, {len(ids)} tests, {len(missing)} missing, {len(raw)} JSON bytes -> {output.stat().st_size} bytes')
    sources = {}
    for rel in sorted(tracked):
        if rel.endswith('.py'):
            sources[rel] = (source / rel).read_text()
    output.with_name('SQLGLOT_LICENSE').write_text((source / 'LICENSE').read_text())
    source_output = output.with_name('baseline_sources.json.gz')
    source_output.write_bytes(gzip.compress(json.dumps(sources, separators=(',', ':'), sort_keys=True).encode(), mtime=0))
    print(f'Baseline sources: {source_output.stat().st_size} bytes')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--catalog', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--export-only', action='store_true')
    p.add_argument('--scope-supplement', action='store_true', help='Run cheapest catalog test per class to capture shared fixture setup')
    p.add_argument('--timeout', type=int, default=300)
    p.add_argument('--select-test', action='append', default=[], help='Select a node ID while retaining the full catalog in the exported map')
    p.add_argument('--retry-test', action='append', default=[], help='Rerun this node ID and merge with existing work data; may be repeated')
    args = p.parse_args()
    source, work, catalog, output = [x.resolve() for x in (args.source, args.work, args.catalog, args.output)]
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVISION or subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True):
        raise SystemExit('Refusing to map anything except the clean pinned baseline')
    work.mkdir(parents=True, exist_ok=True)
    if not args.export_only:
        env = dict(os.environ, BLAST_SOURCE=str(source), BLAST_WORK=str(work), BLAST_CATALOG=str(catalog), PYTHONPATH=str(Path(__file__).parent.resolve()) + os.pathsep + str(source))
        if args.scope_supplement:
            cheapest = {}
            for test in json.loads(catalog.read_text())['tests']:
                group = test['id'].rsplit('::', 1)[0]
                if group not in cheapest or test['seconds'] < cheapest[group]['seconds']:
                    cheapest[group] = test
            env['BLAST_SELECT'] = json.dumps([test['id'] for test in cheapest.values()])
        if args.select_test:
            env['BLAST_SELECT'] = json.dumps(args.select_test)
        if args.retry_test:
            env['BLAST_RETRY'] = json.dumps(args.retry_test)
        result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-p', 'coverage_plugin', '-q', f'--timeout={args.timeout}'], cwd=source, env=env)
        if result.returncode not in (0, 1):
            raise SystemExit(result.returncode)
    export(source, work, catalog, output)

if __name__ == '__main__':
    main()
