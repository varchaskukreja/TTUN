#!/usr/bin/env python3
"""Conditional comparison-certificate research; never emit selected-test output.

Only a complete, receipt-audited, all-1620-passed baseline capture is eligible
for analysis. Even then reader coverage and instrumentation transparency remain
unproven. Answers are read solely after every conditional selection is fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from comparison_types import qualify_patch
from merge_comparison_maps import read_json
from scheduler import split, milliseconds
from score import evaluate_run

REQUIRED_CATALOG_COUNT = 1620


def interpreter_issues(mapping, proofs):
    """AST proofs and recorded CPython type semantics are minor-version bound."""
    current = list(sys.version_info[:2])
    issues = []
    version = proofs.get('python_version') if isinstance(proofs, dict) else None
    if type(version) is not list or any(type(n) is not int for n in version) or version != current:
        issues.append('factory proof Python minor does not match analysis interpreter')
    if isinstance(mapping, dict):
        for metadata in (mapping, mapping.get('capture_report', {})):
            if isinstance(metadata, dict) and 'python_version' in metadata:
                version = metadata['python_version']
                list_match = type(version) is list and all(type(n) is int for n in version) and version == current
                string_match = type(version) is str and version == '.'.join(map(str, current))
                if not (list_match or string_match):
                    issues.append('comparison capture Python minor does not match analysis interpreter')
    return sorted(set(issues))


def digest(value, *, sort_keys=False):
    return hashlib.sha256(json.dumps(value, sort_keys=sort_keys, separators=(',', ':')).encode()).hexdigest()


def capture_issues(mapping, tests, outcomes=None):
    """Fail closed before invoking production selection or examining any patch."""
    try:
        return _capture_issues(mapping, tests, outcomes)
    except (KeyError, TypeError, AttributeError, ValueError):
        return ['malformed capture, catalog, or outcome audit metadata']


def _capture_issues(mapping, tests, outcomes=None):
    issues = []
    ids = [test['id'] for test in tests]
    if len(ids) != REQUIRED_CATALOG_COUNT or len(set(ids)) != len(ids):
        issues.append('catalog must contain exactly 1620 distinct node IDs')
    if type(mapping) is not dict or type(mapping.get('format')) is not int or mapping.get('format') != 1 or mapping.get('kind') != 'baseline_operand_types':
        return issues + ['invalid comparison mapping format']
    report = mapping.get('capture_report', {})
    if mapping.get('capture_complete') is not True:
        issues.append('baseline capture is incomplete')
    if not isinstance(report, dict):
        return issues + ['missing capture audit report']
    if report.get('errors') != []:
        issues.append('capture audit has errors or no explicit error list')
    if type(report.get('catalog_count')) is not int or report['catalog_count'] != REQUIRED_CATALOG_COUNT:
        issues.append('capture catalog count is not 1620')
    if report.get('all_catalog_passed') is not True:
        issues.append('not every baseline catalog test explicitly passed')
    if report.get('catalog_node_ids_sha256') != digest(sorted(ids)):
        issues.append('capture catalog node digest mismatch')
    if report.get('baseline_outcomes_sha256') != digest(dict.fromkeys(ids, 'passed'), sort_keys=True):
        issues.append('capture all-passed outcome digest mismatch')
    receipt = report.get('receipt_audit', {})
    if not isinstance(receipt, dict):
        issues.append('missing receipt audit')
    else:
        if receipt.get('errors') != [] or receipt.get('must_retain') != []:
            issues.append('worker receipts contain errors, gaps, or untracked contexts')
        if receipt.get('untracked_child_activity_checked') is not True:
            issues.append('child activity receipt audit was not performed')
        if type(receipt.get('submitted')) is not int or receipt['submitted'] < 0:
            issues.append('invalid submitted-job receipt count')
        if not isinstance(receipt.get('worker_contexts'), dict):
            issues.append('missing worker context receipts')
        elif any(not isinstance(contexts, list) or any(context not in set(ids) | {''} for context in contexts)
                 for contexts in receipt['worker_contexts'].values()):
            issues.append('unknown worker context in receipt audit')
    # Every context in the merged observation artifact must already be expanded
    # to exact catalog IDs or the explicit collection context, never a PID tag.
    known = set(ids) | {''}
    try:
        context_lists = []
        for site in mapping['sites'].values():
            context_lists.extend(pair['contexts'] for pair in site['observations'])
            context_lists.extend(code['contexts'] for code in site['execution_codes'].values())
        readers = mapping['read_dependencies']
        context_lists.extend(readers['source'].values())
        context_lists.extend(readers['code'].values())
        context_lists.extend(record['contexts'] for record in readers['code_objects'].values())
        if readers.get('code_object_granularity') is not True:
            issues.append('comparison code-object reader audit is missing')
        if any(not isinstance(contexts, list) or any(context not in known for context in contexts)
               for contexts in context_lists):
            issues.append('unknown or unexpanded baseline observation context')
        if not isinstance(mapping['preexisting_modules'], dict) or not isinstance(mapping['ineligible_files'], list):
            issues.append('missing preexisting/ineligible-file audit')
    except (KeyError, TypeError, AttributeError):
        issues.append('malformed observation or reader context metadata')
    if outcomes is not None:
        if type(outcomes) is not dict:
            return issues + ['invalid baseline outcome sidecar']
        if type(outcomes.get('exitstatus')) is not int or outcomes['exitstatus'] != 0:
            issues.append('baseline outcome sidecar exit status is not zero')
        for key in ('selected', 'collected', 'finished'):
            value = outcomes.get(key)
            if not isinstance(value, list) or len(value) != len(ids) or set(value) != set(ids):
                issues.append(f'baseline sidecar {key} does not exactly match catalog')
        if outcomes.get('missing') != [] or outcomes.get('collection_errors') != []:
            issues.append('baseline sidecar contains missing tests or collection errors')
        if outcomes.get('outcomes') != dict.fromkeys(ids, 'passed'):
            issues.append('baseline sidecar does not show every test passed')
        if set(outcomes.get('details', {})) != set(ids):
            issues.append('baseline phase details do not exactly match catalog')
        for node in ids:
            detail = outcomes.get('details', {}).get(node, {})
            phases = detail.get('phases')
            subtests = detail.get('subtests')
            if phases != {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'} or detail.get('failures') != []:
                issues.append('baseline sidecar has incomplete or non-passing phases')
                break
            if not isinstance(subtests, dict) or any(key != 'passed' or type(count) is not int or count < 0 for key, count in subtests.items()):
                issues.append('baseline sidecar has skipped/failed/invalid subtests')
                break
    return sorted(set(issues))


def conditional_selection(mapping, source, patch, original, tests, production_map):
    """Research proposal only; caller must first pass the complete-capture gate."""
    original = set(original)
    rejected = lambda reason: (original, {'decision': 'retain_production_selection', 'reason': reason, 'removed_count': 0, 'added_reader_count': 0})
    try:
        certificate = qualify_patch(mapping, source, patch)
        if certificate.get('equivalent_for_observed_types') is not True:
            return rejected(certificate.get('reason', 'whole patch did not qualify'))
        path = certificate['path']
        if path in mapping['ineligible_files'] or path in mapping['preexisting_modules'].values():
            return rejected('preexisting or ineligible baseline module')
        if certificate.get('collection_escape_requires_all_tests') is not False:
            return rejected('source/code reader escaped during collection')
        by_id = {test['id']: test['number'] for test in tests}
        observed = set(certificate['contexts'])
        source_readers = set(certificate['must_retain_observed_source_readers'])
        code_readers = set(certificate['must_retain_observed_code_readers'])
        readers = source_readers | code_readers
        if (observed | readers) - (set(by_id) | {''}):
            return rejected('unknown observation or reader context')
        if '' in readers:
            return rejected('collection reader escape')
        site = mapping['sites'][certificate['site']]
        executing_contexts = {context for code in site['execution_codes'].values() for context in code['contexts']}
        observed &= executing_contexts
        protected = {by_id[node] for node in readers}
        info = production_map.get('files', {}).get(path, {})
        if info.get('collection_read'):
            return rejected('production map has a collection source-read escape')
        production_ids = production_map.get('test_ids', [])
        for index in info.get('read_tests', []):
            if type(index) is not int or not 0 <= index < len(production_ids):
                return rejected('unresolved production source-reader index')
            node = production_ids[index]
            if node not in by_id:
                return rejected('unknown production source-reader context')
            protected.add(by_id[node])
        # Missing/unknown production-map tests and non-passing baseline entries
        # stay conservative even when another capture happened to observe them.
        uncertain = set(by_id) - set(production_ids)
        uncertain.update(production_map.get('missing_tests', []))
        uncertain.update(node for node, outcome in production_map.get('baseline_outcomes', {}).items() if outcome != 'passed')
        protected.update(by_id[node] for node in uncertain if node in by_id)
        exact_observed_numbers = {by_id[node] for node in observed if node in by_id}
        removable = (original & exact_observed_numbers) - protected
        proposed = (original - removable) | protected
        return proposed, {'decision': 'conditional_research_proposal', 'site': certificate['site'],
                          'removed_count': len(removable), 'added_reader_count': len(protected - original),
                          'protected_reader_or_unknown_count': len(protected), 'exact_observed_test_count': len(exact_observed_numbers)}
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as exc:
        return rejected(f'invalid certificate or dependency metadata: {type(exc).__name__}')


def _score_selections(rows, tests, answers):
    stdout = ('\n'.join(json.dumps(row) for row in rows) + '\n').encode()
    result = evaluate_run(stdout, tests, answers)
    if not result['ok']:
        return result
    return {'ok': True, 'simulated_milliseconds': result['simulated_milliseconds'], 'misses': len(result['missed_commits'])}


def analyze(mapping, source, tests, patches, production_map, selector, *, outcomes=None, answers_path=None):
    issues = capture_issues(mapping, tests, outcomes)
    report = {'kind': 'conditional_comparison_gain_research', 'no_selected_output': True,
              'runtime_narrowing_authorized': False, 'capture_issues': issues}
    if issues:
        return dict(report, status='incomplete', conditional_analysis=False)
    baseline_rows, conditional_rows, details = [], [], []
    durations = {test['number']: milliseconds(test['seconds']) for test in tests}
    def cost(workers):
        return max((sum(durations[number] for number in worker) for worker in workers), default=0) + 2000 * len(workers)
    for commit, patch in patches:
        original = selector.select(patch)
        proposal, detail = conditional_selection(mapping, source, patch, original, tests, production_map)
        baseline_workers = split(test for test in tests if test['number'] in original)
        proposal_workers = split(test for test in tests if test['number'] in proposal)
        baseline_rows.append({'commit': commit, 'workers': baseline_workers})
        conditional_rows.append({'commit': commit, 'workers': proposal_workers})
        details.append(dict(detail, commit=commit, original_count=len(original), conditional_count=len(proposal),
                            conditional_ci_gain_milliseconds=cost(baseline_workers) - cost(proposal_workers)))
    report.update(status='conditional_estimate', conditional_analysis=True, patches=details,
                  conditional_ci_gain_milliseconds=sum(row['conditional_ci_gain_milliseconds'] for row in details),
                  limitations=['Observed-type equivalence is not proof of future executions, type-slot stability, or instrumentation transparency.',
                               'Reader and untracked-activity audits are explicitly nonexhaustive; complete capture is not exhaustive semantic coverage.',
                               'Retaining a source/code reader does not by itself establish retention of every downstream shared-state consumer.',
                               'Unknown tests remain selected; this report never emits a runtime selection artifact.'])
    if answers_path is not None:
        # First access to mutation labels: every conditional decision is fixed.
        answers = read_json(answers_path)['commits']
        if set(answers) != {row['commit'] for row in baseline_rows}:
            report['evaluation'] = {'ok': False, 'reason': 'answers/patch set mismatch'}
        else:
            report['evaluation'] = {'production': _score_selections(baseline_rows, tests, answers),
                                    'conditional': _score_selections(conditional_rows, tests, answers)}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparisons', type=Path, required=True)
    parser.add_argument('--outcomes', type=Path)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--data', type=Path, default=ROOT / 'sample')
    parser.add_argument('--map', type=Path, default=ROOT / 'maps/coverage.json.gz')
    parser.add_argument('--factory-proofs', type=Path, default=ROOT / 'maps/factory_proofs.json')
    parser.add_argument('--answers', type=Path, help='optional offline labels, read after all decisions')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    tests = read_json(args.data / 'tests.json')['tests']
    try:
        mapping = read_json(args.comparisons)
        outcomes = read_json(args.outcomes) if args.outcomes else None
        proofs = read_json(args.factory_proofs)
        issues = capture_issues(mapping, tests, outcomes) + interpreter_issues(mapping, proofs)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        mapping, outcomes = {}, None
        issues = [f'comparison capture unavailable: {type(exc).__name__}']
    if issues:
        result = {'kind': 'conditional_comparison_gain_research', 'status': 'incomplete', 'conditional_analysis': False,
                  'no_selected_output': True, 'runtime_narrowing_authorized': False, 'capture_issues': issues}
    else:
        from selector import Selector, read_json as read_production_json
        from factory_selection import FactoryCandidates
        from import_guard import ImportGuard
        production_map = read_production_json(args.map)
        selector = Selector(tests, production_map, args.source,
                            class_guard=ImportGuard(args.source, tests),
                            class_candidate=FactoryCandidates(args.source, proofs))
        patches = [(path.stem, path.read_text()) for path in sorted((args.data / 'commits').glob('*.patch'))]
        result = analyze(mapping, args.source, tests, patches, production_map, selector,
                         outcomes=outcomes, answers_path=args.answers)
    encoded = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
