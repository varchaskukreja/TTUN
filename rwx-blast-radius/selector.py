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


class Selector:
    def __init__(self, tests, mapping, source, class_guard=None):
        self.tests = tests
        self.mapping = mapping
        self.source = source
        self.source_cache = {}
        self.ast_cache = {}
        self.class_guard = class_guard
        self.all = {t['number'] for t in tests}
        by_id = {t['id']: t['number'] for t in tests}
        self.translate = {i: by_id[name] for i, name in enumerate(mapping.get('test_ids', [])) if name in by_id}
        self.unmapped = self.all - set(self.translate.values())
        self.unmapped.update(by_id[name] for name in mapping.get('missing_tests', []) if name in by_id)
        self.unmapped.update(by_id[name] for name, outcome in mapping.get('baseline_outcomes', {}).items() if outcome != 'passed' and name in by_id)

    def numbers(self, indices):
        return {self.translate[i] for i in indices if i in self.translate}

    def select(self, patch):
        selected = set()
        narrowed_class = False
        for change in parse_patch(patch):
            path = change['path']
            baseline = self.source / path
            if not baseline.is_file():
                return self.all
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
                    return self.all
                if not info:
                    return self.all
                # Compare the whole file, not edit text or patch IDs. Source readers
                # remain selected even when executable structure is unchanged.
                if original_dump == ast.dump(new_tree, include_attributes=False):
                    selected.update(self.unmapped)
                    continue
                if path.startswith('tests/'):
                    # Class/session fixtures and collection hooks can affect tests
                    # beyond the context in which their setup ran.
                    return self.all
                lines = info.get('lines', {})
                functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                def callable_kinds(node):
                    return [(type(n).__name__, getattr(n, 'name', '<lambda>'),
                             any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(n)))
                            for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                if callable_kinds(tree) != callable_kinds(new_tree):
                    return self.all
                for line in changed:
                    enclosing = [n for n in functions if n.lineno <= line <= n.end_lineno]
                    if not enclosing:
                        # Imports, class attributes and registration tables affect later consumers.
                        if self.class_guard is None:
                            return self.all
                        from class_selection import class_candidates
                        consumers = class_candidates(tree, new_tree, line, info)
                        if consumers is None:
                            return self.all
                        selected.update(self.numbers(consumers))
                        narrowed_class = True
                        continue
                    function = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
                    body_start = function.body[0].lineno if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) else function.lineno
                    if line < body_start or any(body_start <= recorded <= function.end_lineno for recorded in info.get('collection_lines', [])):
                        return self.all
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
                    return self.all
                selected.update(self.numbers(info['tests']))
                selected.update(self.unmapped)
        if narrowed_class:
            try:
                if not self.class_guard(patch):
                    return self.all
            except Exception:
                return self.all
        return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--map', type=Path, default=ROOT / 'maps/coverage.json.gz')
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    parser.add_argument('--class-guard', action='store_true', help='enable class-literal selection after successful cold import and collection checks')
    parser.add_argument('--metrics', action='store_true', help='write local CPU and guard counts to stderr')
    args = parser.parse_args()
    tests = read_json(args.data / 'tests.json')['tests']
    mapping = read_json(args.map) if args.map.exists() else {}
    guard = None
    if args.class_guard:
        from import_guard import ImportGuard
        guard = ImportGuard(args.source.resolve(), tests)
    selector = Selector(tests, mapping, args.source, class_guard=guard)
    cache = {}
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        try:
            selected = selector.select(patch.read_text())
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
