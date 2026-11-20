#!/usr/bin/env python3
"""Generate trace maps exclusively from unmodified pinned sqlglot tests.

Run with the environment that has coverage, pytest and sqlglot test dependencies.
"""
import argparse
import ast
import gzip
import json
import os
import platform
from pathlib import Path
import subprocess
import sys

REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'


def export(source, work, catalog, output):
    from coverage import CoverageData
    tests = json.loads(catalog.read_text())['tests']
    ids = [t['id'] for t in tests]
    indices = {name: i for i, name in enumerate(ids)}
    data = CoverageData(basename=str(work / 'baseline.coverage'))
    data.read()
    files = {}
    for filename in sorted(data.measured_files()):
        try:
            rel = str(Path(filename).relative_to(source))
        except ValueError:
            continue
        contexts = data.contexts_by_lineno(filename)
        lines = {str(line): sorted({indices[c] for c in cs if c in indices}) for line, cs in contexts.items()}
        union = sorted({i for li in lines.values() for i in li})
        entry = {'lines': lines, 'tests': union, 'collection': any('' in cs for cs in contexts.values()), 'functions': [], 'classes': []}
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
    for rel, contexts in json.loads((work / 'reads.json').read_text()).items():
        entry = files.setdefault(rel, {'lines': {}, 'tests': [], 'collection': False, 'functions': [], 'classes': []})
        entry['read_tests'] = sorted({indices[c] for c in contexts if c in indices})
        entry['collection_read'] = '' in contexts
        entry['tests'] = sorted(set(entry['tests']) | set(entry['read_tests']))
    results = json.loads((work / 'results.json').read_text())
    missing = json.loads((work / 'missing_tests.json').read_text())
    payload = {'revision': REVISION, 'test_ids': ids, 'files': files,
               'missing_tests': missing, 'baseline_outcomes': results,
               'environment': {'python': sys.version, 'platform': platform.platform()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    if output.suffix == '.gz':
        output.write_bytes(gzip.compress(raw, mtime=0))
    else:
        output.write_bytes(raw)
    print(f'{len(files)} mapped files, {len(ids)} tests, {len(missing)} missing, {len(raw)} JSON bytes -> {output.stat().st_size} bytes')
    sources = {}
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files'], text=True).splitlines()
    for rel in tracked:
        if rel.endswith('.py'):
            sources[rel] = (source / rel).read_text()
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
    args = p.parse_args()
    source, work, catalog, output = [x.resolve() for x in (args.source, args.work, args.catalog, args.output)]
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVISION or subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only'], text=True):
        raise SystemExit('Refusing to map anything except the clean pinned baseline')
    work.mkdir(parents=True, exist_ok=True)
    if not args.export_only:
        env = dict(os.environ, BLAST_SOURCE=str(source), BLAST_WORK=str(work), BLAST_CATALOG=str(catalog), PYTHONPATH=str(Path(__file__).parent.resolve()) + os.pathsep + str(source))
        result = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-p', 'coverage_plugin', '-q', '--timeout=30'], cwd=source, env=env)
        if result.returncode not in (0, 1):
            raise SystemExit(result.returncode)
    export(source, work, catalog, output)

if __name__ == '__main__':
    main()
