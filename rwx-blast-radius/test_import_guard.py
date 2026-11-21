"""Fail-closed tests against the runtime_probe collection report schema."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from import_guard import ImportGuard, MAX_CAPTURE_BYTES


TESTS = [{'id': 'tests/test_example.py::TestExample::test_one'},
         {'id': 'tests/test_example.py::TestExample::test_two'}]


def success_report():
    # Matches runtime_probe.run(..., collect_only=True, import_patches=True).
    return {'complete': True, 'failing': [], 'requested': 2, 'collected': 2,
            'collect_only': True, 'finished': 0, 'collection_failed': [],
            'collection_errors': [], 'explicit_imports': ['sqlglot.dialects.example'],
            'patch_import_errors': [], 'python_patches_loaded': ['sqlglot/dialects/example.py'],
            'python_patches_not_loaded': [], 'pytest_exitcode': 0, 'reasons': [],
            'cpu_seconds': .2, 'execution_cpu_seconds': .15}


class ImportGuardTests(unittest.TestCase):
    def run_report(self, report, *, returncode=0, stderr=''):
        process = subprocess.CompletedProcess(['probe'], returncode, json.dumps(report), stderr)
        guard = ImportGuard('/baseline', TESTS)
        with patch('import_guard.subprocess.run', return_value=process):
            valid = guard('patch')
        return valid

    def test_success_uses_complete_catalog_and_exact_probe_options(self):
        guard = ImportGuard('/baseline', TESTS)
        def run(command, **kwargs):
            self.assertIn('--collect-only', command)
            self.assertIn('--import-patches', command)
            self.assertEqual(command[command.index('--nodes') + 1], '-')
            self.assertEqual(json.loads(kwargs['input']), [t['id'] for t in TESTS])
            self.assertEqual(kwargs['timeout'], 8)
            self.assertEqual(Path(command[command.index('--patch') + 1]).read_text(), 'patch content')
            self.assertTrue(kwargs['capture_output'])
            return subprocess.CompletedProcess(command, 0, json.dumps(success_report()), 'pytest diagnostics')
        with patch('import_guard.subprocess.run', side_effect=run):
            self.assertTrue(guard('patch content'))
        self.assertEqual(guard.calls[0]['valid'], True)

    def test_every_authoritative_field_is_required(self):
        for field in set(success_report()) - {'cpu_seconds', 'execution_cpu_seconds'}:
            with self.subTest(missing=field):
                report = success_report()
                del report[field]
                self.assertFalse(self.run_report(report))

    def test_wrong_counts_and_boolean_integer_impostors(self):
        for field in ('requested', 'collected', 'finished', 'pytest_exitcode'):
            for value in (-1, 3, False, True, '0', 0.0, None):
                with self.subTest(field=field, value=value):
                    report = success_report()
                    report[field] = value
                    self.assertFalse(self.run_report(report))

    def test_completion_and_collect_only_must_be_true_booleans(self):
        for field in ('complete', 'collect_only'):
            for value in (False, 1, 'true', [], None):
                report = success_report()
                report[field] = value
                self.assertFalse(self.run_report(report))

    def test_any_failure_error_or_unloaded_patch_rejected(self):
        for field in ('failing', 'collection_failed', 'collection_errors',
                      'patch_import_errors', 'python_patches_not_loaded', 'reasons'):
            for value in (['problem'], False, '', None, {}):
                with self.subTest(field=field, value=value):
                    report = success_report()
                    report[field] = value
                    self.assertFalse(self.run_report(report))

    def test_nonempty_python_loaded_and_explicit_imports_required(self):
        for field in ('python_patches_loaded', 'explicit_imports'):
            for value in ([], [''], [None], [1], 'module', None):
                report = success_report()
                report[field] = value
                self.assertFalse(self.run_report(report))

    def test_nonzero_process_exit_rejected_even_with_success_json(self):
        self.assertFalse(self.run_report(success_report(), returncode=1))

    def test_invalid_json_nonobject_duplicate_fields_and_nonfinite_values(self):
        for stdout in ('not json', 'null', '[]', 'true', '{"complete":true,"complete":false}', '{"cpu_seconds":NaN}'):
            with self.subTest(stdout=stdout), patch('import_guard.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout, '')):
                self.assertFalse(ImportGuard('/baseline', TESTS)('patch'))

    def test_timeout_and_oserror_fall_back(self):
        for exc in (subprocess.TimeoutExpired('probe', 8), OSError('cannot start')):
            with self.subTest(exc=exc), patch('import_guard.subprocess.run', side_effect=exc):
                guard = ImportGuard('/baseline', TESTS)
                self.assertFalse(guard('patch'))
                self.assertFalse(guard.calls[-1]['valid'])

    def test_temporary_directory_error_falls_back(self):
        with patch('import_guard.tempfile.TemporaryDirectory', side_effect=OSError('full')):
            self.assertFalse(ImportGuard('/baseline', TESTS)('patch'))

    def test_stdout_and_stderr_combined_byte_limit(self):
        report = success_report()
        # Non-ASCII verifies this is a byte limit, not a character limit.
        self.assertFalse(self.run_report(report, stderr='é' * (MAX_CAPTURE_BYTES // 2)))
        report['extra'] = 'x' * MAX_CAPTURE_BYTES
        self.assertFalse(self.run_report(report))

    def test_exhausted_budget_does_not_start_probe(self):
        guard = ImportGuard('/baseline', TESTS, budget=.5)
        with patch('import_guard.subprocess.run') as run:
            self.assertFalse(guard('patch'))
            run.assert_not_called()

    def test_budget_is_cumulative_and_bounds_last_timeout(self):
        guard = ImportGuard('/baseline', TESTS, budget=10)
        result = subprocess.CompletedProcess([], 0, json.dumps(success_report()), '')
        with patch('import_guard.time.monotonic', side_effect=[100, 108, 500, 502]), patch('import_guard.subprocess.run', return_value=result) as run:
            self.assertTrue(guard('first'))
            self.assertTrue(guard('second'))
            self.assertFalse(guard('third'))
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [8, 2])
        self.assertEqual(guard.wall_seconds, 10)

    def test_timed_out_attempts_consume_budget(self):
        guard = ImportGuard('/baseline', TESTS, budget=8)
        with patch('import_guard.time.monotonic', side_effect=[100, 108]), patch('import_guard.subprocess.run', side_effect=subprocess.TimeoutExpired('probe', 8)) as run:
            self.assertFalse(guard('first'))
            self.assertFalse(guard('second'))
            self.assertEqual(run.call_count, 1)

    def test_invalid_empty_or_duplicate_catalog_never_probed(self):
        for tests in ([], [TESTS[0], TESTS[0]], [{'id': ''}]):
            with patch('import_guard.subprocess.run') as run:
                self.assertFalse(ImportGuard('/baseline', tests)('patch'))
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
