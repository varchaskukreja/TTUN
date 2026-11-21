import copy
import difflib
import enum
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

import analyze_comparison_gain as gain
from comparison_types import Recorder


class GainTests(unittest.TestCase):
    def test_interpreter_versions_must_match_when_declared(self):
        current = list(sys.version_info[:2])
        self.assertEqual(gain.interpreter_issues({'python_version': current}, {'python_version': current}), [])
        for proofs in ({}, {'python_version': [3, 99]}, {'python_version': '3.13'}, {'python_version': [True, 13]}):
            self.assertTrue(gain.interpreter_issues({}, proofs))
        for mapping in ({'python_version': [3, 99]}, {'capture_report': {'python_version': [3, 99]}}):
            self.assertTrue(gain.interpreter_issues(mapping, {'python_version': current}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = 'sqlglot/example.py'
        self.before = 'def compare(value, other):\n    return value is other\n'
        (self.root / 'sqlglot').mkdir()
        (self.root / self.path).write_text(self.before)
        self.tests = [{'id': f'tests/test_synthetic.py::test_{i}', 'number': i, 'seconds': 10 if i == 0 else .001} for i in range(1620)]
        self.ids = [test['id'] for test in self.tests]
        self.context = [self.ids[0]]
        self.recorder = Recorder(lambda: self.context[0])
        self.namespace = self.recorder.execute(self.before, self.path)
        class Color(enum.Enum):
            RED = 1
            BLUE = 2
        self.namespace['compare'](Color.RED, Color.BLUE)
        self.mapping = self.recorder.snapshot()
        self.mapping.setdefault('read_dependencies', {})
        self.mapping['read_dependencies'].update(source={}, code={}, code_objects={}, code_object_granularity=True)
        self.mapping.update(preexisting_modules={}, ineligible_files=[], capture_complete=True)
        self.mapping['capture_report'] = {
            'errors': [], 'catalog_count': 1620, 'all_catalog_passed': True,
            'catalog_node_ids_sha256': gain.digest(sorted(self.ids)),
            'baseline_outcomes_sha256': gain.digest(dict.fromkeys(self.ids, 'passed'), sort_keys=True),
            'receipt_audit': {'errors': [], 'must_retain': [], 'submitted': 0, 'worker_contexts': {}, 'untracked_child_activity_checked': True},
        }
        self.production = {'test_ids': self.ids, 'files': {self.path: {'read_tests': []}}}
        self.patch = self.diff(self.before.replace(' is ', ' == '))

    def diff(self, after):
        return 'diff --git a/{0} b/{0}\n'.format(self.path) + ''.join(difflib.unified_diff(self.before.splitlines(True), after.splitlines(True), fromfile='a/' + self.path, tofile='b/' + self.path))

    def propose(self, original=(0, 1)):
        return gain.conditional_selection(self.mapping, self.root, self.patch, original, self.tests, self.production)

    def test_exact_observation_removes_only_observed_test(self):
        self.assertEqual(gain.capture_issues(self.mapping, self.tests), [])
        selected, detail = self.propose()
        self.assertEqual(selected, {1})
        self.assertEqual(detail['removed_count'], 1)

    def test_source_readers_retained_and_added(self):
        self.mapping['read_dependencies']['source'][self.path] = [self.ids[0], self.ids[2]]
        selected, detail = self.propose()
        self.assertEqual(selected, {0, 1, 2})
        self.assertEqual(detail['added_reader_count'], 1)

    def test_code_object_reader_retained(self):
        site = next(iter(self.mapping['sites'].values()))
        key, execution = next(iter(site['execution_codes'].items()))
        record = dict(execution['ancestry'][-1], contexts=[self.ids[0]])
        self.mapping['read_dependencies']['code_objects'][key] = record
        self.assertEqual(self.propose()[0], {0, 1})

    def test_production_readers_are_unioned(self):
        self.production['files'][self.path]['read_tests'] = [2]
        self.assertEqual(self.propose()[0], {1, 2})

    def test_missing_failed_and_unknown_production_tests_retained(self):
        for field, value in [('missing_tests', [self.ids[0]]), ('baseline_outcomes', {self.ids[0]: 'failed'}), ('test_ids', self.ids[1:])]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.production)
                self.production[field] = value
                self.assertIn(0, self.propose()[0])
                self.production = original

    def test_collection_read_or_preexisting_module_rejects(self):
        self.mapping['read_dependencies']['source'][self.path] = ['']
        self.assertEqual(self.propose()[0], {0, 1})
        self.mapping['read_dependencies']['source'] = {}
        self.mapping['preexisting_modules']['sqlglot.example'] = self.path
        self.assertEqual(self.propose()[0], {0, 1})

    def test_any_unsafe_pair_rejects_globally(self):
        class Custom:
            def __eq__(self, other):
                return True
        self.context[0] = self.ids[1]
        self.namespace['compare'](Custom(), Custom())
        self.mapping['sites'] = self.recorder.snapshot()['sites']
        self.assertEqual(self.propose()[0], {0, 1})

    def test_extra_comment_or_operand_edit_rejected(self):
        for after in (self.before.replace(' is ', ' == ') + '# comment\n', self.before.replace('value is other', 'other == other')):
            self.patch = self.diff(after)
            self.assertEqual(self.propose()[0], {0, 1})

    def test_unknown_context_incomplete(self):
        next(iter(self.mapping['sites'].values()))['observations'][0]['contexts'].append('@worker-unassigned:123')
        self.assertTrue(gain.capture_issues(self.mapping, self.tests))

    def test_malformed_audits_fail_closed(self):
        for outcomes in ([], {'finished': [{}]}, {'details': None}):
            self.assertTrue(gain.capture_issues(self.mapping, self.tests, outcomes))
        self.mapping['capture_report']['receipt_audit']['worker_contexts'] = {'123': [{}]}
        self.assertTrue(gain.capture_issues(self.mapping, self.tests))

    def test_explicit_sidecar_phases_and_catalog_required(self):
        outcomes = {'exitstatus': 0, 'selected': self.ids, 'collected': self.ids, 'finished': self.ids,
                    'missing': [], 'collection_errors': [], 'outcomes': dict.fromkeys(self.ids, 'passed'),
                    'details': {node: {'phases': {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'},
                                      'failures': [], 'subtests': {}} for node in self.ids}}
        self.assertEqual(gain.capture_issues(self.mapping, self.tests, outcomes), [])
        outcomes['details'][self.ids[0]]['phases']['teardown'] = 'failed'
        self.assertTrue(gain.capture_issues(self.mapping, self.tests, outcomes))

    def test_incomplete_gate_precedes_selector_and_answers(self):
        for mutate in (
            lambda m: m.update(capture_complete=False),
            lambda m: m['capture_report'].pop('receipt_audit'),
            lambda m: m['capture_report'].update(all_catalog_passed=False),
            lambda m: m['capture_report'].update(catalog_node_ids_sha256='wrong'),
        ):
            mapping = copy.deepcopy(self.mapping)
            mutate(mapping)
            selector = Mock()
            result = gain.analyze(mapping, self.root, self.tests, [('x', self.patch)], self.production, selector, answers_path=self.root / 'absent')
            self.assertEqual(result['status'], 'incomplete')
            self.assertTrue(result['no_selected_output'])
            selector.select.assert_not_called()

    def test_complete_analysis_has_counts_not_selection_and_answers_after(self):
        selector = Mock()
        selector.select.return_value = {0, 1}
        def answers(_):
            selector.select.assert_called_once()
            return {'commits': {'x': {'failing': []}}}
        with patch.object(gain, 'read_json', side_effect=answers):
            result = gain.analyze(self.mapping, self.root, self.tests, [('x', self.patch)], self.production, selector, answers_path='answers')
        self.assertEqual(result['status'], 'conditional_estimate')
        self.assertGreater(result['conditional_ci_gain_milliseconds'], 0)
        self.assertNotIn('workers', json.dumps(result))
        self.assertTrue(result['no_selected_output'])
        self.assertFalse(result['runtime_narrowing_authorized'])


if __name__ == '__main__':
    unittest.main()
