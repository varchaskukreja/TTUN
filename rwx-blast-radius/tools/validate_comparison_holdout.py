#!/usr/bin/env python3
"""Plan-only comparison holdouts from complete evidence; never run mutants.

Plans two strict observed-equivalence edits and one globally unsafe-pair
countercase. Excludes every official sample file. Selection is frozen before
any outcomes exist; full-catalog physical-tree runs require separate approval
following conditional-gain review. No answer files are read by this module.
"""
from __future__ import annotations
import argparse
import ast
import difflib
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tokenize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyze_comparison_gain import capture_issues, conditional_selection, interpreter_issues
from comparison_types import qualify_patch, site_key
from patches import parse_patch
from selector import Selector, read_json
from factory_selection import FactoryCandidates
from import_guard import ImportGuard
# Existing runner is deliberately only referenced here. No run_suite call is
# reachable through this plan-only CLI. Future execution must use isolated_tree
# (git archive pinned revision), physically apply patch, then run_suite full IDs.
from validate_holdout import REVISION


def operator_patch(source, site):
    path = site['path']
    text = (source / path).read_text()
    tree = ast.parse(text, filename=path)
    node = next((node for node in ast.walk(tree) if isinstance(node, ast.Compare)
                 and site_key(path, node) == f"{path}:{site['line']}:{site['column']}:{site['end_line']}:{site['end_column']}"), None)
    if node is None or len(node.ops) != 1 or type(node.ops[0]) not in (ast.Is, ast.Eq):
        return None
    old, new = ('is', '==') if isinstance(node.ops[0], ast.Is) else ('==', 'is')
    lines = text.splitlines(keepends=True)
    def byte_position(position):
        row, column = position
        return (row, len(lines[row - 1][:column].encode()))
    lower = (node.left.end_lineno, node.left.end_col_offset)
    upper = (node.comparators[0].lineno, node.comparators[0].col_offset)
    matches = [token for token in tokenize.generate_tokens(io.StringIO(text).readline)
               if token.string == old and token.type in (tokenize.NAME, tokenize.OP)
               and lower <= byte_position(token.start) and byte_position(token.end) <= upper]
    if len(matches) != 1:
        return None
    token = matches[0]
    row, start = token.start
    _, end = token.end
    updated = list(lines)
    updated[row - 1] = lines[row - 1][:start] + new + lines[row - 1][end:]
    after = ''.join(updated)
    compile(after, path, 'exec')
    return f'diff --git a/{path} b/{path}\n' + ''.join(difflib.unified_diff(lines, after.splitlines(keepends=True), fromfile='a/' + path, tofile='b/' + path))


def plan_holdouts(mapping, source, tests, production_map, selector, sample_directory, seed):
    issues = capture_issues(mapping, tests)
    if issues:
        return {'status': 'incomplete', 'no_selected_output': True, 'issues': issues, 'mutations': []}
    excluded = {change['path'] for patch in sorted(sample_directory.glob('*.patch'))
                for change in parse_patch(patch.read_text())}
    sites = sorted(mapping['sites'].items())
    random.Random(seed).shuffle(sites)
    eligible, unsafe = [], []
    used_paths = set()
    by_number = {test['number']: test['id'] for test in tests}
    for key, site in sites:
        path = site['path']
        if path in excluded or path in used_paths or not path.startswith('sqlglot/'):
            continue
        if path in mapping['ineligible_files'] or path in mapping['preexisting_modules'].values():
            continue
        patch = operator_patch(source, site)
        if patch is None:
            continue
        certificate = qualify_patch(mapping, source, patch)
        is_eligible = certificate.get('equivalent_for_observed_types') is True
        is_unsafe = certificate.get('reason') == 'unproved operand type pair'
        if not ((is_eligible and len(eligible) < 2) or (is_unsafe and not unsafe)):
            continue
        original = set(selector.select(patch))
        conditional, decision = conditional_selection(mapping, source, patch, original, tests, production_map)
        if is_eligible and decision.get('removed_count', 0) <= 0:
            continue
        if is_unsafe and conditional != original:
            raise AssertionError('unsafe-pair countercase altered baseline selection')
        row = {'kind': 'eligible' if is_eligible else 'globally_unsafe_pair', 'site': key, 'path': path,
               'patch': patch, 'patch_sha256': hashlib.sha256(patch.encode()).hexdigest(),
               'production_ids': sorted(by_number[number] for number in original),
               'conditional_ids': sorted(by_number[number] for number in conditional),
               'decision': decision, 'actual_failing_ids': None, 'actual_missing_ids': None,
               'full_catalog_run_performed': False}
        (eligible if is_eligible else unsafe).append(row)
        used_paths.add(path)
        if len(eligible) == 2 and len(unsafe) == 1:
            break
    return {'status': 'planned' if len(eligible) == 2 and len(unsafe) == 1 else 'insufficient_candidates',
            'seed': seed, 'baseline_revision': REVISION, 'runtime_narrowing_authorized': False,
            'full_catalog_run_performed': False, 'excluded_sample_files': sorted(excluded),
            'execution_prerequisites': ['Review conditional gain from this complete capture.',
                'Use validate_holdout.isolated_tree for fresh pinned physical baseline and mutant trees.',
                'Apply each exact patch physically; run validate_holdout.run_suite with nodes=None.',
                'Require complete full-catalog baseline and mutant outcomes; record exact failing and missing IDs offline.'],
            'mutations': eligible + unsafe}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparisons', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'sample/tests.json')
    parser.add_argument('--map', type=Path, default=ROOT / 'maps/coverage.json.gz')
    parser.add_argument('--factory-proofs', type=Path, default=ROOT / 'maps/factory_proofs.json')
    parser.add_argument('--samples', type=Path, default=ROOT / 'sample/commits')
    parser.add_argument('--seed', type=int, default=20261120)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tests, mapping, proofs = read_json(args.catalog)['tests'], read_json(args.comparisons), read_json(args.factory_proofs)
    issues = capture_issues(mapping, tests) + interpreter_issues(mapping, proofs)
    if issues:
        report = {'status': 'incomplete', 'no_selected_output': True, 'issues': issues, 'mutations': []}
    else:
        production_map = read_json(args.map)
        selector = Selector(tests, production_map, args.source, class_guard=ImportGuard(args.source, tests),
                            class_candidate=FactoryCandidates(args.source, proofs))
        report = plan_holdouts(mapping, args.source, tests, production_map, selector, args.samples, args.seed)
    report['comparison_capture_sha256'] = hashlib.sha256(args.comparisons.read_bytes()).hexdigest()
    report['catalog_sha256'] = hashlib.sha256(args.catalog.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
