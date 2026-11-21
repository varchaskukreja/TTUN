#!/usr/bin/env python3
"""Baseline-only AST evidence for class-attribute escapes, not a safety proof.

No patch, outcomes, or test-selection inputs. Dynamic Python can evade this
analysis: unresolved attribute flows must remain conservative in any consumer.
"""
import argparse
import ast
import json
from pathlib import Path


def chain(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = chain(node.value)
        return f'{prefix}.{node.attr}' if prefix else None
    return None


def targets(node):
    if isinstance(node, (ast.Tuple, ast.List)):
        return [value for item in node.elts for value in targets(item)]
    value = chain(node)
    return [value] if value else []


def analyze(source):
    source = Path(source)
    modules, classes = {}, {}
    for path in sorted((source / 'sqlglot').rglob('*.py')):
        relative = path.relative_to(source).as_posix()
        parts = list(path.relative_to(source).with_suffix('').parts)
        if parts[-1] == '__init__': parts.pop()
        module = '.'.join(parts)
        tree = ast.parse(path.read_text(), filename=relative)
        imports = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name != '*': imports[alias.asname or alias.name] = f'{node.module}.{alias.name}'
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        modules[module] = {'path': relative, 'tree': tree, 'imports': imports}
        def collect(body, scope=''):
            for node in body:
                if not isinstance(node, ast.ClassDef): continue
                name = f'{scope}.{node.name}' if scope else node.name
                key = f'{module}.{name}'
                attrs, methods = {}, {}
                for member in node.body:
                    if isinstance(member, (ast.Assign, ast.AnnAssign)):
                        assigned = member.targets if isinstance(member, ast.Assign) else [member.target]
                        for target in assigned:
                            if isinstance(target, ast.Name):
                                try:
                                    ast.literal_eval(member.value)
                                    literal = True
                                except (ValueError, TypeError):
                                    literal = False
                                attrs[target.id] = {'line': member.lineno, 'literal': literal}
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        reads = {n.attr for n in ast.walk(member) if isinstance(n, ast.Attribute)
                                 and isinstance(n.ctx, ast.Load) and chain(n.value) in ('self', 'cls')}
                        calls = {n.func.attr for n in ast.walk(member) if isinstance(n, ast.Call)
                                 and isinstance(n.func, ast.Attribute) and chain(n.func.value) in ('self', 'cls')}
                        methods[member.name] = {'reads': reads, 'calls': calls, 'line': member.lineno}
                classes[key] = {'module': module, 'qualname': name, 'node': node, 'attributes': attrs, 'methods': methods}
                collect(node.body, name)
        collect(tree.body)

    def resolve(module, name, owner=None):
        first, *rest = name.split('.')
        imported = modules[module]['imports'].get(first)
        if imported: return '.'.join([imported, *rest])
        if owner:
            local = owner + '.' + name
            if local in classes: return local
        return module + '.' + name

    def ancestors(key, visited=None):
        visited = set() if visited is None else visited
        if key in visited or key not in classes: return []
        visited.add(key)
        record = classes[key]
        values = [key]
        for base in record['node'].bases:
            name = chain(base)
            if name: values.extend(ancestors(resolve(record['module'], name), visited))
        return values

    def method_summary(key, method, visited=None):
        visited = set() if visited is None else visited
        if (key, method) in visited: return set(), None
        visited.add((key, method))
        for owner in ancestors(key):
            member = classes[owner]['methods'].get(method)
            if member:
                reads = set(member['reads'])
                for called in member['calls']:
                    more, _ = method_summary(key, called, visited)
                    reads.update(more)
                return reads, owner
        return set(), None

    # Class-valued attributes connect separately declared implementations to
    # their owning dialect/container without requiring an implementation receiver.
    class_bindings = {}
    changed = True
    while changed:
        changed = False
        for owner, definition in classes.items():
            for statement in definition['node'].body:
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
                    continue
                reference = chain(statement.value)
                if not reference:
                    continue
                resolved = resolve(definition['module'], reference, owner)
                source_class = resolved if resolved in classes else class_bindings.get(resolved, {}).get('source')
                if source_class is None:
                    local_reference = owner + '.' + reference
                    source_class = class_bindings.get(local_reference, {}).get('source')
                if source_class is None:
                    continue
                assigned = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in assigned:
                    if not isinstance(target, ast.Name):
                        continue
                    key = owner + '.' + target.id
                    evidence = {'source': source_class, 'owner': owner, 'attribute': target.id,
                                'path': modules[definition['module']]['path'], 'line': statement.lineno}
                    if key not in class_bindings:
                        class_bindings[key] = evidence
                        changed = True

    direct, dynamic, local, registries = [], [], [], []
    for module, record in modules.items():
        def walk(body, owner=None, function=None):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    nested = f'{owner}.{node.name}' if owner else f'{module}.{node.name}'
                    walk(node.body, nested, None)
                    continue
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(node.body, owner, node.name)
                    continue
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    lhs = node.targets if isinstance(node, ast.Assign) else [node.target]
                    dests = [name for target in lhs for name in targets(target)]
                    attrs = [chain(n) for n in ast.walk(node.value) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)]
                    evidence = {'path': record['path'], 'line': node.lineno, 'owner': owner, 'function': function}
                    for dest in dests:
                        if owner and function is None:
                            referenced = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                            for name in referenced & set(classes[owner]['attributes']):
                                local.append(dict(evidence, source=f'{owner}.{name}', target=f'{owner}.{dest}', kind='same_class_derived'))
                        for attr in attrs:
                            if not attr: continue
                            prefix, name = attr.rsplit('.', 1)
                            resolved = resolve(module, prefix, owner)
                            if resolved in classes:
                                destination = f'{owner}.{dest}' if owner and function is None else f'{module}.{dest}' if owner is None and function is None else dest
                                direct.append(dict(evidence, source=f'{resolved}.{name}', target=destination,
                                                   kind='same_class_derived' if resolved == owner else 'class_attribute_escape'))
                            elif function and '.' in dest:
                                destination_receiver = dest.rsplit('.', 1)[0]
                                if prefix == destination_receiver:
                                    local.append(dict(evidence, source=attr, target=dest, kind='same_receiver_derived'))
                                elif prefix.startswith(destination_receiver + '.'):
                                    dynamic.append(dict(evidence, source=attr, target=dest, kind='cross_receiver_copy'))
                    # Registry construction via a method on a dynamic class value.
                    if owner is None and function is None:
                        for call in (n for n in ast.walk(node.value) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                            method = call.func.attr
                            grouped = {}
                            for cls, definition in classes.items():
                                reads, method_owner = method_summary(cls, method)
                                for attr in reads:
                                    provider = next((base for base in ancestors(cls) if attr in classes[base]['attributes']), None)
                                    if provider and classes[provider]['attributes'][attr]['literal']:
                                        grouped.setdefault((method_owner, attr), set()).add(f'{provider}.{attr}')
                            for (method_owner, attr), sources in grouped.items():
                                registries.append(dict(evidence, targets=[f'{module}.{d}' for d in dests],
                                    called_method=method, method_owner=method_owner, receiver_attribute=attr,
                                    possible_literal_sources=sorted(sources), kind='polymorphic_method_to_module_registry'))
                # Other reads (returns, expression statements and branch conditions)
                # also escape the source receiver, even without an assignment.
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for field_name, value in ast.iter_fields(node):
                        if field_name in ('body', 'orelse', 'finalbody', 'handlers'): continue
                        values = value if isinstance(value, list) else [value]
                        for expression in values:
                            if not isinstance(expression, ast.AST): continue
                            for read in ast.walk(expression):
                                if not isinstance(read, ast.Attribute) or not isinstance(read.ctx, ast.Load): continue
                                reference = chain(read)
                                if not reference: continue
                                prefix, attribute = reference.rsplit('.', 1)
                                resolved = resolve(module, prefix, owner)
                                if resolved in classes:
                                    direct.append({'path': record['path'], 'line': read.lineno, 'owner': owner,
                                        'function': function, 'source': f'{resolved}.{attribute}', 'target': '<read>',
                                        'kind': 'class_attribute_read'})
                # Traverse compound statements without duplicating nested functions/classes.
                for field in ('body', 'orelse', 'finalbody'):
                    nested = getattr(node, field, None)
                    if isinstance(nested, list): walk(nested, owner, function)
                for handler in getattr(node, 'handlers', []): walk(handler.body, owner, function)
        walk(record['tree'].body)
    return {'format': 1, 'kind': 'baseline_class_attribute_exports', 'classes': len(classes),
            'class_definitions': {key: {'path': modules[value['module']]['path'], 'qualname': value['qualname'], 'attributes': value['attributes'],
                'bases': [resolve(value['module'], name) for base in value['node'].bases if (name := chain(base))]}
                for key, value in classes.items()},
            'class_value_bindings': list(class_bindings.values()),
            'direct_class_flows': direct, 'cross_receiver_flows': dynamic,
            'same_receiver_derivations': local, 'module_registry_flows': registries,
            'analysis_complete': False, 'narrowing_authorized': False,
            'limitations': ['AST evidence only; no runtime receiver or descriptor proof',
                'dynamic imports, star reexports, reflection, decorators and monkeypatches unresolved',
                'same-receiver derivation is not globally safe if its derived attribute later escapes',
                'method summaries overapproximate reads and polymorphic registry candidates',
                'absence of a reported escape does not prove receiver-only dependency']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.source), sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
