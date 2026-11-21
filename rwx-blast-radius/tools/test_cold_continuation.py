import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import continue_comparison_cold as c


class BaselineIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.ids = {f'test::{i}' for i in range(1620)}
        cls.runtime = {'python': 'test'}
        c.write_json(cls.root/'freeze.json', {'runtime': cls.runtime,
            'driver_sha256': c.sha(c.frozen_driver.__file__), 'execution_helper_sha256': {},
            'source': {'archive_sha256': 'archive'}, 'catalog_sha256': 'catalog', 'plan_sha256': 'plan'})
        c.write_json(cls.root/'review.json', {'reviewed': True, 'freeze_sha256': c.sha(cls.root/'freeze.json')})
        rows = []
        directory = cls.root/'cold/baseline'
        for i, node in enumerate(sorted(cls.ids)):
            d = directory/f'node-{i:04d}'; d.mkdir(parents=True)
            report = {'complete': True, 'process_timeout': False, 'process_exitcode': 0,
                'collection_errors': [], 'unresolved': [], 'outcomes': {node: 'passed'},
                'phase_audit': {node: {'phases': dict.fromkeys(['setup','call','teardown'], 'passed'), 'subtests': {'passed': 1}}}}
            report.update({key: [node] for key in ('requested','collected','finished','collected_order')})
            c.write_json(d/'pytest.json', report); (d/'pytest.log').write_text('ok')
            row = {'nodeid': node, 'source_cleaned': True, 'patch_sha256': None, 'scope_count': 1,
                'archive_sha256': 'archive', 'report': str(d/'pytest.json'), 'report_sha256': c.sha(d/'pytest.json'),
                'log_sha256': c.sha(d/'pytest.log'), 'completed_requested_scope': True, 'outcomes': report['outcomes']}
            c.write_json(d/'receipt.json', row); rows.append(row)
        summary = c.aggregate_cold(rows, cls.ids)
        summary['per_node_receipts_sha256'] = {str(p.relative_to(directory)): c.sha(p) for p in directory.glob('node-*/receipt.json')}
        c.write_json(directory/'summary.json', summary)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_1620_verified(self):
        result = c.verify_baseline(self.root, self.ids, self.runtime)
        self.assertEqual(len(result['report_sha256']), 1620)
        self.assertEqual(result['failing_ids'], [])

    def test_environment_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'runtime/environment'):
            c.verify_baseline(self.root, self.ids, {'python': 'different'})

    def test_mutant_already_attempted(self):
        p = self.root/'cold/mutant-0'; p.mkdir()
        try:
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                c.verify_baseline(self.root, self.ids, self.runtime)
        finally:
            p.rmdir()

    def test_changed_report_and_log_rejected(self):
        for name in ('pytest.json', 'pytest.log', 'receipt.json'):
            p = self.root/'cold/baseline/node-0000'/name; original = p.read_bytes()
            try:
                p.write_bytes(original+b' ')
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    c.verify_baseline(self.root, self.ids, self.runtime)
            finally:
                p.write_bytes(original)

    def test_partial_summary_rejected(self):
        p = self.root/'cold/baseline/summary.json'; original = p.read_bytes()
        try:
            value = json.loads(original); value['complete'] = False; c.write_json(p, value)
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                c.verify_baseline(self.root, self.ids, self.runtime)
        finally:
            p.write_bytes(original)

    def test_phase_audit_still_required_when_hashes_match(self):
        with patch.object(c, 'audit_scope', return_value=False):
            with self.assertRaisesRegex(ValueError, 'phase/outcome'):
                c.verify_baseline(self.root, self.ids, self.runtime)


class ContinuationGates(unittest.TestCase):
    def test_no_review_no_execution(self):
        driver = object.__new__(c.Continuation)
        with tempfile.TemporaryDirectory() as directory:
            driver.output = Path(directory)
            c.write_json(driver.output/'review.json', {'reviewed': False})
            with self.assertRaisesRegex(ValueError, 'root review'):
                driver.run()
            self.assertFalse((driver.output/'results.json').exists())

    def test_exception_keeps_results_incomplete(self):
        driver = object.__new__(c.Continuation)
        with tempfile.TemporaryDirectory() as directory:
            driver.output = Path(directory); driver.baseline = {'summary_sha256': 'hash', 'failing_ids': []}
            driver.items = [{'site':'first','conditional_ids':[]}, {'site':'second','conditional_ids':[]}]
            first = {'complete': True, 'failing_ids': []}
            with patch.object(driver, 'authorize'), patch.object(driver, 'cold_case', side_effect=[first, RuntimeError('stop')]):
                with self.assertRaises(RuntimeError): driver.run()
            result = json.loads((driver.output/'results.json').read_text())
            self.assertFalse(result['complete']); self.assertEqual(len(result['mutations']), 1)
            self.assertEqual(result['expected_mutations'], 2)

if __name__ == '__main__': unittest.main()
