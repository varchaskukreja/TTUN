#!/usr/bin/env python3
"""Separately reviewed, bounded continuation of never-attempted cold mutants."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
from validate_comparison_robustness import (ResearchDriver, aggregate_cold, audit_scope,
    comparison_summary, runtime_identity, sha, write_json)
import validate_comparison_robustness as frozen_driver


def verify_baseline(prior, catalog_ids, runtime):
    prior = Path(prior).resolve()
    freeze_path = prior / 'freeze.json'
    freeze = json.loads(freeze_path.read_text())
    if freeze['runtime'] != runtime:
        raise ValueError('baseline runtime/environment mismatch; reuse forbidden')
    if sha(frozen_driver.__file__) != freeze['driver_sha256']:
        raise ValueError('baseline driver changed')
    for path, digest in freeze['execution_helper_sha256'].items():
        if sha(path) != digest:
            raise ValueError('baseline execution helper changed')
    review = json.loads((prior / 'review.json').read_text())
    if review.get('reviewed') is not True or review.get('freeze_sha256') != sha(freeze_path):
        raise ValueError('baseline freeze not reviewed')
    if any((prior / 'cold' / f'mutant-{i}').exists() for i in range(2)) or (prior / 'cold-results.json').exists():
        raise ValueError('mutants already attempted in prior stage')
    directory = prior / 'cold/baseline'
    summary_path = directory / 'summary.json'
    summary = json.loads(summary_path.read_text())
    if summary.get('complete') is not True or summary.get('observed_count') != 1620:
        raise ValueError('baseline incomplete')
    receipts = sorted(directory.glob('node-*/receipt.json'))
    expected_hashes = summary.get('per_node_receipts_sha256')
    actual_hashes = {str(p.relative_to(directory)): sha(p) for p in receipts}
    if len(receipts) != 1620 or actual_hashes != expected_hashes:
        raise ValueError('baseline receipt integrity/count mismatch')
    rows, report_hashes = [], {}
    for path in receipts:
        row = json.loads(path.read_text())
        report_path = path.parent / 'pytest.json'
        log_path = path.parent / 'pytest.log'
        node = row.get('nodeid')
        if (node not in catalog_ids or row.get('source_cleaned') is not True
            or row.get('patch_sha256') is not None or row.get('scope_count') != 1
            or row.get('archive_sha256') != freeze['source']['archive_sha256']
            or Path(row['report']).resolve() != report_path.resolve()
            or sha(report_path) != row['report_sha256'] or sha(log_path) != row['log_sha256']):
            raise ValueError('baseline physical report integrity mismatch')
        report = json.loads(report_path.read_text())
        if not audit_scope(report, {node}) or report['outcomes'] != row['outcomes']:
            raise ValueError('baseline phase/outcome audit failed')
        rows.append(row)
        report_hashes[str(report_path.relative_to(directory))] = sha(report_path)
    actual = aggregate_cold(rows, catalog_ids)
    if not actual['complete'] or any(summary.get(k) != v for k, v in actual.items()):
        raise ValueError('baseline aggregation mismatch')
    return {'freeze_sha256': sha(freeze_path), 'summary_sha256': sha(summary_path),
            'receipt_sha256': actual_hashes, 'report_sha256': report_hashes,
            'failing_ids': actual['failing_ids'], 'source': freeze['source'],
            'catalog_sha256': freeze['catalog_sha256'], 'plan_sha256': freeze['plan_sha256']}


class Continuation(ResearchDriver):
    def __init__(self, args):
        self.prior = args.prior.resolve()
        ids = {r['id'] for r in json.loads(args.catalog.read_text())['tests']}
        baseline = verify_baseline(self.prior, ids, runtime_identity())
        super().__init__(args)
        for key in ('source', 'catalog_sha256', 'plan_sha256'):
            if self.freeze[key] != baseline[key]:
                raise ValueError('continuation provenance mismatch: ' + key)
        self.baseline = baseline
        self.freeze.update(kind='separate_cold_mutant_continuation',
            continuation_driver_sha256=sha(__file__), baseline=baseline,
            prior_stage=str(self.prior), total_wall_budget_seconds=2700,
            cold_cases=['mutant-0', 'mutant-1'], orders_authorized=False)
        write_json(self.output / 'freeze.json', self.freeze)

    def authorize(self):
        review = json.loads((self.output / 'review.json').read_text())
        if (review.get('reviewed') is not True or review.get('freeze_sha256') != sha(self.output/'freeze.json')
            or review.get('baseline_summary_sha256') != self.baseline['summary_sha256']):
            raise ValueError('exact continuation and baseline root review required')
        if sha(__file__) != self.freeze['continuation_driver_sha256'] or runtime_identity() != self.freeze['runtime']:
            raise ValueError('continuation runtime changed')
        self.verify_helpers()
        if verify_baseline(self.prior, self.ids, runtime_identity()) != self.baseline:
            raise ValueError('baseline changed after preparation')
        if self.deadline is not None:
            raise ValueError('continuation already attempted')
        self.deadline = time.monotonic() + 2700
        write_json(self.output/'budget-start.json', {'unix_started': time.time(), 'wall_budget_seconds': 2700})

    def run(self):
        self.authorize()
        target = self.output/'results.json'
        if target.exists() or any((self.output/'cold'/f'mutant-{i}').exists() for i in range(2)):
            raise ValueError('mutant scope already attempted')
        result = {'complete': False, 'expected_mutations': 2, 'baseline_summary_sha256': self.baseline['summary_sha256'],
                  'mutations': [], 'runtime_narrowing_authorized': False}
        write_json(target, result)
        for index, item in enumerate(self.items):
            summary = self.cold_case(f'mutant-{index}', item)
            row = {'site': item['site'], 'summary': summary}
            if summary['complete']:
                row['comparison'] = comparison_summary(set(summary['failing_ids']), set(self.baseline['failing_ids']), set(item['conditional_ids']))
            result['mutations'].append(row)
            write_json(target, result)
        result['complete'] = len(result['mutations']) == 2 and all(r['summary']['complete'] for r in result['mutations'])
        write_json(target, result)
        return {'complete': result['complete'], 'results': str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'source', 'catalog', 'output', 'prior'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--cpu-count', type=int, default=2, choices=[2])
    args = parser.parse_args()
    os.environ['PYTHON_CPU_COUNT'] = '2'
    driver = Continuation(args)
    print(json.dumps({'event': 'prepared_no_runs', 'pid': os.getpid(), 'freeze_sha256': sha(driver.output/'freeze.json')}), flush=True)
    while True:
        try:
            command = input('continuation-review-gate> ').strip()
        except EOFError:
            break
        if command == 'exit':
            break
        if command != 'run':
            print('commands: run, exit', flush=True)
            continue
        try:
            print(json.dumps(driver.run()), flush=True)
        except Exception as error:
            print(json.dumps({'event': 'rejected_or_incomplete', 'error': str(error)}), flush=True)


if __name__ == '__main__':
    main()
