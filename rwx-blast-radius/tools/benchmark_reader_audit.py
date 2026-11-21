#!/usr/bin/env python3
"""Isolated reader-audit benchmark. Does not change capture or production.

The candidate caches only within ONE synchronous audit event. Every new event
rechecks filesystem resolution and every context is recorded. Code metadata is
computed only for a newly inserted key, matching setdefault's existing value.
"""
import argparse
import cProfile
import io
import json
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time
import types
import unittest

from comparison_types import ReaderAudit, code_key, code_metadata


class EventLocalReaderAudit(ReaderAudit):
    def _record_codes(self, initial):
        pending = list(initial)
        seen, paths = set(), {}
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            filename = current.co_filename
            if filename not in paths:
                path = Path(filename).resolve()
                paths[filename] = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) and path.suffix == '.py' else None
            relative = paths[filename]
            if relative is not None:
                key = code_key(relative, current)
                record = self.code_objects.get(key)
                if record is None:
                    record = dict(code_metadata(relative, current), contexts=set())
                    self.code_objects[key] = record
                record['contexts'].add(self.context())
                self.code.setdefault(relative, set()).add(self.context())
            pending.extend(value for value in current.co_consts if type(value) is types.CodeType)

    def record_code(self, code):
        self._record_codes((code,))

    def record_frame(self, frame):
        codes = []
        while frame is not None:
            codes.append(object.__getattribute__(frame, 'f_code'))
            frame = object.__getattribute__(frame, 'f_back')
        # Maintain original traversal ordering to preserve first-key metadata
        # when distinct code objects share path/firstline/qualname.
        self._record_codes(reversed(codes))


def fixture(root, count=80):
    source = 'import sys\ndef outer():\n'
    source += ''.join(f'    def nested_{i}(value):\n        return value is None\n' for i in range(count))
    source += '    return sys._getframe()\n'
    namespace = {}
    code = compile(source, str(root / 'module.py'), 'exec')
    exec(code, namespace)
    return namespace['outer'](), code


class ReaderEquivalenceTests(unittest.TestCase):
    def test_contexts_nested_ancestors_and_repeated_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, code = fixture(root)
            context = ['']
            audits = [cls(root, lambda: context[0]) for cls in (ReaderAudit, EventLocalReaderAudit)]
            for name in ('', 'first', 'second', 'first'):
                context[0] = name
                for audit in audits:
                    audit.record_frame(frame)
                    audit.record_code(code)
                    audit.record_code(code)
            self.assertEqual(audits[0].snapshot(), audits[1].snapshot())
            self.assertTrue(all(row['contexts'] == ['', 'first', 'second'] for row in audits[1].snapshot()['code_objects'].values()))

    def test_symlink_changes_between_events_are_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a').mkdir(); (root / 'b').mkdir()
            link = root / 'link'; link.symlink_to(root / 'a', target_is_directory=True)
            code = compile('value = 1', str(link / 'module.py'), 'exec')
            audits = [cls(root, lambda: 'test') for cls in (ReaderAudit, EventLocalReaderAudit)]
            for audit in audits:
                audit.record_code(code)
            link.unlink(); link.symlink_to(root / 'b', target_is_directory=True)
            for audit in audits:
                audit.record_code(code)
            self.assertEqual(audits[0].snapshot(), audits[1].snapshot())
            self.assertEqual(set(audits[1].code), {'a/module.py', 'b/module.py'})

    def test_replaced_code_same_key_retains_first_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = compile('x=1', str(root / 'module.py'), 'exec')
            second = compile('x=1\ny=2\nz=3', str(root / 'module.py'), 'exec')
            audits = [cls(root, lambda: 'test') for cls in (ReaderAudit, EventLocalReaderAudit)]
            for audit in audits:
                audit.record_code(first); audit.record_code(second)
            self.assertEqual(audits[0].snapshot(), audits[1].snapshot())

    def test_frame_lasti_audit_still_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, _ = fixture(root, 2)
            audits = [cls(root, lambda: 'observer') for cls in (ReaderAudit, EventLocalReaderAudit)]
            for audit in audits:
                audit.active = True
                audit.audit('sys._getframe', (frame,))
                audit.active = False
            self.assertEqual(audits[0].snapshot(), audits[1].snapshot())


def benchmark(iterations, repeats):
    results = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        frame, code = fixture(root)
        for workload in ('outer_code', 'frame_ancestors'):
            measurements = {'original': [], 'candidate': []}
            snapshots = {}
            profiles = {}
            for label, cls in [('original', ReaderAudit), ('candidate', EventLocalReaderAudit)]:
                for _ in range(repeats):
                    context = ['first']
                    audit = cls(root, lambda: context[0])
                    operation = (lambda: audit.record_code(code)) if workload == 'outer_code' else (lambda: audit.record_frame(frame))
                    start = time.process_time()
                    for index in range(iterations):
                        context[0] = 'first' if index % 2 else 'second'
                        operation()
                    measurements[label].append(time.process_time() - start)
                    snapshots[label] = audit.snapshot()
                profile = cProfile.Profile()
                profile.runcall(lambda: [operation() for _ in range(min(iterations, 100))])
                stream = io.StringIO()
                pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(12)
                profiles[label] = stream.getvalue()
            if snapshots['original'] != snapshots['candidate']:
                raise AssertionError('reader observations differ')
            medians = {key: statistics.median(values) for key, values in measurements.items()}
            results.append({'workload': workload, 'cpu_seconds': measurements, 'median_cpu_seconds': medians,
                            'speedup': medians['original'] / medians['candidate'], 'snapshots_equal': True, 'profiles': profiles})
    return {'format': 1, 'kind': 'reader_audit_event_local_benchmark', 'python': sys.version,
            'iterations': iterations, 'repeats': repeats, 'results': results, 'capture_modified': False,
            'limitations': ['Synthetic timings do not predict full-suite speedup.',
                            'Filesystem resolution is shared only within one synchronous event; concurrent external symlink mutation during the event is not modeled.',
                            'Context getter is assumed pure and stable during one event, as in the baseline tracer.',
                            'No persistent code/type/operand caches and no source/code-reader contexts discarded.']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iterations', type=int, default=1000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(ReaderEquivalenceTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    report = benchmark(args.iterations, args.repeats)
    report['tests_passed'] = result.testsRun
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps([{key: value for key, value in row.items() if key not in ('profiles', 'cpu_seconds')} for row in report['results']], indent=2))


if __name__ == '__main__':
    main()
