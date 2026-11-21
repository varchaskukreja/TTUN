#!/usr/bin/env python3
"""Research-only physical comparison holdouts, gated on complete reviewed evidence.

No production imports this runner. A separate review receipt binds the exact
plan and conditional-gain report; this CLI never creates that receipt itself.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patches import parse_patch, apply_file
from selector import read_json
from analyze_comparison_gain import capture_issues, interpreter_issues
from validate_holdout import REVISION, isolated_tree, run_suite


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_identity(source):
    source = Path(source).resolve()
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True).strip()
    if revision != REVISION or dirty:
        raise ValueError('source must be the clean pinned revision')
    archive = subprocess.check_output(['git', '-C', str(source), 'archive', REVISION])
    return {'revision': revision, 'archive_sha256': hashlib.sha256(archive).hexdigest()}


def runtime_identity():
    # Match run_suite's inherited environment without persisting secret values.
    overridden = {'PYTEST_ADDOPTS', 'PYTHONPATH', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONDONTWRITEBYTECODE'}
    inherited = {key: value for key, value in os.environ.items()
                 if not key.startswith(('BLAST_', 'HOLDOUT_')) and key not in overridden}
    return {'python': sys.version, 'implementation': sys.implementation.name,
            'executable': str(Path(sys.executable).resolve()), 'platform': platform.platform(),
            'inherited_environment_sha256': canonical(inherited),
            'process_cpu_count': getattr(os, 'process_cpu_count', os.cpu_count)(),
            'distributions': sorted([dist.metadata['Name'], dist.version] for dist in importlib.metadata.distributions()),
            'runner_sha256': sha(__file__), 'physical_runner_sha256': sha(Path(__file__).with_name('validate_holdout.py'))}


def plan_provenance(source, map_path, proofs_path, samples):
    from fast_comparison_qualifier import qualifier_hashes
    return {'source': source_identity(source), 'runtime': runtime_identity(),
            'qualification_runtime_sha256': qualifier_hashes(),
            'production_map_sha256': sha(map_path), 'factory_proofs_sha256': sha(proofs_path),
            'sample_patch_hashes': {p.name: sha(p) for p in sorted(Path(samples).glob('*.patch'))},
            'selection_runtime_sha256': {name: sha(ROOT / name) for name in
                ('selector.py', 'class_selection.py', 'factory_selection.py', 'import_guard.py')},
            'research_runtime_sha256': {name: sha(Path(__file__).with_name(name)) for name in
                ('comparison_types.py', 'fast_comparison_qualifier.py', 'linear_operator_edit.py', 'analyze_comparison_gain.py', 'validate_comparison_holdout.py')}}


def complete_result(report, catalog_ids, *, baseline=False):
    """Require exact full-catalog completion, not just the runner's summary bool."""
    try:
        if report.get('complete') is not True or report.get('process_timeout') is not False:
            return False
        code = report.get('process_exitcode')
        if type(code) is not int or code not in ((0,) if baseline else (0, 1)):
            return False
        if report.get('collection_errors') != [] or report.get('unresolved') != []:
            return False
        if report.get('collection_order_policy') not in ('normal', 'reversed', 'shuffle') or type(report.get('collection_order_seed')) is not int:
            return False
        for field in ('requested', 'collected', 'finished', 'collected_order'):
            values = report[field]
            if type(values) is not list or len(values) != 1620 or set(values) != catalog_ids:
                return False
        outcomes = report['outcomes']
        allowed = {'passed'} if baseline else {'passed', 'failed'}
        if len(catalog_ids) != 1620 or set(outcomes) != catalog_ids or not set(outcomes.values()) <= allowed:
            return False
        audit = report['phase_audit']
        if set(audit) != catalog_ids:
            return False
        for node, outcome in outcomes.items():
            phases, subtests = audit[node]['phases'], audit[node]['subtests']
            if type(phases) is not dict or type(subtests) is not dict:
                return False
            if not set(phases.values()) <= allowed or not set(subtests) <= allowed:
                return False
            if any(type(count) is not int or count < 0 for count in subtests.values()):
                return False
            expected = {'setup', 'teardown'} if phases.get('setup') == 'failed' else {'setup', 'call', 'teardown'}
            if set(phases) != expected:
                return False
            observed_failure = 'failed' in phases.values() or subtests.get('failed', 0) > 0
            if observed_failure != (outcome == 'failed'):
                return False
        return code == int('failed' in outcomes.values())
    except (KeyError, TypeError, AttributeError):
        return False


def apply_verified(tree, item):
    """Apply to a physical archive only after both preimage and postimage checks."""
    if hashlib.sha256(item['patch'].encode()).hexdigest() != item['patch_sha256']:
        raise ValueError('patch digest mismatch')
    changes = parse_patch(item['patch'])
    if len(changes) != 1 or changes[0]['path'] != item['path']:
        raise ValueError('patch path mismatch')
    target = (Path(tree) / item['path']).resolve()
    if not target.is_relative_to(Path(tree).resolve()) or not target.is_file():
        raise ValueError('unsafe mutation target')
    if sha(target) != item['source_sha256']:
        raise ValueError('physical baseline preimage mismatch')
    updated, _ = apply_file(target.read_text(), changes[0])
    if hashlib.sha256(updated.encode()).hexdigest() != item['mutated_source_sha256']:
        raise ValueError('mutation postimage mismatch')
    compile(updated, item['path'], 'exec')
    target.write_text(updated)
    if sha(target) != item['mutated_source_sha256']:
        raise ValueError('written mutation digest mismatch')


def check_review(review, plan_hash, capture_hash, gain_hash, gain):
    if not (type(review) is dict and review.get('reviewed') is True
            and review.get('plan_sha256') == plan_hash
            and review.get('comparison_capture_sha256') == capture_hash
            and review.get('gain_report_sha256') == gain_hash
            and gain.get('status') == 'conditional_estimate'
            and gain.get('conditional_analysis') is True
            and gain.get('comparison_capture_sha256') == capture_hash):
        raise ValueError('missing or mismatched complete-capture gain review receipt')


def verify_plan(args):
    from validate_comparison_holdout import plan_holdouts
    from selector import Selector
    from factory_selection import FactoryCandidates
    from import_guard import ImportGuard
    tests = read_json(args.catalog)['tests']
    mapping, proofs, plan = read_json(args.comparisons), read_json(args.proofs), read_json(args.plan)
    issues = capture_issues(mapping, tests) + interpreter_issues(mapping, proofs)
    if issues:
        raise ValueError('ineligible comparison capture: ' + '; '.join(issues))
    if plan.get('status') != 'planned' or plan.get('comparison_capture_sha256') != sha(args.comparisons) or plan.get('catalog_sha256') != sha(args.catalog):
        raise ValueError('plan capture/catalog mismatch or incomplete plan')
    gain = read_json(args.gain_report)
    check_review(read_json(args.review), sha(args.plan), sha(args.comparisons), sha(args.gain_report), gain)
    provenance = plan_provenance(args.source, args.map, args.proofs, args.samples)
    if plan.get('provenance') != provenance:
        raise ValueError('plan source/runtime/map/proof/sample provenance mismatch')
    if (gain.get('catalog_sha256') != sha(args.catalog)
            or gain.get('production_map_sha256') != provenance['production_map_sha256']
            or gain.get('factory_proofs_sha256') != provenance['factory_proofs_sha256']
            or gain.get('source_identity') != provenance['source']
            or gain.get('qualification_runtime_sha256') != provenance['qualification_runtime_sha256']):
        raise ValueError('gain report source/map/proofs/catalog provenance mismatch')
    production = read_json(args.map)
    selector = Selector(tests, production, args.source, class_guard=ImportGuard(args.source, tests),
                        class_candidate=FactoryCandidates(args.source, proofs))
    fresh = plan_holdouts(mapping, args.source, tests, production, selector, args.samples, plan['seed'])
    for key, value in fresh.items():
        if plan.get(key) != value:
            raise ValueError('plan or selected sets changed: ' + key)
    return tests, plan, provenance


def reuse_baseline(report_path, receipt_path, identity, ids):
    if report_path is None or receipt_path is None:
        return None
    try:
        receipt, report = read_json(receipt_path), read_json(report_path)
        actual_order = {'policy': report.get('collection_order_policy'), 'seed': report.get('collection_order_seed')}
        if receipt.get('baseline_identity') == identity and identity.get('order') == actual_order and receipt.get('report_sha256') == sha(report_path) and complete_result(report, ids, baseline=True):
            return report
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def execute(args):
    plan_bytes = args.plan.read_bytes()
    capture_hash = sha(args.comparisons)
    tests, plan, provenance = verify_plan(args)
    if args.plan.read_bytes() != plan_bytes or sha(args.comparisons) != capture_hash:
        raise ValueError('inputs changed during plan verification')
    ids = {test['id'] for test in tests}
    # Exclusive directory creation prevents accidental replacement of evidence.
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'frozen-plan.json').write_bytes(plan_bytes)
    identity = {'source': provenance['source'], 'runtime': provenance['runtime'], 'catalog_sha256': sha(args.catalog),
                'order': {'policy': 'normal', 'seed': 0}}
    result = {'kind': 'comparison_physical_holdouts', 'runtime_narrowing_authorized': False,
              'complete': False, 'expected_mutations': len(plan['mutations']),
              'baseline_identity': identity, 'plan_sha256': hashlib.sha256(plan_bytes).hexdigest(),
              'comparison_capture_sha256': capture_hash, 'mutations': []}
    def save():
        (args.output / 'results.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    save()
    baseline = reuse_baseline(args.reference_report, args.reference_receipt, identity, ids)
    reused = baseline is not None
    if baseline is None:
        directory = args.output / 'reference'
        isolated_tree(args.source, directory / 'source')
        baseline = run_suite(directory / 'source', args.catalog.resolve(), directory, args.timeout)
        if not complete_result(baseline, ids, baseline=True):
            raise ValueError('baseline full catalog did not explicitly pass')
        report_path = directory / 'pytest.json'
        (directory / 'receipt.json').write_text(json.dumps({'baseline_identity': identity, 'report_sha256': sha(report_path)}, indent=2))
    result['baseline_reused'] = reused
    save()
    # Every selected set is already frozen above, before baseline or mutant outcomes.
    for index, item in enumerate(plan['mutations']):
        directory = args.output / f'mutation-{index}'
        isolated_tree(args.source, directory / 'source')
        apply_verified(directory / 'source', item)
        actual = run_suite(directory / 'source', args.catalog.resolve(), directory, args.timeout)
        if not complete_result(actual, ids):
            raise ValueError(f'incomplete mutant full catalog: {index}')
        failing = {node for node, status in actual['outcomes'].items() if status == 'failed'}
        result['mutations'].append({'site': item['site'], 'kind': item['kind'], 'patch_sha256': item['patch_sha256'],
            'actual_report_sha256': sha(directory / 'pytest.json'), 'actual_failing_ids': sorted(failing),
            'production_missing_ids': sorted(failing - set(item['production_ids'])),
            'conditional_missing_ids': sorted(failing - set(item['conditional_ids'])),
            'wall_seconds': actual['wall_seconds']})
        save()
    result['complete'] = True
    save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'comparisons', 'source', 'catalog', 'map', 'proofs', 'samples', 'gain-report', 'review', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--reference-report', type=Path)
    parser.add_argument('--reference-receipt', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    print(json.dumps(execute(args), indent=2))


if __name__ == '__main__':
    main()
