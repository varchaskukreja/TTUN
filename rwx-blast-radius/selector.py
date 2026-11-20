"""Select tests from baseline dependencies; never consult sample answers."""
import argparse
import ast
import gzip
import json
import sys
from pathlib import Path
from patches import parse_patch, apply_file
from scheduler import split

ROOT = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_text())


class Selector:
    def __init__(self, tests, mapping, source):
        self.tests = tests
        self.mapping = mapping
        self.source = source
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
        for change in parse_patch(patch):
            path = change['path']
            baseline = self.source / path
            if not baseline.is_file():
                return self.all
            source = baseline.read_text()
            updated, changed = apply_file(source, change)
            info = self.mapping.get('files', {}).get(path)
            if info:
                selected.update(self.numbers(info.get('read_tests', [])))
            if path.endswith('.py'):
                try:
                    tree = ast.parse(source)
                    new_tree = ast.parse(updated)
                except SyntaxError:
                    return self.all
                # Syntax-equivalent changes still use dependencies, since tests may inspect source.
                if not info:
                    return self.all
                lines = info.get('lines', {})
                functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
                for line in changed:
                    enclosing = [n for n in functions if n.lineno <= line <= n.end_lineno]
                    if not enclosing:
                        # Imports, class attributes and registration tables affect later consumers.
                        return self.all
                    function = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
                    # Statements span continuation lines that may have no trace event.
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
                if not info or not info.get('tests'):
                    return self.all
                selected.update(self.numbers(info['tests']))
                selected.update(self.unmapped)
        return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--map', type=Path, default=ROOT / 'coverage-map.json.gz')
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    args = parser.parse_args()
    tests = read_json(args.data / 'tests.json')['tests']
    mapping = read_json(args.map) if args.map.exists() else {}
    selector = Selector(tests, mapping, args.source)
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


if __name__ == '__main__':
    main()
