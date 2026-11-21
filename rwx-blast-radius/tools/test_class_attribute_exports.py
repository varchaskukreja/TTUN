from pathlib import Path
import tempfile
import unittest

from class_attribute_exports import analyze


class ExportTests(unittest.TestCase):
    def analyze(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sqlglot').mkdir()
            for name, content in files.items():
                (root / 'sqlglot' / name).write_text(content)
            return analyze(root)

    def test_imported_alias_copy_to_other_class(self):
        result = self.analyze({'a.py': 'class Original:\n    MAPPING = {"x": "y"}\n',
                              'b.py': 'from sqlglot.a import Original as O\nclass Other:\n    MAPPING = O.MAPPING\n'})
        flow = result['direct_class_flows'][0]
        self.assertEqual(flow['source'], 'sqlglot.a.Original.MAPPING')
        self.assertEqual(flow['target'], 'sqlglot.b.Other.MAPPING')
        self.assertEqual(flow['kind'], 'class_attribute_escape')

    def test_inherited_method_exports_literal_to_global_registry(self):
        result = self.analyze({'a.py': '''class Base:
    @classmethod
    def names(cls):
        return cls._names
class Concrete(Base):
    _names = ["alias"]
REGISTRY = {name: cls for cls in ALL_CLASSES for name in cls.names()}
'''})
        flow = result['module_registry_flows'][0]
        self.assertEqual(flow['possible_literal_sources'], ['sqlglot.a.Concrete._names'])
        self.assertEqual(flow['targets'], ['sqlglot.a.REGISTRY'])
        self.assertEqual(flow['method_owner'], 'sqlglot.a.Base')

    def test_local_derivation_distinguished_from_cross_receiver(self):
        result = self.analyze({'a.py': '''class Tokenizer:
    QUOTES = ["'"]
    DERIVED = tuple(QUOTES)
    def init(cls):
        cls._QUOTES = convert(cls.QUOTES)
class Dialect:
    def init(klass):
        klass.QUOTE_START = list(klass.tokenizer_class._QUOTES)[0]
'''})
        self.assertTrue(any(f['source'] == 'sqlglot.a.Tokenizer.QUOTES' for f in result['same_receiver_derivations']))
        self.assertTrue(any(f['source'] == 'cls.QUOTES' and f['target'] == 'cls._QUOTES' for f in result['same_receiver_derivations']))
        self.assertTrue(any(f['source'] == 'klass.tokenizer_class._QUOTES' and f['target'] == 'klass.QUOTE_START' for f in result['cross_receiver_flows']))
        self.assertFalse(result['analysis_complete'])
        self.assertFalse(result['narrowing_authorized'])

    def test_subclass_method_export_uses_inherited_attribute_provider(self):
        result = self.analyze({'a.py': "class Base:\n    VALUE = [1]\nclass Derived(Base):\n    @classmethod\n    def values(cls): return cls.VALUE\nREGISTRY = [x.values() for x in CLASSES]\n"})
        self.assertEqual(result['module_registry_flows'][0]['possible_literal_sources'], ['sqlglot.a.Base.VALUE'])

    def test_module_global_direct_attribute_is_escape(self):
        result = self.analyze({'a.py': 'class C:\n    NAMES = ["x"]\nREGISTRY = dict.fromkeys(C.NAMES)\n'})
        self.assertTrue(any(f['source'] == 'sqlglot.a.C.NAMES' and f['target'] == 'sqlglot.a.REGISTRY' for f in result['direct_class_flows']))


if __name__ == '__main__':
    unittest.main()
