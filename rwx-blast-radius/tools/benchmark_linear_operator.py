#!/usr/bin/env python3
"""Bounded baseline-source benchmark; no test failures or sample labels read."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tokenize

from linear_operator_edit import exact_operator_edit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--child', choices=('legacy', 'linear'))
    parser.add_argument('--case', type=Path)
    args = parser.parse_args()
    if args.child:
        case = json.loads(args.case.read_text())
        if args.child == 'legacy':
            from test_linear_operator_edit import reference as checker
        else:
            checker = exact_operator_edit
        start = time.process_time()
        result = checker(case['path'], case['before'], case['after'])
        print(json.dumps({'cpu_seconds': time.process_time()-start, 'edit': result}))
        return
    rows = []
    with tempfile.TemporaryDirectory(prefix='linear-comparison-') as temporary:
        case_path = Path(temporary)/'case.json'
        for relative in ('sqlglot/dialects/dialect.py', 'sqlglot/expressions/core.py', 'sqlglot/parser.py'):
            before = (args.source/relative).read_text()
            lines = before.splitlines(True)
            offsets = [0]
            for line in lines: offsets.append(offsets[-1]+len(line))
            chosen = None
            for token in tokenize.generate_tokens(io.StringIO(before).readline):
                if token.string not in ('is', '=='): continue
                offset = offsets[token.start[0]-1]+token.start[1]
                replacement = '==' if token.string == 'is' else 'is'
                after = before[:offset]+replacement+before[offset+2:]
                edit = exact_operator_edit(relative, before, after)
                if edit:
                    chosen = {'path': relative, 'before': before, 'after': after}
                    break
            if chosen is None: raise ValueError(f'no supported baseline comparison: {relative}')
            case_path.write_text(json.dumps(chosen))
            row = {'path': relative, 'source_characters': len(before), 'candidate_edit': edit}
            for mode in ('legacy', 'linear'):
                start = time.monotonic()
                try:
                    run = subprocess.run([sys.executable, __file__, '--child', mode, '--case', str(case_path)],
                                         text=True, capture_output=True, timeout=args.timeout, check=True)
                    row[mode] = dict(json.loads(run.stdout), wall_seconds=time.monotonic()-start)
                except subprocess.TimeoutExpired:
                    row[mode] = {'timed_out': True, 'wall_limit_seconds': args.timeout}
            if 'edit' in row['legacy'] and row['legacy']['edit'] is not None:
                assert row['legacy']['edit'] == row['linear']['edit']
            rows.append(row)
    report = {'kind': 'research_linear_exact_operator_benchmark', 'python': sys.version, 'cases': rows,
              'criterion': 'one equal-width is/== token; identical all other characters; exactly one unchained AST operator difference',
              'legacy_heuristic_equivalent': False,
              'legacy_difference': 'SequenceMatcher can reject exact valid edits in repeated text; linear gate recovers only explicitly text/token/AST-proved cases.',
              'failure_labels_read': False}
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(rows,indent=2))


if __name__ == '__main__': main()
