"""Independent context/provenance checks for the baseline union pipeline."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from tools.merge_isolated_maps import added_links, audit_work, context_indices, dependency_payload, provenance, successful


IDS = {'tests/test_a.py::Case::test_one': 0,
       'tests/test_a.py::Case::test_two': 1,
       'tests/test_b.py::Other::test_three': 2}


class ContextTests(unittest.TestCase):
    def test_collection_context_never_becomes_a_test_dependency(self):
        self.assertEqual(context_indices('', IDS), set())

    def test_fixture_scope_is_limited_to_its_actual_class(self):
        self.assertEqual(context_indices('@scope|class|tests/test_a.py::Case', IDS), {0, 1})
        self.assertEqual(context_indices('@scope|class|tests/test_a.py::CaseExtra', IDS), set())
        self.assertEqual(context_indices('@scope|session|', IDS), {0, 1, 2})

    def test_unknown_contexts_are_rejected_instead_of_silently_ignored(self):
        with self.assertRaises(ValueError):
            context_indices('unknown-test', IDS)

    def test_added_links_count_only_new_line_test_edges(self):
        original = {'files': {'sqlglot/x.py': {'lines': {'10': [0, 1], '11': [2]}}}}
        union = {'files': {'sqlglot/x.py': {'lines': {'10': [0, 1, 2], '11': [2], '12': [0]}}}}
        self.assertEqual(added_links(original, union), {('sqlglot/x.py', 10): {2}, ('sqlglot/x.py', 12): {0}})

    def test_failed_subtest_cannot_be_cleared_by_passed_main_call(self):
        node = next(iter(IDS))
        row = {'nodeid': node, 'results': {node: 'failed'}, 'call_outcomes': {node: 'passed'}}
        self.assertFalse(successful(row))

    def test_payload_equivalence_excludes_only_explanatory_metadata(self):
        left = {'files': {'x': 1}, 'baseline_outcomes': {'test': 'passed'}, 'isolated_baseline_supplement': {'old': True}}
        right = {**left, 'isolated_baseline_supplement': {'new': True}}
        self.assertEqual(dependency_payload(left), dependency_payload(right))
        right['baseline_outcomes'] = {'test': 'failed'}
        self.assertNotEqual(dependency_payload(left), dependency_payload(right))
        row['results'][node] = 'passed'
        self.assertTrue(successful(row))
        row['errors'] = ['disk failure']
        self.assertFalse(successful(row))


@unittest.skipUnless(importlib.util.find_spec('coverage'), 'coverage.py needed for raw artifact tests')
class RawCoverageTests(unittest.TestCase):
    def setUp(self):
        from coverage import CoverageData
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.path = self.work / 'baseline.coverage.synthetic'
        self.data = CoverageData(basename=str(self.path))
        self.source = str(self.work / 'source/sqlglot/x.py')

    def test_single_shard_cannot_mix_independent_test_contexts(self):
        for node in list(IDS)[:2]:
            self.data.set_context(node)
            self.data.add_lines({self.source: {10}})
        self.data.write()
        with self.assertRaisesRegex(ValueError, 'cross-test'):
            audit_work(self.work, IDS)

    def test_added_links_have_direct_or_fixture_evidence_not_collection(self):
        self.data.set_context(next(iter(IDS)))
        self.data.add_lines({self.source: {10}})
        self.data.set_context('@scope|class|tests/test_a.py::Case')
        self.data.add_lines({self.source: {10}})
        self.data.set_context('')
        self.data.add_lines({self.source: {11}})
        self.data.write()
        report = provenance({('sqlglot/x.py', 10): {0, 1}}, [self.path], IDS)
        self.assertEqual(report['counts'], {'direct_only': 0, 'scope_only': 1, 'both': 1, 'unexplained': 0})
        with self.assertRaisesRegex(ValueError, 'lack raw context evidence'):
            provenance({('sqlglot/x.py', 11): {0}}, [self.path], IDS)


if __name__ == '__main__':
    unittest.main()
