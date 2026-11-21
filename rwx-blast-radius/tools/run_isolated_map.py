#!/usr/bin/env python3
"""Generate a baseline-only cold-test coverage supplement; pilot by default.

This changes no production map or existing tracing pipeline. Run in the Linux
coverage-tools image. A full catalog run requires --all explicitly. Every child
starts from the parent's collection-only state; collection itself is shared.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from build_map import REVISION, export


PILOT_FILES = (
    'tests/test_parser.py', 'tests/test_tokens.py', 'tests/test_expressions.py',
    'tests/test_serde.py', 'tests/test_optimizer.py', 'tests/test_executor.py',
    'tests/dialects/test_snowflake.py', 'tests/dialects/test_tsql.py',
    'tests/dialects/test_bigquery.py', 'tests/dialects/test_dremio.py',
)


def pilot(tests):
    chosen = []
    for filename in PILOT_FILES:
        matches = [test for test in tests if test['file'] == filename]
        if matches:
            # Include one cheap process-pool fixture test to verify context
            # propagation into spawned grandchildren as well as fork children.
            pooled = [test for test in matches if test['id'].endswith('::test_eliminate_subqueries')]
            chosen.append((pooled[0] if pooled else min(matches, key=lambda test: (test['seconds'], test['id'])))['id'])
    if len(chosen) != 10:
        raise ValueError(f'expected ten diverse pilot files, found {len(chosen)}')
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=2)
    parser.add_argument('--timeout', type=int, default=300, help='hard wall-time bound per test including setup')
    parser.add_argument('--all', action='store_true', help='explicitly run the entire catalog rather than ten-test pilot')
    parser.add_argument('--select-test', action='append', default=[])
    parser.add_argument('--classes', action='store_true', help='also collect fresh per-test class consumer contexts')
    args = parser.parse_args()
    source, work, catalog, output = [path.resolve() for path in (args.source, args.work, args.catalog, args.output)]
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != REVISION:
        raise SystemExit('isolated mapping requires the pinned baseline revision')
    if subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True):
        raise SystemExit('isolated mapping refuses modified baseline files')
    if work.exists() and any(work.iterdir()):
        raise SystemExit('use a fresh empty --work directory to avoid stale child artifacts')
    work.mkdir(parents=True, exist_ok=True)
    tests = json.loads(catalog.read_text())['tests']
    selected = args.select_test or ([test['id'] for test in tests] if args.all else pilot(tests))
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= {test['id'] for test in tests}:
        raise SystemExit('selection must contain distinct catalog node IDs')
    (work / 'selected.json').write_text(json.dumps(selected, indent=2))
    env = dict(os.environ, BLAST_SOURCE=str(source), BLAST_WORK=str(work),
               BLAST_CATALOG=str(catalog), BLAST_SELECT=json.dumps(selected),
               BLAST_ISOLATED_JOBS=str(args.jobs), BLAST_ISOLATED_TIMEOUT=str(args.timeout),
               PYTHONPATH=str(Path(__file__).parent.resolve()) + os.pathsep + str(source),
               PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
    env.pop('BLAST_RETRY', None)
    env.pop('BLAST_CONTEXT', None)
    env.pop('BLAST_CLASSES', None)
    env.pop('PYTEST_ADDOPTS', None)
    if args.classes:
        env['BLAST_CLASSES'] = '1'
    result = subprocess.run([
        sys.executable, '-m', 'pytest', 'tests', '-p', 'coverage_plugin', '-p', 'isolated_plugin',
        '-p', 'no:cacheprovider', '-s', '-q', '--tb=short',
    ], cwd=source, env=env)
    if result.returncode not in (0, 1):
        raise SystemExit(result.returncode)
    manifest = json.loads((work / 'isolated-manifest.json').read_text())
    actual = {row['nodeid'] for row in manifest['completed']}
    if actual != set(selected):
        raise SystemExit(f'isolated run incomplete: {len(actual)} of {len(selected)} results')
    export(source, work, catalog, output)
    print(f'Isolated map generated for {len(selected)} tests; union with the complete baseline map before use.')


if __name__ == '__main__':
    main()
