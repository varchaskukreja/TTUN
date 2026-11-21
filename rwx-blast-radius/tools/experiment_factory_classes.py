#!/usr/bin/env python3
"""Opt-in baseline factory-proof experiment; never a production entrypoint.

Proofs only establish closure construction. ImportGuard and empirical holdout
validation remain necessary; class-consumer completeness is not proven here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from import_guard import ImportGuard
from scheduler import split
from selector import Selector, read_json

from factory_selection import FactoryCandidates


class FactorySelector(Selector):
    def __init__(self, tests, mapping, source, proofs, class_guard):
        super().__init__(tests, mapping, source, class_guard=class_guard,
                         class_candidate=FactoryCandidates(source, proofs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--map', type=Path, required=True)
    parser.add_argument('--proofs', type=Path, required=True)
    args = parser.parse_args()
    tests = read_json(args.data / 'tests.json')['tests']
    guard = ImportGuard(args.source, tests)
    engine = FactorySelector(tests, read_json(args.map), args.source, read_json(args.proofs), guard)
    for path in sorted((args.data / 'commits').glob('*.patch')):
        try:
            selected = engine.select(path.read_text())
        except Exception as exc:
            print(f'{path.stem}: fallback: {exc}', file=sys.stderr)
            selected = engine.all
        print(json.dumps({'commit': path.stem, 'workers': split(test for test in tests if test['number'] in selected)}))
    own = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'guards': guard.calls, 'cpu_seconds': own.ru_utime + own.ru_stime + child.ru_utime + child.ru_stime}), file=sys.stderr)


if __name__ == '__main__':
    main()
