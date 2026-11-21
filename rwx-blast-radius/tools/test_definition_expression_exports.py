import tempfile
import unittest
from pathlib import Path
from definition_expression_exports import augment
from propagate_class_consumers import propagate

class DefinitionExportsTests(unittest.TestCase):
    def evidence(self, text):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'sqlglot');p.mkdir();(p/'a.py').write_text(text)
            return augment(d)

    def test_defaults_decorators_annotations(self):
        e=self.evidence('class C:\n X=1\n@decorate(C.X)\ndef f(a:C.X=C.X,/,*,b=C.X)->C.X: pass\n')
        kinds={r['expression_kind'] for r in e['definition_expression_evidence']}
        self.assertEqual(kinds, {'default','decorator','annotation'})
        _, report=propagate({'files':{}},e)
        self.assertEqual(report['class_attribute_global_exports']['sqlglot.a.C'], ['X'])

    def test_future_annotations_not_eager(self):
        e=self.evidence('from __future__ import annotations\nclass C:\n X=1\ndef f(x:C.X)->C.X: pass\n')
        self.assertFalse(e['definition_expression_evidence'])
        self.assertEqual(len(e['deferred_definition_expressions']),2)

    def test_nested_definition_and_lambda_timing(self):
        e=self.evidence('class C:\n X=1\ndef outer():\n def inner(x=C.X): pass\n return lambda x=C.X: C.X\n')
        r=e['definition_expression_evidence']
        self.assertEqual(len(r),2)
        self.assertTrue(all(x['timing']=='when_enclosing_callable_executes:outer' for x in r))

    def test_lambda_body_not_evaluated_in_default(self):
        e=self.evidence('class C:\n X=1\ndef f(a=lambda: C.X): pass\n')
        self.assertFalse(e['definition_expression_evidence'])

    def test_class_local_default_and_body_scope(self):
        e=self.evidence('class C:\n X=1\n def f(self,a=X):\n  def g(b=X): pass\n')
        r=e['definition_expression_evidence']
        self.assertEqual(len(r),1)
        self.assertEqual(r[0]['source'],'sqlglot.a.C.X')

    def test_import_alias_and_class_object_capture(self):
        e=self.evidence('class C:\n X=1\ndef f(x=C): pass\n')
        self.assertEqual(e['definition_expression_evidence'][0]['source'],'sqlglot.a.C.*')

    def test_body_attributes_do_not_become_header_reads(self):
        e=self.evidence('class C:\n X=1\ndef f():\n return C.X\n')
        self.assertFalse(e['definition_expression_evidence'])

    def test_class_valued_attribute_capture_follows_binding(self):
        e=self.evidence('class Impl:\n X=1\nclass Owner:\n Parser=Impl\ndef f(x=Owner.Parser): pass\n')
        refs={r['source'] for r in e['definition_expression_evidence']}
        self.assertTrue({'sqlglot.a.Owner.Parser','sqlglot.a.Impl.*'} <= refs)

    def test_import_alias_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d,'sqlglot');p.mkdir()
            (p/'a.py').write_text('class C:\n X=1\n')
            (p/'b.py').write_text('from sqlglot.a import C as Alias\ndef f(x=Alias.X): pass\n')
            e=augment(d)
            self.assertEqual(e['definition_expression_evidence'][0]['source'],'sqlglot.a.C.X')

    def test_class_header_excluded_from_minimal_mode(self):
        e=self.evidence('class C:\n X=1\n@decorate(C.X)\nclass D(C, metaclass=C.X): pass\n')
        self.assertFalse(e['definition_expression_evidence'])

if __name__=='__main__': unittest.main()
