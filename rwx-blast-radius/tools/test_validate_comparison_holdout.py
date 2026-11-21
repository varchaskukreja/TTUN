import copy
from pathlib import Path
import unittest
from unittest.mock import Mock

import test_analyze_comparison_gain as fixtures
from comparison_types import Recorder
from validate_comparison_holdout import operator_patch, plan_holdouts


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GainTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.samples = f.root / 'samples'
        self.samples.mkdir()
        for path, safe in [('sqlglot/second.py', True), ('sqlglot/unsafe.py', False)]:
            (f.root / path).write_text(f.before)
            if safe:
                site = copy.deepcopy(next(iter(f.mapping['sites'].values())))
                site['path'] = path
                site['execution_codes'] = {key.replace(f.path, path): value for key, value in site['execution_codes'].items()}
                for execution in site['execution_codes'].values():
                    for ancestor in execution['ancestry']:
                        ancestor['path'] = path
                        ancestor['key'] = ancestor['key'].replace(f.path, path)
                key = next(iter(f.mapping['sites'])).replace(f.path, path)
                f.mapping['sites'][key] = site
            else:
                class Custom:
                    def __eq__(self, other):
                        return True
                recorder = Recorder(lambda: f.ids[0])
                recorder.execute(f.before, path)['compare'](Custom(), Custom())
                f.mapping['sites'].update(recorder.snapshot()['sites'])
        self.selector = Mock()
        self.selector.select.return_value = {0, 1}

    def plan(self):
        f = self.fixture
        return plan_holdouts(f.mapping, f.root, f.tests, f.production, self.selector, self.samples, 123)

    def test_deterministic_two_eligible_and_unsafe_retained(self):
        first, second = self.plan(), self.plan()
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'planned')
        self.assertEqual([row['kind'] for row in first['mutations']], ['eligible', 'eligible', 'globally_unsafe_pair'])
        unsafe = first['mutations'][-1]
        self.assertEqual(unsafe['production_ids'], unsafe['conditional_ids'])
        for row in first['mutations']:
            self.assertFalse(row['full_catalog_run_performed'])
            self.assertIsNone(row['actual_failing_ids'])

    def test_every_sample_file_excluded(self):
        (self.samples / 'sample.patch').write_text(self.fixture.patch)
        result = self.plan()
        self.assertEqual(result['status'], 'insufficient_candidates')
        self.assertNotIn(self.fixture.path, [row['path'] for row in result['mutations']])

    def test_incomplete_capture_does_not_select(self):
        self.fixture.mapping['capture_complete'] = False
        result = self.plan()
        self.assertEqual(result['status'], 'incomplete')
        self.selector.select.assert_not_called()

    def test_operator_patch_uses_exact_ast_site(self):
        f = self.fixture
        site = next(iter(f.mapping['sites'].values()))
        self.assertEqual(operator_patch(f.root, site), f.patch)
        wrong = dict(site, column=999)
        self.assertIsNone(operator_patch(f.root, wrong))


if __name__ == '__main__':
    unittest.main()
