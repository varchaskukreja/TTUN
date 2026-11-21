import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

import validate_comparison_robustness as research


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = 'tests/test_one.py::test_one'
        self.report = {'complete': True, 'process_timeout': False, 'process_exitcode': 0,
            'collection_errors': [], 'unresolved': [], 'requested': [self.node], 'collected': [self.node],
            'finished': [self.node], 'collected_order': [self.node], 'outcomes': {self.node: 'passed'},
            'phase_audit': {self.node: {'phases': dict.fromkeys(('setup', 'call', 'teardown'), 'passed'), 'subtests': {'passed': 2}}}, 'wall_seconds': .1}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            payload = b'x = 1\n'; info = tarfile.TarInfo('module.py'); info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        self.archive = stream.getvalue(); self.digest = hashlib.sha256(self.archive).hexdigest()

    def test_scope_audit_rejects_missing_phase_skip_or_partial(self):
        self.assertTrue(research.audit_scope(self.report, {self.node}))
        for alter in (lambda r: r['phase_audit'][self.node]['phases'].pop('teardown'),
                      lambda r: r['phase_audit'][self.node]['subtests'].update(skipped=1),
                      lambda r: r.update(finished=[]), lambda r: r.update(process_timeout=True)):
            report = copy.deepcopy(self.report); alter(report)
            self.assertFalse(research.audit_scope(report, {self.node}))

    def test_cold_aggregation_never_claims_subset_complete(self):
        ids = {f'test::{i}' for i in range(1620)}
        rows = [{'nodeid': node, 'completed_requested_scope': True, 'outcomes': {node: 'passed'}} for node in sorted(ids)]
        self.assertTrue(research.aggregate_cold(rows, ids)['all_passed'])
        self.assertFalse(research.aggregate_cold(rows[:-1], ids)['complete'])
        self.assertFalse(research.aggregate_cold(rows + [rows[0]], ids)['complete'])
        rows[0]['completed_requested_scope'] = False
        self.assertFalse(research.aggregate_cold(rows, ids)['complete'])

    def test_baseline_failures_separate_from_new_failures_and_raw_misses(self):
        result = research.comparison_summary({'old', 'new'}, {'old'}, {'new'})
        self.assertEqual(result['new_failing_ids'], ['new'])
        self.assertEqual(result['raw_conditional_total_missing_ids'], ['old'])
        self.assertEqual(result['baseline_unstable_not_selected_ids'], ['old'])
        self.assertEqual(result['new_failure_missing_ids'], [])
        self.assertEqual(result['missing_after_accounting_for_baseline_ids'], [])

    def test_archive_integrity_and_owned_cleanup(self):
        owned = self.root / 'owned'
        with self.assertRaises(ValueError): research.extract_owned(self.archive, 'wrong', owned, 'token')
        self.assertFalse(owned.exists())
        tree = research.extract_owned(self.archive, self.digest, owned, 'token')
        with self.assertRaises(ValueError): research.cleanup_owned(tree, owned, 'wrong')
        self.assertTrue(tree.exists())
        research.cleanup_owned(tree, owned, 'token'); self.assertFalse(tree.exists())
        outside = self.root / 'node-other'; outside.mkdir(); (outside / '.research-owned-source').write_text('token')
        with self.assertRaises(ValueError): research.cleanup_owned(outside, owned, 'token')
        self.assertTrue(outside.exists())

    def test_scope_cleans_source_and_preserves_report_and_log(self):
        directory = self.root / 'case'; owned = self.root / 'owned'
        def run(tree, catalog, output, timeout, **kwargs):
            self.assertLessEqual(timeout, 180)
            self.assertEqual((tree / 'module.py').read_text(), 'x = 1\n')
            (output / 'pytest.json').write_text(json.dumps(self.report)); (output / 'pytest.log').write_text('log')
            return self.report
        with patch.object(research, 'run_suite', side_effect=run):
            row = research.execute_scope(self.archive, self.digest, 'token', owned, directory,
                self.root/'catalog.json', {self.node}, time.monotonic()+200, node=self.node)
        self.assertTrue(row['completed_requested_scope']); self.assertTrue(row['source_cleaned'])
        self.assertEqual(list(owned.iterdir()), [])
        self.assertTrue((directory/'pytest.json').exists()); self.assertTrue((directory/'pytest.log').exists())

    def test_error_or_mutation_integrity_failure_still_cleans_owned_source(self):
        owned = self.root / 'owned'
        with patch.object(research, 'apply_verified', side_effect=ValueError('preimage mismatch')), patch.object(research, 'run_suite') as run:
            row = research.execute_scope(self.archive, self.digest, 'token', owned, self.root/'case',
                self.root/'catalog.json', {self.node}, time.monotonic()+30, item={'patch_sha256':'patch'}, node=self.node)
        run.assert_not_called(); self.assertFalse(row['completed_requested_scope']); self.assertTrue(row['source_cleaned'])
        self.assertEqual(list(owned.iterdir()), [])

    def test_exhausted_budget_launches_no_pytest(self):
        with patch.object(research, 'run_suite') as run:
            row = research.execute_scope(self.archive, self.digest, 'token', self.root/'owned', self.root/'case',
                self.root/'catalog.json', {self.node}, time.monotonic()-1, node=self.node)
        run.assert_not_called(); self.assertFalse(row['completed_requested_scope'])
        self.assertIn('budget exhausted', row['error'])

    def test_cold_scheduler_bounds_primary_jobs_to_two(self):
        driver = research.ResearchDriver.__new__(research.ResearchDriver)
        driver.output = self.root
        driver.tests = [{'id': f'node::{i}'} for i in range(6)]
        driver.ids = {test['id'] for test in driver.tests}
        driver.deadline = time.monotonic() + 10
        lock = threading.Lock(); counts = {'live': 0, 'peak': 0, 'calls': 0}
        def scope(directory, item=None, node=None):
            with lock:
                counts['live'] += 1; counts['calls'] += 1
                counts['peak'] = max(counts['peak'], counts['live'])
            time.sleep(.01)
            with lock: counts['live'] -= 1
            return {'nodeid': node, 'completed_requested_scope': True, 'outcomes': {node: 'passed'}}
        driver.scope = scope
        result = driver.cold_case('bounded')
        self.assertEqual(counts['calls'], 6); self.assertEqual(counts['peak'], 2)
        self.assertFalse(result['complete'])  # Six nodes are never a full-catalog claim.

    def test_cold_baseline_failure_requires_review_before_mutants(self):
        driver = research.ResearchDriver.__new__(research.ResearchDriver)
        driver.output = self.root; driver.authorize = lambda: None
        directory = self.root/'cold/baseline'; directory.mkdir(parents=True)
        research.write_json(directory/'summary.json', {'complete': True, 'failing_ids': ['failed']})
        with patch.object(driver, 'cold_case') as run:
            with self.assertRaises(FileNotFoundError): driver.cold_mutants()
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
