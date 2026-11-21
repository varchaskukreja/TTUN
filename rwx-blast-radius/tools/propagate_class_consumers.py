#!/usr/bin/env python3
"""Monotone class consumer closure from baseline AST references.

No patch identities, expected failures, or test-name rules are accepted. Existing
consumers are never removed. Registry escapes are reported, not silently solved.
"""
import argparse
import copy
import gzip
import json
from pathlib import Path

from class_attribute_exports import analyze


def propagate(mapping, exports, lexical_outer=False):
    definitions = exports['class_definitions']
    consumers = {}
    for symbol, definition in definitions.items():
        consumers[symbol] = set(mapping['files'].get(definition['path'], {}).get('class_consumers', {}).get(definition['qualname'], []))
    edges = set()
    reasons = []
    for flow in exports['direct_class_flows']:
        # Class bodies statically copy/derive another class's attribute. A
        # downstream instance may consume that value without the source class.
        source_class = flow['source'].rsplit('.', 1)[0]
        destination_class = flow.get('owner')
        if destination_class and source_class in definitions and destination_class in definitions and source_class != destination_class:
            edges.add((source_class, destination_class))
            reasons.append({'source': source_class, 'consumer': destination_class, 'kind': 'class_body_reference' if flow['function'] is None else 'other_class_method_reference',
                            'path': flow['path'], 'line': flow['line'], 'attribute': flow['source'].rsplit('.', 1)[1]})
    # Runtime MRO tracing usually already covers inheritance, but retaining
    # explicit transitive subclass consumers makes the closure self-contained.
    for symbol, definition in definitions.items():
        for base in definition['bases']:
            if base in definitions:
                edges.add((base, symbol))
                reasons.append({'source': base, 'consumer': symbol, 'kind': 'inheritance'})
        if lexical_outer and '.' in definition['qualname']:
            outer = symbol.rsplit('.', 1)[0]
            if outer in definitions:
                edges.add((symbol, outer))
                reasons.append({'source': symbol, 'consumer': outer, 'kind': 'lexical_outer'})
    before = {key: set(values) for key, values in consumers.items()}
    changed = True
    rounds = 0
    while changed:
        changed = False
        rounds += 1
        for source, destination in sorted(edges):
            new = consumers[destination] - consumers[source]
            if new:
                consumers[source].update(new)
                changed = True
    result = copy.deepcopy(mapping)
    differences = {}
    for symbol, values in consumers.items():
        additions = values - before[symbol]
        if not additions:
            continue
        definition = definitions[symbol]
        result['files'].setdefault(definition['path'], {}).setdefault('class_consumers', {})[definition['qualname']] = sorted(values)
        differences[symbol] = {'before': len(before[symbol]), 'after': len(values), 'added': len(additions)}
    unresolved = [{'targets': flow['targets'], 'source_method': flow['method_owner'] + '.' + flow['called_method'],
                   'attribute': flow['receiver_attribute'], 'possible_literal_sources': flow['possible_literal_sources']}
                  for flow in exports['module_registry_flows']]
    global_exports = {}
    for flow in exports['module_registry_flows']:
        for reference in flow['possible_literal_sources']:
            symbol, attribute = reference.rsplit('.', 1)
            global_exports.setdefault(symbol, set()).add(attribute)
    for flow in exports['direct_class_flows']:
        # Direct reads in free functions/module globals have no source-class
        # receiver observation. Mark that attribute for conservative fallback.
        if flow.get('owner') is None:
            symbol, attribute = flow['source'].rsplit('.', 1)
            if symbol in definitions:
                known = definitions[symbol].get('attributes', {})
                global_exports.setdefault(symbol, set()).add(attribute if attribute in known else '*')
    for symbol, attributes in global_exports.items():
        definition = definitions[symbol]
        field = result['files'].setdefault(definition['path'], {}).setdefault('class_attribute_global_exports', {})
        field[definition['qualname']] = sorted(set(field.get(definition['qualname'], [])) | attributes)
    report = {'kind': 'baseline_class_consumer_closure', 'lexical_outer': lexical_outer,
              'edges': reasons, 'closure_rounds': rounds, 'changed_classes': differences,
              'added_class_test_links': sum(v['added'] for v in differences.values()),
              'unresolved_registry_escapes': unresolved,
              'class_attribute_global_exports': {symbol: sorted(values) for symbol, values in global_exports.items()}, 'analysis_complete': False,
              'limitations': ['Static class copies and known inheritance only; dynamic reference graphs are incomplete.',
                  'Lexical outer expansion is conservative supplemental evidence, not proof of all metaclass flows.',
                  'Module registry exports still require conservative fallback or a complete downstream dependency graph.',
                  'No existing consumer links were removed; no test outcomes or patch identities were used.']}
    result['class_consumer_closure'] = report
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--coverage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--lexical-outer', action='store_true')
    args = parser.parse_args()
    with gzip.open(args.coverage, 'rt') as stream:
        mapping = json.load(stream)
    result, report = propagate(mapping, analyze(args.source), args.lexical_outer)
    with gzip.GzipFile(filename=str(args.output), mode='wb', mtime=0) as stream:
        stream.write(json.dumps(result, sort_keys=True, separators=(',', ':')).encode())
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')


if __name__ == '__main__':
    main()
