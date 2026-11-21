"""Baseline-only syntax and alias resolution tests for closure-factory proofs."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('factory_proofs', Path(__file__).parent / 'tools/build_factory_proofs.py')
proofs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proofs)

FACTORY = 'def factory(value):\n    def inner(arg):\n        return consume(value, arg)\n    return inner\n'


class FactoryProofTests(unittest.TestCase):
    def recognized(self, code, future=False):
        return proofs.is_closure_factory(ast.parse(code).body[0], future)

    def build(self, files):
        with tempfile.TemporaryDirectory() as root:
            for path, code in files.items():
                target = Path(root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code)
            return proofs.build_proofs(Path(root))

    def test_plain_factory_and_optional_docstring(self):
        self.assertTrue(self.recognized(FACTORY))
        self.assertTrue(self.recognized(FACTORY.replace('    def inner', '    "factory docs"\n    def inner')))

    def test_inner_body_can_have_calls_and_assignments_not_executed_at_creation(self):
        code = 'def factory(value):\n    def inner():\n        result = side_effect(value)\n        if result:\n            raise ValueError(result)\n        return result\n    return inner\n'
        self.assertTrue(self.recognized(code))

    def test_return_lambda(self):
        self.assertTrue(self.recognized('def factory(value):\n    return lambda arg: consume(value, arg)\n'))

    def test_factory_body_effects_rejected(self):
        for inserted in ['side_effect()\n', 'value = 1\n', 'if value:\n        return None\n', 'pass\n']:
            with self.subTest(inserted=inserted):
                self.assertFalse(self.recognized(FACTORY.replace('    def inner', '    ' + inserted + '    def inner')))

    def test_outer_and_inner_decorators_rejected(self):
        self.assertFalse(self.recognized('@decorate\n' + FACTORY))
        self.assertFalse(self.recognized(FACTORY.replace('    def inner', '    @decorate\n    def inner')))

    def test_nested_positional_and_keyword_defaults_rejected(self):
        for arguments in ['arg=1', 'arg=effect()', '*, arg=1', '*, arg=effect()']:
            self.assertFalse(self.recognized(FACTORY.replace('inner(arg)', f'inner({arguments})')))

    def test_lambda_defaults_rejected(self):
        for arguments in ['arg=1', '*, arg=effect()']:
            self.assertFalse(self.recognized(f'def factory(value):\n    return lambda {arguments}: value\n'))

    def test_annotations_require_future_at_nested_definition(self):
        code = FACTORY.replace('inner(arg)', 'inner(arg: side_effect()) -> annotation()')
        self.assertFalse(self.recognized(code))
        self.assertTrue(self.recognized(code, future=True))

    def test_variadic_annotations_require_future(self):
        code = FACTORY.replace('inner(arg)', 'inner(*arg: effect(), **kwargs: effect())')
        self.assertFalse(self.recognized(code))
        self.assertTrue(self.recognized(code, future=True))

    def test_wrong_return_extra_nested_function_and_async_rejected(self):
        self.assertFalse(self.recognized(FACTORY.replace('return inner', 'return other')))
        self.assertFalse(self.recognized(FACTORY.replace('return inner', 'return inner()')))
        self.assertFalse(self.recognized(FACTORY.replace('    return inner', '    def other():\n        return 1\n    return inner')))
        self.assertFalse(self.recognized('async ' + FACTORY))
        self.assertFalse(self.recognized(FACTORY.replace('    def inner', '    async def inner')))

    def test_future_annotations_discovered_from_module(self):
        code = FACTORY.replace('inner(arg)', 'inner(arg: SomeType) -> OtherType')
        result = self.build({'sqlglot/a.py': 'from __future__ import annotations\n' + code})
        self.assertIn('factory', result['files']['sqlglot/a.py'])
        self.assertNotIn('sqlglot/a.py', self.build({'sqlglot/a.py': code})['files'])

    def test_import_alias_and_relative_reexport_chain(self):
        result = self.build({'sqlglot/a.py': FACTORY,
                             'sqlglot/b.py': 'from .a import factory as alias\n',
                             'sqlglot/c.py': 'from sqlglot.b import alias as callable_name\n'})
        record = result['files']['sqlglot/c.py']['callable_name']
        self.assertEqual(record['factory_file'], 'sqlglot/a.py')
        self.assertEqual(record['factory_name'], 'factory')
        self.assertEqual(len(record['import_chain']), 2)
        self.assertEqual(record['factory_ast_sha256'], proofs.ast_hash(ast.parse(FACTORY).body[0]))
        self.assertEqual(result['baseline_revision'], proofs.REVISION)

    def test_relative_package_init_import(self):
        result = self.build({'sqlglot/pkg/a.py': FACTORY,
                             'sqlglot/pkg/__init__.py': 'from .a import factory\n',
                             'sqlglot/consumer.py': 'from .pkg import factory\n'})
        self.assertIn('factory', result['files']['sqlglot/consumer.py'])

    def test_rebound_factory_rejected(self):
        for suffix in ['factory = unknown\n', 'del factory\n',
                       'if condition:\n    factory = other\n',
                       'from elsewhere import factory\n',
                       'def mutate():\n    global factory\n    factory = other\n',
                       'def mutate():\n    globals()["factory"] = other\n',
                       'from elsewhere import *\n']:
            with self.subTest(suffix=suffix):
                result = self.build({'sqlglot/a.py': FACTORY + suffix,
                                     'sqlglot/b.py': 'from sqlglot.a import factory\n'})
                self.assertFalse(result['files'])

    def test_rebound_import_alias_rejected(self):
        result = self.build({'sqlglot/a.py': FACTORY,
                             'sqlglot/b.py': 'from .a import factory as f\nf = other\n'})
        self.assertNotIn('sqlglot/b.py', result['files'])

    def test_unrelated_local_name_does_not_rebind_module_factory(self):
        result = self.build({'sqlglot/a.py': FACTORY + 'def other():\n    factory = 1\n    return factory\n'})
        self.assertIn('factory', result['files']['sqlglot/a.py'])

    def test_nested_or_conditional_factory_not_visible_module_proof(self):
        for prefix in ['if condition:\n', 'class C:\n', 'def outer():\n']:
            result = self.build({'sqlglot/a.py': prefix + ''.join('    ' + line for line in FACTORY.splitlines(True))})
            self.assertFalse(result['files'])

    def test_conditional_import_alias_not_proved(self):
        result = self.build({'sqlglot/a.py': FACTORY,
                             'sqlglot/b.py': 'if condition:\n    from .a import factory\n'})
        self.assertNotIn('sqlglot/b.py', result['files'])

    def test_definition_default_walrus_rebinding_is_rejected(self):
        code = FACTORY + 'def other(arg=(factory := unknown)):\n    return arg\n'
        self.assertFalse(self.build({'sqlglot/a.py': code})['files'])

    def test_ast_hash_ignores_source_locations_and_comments(self):
        self.assertEqual(proofs.ast_hash(ast.parse(FACTORY).body[0]),
                         proofs.ast_hash(ast.parse('\n# context\n' + FACTORY).body[0]))

    def test_module_namespace_attribute_mutation_channels_rejected(self):
        additions = [
            'import sys\nsys.modules[__name__].factory = other\n',
            'import sys as system\nmodule = system.modules[__name__]\nsetattr(module, "factory", other)\n',
            'import sys\nsystem = sys\nsystem.modules[__name__].factory = other\n',
            'from sys import modules as loaded\nloaded[__name__].factory = other\n',
            'import sqlglot.provider as module\nmodule.factory = other\n',
            'import sqlglot.provider as module\nalias = module\nalias.factory = other\n',
            'import sqlglot.provider as module\nsetattr(module, "factory", other)\n',
            'import sqlglot.provider as module\nfrom builtins import setattr as update\nupdate(module, "factory", other)\n',
            'import sqlglot.provider as module\ndelattr(module, "factory")\n',
            'import sqlglot.provider as module\nnamespace = module.__dict__\nnamespace["factory"] = other\n',
            'import sqlglot.provider as module\nnamespace = vars(module)\nnamespace["factory"] = other\n',
            'alias = factory\nalias.__code__ = other.__code__\n',
        ]
        for suffix in additions:
            with self.subTest(suffix=suffix):
                self.assertFalse(self.build({'sqlglot/a.py': FACTORY + suffix})['files'])

    def test_ordinary_local_attribute_mutation_does_not_rebind_factory(self):
        result = self.build({'sqlglot/a.py': FACTORY + 'def other(obj):\n    obj.value = 1\n'})
        self.assertIn('factory', result['files']['sqlglot/a.py'])


if __name__ == '__main__':
    unittest.main()
