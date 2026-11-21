import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comparison_types import Recorder
from merge_comparison_maps import merge_snapshots, merge_capture


def passed_report():
    return {'selected': ['a'], 'collected': ['a'], 'finished': ['a'], 'missing': [],
            'collection_errors': [], 'outcomes': {'a': 'passed'}, 'exitstatus': 0,
            'details': {'a': {'phases': {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'},
                              'subtests': {}, 'failures': []}}}


class MergeTests(unittest.TestCase):
    def snapshot(self, context):
        recorder = Recorder(lambda: context, root='/baseline')
        recorder.execute('result = value is None\n', path='sqlglot/a.py', globals_={'value': None})
        mapping = recorder.snapshot()
        mapping['read_dependencies']['source']['sqlglot/a.py'] = [context]
        mapping['read_dependencies']['code']['sqlglot/a.py'] = [context]
        site = next(iter(mapping['sites'].values()))
        key, execution = next(iter(site['execution_codes'].items()))
        metadata = {k: v for k, v in execution['ancestry'][-1].items() if k != 'key'}
        mapping['read_dependencies']['code_objects'][key] = dict(metadata, contexts=[context])
        return mapping

    def test_union_preserves_pairs_code_and_readers(self):
        first, second = self.snapshot('a'), self.snapshot('b')
        result = merge_snapshots([('first', first), ('second', second)], ['a', 'b'], {})
        self.assertEqual(result['merge_errors'], [])
        site = next(iter(result['sites'].values()))
        self.assertEqual(site['observations'][0]['contexts'], ['a', 'b'])
        self.assertEqual(next(iter(site['execution_codes'].values()))['contexts'], ['a', 'b'])
        self.assertEqual(result['read_dependencies']['source']['sqlglot/a.py'], ['a', 'b'])
        self.assertEqual(next(iter(result['read_dependencies']['code_objects'].values()))['contexts'], ['a', 'b'])
        self.assertFalse(result['narrowing_authorized'])

    def test_worker_expansion_and_collection_global(self):
        result = merge_snapshots([('worker', self.snapshot('@worker-unassigned:23')), ('parent', self.snapshot(''))],
                                 ['a', 'b'], {'23': ['a', 'b']})
        self.assertEqual(result['merge_errors'], [])
        self.assertEqual(next(iter(result['sites'].values()))['observations'][0]['contexts'], ['', 'a', 'b'])

    def test_preexisting_and_ineligible_union(self):
        first, second = self.snapshot('a'), self.snapshot('b')
        first['preexisting_modules'] = {'sqlglot.old': 'sqlglot/old.py'}
        second['ineligible_files'] = ['sqlglot/another.py']
        result = merge_snapshots([('1', first), ('2', second)], ['a', 'b'], {})
        self.assertEqual(result['ineligible_files'], ['sqlglot/another.py', 'sqlglot/old.py'])
        self.assertEqual(result['preexisting_modules'], first['preexisting_modules'])

    def test_unknown_context_and_conflicts_incomplete(self):
        first = self.snapshot('a')
        for change in ('hash', 'operator', 'ancestry', 'unknown_context', 'missing_readers'):
            second = copy.deepcopy(first)
            site = next(iter(second['sites'].values()))
            if change == 'hash': site['source_sha256'] = 'f' * 64
            if change == 'operator': site['operator'] = 'Eq'
            if change == 'ancestry': next(iter(site['execution_codes'].values()))['ancestry'][0]['span_last_line'] += 1
            if change == 'unknown_context': site['observations'][0]['contexts'] = ['unknown']
            if change == 'missing_readers': del second['read_dependencies']
            with self.subTest(change=change):
                result = merge_snapshots([('1', first), ('2', second)], ['a'], {})
                self.assertTrue(result['merge_errors'])
                self.assertFalse(result['capture_complete'])

    def test_missing_and_malformed_files_global_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            catalog = directory / 'catalog.json'
            outcomes = directory / 'outcomes.json'
            parent = directory / 'parent.json'
            catalog.write_text(json.dumps({'tests': [{'id': 'a'}]}))
            outcomes.write_text(json.dumps(passed_report()))
            for contents in (None, '{broken', '{"format":1,"format":1}'):
                if contents is not None: parent.write_text(contents)
                with self.subTest(contents=contents):
                    result = merge_capture(parent, directory, catalog, outcomes, required_count=1)
                    self.assertFalse(result['capture_complete'])
                    self.assertTrue(result['capture_report']['errors'])

    def test_complete_requires_finished_and_teardown(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'parent').write_text(json.dumps(self.snapshot('a')))
            (directory / 'catalog').write_text(json.dumps({'tests': [{'id': 'a'}]}))
            receipt = {'errors': [], 'must_retain': [], 'worker_contexts': {}, 'submitted': 1}
            for change in ('none', 'finished', 'teardown'):
                report = passed_report()
                if change == 'finished': report['finished'] = []
                if change == 'teardown': del report['details']['a']['phases']['teardown']
                (directory / 'outcomes').write_text(json.dumps(report))
                with patch('merge_comparison_maps.audit_receipts', return_value=receipt):
                    result = merge_capture(directory / 'parent', directory, directory / 'catalog', directory / 'outcomes', required_count=1)
                self.assertEqual(result['capture_complete'], change == 'none')
                self.assertFalse(result['capture_report']['source_reader_coverage_complete'])
                self.assertIn('catalog_node_ids_sha256', result['capture_report'])

    def test_receipt_gap_global_even_all_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name, value in [('parent', self.snapshot('a')), ('catalog', {'tests': [{'id': 'a'}]}),
                                ('outcomes', passed_report())]:
                (directory / name).write_text(json.dumps(value))
            receipt = {'errors': [], 'must_retain': ['a'], 'worker_contexts': {}, 'submitted': 1}
            with patch('merge_comparison_maps.audit_receipts', return_value=receipt):
                result = merge_capture(directory / 'parent', directory, directory / 'catalog', directory / 'outcomes', required_count=1)
            self.assertFalse(result['capture_complete'])
            self.assertTrue(result['capture_report']['all_catalog_passed'])
            self.assertTrue(result['capture_report']['errors'])


if __name__ == '__main__':
    unittest.main()
