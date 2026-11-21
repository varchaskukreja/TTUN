"""Experimental capture gates extracted unchanged from reviewed research analyzer."""
import hashlib
import json
REQUIRED_CATALOG_COUNT = 1620

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
