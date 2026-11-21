#!/usr/bin/env python3
"""Standalone additive research evidence for definition-time class captures.

Does not modify the existing export walker. Python 3.13 semantics: future
annotations are strings; generic annotation/type-parameter scopes may be lazy.
All recognized class captures conservatively escape to function metadata.
"""
import argparse
import ast
import gzip
import json
from pathlib import Path
from class_attribute_exports import analyze, chain
from propagate_class_consumers import propagate


def definition_reads(source, exports, include_class_headers=False):
    root = Path(source)
    definitions = exports['class_definitions']
    records, deferred = [], []
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        parts = list(path.relative_to(root).with_suffix('').parts)
        if parts[-1] == '__init__': parts.pop()
        module = '.'.join(parts)
        tree = ast.parse(path.read_text())
        future = any(isinstance(n, ast.ImportFrom) and n.module == '__future__' and any(a.name == 'annotations' for a in n.names) for n in tree.body)
        imports = {}
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                imports.update({a.asname or a.name: n.module + '.' + a.name for a in n.names if a.name != '*'})
            if isinstance(n, ast.Import):
                imports.update({a.asname or a.name.split('.')[0]: a.name if a.asname else a.name.split('.')[0] for a in n.names})
        def resolve(name):
            first, *rest = name.split('.')
            return '.'.join([imports.get(first, module + '.' + first), *rest])

        def expression(node, kind, definition, timing, class_scope):
            # Traverse expression evaluation only. Lambda bodies run later.
            if isinstance(node, ast.Lambda):
                for value in node.args.defaults + [v for v in node.args.kw_defaults if v is not None]:
                    expression(value, 'lambda_default', definition, timing, class_scope)
                return
            reference = chain(node)
            source_ref = None
            if isinstance(node, ast.Attribute) and reference:
                prefix, attr = reference.rsplit('.', 1)
                resolved = resolve(prefix)
                if resolved in definitions:
                    source_ref = resolved + '.' + attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if class_scope and node.id in definitions.get(class_scope, {}).get('attributes', {}):
                    source_ref = class_scope + '.' + node.id
                elif kind != 'class_base' and resolve(node.id) in definitions:
                    source_ref = resolve(node.id) + '.*'
            if source_ref:
                record = {'path': relative, 'line': node.lineno, 'source': source_ref,
                          'target': '<definition-metadata>', 'owner': None, 'function': definition,
                          'lexical_class': class_scope, 'kind': 'definition_expression_capture',
                          'expression_kind': kind, 'timing': timing}
                if record not in records: records.append(record)
                # Avoid also marking the class prefix as a wildcard capture.
                return
            for child in ast.iter_child_nodes(node):
                expression(child, kind, definition, timing, class_scope)

        def walk(node, scope='', enclosing_function=None, class_scope=None):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                name = getattr(node, 'name', '<lambda>')
                qualified = scope + name
                timing = 'module_initialization' if enclosing_function is None else 'when_enclosing_callable_executes:' + enclosing_function
                for value in (getattr(node, 'decorator_list', []) if include_class_headers or not isinstance(node, ast.ClassDef) else []):
                    expression(value, 'decorator', qualified, timing, class_scope)
                if isinstance(node, ast.ClassDef):
                    for value in (node.bases if include_class_headers else []):
                        expression(value, 'class_base', qualified, timing, class_scope)
                    for keyword in (node.keywords if include_class_headers else []):
                        expression(keyword.value, 'class_keyword', qualified, timing, class_scope)
                    for value in node.body:
                        walk(value, qualified + '.', enclosing_function, module + '.' + qualified)
                    return
                for value in node.args.defaults + [v for v in node.args.kw_defaults if v is not None]:
                    expression(value, 'lambda_default' if isinstance(node, ast.Lambda) else 'default', qualified, timing, class_scope)
                annotations = [a.annotation for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + ([node.args.vararg] if node.args.vararg else []) + ([node.args.kwarg] if node.args.kwarg else []) if a.annotation]
                if getattr(node, 'returns', None): annotations.append(node.returns)
                for value in annotations:
                    if future:
                        deferred.append({'path': relative, 'line': value.lineno, 'definition': qualified, 'kind': 'future_string_annotation_not_evaluated'})
                    else:
                        expression(value, 'annotation', qualified, timing, class_scope)
                for param in getattr(node, 'type_params', []):
                    deferred.append({'path': relative, 'line': param.lineno, 'definition': qualified, 'kind': 'lazy_type_parameter_requires_resolution'})
                    expression(param, 'lazy_type_parameter', qualified, 'deferred_until_access', class_scope)
                # Discover definitions inside bodies, but do not claim their
                # ordinary expressions execute when this function is defined.
                for value in ([node.body] if isinstance(node, ast.Lambda) else node.body):
                    walk(value, qualified + '.', qualified, None)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, scope, enclosing_function, class_scope)
        walk(tree)
    bindings = {b['owner'] + '.' + b['attribute']: b['source'] for b in exports.get('class_value_bindings', [])}
    for record in list(records):
        if record['source'] in bindings:
            records.append(dict(record, source=bindings[record['source']] + '.*', binding_capture=record['source']))
    return records, deferred


def augment(source, include_class_headers=False):
    exports = analyze(source)
    records, deferred = definition_reads(source, exports, include_class_headers)
    exports['direct_class_flows'].extend(records)
    exports['definition_expression_evidence'] = records
    exports['deferred_definition_expressions'] = deferred
    exports['limitations'].append('Definition capture fallback is conservative; lexical shadowing, lazy annotation scopes and dynamic bindings remain unresolved.')
    return exports


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True)
    p.add_argument('--coverage', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--report', required=True)
    p.add_argument('--include-class-headers', action='store_true', help='broader experiment; default adds function/lambda headers only')
    a = p.parse_args()
    with gzip.open(a.coverage, 'rt') as f: mapping = json.load(f)
    exports = augment(a.source, a.include_class_headers)
    result, report = propagate(mapping, exports, lexical_outer=True)
    report['definition_expression_evidence'] = exports['definition_expression_evidence']
    report['deferred_definition_expressions'] = exports['deferred_definition_expressions']
    with gzip.GzipFile(a.output, 'wb', mtime=0) as f: f.write(json.dumps(result, separators=(',', ':'), sort_keys=True).encode())
    Path(a.report).write_text(json.dumps(report, indent=2) + '\n')

if __name__ == '__main__': main()
