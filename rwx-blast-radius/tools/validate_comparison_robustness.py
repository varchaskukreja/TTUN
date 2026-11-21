#!/usr/bin/env python3
"""Standalone research order/cold controls; never imported by production.

Interactive stages require a root review receipt. At most two primary pytest
processes run concurrently; test-created descendants use the declared CPU limit.
A single wall deadline includes review waits after the first executed stage.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from validate_holdout import REVISION, run_suite
from run_comparison_holdouts import apply_verified, canonical, runtime_identity, sha, source_identity


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def audit_scope(report, expected):
    """Completed failures are valid observations; skipped/missing phases are unknown."""
    try:
        if report.get('complete') is not True or report.get('process_timeout') is not False:
            return False
        if type(report.get('process_exitcode')) is not int or report['process_exitcode'] not in (0, 1):
            return False
        if report.get('collection_errors') != [] or report.get('unresolved') != []:
            return False
        for key in ('requested', 'collected', 'finished', 'collected_order'):
            if type(report[key]) is not list or len(report[key]) != len(expected) or set(report[key]) != expected:
                return False
        outcomes, audit = report['outcomes'], report['phase_audit']
        if set(outcomes) != expected or set(audit) != expected:
            return False
        for node in expected:
            phases, subtests = audit[node]['phases'], audit[node]['subtests']
            if type(phases) is not dict or type(subtests) is not dict:
                return False
            if not set(phases.values()) <= {'passed', 'failed'} or not set(subtests) <= {'passed', 'failed'}:
                return False
            if any(type(count) is not int or count < 0 for count in subtests.values()):
                return False
            needed = {'setup', 'teardown'} if phases.get('setup') == 'failed' else {'setup', 'call', 'teardown'}
            if set(phases) != needed or outcomes[node] not in ('passed', 'failed'):
                return False
            failed = 'failed' in phases.values() or subtests.get('failed', 0) > 0
            if failed != (outcomes[node] == 'failed'):
                return False
        return report['process_exitcode'] == int('failed' in outcomes.values())
    except (KeyError, TypeError, AttributeError):
        return False


def aggregate_cold(records, catalog):
    seen, failed, unknown = set(), set(), set()
    duplicate = False
    for row in records:
        node = row['nodeid']
        duplicate |= node in seen
        seen.add(node)
        if not row.get('completed_requested_scope'):
            unknown.add(node)
        elif row.get('outcomes', {}).get(node) == 'failed':
            failed.add(node)
    unknown |= catalog - seen
    complete = len(catalog) == 1620 and seen == catalog and not unknown and not duplicate
    return {'expected_count': len(catalog), 'observed_count': len(seen), 'complete': complete,
            'all_passed': complete and not failed, 'failing_ids': sorted(failed),
            'unknown_ids': sorted(unknown), 'unexpected_ids': sorted(seen - catalog), 'duplicate_records': duplicate}


def comparison_summary(failing, baseline_failed, selected):
    return {'total_failing_ids': sorted(failing), 'baseline_failing_ids': sorted(baseline_failed),
            'new_failing_ids': sorted(failing - baseline_failed),
            'raw_conditional_total_missing_ids': sorted(failing - selected),
            'baseline_unstable_not_selected_ids': sorted(baseline_failed - selected),
            'new_failure_missing_ids': sorted((failing - baseline_failed) - selected),
            'missing_after_accounting_for_baseline_ids': sorted(failing - (selected | baseline_failed))}


def extract_owned(archive, expected_sha, owned_root, token):
    if hashlib.sha256(archive).hexdigest() != expected_sha:
        raise ValueError('immutable archive digest mismatch')
    owned_root.mkdir(parents=True, exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix='node-', dir=owned_root))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(tree, filter='data')
        (tree / '.research-owned-source').write_text(token)
        return tree
    except BaseException:
        shutil.rmtree(tree)
        raise


def cleanup_owned(tree, owned_root, token):
    tree, owned_root = Path(tree), Path(owned_root)
    if tree.is_symlink() or tree.resolve().parent != owned_root.resolve() or not tree.name.startswith('node-'):
        raise ValueError('refusing cleanup outside owned ephemeral root')
    if (tree / '.research-owned-source').read_text() != token:
        raise ValueError('source ownership token mismatch')
    shutil.rmtree(tree)


def execute_scope(archive, archive_sha, token, owned_root, directory, catalog_path, expected, deadline,
                  item=None, node=None, order='normal', seed=0):
    directory.mkdir(parents=True, exist_ok=False)
    row = {'nodeid': node, 'scope_count': len(expected), 'completed_requested_scope': False,
           'archive_sha256': archive_sha, 'patch_sha256': item['patch_sha256'] if item else None,
           'source_cleaned': False, 'outcomes': {}}
    tree = None
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError('total wall budget exhausted before source extraction')
        tree = extract_owned(archive, archive_sha, owned_root, token)
        if item:
            apply_verified(tree, item)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('total wall budget exhausted before pytest')
        report = run_suite(tree, catalog_path, directory, min(180, remaining),
                           nodes=[node] if node else None, order=order, order_seed=seed)
        row.update(completed_requested_scope=audit_scope(report, expected), outcomes=report.get('outcomes', {}),
                   report=str(directory / 'pytest.json'), report_sha256=sha(directory / 'pytest.json'),
                   collected_order_sha256=canonical(report.get('collected_order', [])),
                   wall_seconds=report.get('wall_seconds'), process_timeout=report.get('process_timeout'))
    except Exception as error:
        row['error'] = f'{type(error).__name__}: {error}'
    finally:
        if tree is not None:
            try:
                cleanup_owned(tree, owned_root, token)
                row['source_cleaned'] = True
            except Exception as error:
                row['cleanup_error'] = f'{type(error).__name__}: {error}'
                row['completed_requested_scope'] = False
        log = directory / 'pytest.log'
        if log.exists():
            row['log_sha256'] = sha(log)
        write_json(directory / 'receipt.json', row)
    return row


class ResearchDriver:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.owned = self.output / 'ephemeral'
        self.token = uuid.uuid4().hex
        self.archive = subprocess.check_output(['git', '-C', str(args.source), 'archive', REVISION])
        plan = json.loads(args.plan.read_text())
        prior_review_path = args.plan.parent / 'review.json'
        prior_review = json.loads(prior_review_path.read_text())
        if prior_review.get('reviewed') is not True or prior_review.get('plan_sha256') != sha(args.plan) or prior_review.get('comparison_capture_sha256') != plan.get('comparison_capture_sha256'):
            raise ValueError('input is not the exact previously reviewed frozen plan')
        self.items = [item for item in plan['mutations'] if item['kind'] == 'eligible']
        if plan.get('status') != 'planned' or len(self.items) != 2:
            raise ValueError('require exactly two eligible frozen-plan mutants')
        self.tests = json.loads(args.catalog.read_text())['tests']
        self.ids = {test['id'] for test in self.tests}
        if len(self.tests) != 1620 or len(self.ids) != 1620 or sha(args.catalog) != plan['catalog_sha256']:
            raise ValueError('catalog mismatch')
        source = source_identity(args.source)
        if source != plan['provenance']['source'] or hashlib.sha256(self.archive).hexdigest() != source['archive_sha256']:
            raise ValueError('pinned archive differs from reviewed plan')
        for field in ('selection_runtime_sha256', 'research_runtime_sha256'):
            directory = ROOT if field == 'selection_runtime_sha256' else Path(__file__).parent
            for name, digest in plan['provenance'][field].items():
                if sha(directory / name) != digest:
                    raise ValueError('frozen helper changed: ' + name)
        for name, digest in plan['provenance']['qualification_runtime_sha256'].items():
            directory = ROOT if name == 'patches.py' else Path(__file__).parent
            if sha(directory / name) != digest:
                raise ValueError('frozen qualification helper changed: ' + name)
        self.helper_hashes = {str(path.resolve()): sha(path) for path in
            [ROOT / 'patches.py', Path(__file__).with_name('validate_holdout.py'),
             Path(__file__).with_name('run_comparison_holdouts.py')]}
        self.archive_sha = source['archive_sha256']
        (self.output / 'source.tar').write_bytes(self.archive)
        (self.output / 'frozen-plan.json').write_bytes(args.plan.read_bytes())
        self.freeze = {'kind': 'comparison_order_and_cold_validation', 'driver_sha256': sha(__file__),
            'prior_review_sha256': sha(prior_review_path), 'execution_helper_sha256': self.helper_hashes,
            'source': source, 'catalog_sha256': sha(args.catalog), 'plan_sha256': sha(args.plan),
            'runtime': runtime_identity(), 'frozen_plan_provenance': plan['provenance'],
            'primary_pytest_concurrency': 2, 'test_descendant_cpu_count_limit': args.cpu_count,
            'total_wall_budget_seconds': 3600, 'per_scope_timeout_seconds': 180,
            'deadline_starts': 'first authorized stage; review waits then consume remaining budget',
            'order_controls': [{'order': 'reversed', 'seed': 0}, {'order': 'shuffle', 'seed': 20261120}],
            'cold_cases': ['baseline', 'mutant-0', 'mutant-1'], 'required_nodes_per_cold_case': 1620,
            'mutations': [{'site': item['site'], 'patch_sha256': item['patch_sha256'],
                           'conditional_selected_count': len(item['conditional_ids'])} for item in self.items],
            'runtime_narrowing_authorized': False}
        write_json(self.output / 'freeze.json', self.freeze)
        self.deadline = None

    def authorize(self):
        review = json.loads((self.output / 'review.json').read_text())
        if review.get('reviewed') is not True or review.get('freeze_sha256') != sha(self.output / 'freeze.json'):
            raise ValueError('root review receipt missing or mismatched')
        if sha(__file__) != self.freeze['driver_sha256'] or runtime_identity() != self.freeze['runtime']:
            raise ValueError('driver/runtime/environment changed since freeze')
        self.verify_helpers()
        if self.deadline is None:
            self.deadline = time.monotonic() + 3600
            write_json(self.output / 'budget-start.json', {'unix_started': time.time(), 'wall_budget_seconds': 3600})
        if time.monotonic() >= self.deadline:
            raise TimeoutError('total validation wall budget exhausted')

    def scope(self, directory, item=None, node=None, order='normal', seed=0):
        self.verify_helpers()
        return execute_scope(self.archive, self.archive_sha, self.token, self.owned, directory,
            self.args.catalog.resolve(), {node} if node else self.ids, self.deadline,
            item=item, node=node, order=order, seed=seed)

    def verify_helpers(self):
        if any(sha(path) != digest for path, digest in self.helper_hashes.items()):
            raise ValueError('execution helper changed after freeze')

    def orders(self):
        self.authorize()
        result = {'complete': False, 'runtime_narrowing_authorized': False, 'orders': []}
        target = self.output / 'orders.json'
        if target.exists():
            raise ValueError('order stage already attempted; no automatic retries')
        write_json(target, result)
        for order, seed in [('reversed', 0), ('shuffle', 20261120)]:
            base = self.scope(self.output / 'orders' / order / 'baseline', order=order, seed=seed)
            group = {'order': order, 'seed': seed, 'baseline': base, 'mutations': []}
            result['orders'].append(group); write_json(target, result)
            if not base['completed_requested_scope']:
                continue
            baseline_failed = {n for n, s in base['outcomes'].items() if s == 'failed'}
            for index, item in enumerate(self.items):
                actual = self.scope(self.output / 'orders' / order / f'mutant-{index}', item=item, order=order, seed=seed)
                row = {'site': item['site'], 'actual': actual}
                if actual['completed_requested_scope']:
                    failed = {n for n, s in actual['outcomes'].items() if s == 'failed'}
                    row['comparison'] = comparison_summary(failed, baseline_failed, set(item['conditional_ids']))
                group['mutations'].append(row); write_json(target, result)
        result['complete'] = len(result['orders']) == 2 and all(group['baseline']['completed_requested_scope'] and len(group['mutations']) == 2 and all(row['actual']['completed_requested_scope'] for row in group['mutations']) for group in result['orders'])
        write_json(target, result)
        return {'stage': 'orders', 'complete': result['complete']}

    def cold_case(self, name, item=None):
        directory = self.output / 'cold' / name
        directory.mkdir(parents=True, exist_ok=False)
        rows = []
        iterator = iter(enumerate(self.tests))
        pending = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            exhausted = False
            while pending or not exhausted:
                while len(pending) < 2 and not exhausted:
                    if time.monotonic() >= self.deadline:
                        exhausted = True; break
                    try:
                        index, test = next(iterator)
                    except StopIteration:
                        exhausted = True; break
                    future = pool.submit(self.scope, directory / f'node-{index:04d}', item, test['id'])
                    pending[future] = test['id']
                if not pending:
                    break
                done, _ = concurrent.futures.wait(pending, timeout=5, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    node = pending.pop(future)
                    try:
                        rows.append(future.result())
                    except Exception as error:
                        rows.append({'nodeid': node, 'completed_requested_scope': False, 'error': str(error)})
                summary = aggregate_cold(rows, self.ids)
                summary.update(case=name, runtime_narrowing_authorized=False)
                write_json(directory / 'summary.json', summary)
                if done and len(rows) % 50 == 0:
                    print(json.dumps({'event': 'cold_progress', 'case': name, 'observed': len(rows), 'failed': len(summary['failing_ids']), 'remaining_wall_seconds': round(self.deadline-time.monotonic(), 1)}), flush=True)
        summary = aggregate_cold(rows, self.ids)
        summary.update(case=name, runtime_narrowing_authorized=False,
                       per_node_receipts_sha256={str(path.relative_to(directory)): sha(path) for path in sorted(directory.glob('node-*/receipt.json'))})
        write_json(directory / 'summary.json', summary)
        return summary

    def cold_baseline(self):
        self.authorize()
        return self.cold_case('baseline')

    def cold_mutants(self):
        self.authorize()
        path = self.output / 'cold/baseline/summary.json'
        baseline = json.loads(path.read_text())
        if not baseline['complete']:
            raise ValueError('cold baseline incomplete; no cold mutants authorized')
        if baseline['failing_ids']:
            review = json.loads((self.output / 'cold-review.json').read_text())
            if review.get('reviewed') is not True or review.get('baseline_summary_sha256') != sha(path):
                raise ValueError('failing cold baseline requires exact root review')
        result = {'complete': False, 'baseline_summary_sha256': sha(path), 'mutations': [], 'runtime_narrowing_authorized': False}
        output = self.output / 'cold-results.json'
        if output.exists():
            raise ValueError('cold mutants stage already attempted')
        write_json(output, result)
        for index, item in enumerate(self.items):
            summary = self.cold_case(f'mutant-{index}', item)
            row = {'site': item['site'], 'summary': summary}
            if summary['complete']:
                row['comparison'] = comparison_summary(set(summary['failing_ids']), set(baseline['failing_ids']), set(item['conditional_ids']))
            result['mutations'].append(row); write_json(output, result)
        result['complete'] = all(row['summary']['complete'] for row in result['mutations']) and len(result['mutations']) == 2
        write_json(output, result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'source', 'catalog', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--cpu-count', type=int, default=2)
    args = parser.parse_args()
    if args.cpu_count < 1:
        parser.error('cpu-count must be positive')
    os.environ['PYTHON_CPU_COUNT'] = str(args.cpu_count)
    driver = ResearchDriver(args)
    print(json.dumps({'event': 'prepared_no_runs_started', 'pid': os.getpid(), 'freeze': str(driver.output/'freeze.json'), 'freeze_sha256': sha(driver.output/'freeze.json'), 'archive_bytes': len(driver.archive)}), flush=True)
    commands = {'orders': driver.orders, 'cold-baseline': driver.cold_baseline, 'cold-mutants': driver.cold_mutants}
    while True:
        try:
            command = input('research-review-gate> ').strip()
        except EOFError:
            break
        if command == 'exit':
            break
        try:
            if command == 'status':
                result = {'deadline_remaining': None if driver.deadline is None else driver.deadline-time.monotonic()}
            elif command in commands:
                result = commands[command]()
            else:
                raise ValueError('commands: orders, cold-baseline, cold-mutants, status, exit')
            print(json.dumps({'event': 'stage_terminal', 'result': result}), flush=True)
        except Exception as error:
            print(json.dumps({'event': 'stage_rejected_or_incomplete', 'error': f'{type(error).__name__}: {error}'}), flush=True)


if __name__ == '__main__':
    main()
