"""Select tests from baseline dependencies; never consult sample answers."""
import argparse
import ast
import gzip
import json
import resource
import sys
from pathlib import Path
from patches import parse_patch, apply_file
from scheduler import split

ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_text())


_CLASS_STATE_MUTATORS = frozenset({
    'clear', 'update', 'setdefault', 'pop', 'popitem', 'append', 'extend',
    'insert', 'remove', 'discard', 'add', 'difference_update',
    'intersection_update', 'symmetric_difference_update', 'sort', 'reverse',
    '__setitem__', '__delitem__', '__setattr__', '__delattr__',
})


def _explicit_classmethod(function):
    return isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        (isinstance(decorator, ast.Name) and decorator.id == 'classmethod')
        or (isinstance(decorator, ast.Attribute) and decorator.attr == 'classmethod')
        for decorator in function.decorator_list
    )


def persistent_class_state_write(function):
    """Recognize shared-state writes rooted at an explicit classmethod receiver.

    Local aliases are a monotone overapproximation, including aliases extracted
    from containers/getattr and bound mutators. This is not a purity proof:
    decorator aliases, opaque helper side effects, dynamic reflection and state
    reached through unrelated global aliases are not resolved here.
    """
    if not _explicit_classmethod(function):
        return False
    arguments = function.args.posonlyargs + function.args.args
    if not arguments:
        return True  # Unknown receiver through *args cannot establish read-only use.
    aliases, mutators = {arguments[0].arg}, set()
    nodes = list(ast.walk(function))

    def rooted(expression):
        if isinstance(expression, ast.Name):
            return expression.id in aliases
        if isinstance(expression, (ast.Attribute, ast.Subscript)):
            return rooted(expression.value)
        if isinstance(expression, ast.Call):
            name = expression.func.id if isinstance(expression.func, ast.Name) else getattr(expression.func, 'attr', None)
            return name == 'getattr' and bool(expression.args) and rooted(expression.args[0])
        return False

    def target_names(target):
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return set().union(*(target_names(item) for item in target.elts))
        return set()

    # Never kill aliases on reassignment: control flow and closures may retain
    # an earlier shared value. False positives keep the safe full fallback.
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                value, targets = node.iter, [node.target]
            else:
                continue
            if value is None:
                continue
            names = set().union(*(target_names(target) for target in targets))
            related = any(rooted(item) for item in ast.walk(value))
            bound_mutator = (isinstance(value, ast.Attribute)
                             and value.attr in _CLASS_STATE_MUTATORS and rooted(value.value))
            bound_mutator |= isinstance(value, ast.Name) and value.id in mutators
            if related and names - aliases:
                aliases.update(names)
                changed = True
            if bound_mutator and names - mutators:
                mutators.update(names)
                changed = True

    def write_target(target):
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            return rooted(target.value)
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(write_target(item) for item in target.elts)
        return False

    for node in nodes:
        if isinstance(node, ast.Assign) and any(write_target(target) for target in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and node.value is not None and write_target(node.target):
            return True
        if isinstance(node, ast.AugAssign) and (write_target(node.target) or rooted(node.target)):
            return True
        if isinstance(node, ast.Delete) and any(write_target(target) for target in node.targets):
            return True
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in mutators:
            return True
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', None)
        if name in ('setattr', 'delattr') and node.args and rooted(node.args[0]):
            return True
        if name in _CLASS_STATE_MUTATORS:
            if isinstance(node.func, ast.Attribute) and rooted(node.func.value):
                return True
            # Unbound builtin forms such as dict.update(cls.registry, values).
            if node.args and rooted(node.args[0]):
                return True
    return False


class Selector:
    def __init__(self, tests, mapping, source, class_guard=None, class_candidate=None):
        self.last_selection_status = {"complete": False, "reason": "not_started"}
        self.tests = tests
        self.mapping = mapping
        self.source = source
        self.source_cache = {}
        self.ast_cache = {}
        self.class_guard = class_guard
        self.class_candidate = class_candidate
        self.all = {t['number'] for t in tests}
        by_id = {t['id']: t['number'] for t in tests}
        self.translate = {i: by_id[name] for i, name in enumerate(mapping.get('test_ids', [])) if name in by_id}
        self.unmapped = self.all - set(self.translate.values())
        self.unmapped.update(by_id[name] for name in mapping.get('missing_tests', []) if name in by_id)
        self.unmapped.update(by_id[name] for name, outcome in mapping.get('baseline_outcomes', {}).items() if outcome != 'passed' and name in by_id)
        # Failures in unmodified order/isolation controls cannot be excluded
        # using dependencies observed in a different baseline execution.
        self.unmapped.update(by_id[name] for name in mapping.get('baseline_unstable_tests', []) if name in by_id)

    def numbers(self, indices):
        return {self.translate[i] for i in indices if i in self.translate}

    def _fallback(self, reason):
        self.last_selection_status = {'complete': False, 'reason': reason}
        return self.all

    def select(self, patch):
        self.last_selection_status = {'complete': False, 'reason': 'unknown_or_exception'}
        return self._select_base(patch)

    def _select_base(self, patch):
        selected = set()
        narrowed_class = False
        changes = parse_patch(patch)
        changed_paths = {change['path'] for change in changes}
        for change in changes:
            path = change['path']
            baseline = self.source / path
            if not baseline.is_file():
                return self._fallback('missing_source')
            if path not in self.source_cache:
                self.source_cache[path] = baseline.read_text()
            source = self.source_cache[path]
            updated, changed = apply_file(source, change)
            info = self.mapping.get('files', {}).get(path)
            if info:
                selected.update(self.numbers(info.get('read_tests', [])))
            if path.endswith('.py'):
                try:
                    # AST-only parsing accepts context-invalid constructs such as
                    # break outside a loop. Import failures require full fallback.
                    compile(updated.encode('utf-8'), path, 'exec')
                    if path not in self.ast_cache:
                        parsed = compile(source.encode('utf-8'), path, 'exec', ast.PyCF_ONLY_AST)
                        self.ast_cache[path] = (parsed, ast.dump(parsed, include_attributes=False))
                    tree, original_dump = self.ast_cache[path]
                    new_tree = compile(updated.encode('utf-8'), path, 'exec', ast.PyCF_ONLY_AST)
                except SyntaxError:
                    return self._fallback('invalid_syntax')
                if not info:
                    return self._fallback('missing_file_dependencies')
                # Compare the whole file, not edit text or patch IDs. Source readers
                # remain selected even when executable structure is unchanged.
                if original_dump == ast.dump(new_tree, include_attributes=False):
                    selected.update(self.unmapped)
                    continue
                if path.startswith('tests/'):
                    # Class/session fixtures and collection hooks can affect tests
                    # beyond the context in which their setup ran.
                    return self._fallback('test_module_global_effect')
                lines = info.get('lines', {})
                functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                new_functions = [n for n in ast.walk(new_tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                def callable_kinds(callables):
                    return [(type(n).__name__, getattr(n, 'name', '<lambda>'),
                             any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(n)))
                            for n in callables]
                if callable_kinds(functions) != callable_kinds(new_functions):
                    return self._fallback('changed_callable_kind')
                for old_function, new_function in zip(functions, new_functions):
                    if ((_explicit_classmethod(old_function) or _explicit_classmethod(new_function))
                            and ast.dump(old_function, include_attributes=False) != ast.dump(new_function, include_attributes=False)
                            and (persistent_class_state_write(old_function)
                                 or persistent_class_state_write(new_function))):
                        # Writer-only execution coverage omits later consumers of
                        # persistent state, including consumers in other tests.
                        return self._fallback('persistent_class_state')
                for line in changed:
                    enclosing = [n for n in functions if n.lineno <= line <= n.end_lineno]
                    if not enclosing:
                        # Imports, class attributes and registration tables affect later consumers.
                        if self.class_guard is None:
                            return self._fallback('global_effect_without_guard')
                        from class_selection import class_candidates
                        consumers = (self.class_candidate(tree, new_tree, line, info, path, changed_paths)
                                     if self.class_candidate is not None else
                                     class_candidates(tree, new_tree, line, info))
                        if consumers is None:
                            return self._fallback('unproved_class_effect')
                        selected.update(self.numbers(consumers))
                        narrowed_class = True
                        continue
                    function = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
                    body_start = function.body[0].lineno if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) else function.lineno
                    if line < body_start or any(body_start <= recorded <= function.end_lineno for recorded in info.get('collection_lines', [])):
                        return self._fallback('definition_or_collection_effect')
                    # Function coverage includes callers affected by inserted paths
                    # and continuation lines without their own trace event.
                    statement = function
                    hits = set()
                    for number in range(statement.lineno, statement.end_lineno + 1):
                        hits.update(lines.get(str(number), []))
                    if not hits:
                        for number in range(function.lineno, function.end_lineno + 1):
                            hits.update(lines.get(str(number), []))
                    selected.update(self.numbers(hits))
                selected.update(self.unmapped)
            else:
                if not info or not info.get('tests') or info.get('collection_read'):
                    return self._fallback('unknown_fixture_or_global_reader')
                selected.update(self.numbers(info['tests']))
                selected.update(self.unmapped)
        if narrowed_class:
            try:
                if not self.class_guard(patch):
                    return self._fallback('class_import_guard_failed')
            except Exception:
                return self._fallback('class_import_guard_exception')
        self.last_selection_status = {'complete': True, 'reason': 'dependency_selection'}
        return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--map', type=Path, default=ROOT / 'maps/coverage.json.gz')
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    parser.add_argument('--class-guard', action='store_true', help='enable class-literal selection after successful cold import and collection checks')
    parser.add_argument('--factory-proofs', type=Path, help='opt-in baseline factory proofs; requires --class-guard')
    parser.add_argument('--experimental-comparisons', action='store_true', help='research only; explicitly enable conditional comparison proposals')
    parser.add_argument('--comparison-evidence', type=Path, default=ROOT / 'maps/comparison-experimental.json.gz')
    parser.add_argument('--comparison-manifest', type=Path, default=ROOT / 'maps/comparison-experimental-manifest.json')
    parser.add_argument('--metrics', action='store_true', help='write local CPU and guard counts to stderr')
    args = parser.parse_args()
    if args.factory_proofs is not None and not args.class_guard:
        parser.error('--factory-proofs requires --class-guard')
    tests = read_json(args.data / 'tests.json')['tests']
    mapping = read_json(args.map) if args.map.exists() else {}
    guard = None
    if args.class_guard:
        from import_guard import ImportGuard
        guard = ImportGuard(args.source.resolve(), tests)
    candidate = None
    if args.factory_proofs is not None:
        from factory_selection import FactoryCandidates
        try:
            proof_data = read_json(args.factory_proofs)
        except (OSError, ValueError) as exc:
            print(f'factory proofs unavailable; strict class fallback: {exc}', file=sys.stderr)
            proof_data = {}
        candidate = FactoryCandidates(args.source, proof_data)
    selector = Selector(tests, mapping, args.source, class_guard=guard, class_candidate=candidate)
    comparisons = None
    if args.experimental_comparisons:
        from comparison_selection import ExperimentalComparisons
        comparisons = ExperimentalComparisons(tests, mapping, args.source, args.comparison_evidence, args.comparison_manifest, enabled=True)
    cache = {}
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        try:
            patch_text = patch.read_text()
            selected = selector.select(patch_text)
            if comparisons is not None:
                selected = comparisons.propose(patch_text, selected, selector.last_selection_status)
        except Exception as exc:
            print(f'{patch.stem}: conservative fallback: {exc}', file=sys.stderr)
            selected = selector.all
        key = tuple(sorted(selected))
        if key not in cache:
            cache[key] = split([test for test in tests if test['number'] in selected])
        print(json.dumps({'commit': patch.stem, 'workers': cache[key]}, separators=(',', ':')))
    if args.metrics:
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(json.dumps({'cpu_seconds': own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime,
                          'guard_calls': len(guard.calls) if guard else 0,
                          'guard_passed': sum(call['valid'] for call in guard.calls) if guard else 0}), file=sys.stderr)


if __name__ == '__main__':
    main()
