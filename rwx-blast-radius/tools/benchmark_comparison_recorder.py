#!/usr/bin/env python3
"""Standalone observer experiment. Never imported by capture or selection.

Caches only JSON strings keyed by freshly recomputed primitive descriptions.
Type objects, slots, operands, results, and code objects are never cached here.
This optimization does not establish instrumentation transparency.
"""
from __future__ import annotations
import argparse
import enum
import gc
import json
from pathlib import Path
import statistics
import sys
import time
import unittest
import weakref
from unittest.mock import patch

import comparison_types as original


class CachedRecorder(original.Recorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.serialization_cache = {}

    def compare(self, site, operation, left, right):
        # Keep frame acquisition here, not in a wrapper/helper: the original
        # executing function must remain the owner of each observation.
        if self.readers:
            with self.readers.muted():
                caller = sys._getframe(1).f_code
        else:
            caller = sys._getframe(1).f_code
        path = self.sites[site]['path']
        owner = original.code_key(path, caller)
        self.execution_codes.setdefault(site, {}).setdefault(owner, set()).add(self.context())
        left_desc = original.describe_operand(left)
        right_desc = original.describe_operand(right)
        # A class __qualname__ can be a str subclass with hostile __hash__ or
        # __str__. Invoke the base implementation directly before tuple hashing.
        for descriptor in (left_desc, right_desc):
            name = descriptor['qualname']
            if type(name) is not str:
                descriptor['qualname'] = str.__str__(name)
        primitive_key = (tuple(left_desc.values()), tuple(right_desc.values()), type(left) is type(right))
        key = self.serialization_cache.get(primitive_key)
        if key is None:
            pair = {'left': left_desc, 'right': right_desc, 'same_type': type(left) is type(right)}
            key = json.dumps(pair, sort_keys=True, separators=(',', ':'))
            if len(self.serialization_cache) >= 4096:
                self.serialization_cache.clear()
            self.serialization_cache[primitive_key] = key
        self.pairs.setdefault(site, {}).setdefault(key, set()).add(self.context())
        if operation == 'Is':
            return left is right
        if operation == 'IsNot':
            return left is not right
        if operation == 'Eq':
            return left == right
        if operation == 'NotEq':
            return left != right
        raise ValueError(operation)


SOURCE = 'def compare(left, right):\n    return left is right\n'


class NoSerializationCache(dict):
    def get(self, key, default=None):
        return default

    def __setitem__(self, key, value):
        pass


class UncachedRecorder(original.Recorder):
    """Current V2 behavior with only serialization caching disabled."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.serialization_cache = NoSerializationCache()


class Color(enum.Enum):
    RED = 1
    BLUE = 2


class Number(enum.IntEnum):
    ONE = 1
    TWO = 2


class Word(enum.StrEnum):
    ONE = 'one'
    TWO = 'two'


class Override:
    def __eq__(self, other):
        return self


def exercise(recorder_type):
    context = ['first']
    recorder = recorder_type(lambda: context[0])
    functions = [recorder.execute(SOURCE.replace(' is ', f' {operator} '), f'operation_{i}.py')['compare']
                 for i, operator in enumerate(('is', 'is not', '==', '!='))]
    class Changing:
        pass
    class Meta(type):
        def __getattribute__(self, name):
            if name in ('__dict__', '__mro__', '__qualname__'):
                raise AssertionError('custom metaclass access')
            return super().__getattribute__(name)
    class Custom(metaclass=Meta):
        pass
    class HostileName(str):
        def __hash__(self):
            raise AssertionError('custom hash')
        def __str__(self):
            raise AssertionError('custom str')
    class Named:
        pass
    Named.__qualname__ = HostileName('Named')
    a, b = Changing(), Changing()
    operands = [(None, None), (Color.RED, Color.BLUE), (Number.ONE, Number.TWO), (Word.ONE, Word.TWO),
                (int.__new__(Number, 1), Number.ONE), (str.__new__(Word, 'one'), Word.ONE),
                (a, b), (Custom(), Custom()), (Named(), Named()), (Override(), Override())]
    for function in functions:
        for left, right in operands:
            result = function(left, right)
            if function is functions[2] and type(left) is Override:
                assert result is left
    context[0] = 'second'
    Changing.__eq__ = lambda self, other: True
    Changing.__ne__ = lambda self, other: False
    for function in functions:
        assert function(a, b) is (function in (functions[1], functions[2]))
    context[0] = 'third'
    del Changing.__eq__
    del Changing.__ne__
    for function in functions:
        function(a, b)
    return recorder


class EquivalenceTests(unittest.TestCase):
    def test_exact_snapshots_and_primitive_cache(self):
        baseline, candidate = exercise(original.Recorder), exercise(CachedRecorder)
        self.assertEqual(baseline.snapshot(), candidate.snapshot())
        def primitive(value):
            return type(value) in (str, bool) or type(value) is tuple and all(primitive(item) for item in value)
        self.assertTrue(candidate.serialization_cache)
        self.assertTrue(all(primitive(key) and type(value) is str for key, value in candidate.serialization_cache.items()))

    def test_recompute_both_descriptors_every_time(self):
        recorder = CachedRecorder()
        function = recorder.execute(SOURCE)['compare']
        with patch.object(original, 'describe_type', wraps=original.describe_type) as describe:
            for _ in range(20):
                function(None, None)
            self.assertEqual(describe.call_count, 40)
        self.assertEqual(len(recorder.serialization_cache), 1)

    def test_cache_is_bounded(self):
        recorder = CachedRecorder()
        recorder.serialization_cache = {('dummy', i): 'unused' for i in range(4096)}
        recorder.execute(SOURCE)['compare'](None, None)
        self.assertEqual(len(recorder.serialization_cache), 1)

    def test_cache_does_not_retain_operand_types(self):
        recorder = CachedRecorder()
        function = recorder.execute(SOURCE)['compare']
        class Ephemeral:
            pass
        reference = weakref.ref(Ephemeral)
        function(Ephemeral(), Ephemeral())
        del Ephemeral
        gc.collect()
        self.assertIsNone(reference())

    def test_metaclass_slot_mutation_observed(self):
        def run(recorder_type):
            recorder = recorder_type()
            function = recorder.execute(SOURCE)['compare']
            class Meta(type):
                pass
            class A(metaclass=Meta):
                pass
            class B(metaclass=Meta):
                pass
            function(A, B)
            Meta.__eq__ = lambda self, other: True
            function(A, B)
            snapshot = recorder.snapshot()
            pairs = next(iter(snapshot['sites'].values()))['observations']
            self.assertEqual({pair['left']['eq'] for pair in pairs}, {'object_identity', 'overridden'})
            return snapshot
        self.assertEqual(run(original.Recorder), run(CachedRecorder))

    def test_backed_enum_member_map_mutation_observed(self):
        def run(recorder_type):
            class Dynamic(enum.IntEnum):
                ONE = 1
                TWO = 2
            recorder = recorder_type()
            function = recorder.execute(SOURCE)['compare']
            function(Dynamic.ONE, Dynamic.TWO)
            Dynamic._member_map_.pop('ONE')
            function(Dynamic.ONE, Dynamic.TWO)
            snapshot = recorder.snapshot()
            pairs = next(iter(snapshot['sites'].values()))['observations']
            self.assertEqual({pair['left']['canonical_member'] for pair in pairs}, {True, False})
            return snapshot
        self.assertEqual(run(original.Recorder), run(CachedRecorder))


def benchmark(iterations, repeats):
    rows = []
    for name, operands in [('none', (None, None)), ('enum', (Color.RED, Color.BLUE)),
                           ('int_enum', (Number.ONE, Number.TWO)), ('str_enum', (Word.ONE, Word.TWO)),
                           ('overridden_eq', (Override(), Override()))]:
        for operation in ('is', '=='):
            measurements = {'original': [], 'cached': []}
            snapshots = {}
            for repeat in range(repeats):
                order = [('original', UncachedRecorder), ('cached', CachedRecorder)]
                if repeat % 2:
                    order.reverse()
                for label, recorder_type in order:
                    recorder = recorder_type(lambda: 'benchmark')
                    function = recorder.execute(SOURCE.replace(' is ', f' {operation} '))['compare']
                    function(*operands)
                    start = time.process_time()
                    for _ in range(iterations):
                        function(*operands)
                    measurements[label].append(time.process_time() - start)
                    snapshots[label] = recorder.snapshot()
            if snapshots['original'] != snapshots['cached']:
                raise AssertionError(f'snapshot mismatch: {name}/{operation}')
            medians = {label: statistics.median(values) for label, values in measurements.items()}
            rows.append({'workload': name, 'operation': operation, 'cpu_seconds': measurements,
                         'median_cpu_seconds': medians, 'speedup': medians['original'] / medians['cached'],
                         'cpu_reduction_fraction': 1 - medians['cached'] / medians['original'], 'exact_snapshot_equal': True})
    return {'kind': 'comparison_serialization_cache_benchmark', 'recorder_version': 'V2 operand flags',
            'reference': 'current V2 with serialization caching disabled',
            'historical_v1_report': '.work/comparison-cache-benchmark.json (not comparable whole-suite evidence)',
            'python': sys.version, 'iterations': iterations,
            'repeats': repeats, 'production_integration': False, 'results': rows,
            'limitations': ['Synthetic CPU timings do not predict whole-suite speedup.',
                            'Every comparison still recomputes both type descriptors and effective slots.',
                            'Observer stack, timing, reference lifetimes and incomplete reflection detection remain semantic caveats.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=100000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EquivalenceTests)
    result = unittest.TextTestRunner(stream=sys.stderr).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    report = benchmark(args.iterations, args.repeats)
    report['synthetic_tests_passed'] = result.testsRun
    text = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
