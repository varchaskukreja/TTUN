#!/usr/bin/env python3
"""Analysis-only cold-test timings; never remove tests or emit submissions.

The optional child-local reporter distinguishes explicit passing phases from
skips. Even an explicit cold pass does not prove a pass in a shared-state suite.
No sample patches, labels, or patch-specific rules are used by this benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def explicit_cold_pass(report, nodes):
    if not nodes:
        return False
    if not isinstance(report, dict) or report.get('complete') is not True or report.get('collect_only') is not False:
        return False
    for key in ('requested', 'collected', 'finished'):
        if type(report.get(key)) is not int or report[key] != len(nodes):
            return False
    if type(report.get('pytest_exitcode')) is not int or report['pytest_exitcode'] != 0:
        return False
    for key in ('failing', 'collection_failed', 'collection_errors', 'patch_import_errors', 'python_patches_not_loaded', 'reasons'):
        if report.get(key) != [] or not isinstance(report.get(key), list):
            return False
    phases = report.get('phase_outcomes')
    if not isinstance(phases, dict) or set(phases) != set(nodes):
        return False
    for node in nodes:
        outcomes = phases[node]
        if not isinstance(outcomes, list) or not outcomes:
            return False
        if any(not isinstance(outcome, dict) or outcome.get('outcome') != 'passed'
               or outcome.get('wasxfail') is not False for outcome in outcomes):
            return False
        if {'setup', 'call', 'teardown'} != {outcome.get('when') for outcome in outcomes}:
            return False
    return True


def baseline_noop_patch(source):
    """An exact-text replacement exercises the overlay without changing source."""
    path = 'sqlglot/__init__.py'
    first = (source / path).read_text().splitlines(keepends=True)[0]
    if not first.endswith('\n'):
        raise ValueError('baseline first line must have newline')
    return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{first}+{first}'


def worker(source, patch_path, plain=False):
    import runtime_probe
    nodes = json.load(sys.stdin)
    reports = []
    original_reports = runtime_probe.ProbeReports

    class PhaseReports(original_reports):
        def __init__(self, requested):
            super().__init__(requested)
            self.phase_outcomes = {}
            reports.append(self)

        def pytest_runtest_logreport(self, report):
            super().pytest_runtest_logreport(report)
            if report.nodeid in self.nodes:
                self.phase_outcomes.setdefault(report.nodeid, []).append({
                    'when': report.when, 'outcome': report.outcome,
                    'wasxfail': hasattr(report, 'wasxfail'),
                })

    runtime_probe.ProbeReports = PhaseReports
    try:
        with runtime_probe.diagnostics_to_stderr():
            if plain:
                os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
                os.environ.pop('PYTEST_ADDOPTS', None)
                sys.dont_write_bytecode = True
                sys.path.insert(0, str(source))
                os.chdir(source)
                import pytest
                observed = PhaseReports(nodes)
                with tempfile.TemporaryDirectory(prefix='plain-baseline-') as temporary:
                    exitcode = pytest.main(['-c', '/dev/null', '--rootdir', str(source), '--confcutdir', str(source),
                                            '--basetemp', temporary, '-p', 'no:cacheprovider', '--assert=plain',
                                            '-q', '--tb=short', '-rN', *nodes], plugins=[observed])
                complete = observed.finished == set(nodes) and not observed.interrupted and not observed.collection_errors
                result = {'complete': complete, 'collect_only': False, 'requested': len(nodes),
                          'collected': len(observed.collected), 'finished': len(observed.finished),
                          'pytest_exitcode': int(exitcode), 'failing': sorted(observed.failed),
                          'collection_failed': sorted(observed.collection_failed),
                          'collection_errors': sorted(observed.collection_errors), 'patch_import_errors': [],
                          'python_patches_not_loaded': [], 'python_patches_loaded': [], 'reasons': [] if complete else ['incomplete baseline execution']}
            else:
                result = runtime_probe.run(source, patch_path, nodes)
        result['phase_outcomes'] = reports[0].phase_outcomes if reports else {}
    except Exception as exc:
        result = {'complete': False, 'phase_outcomes': {}, 'reasons': [f'{type(exc).__name__}: {exc}']}
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result['own_cpu_seconds'] = own.ru_utime + own.ru_stime
    result['child_cpu_seconds'] = children.ru_utime + children.ru_stime
    result['cpu_seconds'] = result['own_cpu_seconds'] + result['child_cpu_seconds']
    result['environment'] = {'cpu_count': os.cpu_count(), 'process_cpu_count': os.process_cpu_count(),
                             'affinity': sorted(os.sched_getaffinity(0))}
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--top', type=int, default=3)
    parser.add_argument('--rank-offset', type=int, default=0)
    parser.add_argument('--plain-baseline', action='store_true', help='time ordinary baseline pytest with subprocess support, no patch overlay')
    parser.add_argument('--affinity-cpus', type=int, help='optional local experiment: restrict affinity before launching probes')
    parser.add_argument('--minimum-test-seconds', type=float, default=5)
    parser.add_argument('--budget', type=float, default=90)
    parser.add_argument('--probe-timeout', type=float, default=35)
    parser.add_argument('--worker-patch', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.affinity_cpus is not None:
        if args.affinity_cpus < 1:
            parser.error('--affinity-cpus must be positive')
        os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:args.affinity_cpus])
    if args.worker_patch:
        worker(args.source, args.worker_patch, plain=args.plain_baseline)
        return
    if args.catalog is None:
        parser.error('--catalog is required for timings')
    tests = json.loads(args.catalog.read_text())['tests']
    chosen = sorted((test for test in tests if test['seconds'] >= args.minimum_test_seconds),
                    key=lambda test: (-test['seconds'], test['id']))[args.rank_offset:args.rank_offset + args.top]
    rows, spent = [], 0.0
    with tempfile.TemporaryDirectory(prefix='refinement-timing-') as directory:
        patch_path = Path(directory) / 'baseline.patch'
        patch_path.write_text(baseline_noop_patch(args.source))
        for test in chosen:
            remaining = args.budget - spent
            if remaining < 1:
                break
            start = time.monotonic()
            row = {'id': test['id'], 'catalog_seconds': test['seconds']}
            try:
                command = [sys.executable, str(Path(__file__).resolve()), '--source', str(args.source),
                           '--worker-patch', str(patch_path), *(['--plain-baseline'] if args.plain_baseline else [])]
                child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    stdout, stderr = child.communicate(json.dumps([test['id']]), timeout=min(args.probe_timeout, remaining))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.communicate()
                    raise
                process = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
                if len(process.stdout.encode()) + len(process.stderr.encode()) > 2 * 1024 * 1024:
                    raise ValueError('probe output exceeded 2 MiB')
                report = json.loads(process.stdout)
                row.update({'returncode': process.returncode, 'report': report,
                            'explicit_cold_pass': process.returncode == 0 and explicit_cold_pass(report, [test['id']]),
                            'diagnostics_tail': process.stderr[-1500:]})
            except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
                row.update({'explicit_cold_pass': False, 'inconclusive': type(exc).__name__})
            elapsed = time.monotonic() - start
            spent += elapsed
            row['wall_seconds'] = round(elapsed, 6)
            rows.append(row)
            print(json.dumps(row), file=sys.stderr, flush=True)
    print(json.dumps({'purpose': 'baseline cold execution timing only', 'plain_baseline': args.plain_baseline, 'tests_removed': 0,
                      'environment': {'cpu_count': os.cpu_count(), 'process_cpu_count': os.process_cpu_count(),
                                      'affinity': sorted(os.sched_getaffinity(0))},
                      'warning': 'An explicit cold pass does not prove an official shared-state-suite pass.',
                      'cumulative_probe_wall_seconds': spent, 'results': rows}, indent=2))


if __name__ == '__main__':
    main()
