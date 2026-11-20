"""Class dependency tracing against isolated, synthetic baseline modules."""

import sys
import tempfile
import types
import unittest
from pathlib import Path

from tools.class_dependencies import ClassDependencies, merge_snapshots


@unittest.skipUnless(hasattr(sys, 'monitoring'), 'requires Python monitoring API')
class ClassDependencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.context = ''
        self.collector = ClassDependencies(self.root, lambda: self.context)
        self.addCleanup(self.collector.stop)
        self.base = self.module('base', '''
class Base:
    def value(self):
        return 7
    @classmethod
    def class_value(cls):
        return cls.__name__
''')
        self.derived = self.module('derived', '''
class Outer:
    class Nested(Base):
        pass
class Other(Base):
    pass
''', Base=self.base.Base)

    def module(self, suffix, source, **namespace):
        name = f'sqlglot._blast_collector_test_{suffix}'
        filename = self.root / 'sqlglot' / f'{suffix}.py'
        filename.parent.mkdir(exist_ok=True)
        filename.write_text(source)
        module = types.ModuleType(name)
        module.__file__ = str(filename)
        module.__dict__.update(namespace)
        previous = sys.modules.get(name)
        sys.modules[name] = module

        def restore():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

        self.addCleanup(restore)
        exec(compile(source, str(filename), 'exec'), module.__dict__)
        return module

    def test_inherited_method_records_runtime_nested_class_and_base(self):
        # Import and construct before tracing: neither step should be required
        # for recognizing the receiver's runtime class and defining source.
        instance = self.derived.Outer.Nested()
        self.context = 'test_nested'
        self.collector.start()
        self.assertEqual(instance.value(), 7)
        self.collector.stop()
        self.assertEqual(self.collector.snapshot(), {
            'sqlglot/base.py': {'Base': ['test_nested']},
            'sqlglot/derived.py': {'Outer.Nested': ['test_nested']},
        })

    def test_classmethod_preserves_derived_class_identity(self):
        self.context = 'test_classmethod'
        self.collector.start()
        self.assertEqual(self.derived.Other.class_value(), 'Other')
        self.collector.stop()
        self.assertEqual(self.collector.snapshot()['sqlglot/derived.py'], {
            'Other': ['test_classmethod'],
        })

    def test_context_switch_separates_classes_and_retains_collection(self):
        nested = self.derived.Outer.Nested()
        other = self.derived.Other()
        self.collector.start()
        nested.value()  # Empty context is collection/import, not a test.
        self.context = 'test_a'
        nested.value()
        nested.value()  # Repeated calls must not duplicate consumers.
        self.context = 'test_b'
        other.value()
        self.collector.stop()
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot['sqlglot/base.py']['Base'], ['', 'test_a', 'test_b'])
        self.assertEqual(snapshot['sqlglot/derived.py'], {
            'Other': ['test_b'], 'Outer.Nested': ['', 'test_a'],
        })

    def test_stop_and_restart_do_not_leak_context_or_monitoring_slot(self):
        owners = {i: sys.monitoring.get_tool(i) for i in range(6)}
        instance = self.derived.Other()
        self.context = 'first'
        self.collector.start()
        instance.value()
        self.collector.stop()
        self.context = 'not_recorded'
        instance.value()
        self.context = 'second'
        self.collector.start()
        instance.value()
        self.collector.stop()
        self.collector.stop()  # Cleanup is idempotent after normal shutdown.
        self.assertEqual(self.collector.snapshot()['sqlglot/derived.py']['Other'], ['first', 'second'])
        self.assertEqual({i: sys.monitoring.get_tool(i) for i in range(6)}, owners)

    def test_exception_cleanup_restores_monitoring_and_allows_next_run(self):
        owners = {i: sys.monitoring.get_tool(i) for i in range(6)}
        self.context = 'failing_test'
        with self.assertRaisesRegex(ValueError, 'intentional'):
            self.collector.start()
            try:
                self.derived.Other().value()
                raise ValueError('intentional')
            finally:
                self.collector.stop()
        self.assertEqual({i: sys.monitoring.get_tool(i) for i in range(6)}, owners)
        self.context = 'next_test'
        self.collector.start()
        self.derived.Other().value()
        self.collector.stop()
        self.assertEqual(self.collector.snapshot()['sqlglot/derived.py']['Other'], ['failing_test', 'next_test'])

    def test_double_start_rejected_without_disabling_current_collection(self):
        self.collector.start()
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            self.collector.start()
        self.context = 'still_recorded'
        self.derived.Other().value()
        self.collector.stop()
        self.assertEqual(self.collector.snapshot()['sqlglot/derived.py']['Other'], ['still_recorded'])

    def test_worker_snapshots_union_contexts_without_mutating_inputs(self):
        first = {'sqlglot/base.py': {'Base': ['test_a']}}
        second = {'sqlglot/base.py': {'Base': ['test_b', 'test_a']},
                  'sqlglot/derived.py': {'Outer.Nested': ['test_b']}}
        merged = merge_snapshots(first, second, first)
        self.assertEqual(merged, {
            'sqlglot/base.py': {'Base': ['test_a', 'test_b']},
            'sqlglot/derived.py': {'Outer.Nested': ['test_b']},
        })
        self.assertEqual(first, {'sqlglot/base.py': {'Base': ['test_a']}})
        self.assertEqual(second['sqlglot/base.py']['Base'], ['test_b', 'test_a'])


if __name__ == '__main__':
    unittest.main()
