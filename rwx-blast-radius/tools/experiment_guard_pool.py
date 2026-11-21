#!/usr/bin/env python3
"""ImportGuard prefork performance experiment; never imported by production.

Every child is discarded. Parent never imports target modules or runs pytest.
Native thread detection is a necessary precaution, not proof of fork safety.
"""
import argparse
import difflib
import importlib
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
from import_guard import MAX_CAPTURE_BYTES, _reject_constant, _unique_object, _valid_report


def clean_parent():
    polluted = [n for n in sys.modules if n == 'sqlglot' or n.startswith('sqlglot.')
                or n == 'tests' or n.startswith('tests.')]
    if polluted:
        raise RuntimeError(f'parent imported target modules: {polluted}')
    threads = len(list(Path('/proc/self/task').iterdir()))
    if threads != 1:
        raise RuntimeError(f'parent has {threads} native threads; refusing fork')
    return threads


def read_report(output, log):
    """Reject oversized, truncated, or ambiguous child output without aborting later probes."""
    try:
        size = sum(p.stat().st_size for p in (output, log) if p.exists())
        if size > MAX_CAPTURE_BYTES or not output.exists():
            return {}, size
        report = json.loads(output.read_text(), parse_constant=_reject_constant,
                            object_pairs_hook=_unique_object)
        return report, size
    except (OSError, UnicodeError, ValueError):
        return {}, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--mode', choices=('cold', 'fork'), required=True)
    parser.add_argument('--preload', default='pytest,numpy,pandas,duckdb')
    parser.add_argument('--repeat', type=int, default=4)
    parser.add_argument('--patch', type=Path, action='append', default=[])
    parser.add_argument('--adversarial', action='store_true')
    parser.add_argument('--timeout', type=float, default=8)
    args = parser.parse_args()
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[name] = '1'
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ.pop('PYTEST_ADDOPTS', None)
    sys.dont_write_bytecode = True
    catalog = json.loads(args.catalog.read_text())
    nodes = [x['id'] for x in catalog] if isinstance(catalog, list) else [x['id'] for x in catalog['tests']]
    if not nodes or len(set(nodes)) != len(nodes):
        raise ValueError('invalid catalog')
    wall_start = time.monotonic()
    initial_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    loaded = []
    if args.mode == 'fork':
        for module in args.preload.split(','):
            if module:
                importlib.import_module(module)
                loaded.append(module)
        import runtime_probe
        clean_parent()
    preload_cpu = time.process_time()
    results = []
    with tempfile.TemporaryDirectory(prefix='guard-pool-experiment-') as directory:
        directory = Path(directory)
        # A comment-only baseline change exercises the actual overlay/import path.
        target = args.source / 'sqlglot/dialects/snowflake.py'
        original = target.read_text()
        patch = ''.join(difflib.unified_diff(original.splitlines(True),
                    ('# Baseline guard experiment.\n' + original).splitlines(True),
                    fromfile='a/sqlglot/dialects/snowflake.py', tofile='b/sqlglot/dialects/snowflake.py'))
        patch_path = directory / 'baseline.patch'
        patch_path.write_text('diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n' + patch)
        nodes_path = directory / 'nodes.json'
        nodes_path.write_text(json.dumps(nodes))
        cases = [(f'baseline-{i}', patch_path) for i in range(args.repeat)]
        if args.patch:
            cases = [(p.stem, p.resolve()) for p in args.patch]
        if args.adversarial:
            cases = []
            payloads = [('import_failure', "raise RuntimeError('intentional import failure')"),
                        ('clean_after_failure', "assert not hasattr(__import__('sqlglot'), '_guard_mutation')"),
                        ('module_mutation', "__import__('sqlglot')._guard_mutation = True"),
                        ('clean_after_mutation', "assert not hasattr(__import__('sqlglot'), '_guard_mutation')"),
                        ('timeout', "__import__('time').sleep(60)"),
                        ('clean_after_timeout', "assert not hasattr(__import__('sqlglot'), '_guard_mutation')"),
                        ('stdout_limit', "print('x' * (3 * 1024 * 1024))"),
                        ('clean_after_output', "assert not hasattr(__import__('sqlglot'), '_guard_mutation')")]
            for name, payload in payloads:
                case_path = directory / f'{name}.patch'
                diff = ''.join(difflib.unified_diff(original.splitlines(True),
                    (original + '\n' + payload + '\n').splitlines(True),
                    fromfile='a/sqlglot/dialects/snowflake.py', tofile='b/sqlglot/dialects/snowflake.py'))
                case_path.write_text('diff --git a/sqlglot/dialects/snowflake.py b/sqlglot/dialects/snowflake.py\n' + diff)
                cases.append((name, case_path))
        for index, (case_name, patch_path) in enumerate(cases):
            output = directory / f'{index}.json'
            log = directory / f'{index}.log'
            start = time.monotonic()
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            timed_out = False
            if args.mode == 'cold':
                with output.open('w') as stdout, log.open('w') as stderr:
                    proc = subprocess.Popen([sys.executable, str(ROOT / 'runtime_probe.py'),
                        '--source', str(args.source), '--patch', str(patch_path),
                        '--nodes', str(nodes_path), '--collect-only', '--import-patches'],
                        stdout=stdout, stderr=stderr, start_new_session=True,
                        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES)))
                    try:
                        code = proc.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        os.killpg(proc.pid, signal.SIGKILL)
                        code = proc.wait()
            else:
                clean_parent()
                sys.stdout.flush()
                sys.stderr.flush()
                pid = os.fork()
                if pid == 0:
                    try:
                        os.setsid()
                        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES))
                        with log.open('w') as stream:
                            os.dup2(stream.fileno(), 1)
                            os.dup2(stream.fileno(), 2)
                            with runtime_probe.diagnostics_to_stderr():
                                report = runtime_probe.run(args.source, patch_path, nodes,
                                                           collect_only=True, import_patches=True)
                        output.write_text(json.dumps(report))
                        os._exit(0)
                    except BaseException:
                        os._exit(70)
                while True:
                    reaped, status = os.waitpid(pid, os.WNOHANG)
                    if reaped:
                        code = os.waitstatus_to_exitcode(status)
                        break
                    if time.monotonic() - start > args.timeout:
                        timed_out = True
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            os.kill(pid, signal.SIGKILL)
                        _, status = os.waitpid(pid, 0)
                        code = os.waitstatus_to_exitcode(status)
                        break
                    time.sleep(.01)
                clean_parent()
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            report, size = read_report(output, log)
            results.append({'case': case_name, 'output_bytes': size, 'valid': code == 0 and not timed_out and size is not None and size <= MAX_CAPTURE_BYTES
                            and _valid_report(report, len(nodes)), 'exitcode': code,
                            'timeout': timed_out, 'cpu_seconds': after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
                            'wall_seconds': time.monotonic() - start, 'report': report})
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'mode': args.mode, 'preloaded': loaded, 'parent_cpu_seconds': time.process_time(),
        'preload_cpu_seconds': preload_cpu, 'total_cpu_seconds': time.process_time() + children.ru_utime + children.ru_stime - initial_children.ru_utime - initial_children.ru_stime,
        'wall_seconds': time.monotonic() - wall_start, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
