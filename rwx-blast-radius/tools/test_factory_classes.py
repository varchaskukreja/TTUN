"""Adversarial checks for opt-in factory-call class selection."""
import ast
import copy
import difflib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from build_factory_proofs import build_proofs
from experiment_factory_classes import FactorySelector
from factory_selection import FactoryCandidates
from factory_proof_format import ast_hash
from selector import Selector


FACTORY = 'def factory(*args, **kwargs):\n    def inner(value):\n        return consume(value, args, kwargs)\n    return inner\n'
CONSUMER = "from sqlglot.factories import factory\nclass Example:\n    REG = {'key': factory('THING', target=True), 'other': arbitrary_call()}\n"


def diff(path, before, after):
    return f'diff --git a/{path} b/{path}\n' + ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/' + path, tofile='b/' + path))


class FactoryClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write('sqlglot/factories.py', FACTORY)
        self.write('sqlglot/consumer.py', CONSUMER)

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    def candidates(self, before=CONSUMER, after=None, changed=None, line=3):
        self.write('sqlglot/consumer.py', before)
        proofs = build_proofs(self.root)
        return FactoryCandidates(self.root, proofs)(ast.parse(before), ast.parse(after or before.replace('target=True', 'target=False')),
                                  line, {'class_consumers': {'Example': [0]}},
                                  'sqlglot/consumer.py', changed or {'sqlglot/consumer.py'})

    def test_proven_call_constant_edit_with_arbitrary_unchanged_branch(self):
        self.assertEqual(self.candidates(), {0})

    def test_factory_proof_does_not_override_global_registry_export(self):
        tests = [{'id': 'receiver', 'number': 10, 'seconds': .1},
                 {'id': 'registry_reader', 'number': 11, 'seconds': .1}]
        mapping = {'test_ids': ['receiver', 'registry_reader'], 'files': {
            'sqlglot/consumer.py': {'class_consumers': {'Example': [0]}, 'lines': {},
                                  'class_attribute_global_exports': {'Example': ['REG']}}}}
        engine = FactorySelector(tests, mapping, self.root, build_proofs(self.root), lambda patch: True)
        self.assertEqual(engine.select(diff('sqlglot/consumer.py', CONSUMER,
                                           CONSUMER.replace('target=True', 'target=False'))), {10, 11})

    def test_string_argument_constant_change(self):
        self.assertEqual(self.candidates(after=CONSUMER.replace("'THING'", "'OTHER'")), {0})

    def test_nonconstant_structure_or_constant_type_change_rejected(self):
        self.assertIsNone(self.candidates(after=CONSUMER.replace('target=True', 'different=False')))
        self.assertIsNone(self.candidates(after=CONSUMER.replace('target=True', 'target=0')))

    def test_unproved_call_on_changed_path_rejected(self):
        before = CONSUMER.replace("factory('THING', target=True)", "arbitrary_call('THING', target=True)")
        self.assertIsNone(self.candidates(before))

    def test_unsafe_unchanged_argument_rejected(self):
        for argument in ['unknown', 'obj.attr', 'other()', '1 + 2', '*unknown']:
            with self.subTest(argument=argument):
                before = CONSUMER.replace("'THING'", argument)
                self.assertIsNone(self.candidates(before))

    def test_expanded_keywords_rejected(self):
        before = CONSUMER.replace('target=True', 'target=True, **{}')
        self.assertIsNone(self.candidates(before))

    def test_any_class_scope_shadow_rejected(self):
        for extra in ['    factory = alternate\n', '    def factory(self):\n        return alternate\n',
                      '    from other import factory\n', '    locals()["factory"] = alternate\n',
                      '    try:\n        pass\n    except Exception as factory:\n        pass\n']:
            with self.subTest(extra=extra):
                self.assertIsNone(self.candidates(CONSUMER + extra))

    def test_factory_source_touched_rejected(self):
        self.assertIsNone(self.candidates(changed={'sqlglot/consumer.py', 'sqlglot/factories.py'}))

    def test_intermediate_alias_source_touched_rejected(self):
        self.write('sqlglot/aliases.py', 'from sqlglot.factories import factory\n')
        before = CONSUMER.replace('sqlglot.factories', 'sqlglot.aliases')
        self.assertIsNone(self.candidates(before, changed={'sqlglot/consumer.py', 'sqlglot/aliases.py'}))

    def test_consumer_import_change_rejected(self):
        self.assertIsNone(self.candidates(after=CONSUMER.replace('sqlglot.factories', 'sqlglot.other').replace('target=True', 'target=False')))

    def test_nested_function_default_or_annotation_prevents_proof(self):
        for inner in ['inner(value=side_effect())', 'inner(value: side_effect())']:
            with self.subTest(inner=inner):
                self.write('sqlglot/factories.py', FACTORY.replace('inner(value)', inner))
                self.assertIsNone(self.candidates())

    def test_changed_unproved_sibling_on_same_line_rejected(self):
        before = CONSUMER.replace('arbitrary_call()', 'arbitrary_call(True)')
        self.assertIsNone(self.candidates(before, before.replace('True', 'False')))

    def test_unrelated_literal_edit_without_factory_path_rejected(self):
        before = CONSUMER.replace('arbitrary_call()', '123')
        self.assertIsNone(self.candidates(before, before.replace('123', '456')))

    def test_selector_integration_and_multifile_provenance_fallback(self):
        tests = [{'id': 'a', 'number': 10, 'seconds': .1}, {'id': 'b', 'number': 11, 'seconds': .1}]
        mapping = {'test_ids': ['a', 'b'], 'files': {'sqlglot/consumer.py': {'class_consumers': {'Example': [0]}, 'lines': {}}}}
        proof = build_proofs(self.root)
        guard_calls = []
        engine = FactorySelector(tests, mapping, self.root, proof, lambda patch: guard_calls.append(patch) or True)
        consumer_patch = diff('sqlglot/consumer.py', CONSUMER, CONSUMER.replace('target=True', 'target=False'))
        self.assertEqual(engine.select(consumer_patch), {10})
        self.assertEqual(len(guard_calls), 1)
        provenance_patch = diff('sqlglot/factories.py', FACTORY, FACTORY.replace('return inner', 'return inner  # changed'))
        self.assertEqual(engine.select(consumer_patch + provenance_patch), {10, 11})

    def test_reject_failed_import_guard(self):
        tests = [{'id': 'a', 'number': 10, 'seconds': .1}, {'id': 'b', 'number': 11, 'seconds': .1}]
        mapping = {'test_ids': ['a', 'b'], 'files': {'sqlglot/consumer.py': {'class_consumers': {'Example': [0]}, 'lines': {}}}}
        engine = FactorySelector(tests, mapping, self.root, build_proofs(self.root), lambda patch: False)
        self.assertEqual(engine.select(diff('sqlglot/consumer.py', CONSUMER, CONSUMER.replace('target=True', 'target=False'))), {10, 11})

    def test_schema_version_kind_revision_and_interpreter_must_match(self):
        proof = build_proofs(self.root)
        for key, invalid in [('schema', True), ('schema', 99), ('proof_kind', 'other'),
                             ('baseline_revision', 'wrong'), ('python_version', [0, 0]),
                             ('source_ast_sha256', []), ('files', [])]:
            with self.subTest(key=key, value=invalid):
                altered = dict(proof, **{key: invalid})
                candidate = FactoryCandidates(self.root, altered)
                self.assertFalse(candidate.valid)
                self.assertIsNone(candidate(ast.parse(CONSUMER), ast.parse(CONSUMER.replace('target=True', 'target=False')),
                                            3, {'class_consumers': {'Example': [0]}}, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}))
        for key in proof:
            altered = dict(proof)
            del altered[key]
            self.assertFalse(FactoryCandidates(self.root, altered).valid)

    def test_stale_factory_hash_or_module_hash_rejected(self):
        proof = build_proofs(self.root)
        altered = copy.deepcopy(proof)
        altered['files']['sqlglot/consumer.py']['factory']['factory_ast_sha256'] = 'bad'
        candidate = FactoryCandidates(self.root, altered)
        self.assertIsNone(candidate(ast.parse(CONSUMER), ast.parse(CONSUMER.replace('target=True', 'target=False')),
                                    3, {'class_consumers': {'Example': [0]}}, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}))
        proof['source_ast_sha256']['sqlglot/factories.py'] = 'bad'
        candidate = FactoryCandidates(self.root, proof)
        self.assertIsNone(candidate(ast.parse(CONSUMER), ast.parse(CONSUMER.replace('target=True', 'target=False')),
                                    3, {'class_consumers': {'Example': [0]}}, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}))

    def test_changed_baseline_factory_binding_rejected_even_without_patch(self):
        proof = build_proofs(self.root)
        self.write('sqlglot/factories.py', FACTORY + 'factory = arbitrary\n')
        candidate = FactoryCandidates(self.root, proof)
        self.assertIsNone(candidate(ast.parse(CONSUMER), ast.parse(CONSUMER.replace('target=True', 'target=False')),
                                    3, {'class_consumers': {'Example': [0]}}, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}))

    def test_import_chain_record_cannot_lie_about_binding(self):
        proof = build_proofs(self.root)
        proof['files']['sqlglot/consumer.py']['factory']['import_chain'][0]['imported_name'] = 'other'
        candidate = FactoryCandidates(self.root, proof)
        self.assertIsNone(candidate(ast.parse(CONSUMER), ast.parse(CONSUMER.replace('target=True', 'target=False')),
                                    3, {'class_consumers': {'Example': [0]}}, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}))

    def test_default_selector_still_rejects_factory_calls(self):
        tests = [{'id': 'a', 'number': 10, 'seconds': .1}, {'id': 'b', 'number': 11, 'seconds': .1}]
        mapping = {'test_ids': ['a', 'b'], 'files': {'sqlglot/consumer.py': {'class_consumers': {'Example': [0]}, 'lines': {}}}}
        default = Selector(tests, mapping, self.root, class_guard=lambda patch: True)
        self.assertEqual(default.select(diff('sqlglot/consumer.py', CONSUMER, CONSUMER.replace('target=True', 'target=False'))), {10, 11})

    def test_retained_tree_hash_cache_does_not_cache_commit_eligibility(self):
        candidate = FactoryCandidates(self.root, build_proofs(self.root))
        before = ast.parse(CONSUMER)
        after = ast.parse(CONSUMER.replace('target=True', 'target=False'))
        info = {'class_consumers': {'Example': [0]}}
        with mock_patch('factory_selection.ast_hash', wraps=ast_hash) as hash_node:
            self.assertEqual(candidate(before, after, 3, info, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}), {0})
            first_calls = hash_node.call_count
            self.assertGreater(first_calls, 0)
            self.assertEqual(candidate(before, after, 3, info, 'sqlglot/consumer.py', {'sqlglot/consumer.py'}), {0})
            self.assertEqual(hash_node.call_count, first_calls)
            self.assertIsNone(candidate(before, after, 3, info, 'sqlglot/consumer.py', {'sqlglot/consumer.py', 'sqlglot/factories.py'}))


if __name__ == '__main__':
    unittest.main()
