import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import types

import run_comparison_holdouts as runner
import validate_holdout as physical
import test_validate_comparison_holdout as fixtures


class RunnerTests(unittest.TestCase):
    def test_runtime_identity_survives_json_receipt_roundtrip(self):
        identity = runner.runtime_identity()
        self.assertEqual(json.loads(json.dumps(identity)), identity)

    def test_plan_provenance_survives_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'map.json').write_text('{}')
            (root / 'proofs.json').write_text('{}')
            samples = root / 'samples'; samples.mkdir()
            (samples / 'sample.patch').write_text('example')
            with patch.object(runner, 'source_identity', return_value={'revision': 'pin', 'archive_sha256': 'sha'}):
                provenance = runner.plan_provenance(root, root / 'map.json', root / 'proofs.json', samples)
            self.assertEqual(json.loads(json.dumps(provenance)), provenance)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ids = {f'test::{i}' for i in range(1620)}
        self.report = {'complete': True, 'process_timeout': False, 'process_exitcode': 0,
                       'collected_order': sorted(self.ids), 'collection_order_policy': 'normal', 'collection_order_seed': 0,
                       'requested': sorted(self.ids), 'collected': sorted(self.ids), 'finished': sorted(self.ids),
                       'outcomes': dict.fromkeys(self.ids, 'passed'), 'collection_errors': [], 'unresolved': []}
        self.report['phase_audit'] = {node: {'phases': {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}, 'subtests': {}} for node in self.ids}

    def test_exact_results_require_every_test_and_no_skips(self):
        self.assertTrue(runner.complete_result(self.report, self.ids, baseline=True))
        for key, value in [('finished', sorted(self.ids)[:-1]), ('process_exitcode', True),
                           ('process_timeout', True), ('collection_errors', ['error']), ('requested', sorted(self.ids) + [next(iter(self.ids))])]:
            report = dict(self.report, **{key: value})
            self.assertFalse(runner.complete_result(report, self.ids))
        self.report['outcomes'][next(iter(self.ids))] = 'skipped'
        self.assertFalse(runner.complete_result(self.report, self.ids))

    def test_baseline_failed_rejected_but_complete_mutant_failed_allowed(self):
        self.report['process_exitcode'] = 1
        node = next(iter(self.ids))
        self.report['outcomes'][node] = 'failed'
        self.report['phase_audit'][node]['phases']['call'] = 'failed'
        self.assertFalse(runner.complete_result(self.report, self.ids, baseline=True))
        self.assertTrue(runner.complete_result(self.report, self.ids))

    def test_skipped_subtest_and_missing_phase_rejected(self):
        node = next(iter(self.ids))
        self.report['phase_audit'][node]['subtests'] = {'passed': 2, 'skipped': 1}
        self.assertFalse(runner.complete_result(self.report, self.ids, baseline=True))
        self.assertFalse(runner.complete_result(self.report, self.ids))
        self.report['phase_audit'][node]['subtests'] = {}
        self.report['phase_audit'][node]['phases'].pop('teardown')
        self.assertFalse(runner.complete_result(self.report, self.ids))

    def test_setup_failure_has_no_call_but_requires_teardown(self):
        node = next(iter(self.ids))
        self.report['process_exitcode'] = 1
        self.report['outcomes'][node] = 'failed'
        self.report['phase_audit'][node]['phases'] = {'setup': 'failed', 'teardown': 'passed'}
        self.assertTrue(runner.complete_result(self.report, self.ids))
        self.report['phase_audit'][node]['phases'].pop('teardown')
        self.assertFalse(runner.complete_result(self.report, self.ids))

    def test_physical_plugin_retains_skipped_subtest_after_passed_call(self):
        node = next(iter(self.ids))
        subtest = type('SubtestReport', (types.SimpleNamespace,), {})
        with patch.object(physical, '_requested', {node}), patch.object(physical, '_outcomes', {}), patch.object(physical, '_details', {}), patch.object(physical, '_finished', set()), patch.object(physical, '_phase_audit', {}):
            physical.pytest_runtest_logreport(subtest(nodeid=node, when='call', outcome='skipped', failed=False, skipped=True))
            for phase in ('setup', 'call', 'teardown'):
                physical.pytest_runtest_logreport(types.SimpleNamespace(nodeid=node, when=phase, outcome='passed', failed=False, skipped=False))
            self.assertEqual(physical._outcomes[node], 'passed')  # Legacy summary compatibility.
            self.assertEqual(physical._phase_audit[node]['subtests'], {'skipped': 1})
            self.report['phase_audit'][node] = copy.deepcopy(physical._phase_audit[node])
        self.assertFalse(runner.complete_result(self.report, self.ids))

    def test_review_binds_plan_capture_and_complete_gain(self):
        gain = {'status': 'conditional_estimate', 'conditional_analysis': True, 'comparison_capture_sha256': 'capture'}
        review = {'reviewed': True, 'plan_sha256': 'plan', 'comparison_capture_sha256': 'capture', 'gain_report_sha256': 'gain'}
        runner.check_review(review, 'plan', 'capture', 'gain', gain)
        for field in review:
            altered = dict(review); altered.pop(field)
            with self.assertRaises(ValueError):
                runner.check_review(altered, 'plan', 'capture', 'gain', gain)
        with self.assertRaises(ValueError):
            runner.check_review(review, 'plan', 'capture', 'gain', dict(gain, status='incomplete'))

    def test_baseline_receipt_hash_runtime_and_catalog_bound(self):
        report_path, receipt_path = self.root / 'report.json', self.root / 'receipt.json'
        report_path.write_text(json.dumps(self.report))
        identity = {'source': 'pinned', 'runtime': '3.13', 'catalog_sha256': 'catalog', 'order': {'policy': 'normal', 'seed': 0}}
        receipt_path.write_text(json.dumps({'baseline_identity': identity, 'report_sha256': runner.sha(report_path)}))
        self.assertIsNotNone(runner.reuse_baseline(report_path, receipt_path, identity, self.ids))
        self.assertIsNone(runner.reuse_baseline(report_path, receipt_path, dict(identity, runtime='3.15'), self.ids))
        report_path.write_text(report_path.read_text() + ' ')
        self.assertIsNone(runner.reuse_baseline(report_path, receipt_path, identity, self.ids))

    def test_physical_apply_checks_patch_preimage_and_postimage(self):
        fixture = fixtures.PlanningTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        item = fixture.plan()['mutations'][0]
        source = fixture.fixture.root
        for field in ('patch_sha256', 'source_sha256', 'mutated_source_sha256'):
            with self.subTest(field=field):
                bad = dict(item, **{field: 'wrong'})
                with self.assertRaises(ValueError):
                    runner.apply_verified(source, bad)
                self.assertEqual(runner.sha(source / item['path']), item['source_sha256'])
        runner.apply_verified(source, item)
        self.assertEqual(runner.sha(source / item['path']), item['mutated_source_sha256'])
        with self.assertRaises(ValueError):
            runner.apply_verified(source, item)

    def test_incomplete_capture_prevents_execution_or_output_creation(self):
        args = types.SimpleNamespace(output=self.root / 'output', plan=self.root / 'plan.json', comparisons=self.root / 'capture.json')
        args.plan.write_text('{}'); args.comparisons.write_text('{}')
        with patch.object(runner, 'verify_plan', side_effect=ValueError('incomplete capture')), patch.object(runner, 'run_suite') as run:
            with self.assertRaises(ValueError):
                runner.execute(args)
        run.assert_not_called()
        self.assertFalse(args.output.exists())

    def test_plan_recomputed_before_execution_rejects_tampered_selection(self):
        fixture = fixtures.PlanningTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        f = fixture.fixture
        args = types.SimpleNamespace(source=f.root, samples=fixture.samples)
        values = {'catalog': {'tests': f.tests}, 'comparisons': f.mapping,
                  'proofs': {'python_version': list(runner.sys.version_info[:2])}, 'map': f.production}
        for field, value in values.items():
            path = self.root / (field + '.json'); path.write_text(json.dumps(value)); setattr(args, field, path)
        provenance = {'source': {'revision': 'pinned'}, 'runtime': {},
                      'production_map_sha256': runner.sha(args.map), 'factory_proofs_sha256': runner.sha(args.proofs)}
        plan = fixture.plan()
        plan.update(comparison_capture_sha256=runner.sha(args.comparisons), catalog_sha256=runner.sha(args.catalog), provenance=provenance)
        args.plan = self.root / 'plan.json'; args.gain_report = self.root / 'gain.json'; args.review = self.root / 'review.json'
        gain = {'status': 'conditional_estimate', 'conditional_analysis': True,
                'comparison_capture_sha256': runner.sha(args.comparisons), 'catalog_sha256': runner.sha(args.catalog),
                'production_map_sha256': provenance['production_map_sha256'],
                'factory_proofs_sha256': provenance['factory_proofs_sha256'], 'source_identity': provenance['source']}
        args.gain_report.write_text(json.dumps(gain))
        def save_plan():
            args.plan.write_text(json.dumps(plan))
            args.review.write_text(json.dumps({'reviewed': True, 'plan_sha256': runner.sha(args.plan),
                'comparison_capture_sha256': runner.sha(args.comparisons), 'gain_report_sha256': runner.sha(args.gain_report)}))
        save_plan()
        with patch.object(runner, 'plan_provenance', return_value=provenance), patch('selector.Selector', return_value=fixture.selector):
            runner.verify_plan(args)
            plan['mutations'][0]['conditional_ids'] = []
            save_plan()  # Even a matching receipt cannot bypass recomputation.
            with self.assertRaisesRegex(ValueError, 'selected sets changed'):
                runner.verify_plan(args)

    def test_partial_mutant_stops_without_success_results(self):
        args = types.SimpleNamespace(output=self.root / 'output', source=self.root, catalog=self.root / 'catalog.json',
                                     plan=self.root / 'plan.json', comparisons=self.root / 'capture.json',
                                     reference_report=None, reference_receipt=None, timeout=5)
        for p in (args.catalog, args.plan, args.comparisons): p.write_text('{}')
        tests = [{'id': node} for node in self.ids]
        item = {'site': 'site', 'kind': 'eligible', 'patch_sha256': 'patch', 'production_ids': sorted(self.ids), 'conditional_ids': sorted(self.ids)}
        plan = {'mutations': [item, item]}
        def isolate(source, destination): destination.mkdir(parents=True)
        baseline = copy.deepcopy(self.report)
        baseline['wall_seconds'] = .1
        partial = dict(self.report, complete=False)
        def run(tree, catalog, directory, timeout):
            report = baseline if directory.name in ('reference', 'mutation-0') else partial
            (directory / 'pytest.json').write_text(json.dumps(report))
            return report
        with patch.object(runner, 'verify_plan', return_value=(tests, plan, {'source': {}, 'runtime': {}})), patch.object(runner, 'isolated_tree', side_effect=isolate), patch.object(runner, 'apply_verified'), patch.object(runner, 'run_suite', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'incomplete mutant'):
                runner.execute(args)
        self.assertTrue((args.output / 'frozen-plan.json').exists())
        result = json.loads((args.output / 'results.json').read_text())
        self.assertFalse(result['complete'])
        self.assertEqual(result['expected_mutations'], 2)
        self.assertEqual(len(result['mutations']), 1)


if __name__ == '__main__':
    unittest.main()
