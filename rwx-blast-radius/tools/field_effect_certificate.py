#!/usr/bin/env python3
"""Research-only local effect certificates; never certify closed-world safety."""
import argparse
import ast
import copy
import hashlib
import io
import json
import tokenize
from pathlib import Path


def dump(node):
    return ast.dump(node, include_attributes=False)


def bool_mapping(node):
    if not isinstance(node, ast.Dict) or not node.keys:
        return None
    if not all(isinstance(k, ast.Constant) and type(k.value) is str for k in node.keys):
        return None
    if not all(isinstance(v, ast.Constant) and type(v.value) is bool for v in node.values):
        return None
    keys = [k.value for k in node.keys]
    return keys if len(set(keys)) == len(keys) else None


def locations(tree):
    result = {}
    def visit(body, prefix=''):
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            name = prefix + node.name
            for member in node.body:
                targets = member.targets if isinstance(member, ast.Assign) else [member.target] if isinstance(member, ast.AnnAssign) else []
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    result[(name, targets[0].id)] = member.value
            visit(node.body, name + '.')
    visit(tree.body)
    return result


def offset(text, line, col):
    lines = text.splitlines(keepends=True)
    return sum(len(x) for x in lines[:line-1]) + len(lines[line-1].encode()[:col].decode())


def split_literal(text, node):
    start = offset(text, node.lineno, node.col_offset)
    end = offset(text, node.end_lineno, node.end_col_offset)
    return text[:start], text[start:end], text[end:]


def token_shape(text):
    return [(t.type, '<BOOL>' if t.type == tokenize.NAME and t.string in ('True', 'False') else t.string)
            for t in tokenize.generate_tokens(io.StringIO(text).readline)]


def edit_gate(before, after):
    try:
        old, new = ast.parse(before), ast.parse(after)
        left, right = locations(old), locations(new)
        changed = [key for key in left if key in right and dump(left[key]) != dump(right[key])]
        if len(changed) != 1:
            return {'accepted': False, 'reason': 'not_one_class_field_edit'}
        key = changed[0]
        a, b = left[key], right[key]
        keys = bool_mapping(a)
        if keys is None or keys != bool_mapping(b):
            return {'accepted': False, 'reason': 'not_key_preserving_exact_bool_literal'}
        old_parts, new_parts = split_literal(before, a), split_literal(after, b)
        if old_parts[0] != new_parts[0] or old_parts[2] != new_parts[2] or token_shape(old_parts[1]) != token_shape(new_parts[1]):
            return {'accepted': False, 'reason': 'other_source_changes'}
        replacement = copy.deepcopy(a)
        class Replace(ast.NodeTransformer):
            def visit_Dict(self, node):
                return replacement if node is b else self.generic_visit(node)
        if dump(Replace().visit(new)) != dump(old):
            return {'accepted': False, 'reason': 'other_ast_changes'}
        return {'accepted': True, 'class': key[0], 'field': key[1], 'ordered_keys': keys,
                'effect': 'mapping_values_only', 'changed_keys': [k for k,x,y in zip(keys,a.values,b.values) if x.value != y.value]}
    except (SyntaxError, ValueError, tokenize.TokenError):
        return {'accepted': False, 'reason': 'invalid_source'}


def set_derivation(node, field):
    """Recognize syntax only; call resolution/hook purity remain obligations."""
    if not isinstance(node, ast.SetComp) or len(node.generators) != 1:
        return None
    g = node.generators[0]
    if g.is_async or not isinstance(g.target, ast.Tuple) or len(g.target.elts) != 2:
        return None
    k, v = g.target.elts
    if not isinstance(k, ast.Name) or not isinstance(v, ast.Name) or len(g.ifs) != 1:
        return None
    call = g.iter
    if not (isinstance(call, ast.Call) and not call.args and not call.keywords and isinstance(call.func, ast.Attribute) and call.func.attr == 'items'):
        return None
    attr = call.func.value
    if not (isinstance(attr, ast.Attribute) and attr.attr == field and isinstance(attr.value, ast.Name)):
        return None
    if not (isinstance(node.elt, ast.Name) and node.elt.id == k.id and isinstance(g.ifs[0], ast.Name) and g.ifs[0].id == v.id):
        return None
    return attr.value.id


def analyze(source, relative, after):
    root = Path(source)
    before = (root / relative).read_text()
    gate = edit_gate(before, after)
    result = {'schema': 1, 'edit': gate, 'local_derivations': [], 'observations': [],
              'closed_world_complete': False, 'narrowing_authorized': False,
              'source_sha256': {}, 'unresolved': []}
    if not gate['accepted']:
        return result
    trees = []
    for path in sorted(root.rglob('*.py')):
        if any(part.startswith('.') for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        result['source_sha256'][rel] = hashlib.sha256(raw).hexdigest()
        try:
            trees.append((rel, ast.parse(raw)))
        except SyntaxError:
            result['unresolved'].append({'kind': 'unparsed_source', 'path': rel})
    # Name-based may-alias overapproximation. Never a resolved points-to proof.
    tainted = {gate['field']}
    changed = True
    while changed:
        changed = False
        for rel, tree in trees:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                if not any((isinstance(x, ast.Attribute) and x.attr in tainted) or (isinstance(x, ast.Name) and x.id in tainted) for x in ast.walk(node.value)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    name = target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(target, ast.Name) else None
                    if name and name not in tainted:
                        tainted.add(name)
                        changed = True
                    receiver = set_derivation(node.value, gate['field'])
                    if receiver and isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == receiver:
                        record = {'path': rel, 'line': node.lineno, 'source_field': gate['field'], 'derived_field': target.attr,
                                  'effect': 'set_membership', 'status': 'syntax_only_requires_receiver_and_hook_resolution'}
                        if record not in result['local_derivations']:
                            result['local_derivations'].append(record)
    result['may_tainted_names'] = sorted(tainted)
    for rel, tree in trees:
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'getattr','setattr','vars','eval','exec','repr'}:
                result['unresolved'].append({'kind': 'reflection_or_dynamic_call', 'path': rel, 'line': node.lineno, 'operation': node.func.id})
            if isinstance(node, ast.Attribute) and node.attr == '__dict__':
                result['unresolved'].append({'kind': 'namespace_escape', 'path': rel, 'line': node.lineno})
            if not isinstance(node, ast.Attribute) or node.attr not in tainted or not isinstance(node.ctx, ast.Load):
                continue
            parent = parents.get(node)
            kind = 'value_sensitive_or_unresolved'
            if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
                kind = 'keys_only_if_exact_dict'
            elif isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in {'len','tuple','list','set'} and parent.args == [node]:
                kind = 'keys_only_if_exact_dict_and_builtin'
            elif isinstance(parent, ast.Compare) and all(isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops) and node in parent.comparators:
                kind = 'keys_only_if_exact_dict'
            # Derived sets have changed membership, unlike the original dict.
            if node.attr != gate['field']:
                kind = 'derived_or_alias_observation_unresolved'
            result['observations'].append({'path': rel, 'line': node.lineno, 'field': node.attr, 'kind': kind})
    result['unresolved'] += [{'kind': kind} for kind in (
        'class_identity_alias_and_inherited_provider_resolution',
        'initializer_mro_decorator_metaclass_and_builtin_resolution',
        'external_native_and_reflective_reachability',
        'lifecycle_direct_read_and_cross_test_state_completeness')]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--path', required=True)
    parser.add_argument('--after', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(analyze(args.source, args.path, Path(args.after).read_text()), indent=2) + '\n')


if __name__ == '__main__':
    main()
