import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiment_guard_pool as pool


class ParentChecks(unittest.TestCase):
    def test_target_modules_reject_before_thread_check(self):
        with patch.dict(sys.modules, {'sqlglot.fake': object()}):
            with self.assertRaisesRegex(RuntimeError, 'target modules'):
                pool.clean_parent()

    def test_native_threads_reject(self):
        with patch.object(Path, 'iterdir', return_value=iter([Path('1'), Path('2')])):
            with self.assertRaisesRegex(RuntimeError, '2 native threads'):
                pool.clean_parent()

    def test_clean_single_thread(self):
        with patch.object(Path, 'iterdir', return_value=iter([Path('1')])):
            self.assertEqual(pool.clean_parent(), 1)


class OutputChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / 'report'
        self.log = Path(self.directory.name) / 'log'
        self.log.write_text('')

    def test_ambiguous_or_truncated_report_rejects(self):
        for value in ('{"complete":true,"complete":false}', '{', '{"value":NaN}'):
            with self.subTest(value=value):
                self.output.write_text(value)
                self.assertEqual(pool.read_report(self.output, self.log), ({}, None))

    def test_combined_output_limit(self):
        self.output.write_text('{}')
        self.log.write_text('123456789')
        with patch.object(pool, 'MAX_CAPTURE_BYTES', 10):
            self.assertEqual(pool.read_report(self.output, self.log), ({}, 11))

    def test_valid_json_preserved(self):
        report = {'complete': False, 'reasons': ['intentional import error']}
        self.output.write_text(json.dumps(report))
        self.assertEqual(pool.read_report(self.output, self.log)[0], report)


if __name__ == '__main__':
    unittest.main()
