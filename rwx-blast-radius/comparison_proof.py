"""Experimental runtime proof functions extracted unchanged from frozen research core."""
import ast
import hashlib

def site_key(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}'

def single_operator_edit(path, before, after, changed_files=1):
    """Exactly one operator replacement; no chain or other AST differences."""
    if changed_files != 1:
        return None
    old = compile(before.encode() if isinstance(before, str) else before, path, 'exec', ast.PyCF_ONLY_AST)
    new = compile(after.encode() if isinstance(after, str) else after, path, 'exec', ast.PyCF_ONLY_AST)
    old_comparisons = [node for node in ast.walk(old) if isinstance(node, ast.Compare)]
    new_comparisons = [node for node in ast.walk(new) if isinstance(node, ast.Compare)]
    if len(old_comparisons) != len(new_comparisons):
        return None
    edits = [(left, right) for left, right in zip(old_comparisons, new_comparisons)
             if [type(op) for op in left.ops] != [type(op) for op in right.ops]]
    if len(edits) != 1:
        return None
    comparison, changed_comparison = edits[0]
    if len(comparison.ops) != 1 or len(changed_comparison.ops) != 1:
        return None
    left_op, right_op = comparison.ops[0], changed_comparison.ops[0]
    pair = {type(left_op).__name__, type(right_op).__name__}
    if pair not in ({'Is', 'Eq'}, {'IsNot', 'NotEq'}):
        return None
    original_op = comparison.ops[0]
    comparison.ops[0] = type(right_op)()
    same = ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False)
    comparison.ops[0] = original_op
    if not same:
        return None
    return {'site': site_key(path, comparison), 'old': type(left_op).__name__, 'new': type(right_op).__name__}

def observed_certificate(mapping, path, before, after, changed_files=1):
    """Conditional algebraic certificate, NEVER an unconditional skip decision."""
    if path in mapping.get('ineligible_files', []):
        return {'equivalent_for_observed_types': False, 'reason': 'module imported before instrumentation'}
    edit = single_operator_edit(path, before, after, changed_files)
    if edit is None:
        return {'equivalent_for_observed_types': False, 'reason': 'not one exact supported operator edit'}
    site = mapping['sites'].get(edit['site'])
    raw = before.encode() if isinstance(before, str) else before
    if not site or site['source_sha256'] != hashlib.sha256(raw).hexdigest() or site['operator'] != edit['old']:
        return {'equivalent_for_observed_types': False, 'reason': 'baseline site/hash mismatch'}
    observations = site['observations']
    if not observations:
        return {'equivalent_for_observed_types': False, 'reason': 'site unobserved'}
    require_ne = edit['old'] in ('IsNot', 'NotEq')
    for pair in observations:
        left, right = pair['left'], pair['right']
        identity = all(t['supported_metaclass'] and t['eq'] in ('object_identity', 'none_identity') and (not require_ne or t['ne'] in ('object_identity', 'none_identity')) for t in (left, right))
        none_case = left['none_type'] or right['none_type']
        enum_case = pair['same_type'] and left['standard_enum_type'] and right['standard_enum_type']
        backed_enum_case = pair['same_type'] and all(
            t.get('standard_enum_type') and t.get('supported_metaclass') and
            t.get('backed_enum_kind') in ('str', 'int') and t.get('canonical_member') and t.get('builtin_values_bijective')
            for t in (left, right)) and left.get('backed_enum_kind') == right.get('backed_enum_kind')
        if not (identity and (none_case or enum_case)) and not backed_enum_case:
            return {'equivalent_for_observed_types': False, 'reason': 'unproved operand type pair', 'site': edit['site']}
    dependencies = mapping.get('read_dependencies', {})
    source_readers = dependencies.get('source', {}).get(path, [])
    if dependencies.get('code_object_granularity'):
        if not site.get('execution_codes'):
            return {'equivalent_for_observed_types': False, 'reason': 'executing code object unobserved'}
        # Ancestor acquisition already records all co_consts descendants. Thus
        # matching the executing code key retains both direct and outer reads.
        code_readers = sorted({context for key in site['execution_codes'] for context in dependencies.get('code_objects', {}).get(key, {}).get('contexts', [])})
    else:
        code_readers = dependencies.get('code', {}).get(path, [])
    return {'equivalent_for_observed_types': True, 'unconditional': False, 'narrowing_authorized': False, 'site': edit['site'],
            'must_retain_observed_source_readers': source_readers,
            'must_retain_observed_code_readers': code_readers,
            'collection_escape_requires_all_tests': '' in source_readers or '' in code_readers,
            'contexts': sorted({context for pair in observations for context in pair['contexts']}),
            'requirements': ['complete deterministic relevant executions', 'unchanged effective type slots',
                             'retain all source/code-object/bytecode readers and unobserved contexts', 'instrumentation transparency verified separately']}
