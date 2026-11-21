"""Boundary tests for exploratory pass evidence; no production selection rules."""
import unittest
from pathlib import Path
import tempfile

from experiment_refinement import explicit_cold_pass


def good_report():
    return {'complete': True, 'collect_only': False, 'requested': 1, 'collected': 1, 'finished': 1,
            'pytest_exitcode': 0, 'failing': [], 'collection_failed': [], 'collection_errors': [],
            'patch_import_errors': [], 'python_patches_not_loaded': [], 'reasons': [],
            'phase_outcomes': {'test': [{'when': phase, 'outcome': 'passed', 'wasxfail': False}
                                      for phase in ('setup', 'call', 'teardown')]}}


class RefinementEvidenceTests(unittest.TestCase):
    def test_explicit_pass(self):
        self.assertTrue(explicit_cold_pass(good_report(), ['test']))

    def test_existing_probe_schema_is_not_explicit_pass_evidence(self):
        report = good_report()
        del report['phase_outcomes']
        self.assertFalse(explicit_cold_pass(report, ['test']))

    def test_skip_xfail_and_teardown_error_rejected(self):
        for phase in ('setup', 'call', 'teardown'):
            for outcome in ('failed', 'skipped'):
                report = good_report()
                next(item for item in report['phase_outcomes']['test'] if item['when'] == phase)['outcome'] = outcome
                self.assertFalse(explicit_cold_pass(report, ['test']))
        report = good_report()
        report['phase_outcomes']['test'][1]['wasxfail'] = True
        self.assertFalse(explicit_cold_pass(report, ['test']))

    def test_missing_or_incomplete_phase_rejected(self):
        for phase in ('setup', 'call', 'teardown'):
            report = good_report()
            report['phase_outcomes']['test'] = [item for item in report['phase_outcomes']['test'] if item['when'] != phase]
            self.assertFalse(explicit_cold_pass(report, ['test']))

    def test_wrong_counts_failures_and_unsupported_overlay_rejected(self):
        for key, value in [('collected', 0), ('finished', True), ('requested', 2), ('complete', False),
                           ('reasons', ['unsupported child process']), ('failing', ['test']), ('pytest_exitcode', 1),
                           ('python_patches_not_loaded', ['sqlglot/example.py'])]:
            report = good_report()
            report[key] = value
            self.assertFalse(explicit_cold_pass(report, ['test']))

    def test_cold_pass_does_not_imply_warm_suite_pass(self):
        baseline = 'state = 0\ndef initialize():\n    global state\n    state = 0\ndef check():\n    assert state == 0\n'
        changed = baseline.replace('    state = 0', '    state = 1')
        cold = {}
        exec(changed, cold)
        cold['check']()  # Explicit isolated PASS.
        warm = {}
        exec(changed, warm)
        warm['initialize']()  # A prior test or shared fixture changes state.
        with self.assertRaises(AssertionError):
            warm['check']()

    def test_known_overlay_metadata_gap_stat_sees_baseline_size(self):
        from runtime_probe import Overlay
        with tempfile.TemporaryDirectory() as root:
            fixture = Path(root, 'fixture.txt')
            fixture.write_text('a')
            with Overlay({str(fixture): b'aa'}).install():
                self.assertEqual(fixture.read_text(), 'aa')
                self.assertEqual(fixture.stat().st_size, 1)  # Cold overlay assertion passes.
            fixture.write_text('aa')
            self.assertEqual(fixture.stat().st_size, 2)  # Real patched filesystem differs.


if __name__ == '__main__':
    unittest.main()
