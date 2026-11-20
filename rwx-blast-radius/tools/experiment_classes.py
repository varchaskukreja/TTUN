#!/usr/bin/env python3
"""Opt-in class dependency experiment; never used by the submission entrypoint."""
import argparse
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selector import Selector, read_json
from scheduler import split


class ImportGuard:
    def __init__(self, source, tests, budget=120):
        self.source = source
        self.nodes = json.dumps([test['id'] for test in tests])
        self.deadline = time.monotonic() + budget
        self.calls = []

    def __call__(self, patch):
        remaining = self.deadline - time.monotonic()
        if remaining < 1:
            return False
        with tempfile.TemporaryDirectory(prefix='blast-import-') as directory:
            path = Path(directory) / 'change.patch'
            path.write_text(patch)
            try:
                process = subprocess.run([
                    sys.executable, str(ROOT / 'runtime_probe.py'), '--source', str(self.source),
                    '--patch', str(path), '--nodes', '-', '--collect-only', '--import-patches',
                ], input=self.nodes, text=True, capture_output=True, timeout=min(8, remaining))
                report = json.loads(process.stdout)
                valid = (process.returncode == 0 and report.get('complete') is True
                         and report.get('finished') == 0
                         and report.get('collected') == len(json.loads(self.nodes))
                         and not report.get('python_patches_not_loaded', ['unknown']))
                self.calls.append({'valid': valid, 'cpu_seconds': report.get('cpu_seconds'),
                                   'reasons': report.get('reasons', [])})
                return valid
            except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
                self.calls.append({'valid': False, 'reason': type(exc).__name__})
                return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    args = parser.parse_args()
    tests = read_json(args.data / 'tests.json')['tests']
    guard = ImportGuard(args.source.resolve(), tests)
    selector = Selector(tests, read_json(args.map), args.source, class_guard=guard)
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        try:
            selected = selector.select(patch.read_text())
        except Exception as exc:
            print(f'{patch.stem}: fallback: {exc}', file=sys.stderr)
            selected = selector.all
        print(json.dumps({'commit': patch.stem,
                          'workers': split(test for test in tests if test['number'] in selected)}))
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'guards': guard.calls, 'cpu_seconds': own.ru_utime + own.ru_stime
                     + child.ru_utime + child.ru_stime}), file=sys.stderr)


if __name__ == '__main__':
    main()
