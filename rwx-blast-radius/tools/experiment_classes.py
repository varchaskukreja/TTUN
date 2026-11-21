#!/usr/bin/env python3
"""Opt-in class dependency experiment; never used by the submission entrypoint."""
import argparse
import json
from pathlib import Path
import resource
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from selector import Selector, read_json
from scheduler import split
from import_guard import ImportGuard


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
