import copy
from pathlib import Path
import tempfile
import unittest

from class_attribute_exports import analyze
from propagate_class_consumers import propagate


class ClosureTests(unittest.TestCase):
    def fixture(self, code, consumers):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / 'sqlglot').mkdir()
        (root / 'sqlglot/a.py').write_text(code)
        mapping = {'files': {'sqlglot/a.py': {'class_consumers': consumers}}, 'test_ids': ['a', 'b', 'c']}
        return mapping, analyze(root)

    def test_transitive_copy_inheritance_preserves_existing(self):
        mapping, exports = self.fixture('''class A:
    VALUE = {"a": 1}
class B:
    VALUE = A.VALUE
class C(B):
    pass
''', {'A': [0], 'B': [1], 'C': [2]})
        before = copy.deepcopy(mapping)
        result, report = propagate(mapping, exports)
        self.assertEqual(result['files']['sqlglot/a.py']['class_consumers']['A'], [0, 1, 2])
        self.assertEqual(mapping, before)
        self.assertFalse(report['analysis_complete'])

    def test_nested_class_consumes_lexical_outer_contexts(self):
        mapping, exports = self.fixture('''class Outer:
    class Inner:
        QUOTES = ["'"]
''', {'Outer': [0, 1], 'Outer.Inner': [2]})
        result, _ = propagate(mapping, exports, lexical_outer=True)
        self.assertEqual(result['files']['sqlglot/a.py']['class_consumers']['Outer.Inner'], [0, 1, 2])

    def test_registry_blocks_attribute_not_other_literals(self):
        mapping, exports = self.fixture('''class Base:
    @classmethod
    def names(cls): return cls._names
class Concrete(Base):
    _names = ["alias"]
    SAFE = 3
REGISTRY = {name: cls for cls in CLASSES for name in cls.names()}
''', {'Concrete': [0], 'Base': [0]})
        result, _ = propagate(mapping, exports)
        blocked = result['files']['sqlglot/a.py']['class_attribute_global_exports']
        self.assertEqual(blocked['Concrete'], ['_names'])
        self.assertEqual(result['files']['sqlglot/a.py']['class_consumers']['Concrete'], [0])

    def test_free_function_attribute_read_requires_fallback(self):
        mapping, exports = self.fixture('''class C:
    VALUE = [1]
def read():
    return C.VALUE
''', {'C': [0]})
        result, _ = propagate(mapping, exports)
        self.assertEqual(result['files']['sqlglot/a.py']['class_attribute_global_exports']['C'], ['VALUE'])


    def test_exported_derived_alias_reaches_original_literal(self):
        mapping, exports = self.fixture("""class A:
    VALUE = [1]
    DERIVED = tuple(VALUE)
class B:
    COPY = A.DERIVED
GLOBAL = B.COPY
""", {'A': [0], 'B': [1]})
        result, report = propagate(mapping, exports)
        flags = result['files']['sqlglot/a.py']['class_attribute_global_exports']
        self.assertEqual(flags['A'], ['DERIVED', 'VALUE'])
        self.assertEqual(flags['B'], ['COPY'])
        self.assertIn('sqlglot.a.A.VALUE', report['propagated_global_exports'])

    def test_export_through_subclass_blocks_ancestor_provider(self):
        mapping, exports = self.fixture("""class Base:
    VALUE = [1]
class Child(Base):
    pass
def read():
    return Child.VALUE
""", {'Base': [0], 'Child': [1]})
        result, _ = propagate(mapping, exports)
        flags = result['files']['sqlglot/a.py']['class_attribute_global_exports']
        self.assertEqual(flags['Base'], ['VALUE'])


if __name__ == '__main__':
    unittest.main()
