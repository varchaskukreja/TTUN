#!/usr/bin/env python3
"""Offline comparison of function versus statement dependency selection.

Labels are read only after both algorithms have selected and scheduled tests.
No output is a submission artifact or patch-to-answer lookup table.

WARNING: Even --guarded is not sound for cross-test mutable caches or lazy
globals. Symbol/layout guards do not propagate writes to later state readers.
Executable counterexamples are retained in test_granularity.py. This tool is
exploratory only; a zero-miss sample result does not justify production use.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import symtable
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patches import apply_file, parse_patch
from scheduler import split
from score import evaluate_run
from selector import Selector, read_json


def pure_replacement(change):
    """Every contiguous edit block must replace equal nonzero line counts."""
    for hunk in change["hunks"]:
        removed = added = 0
        for line in hunk["lines"] + [" "]:
            if line.startswith("-"):
                removed += 1
            elif line.startswith("+"):
                added += 1
            else:
                if (removed or added) and (removed == 0 or removed != added):
                    return False
                removed = added = 0
    return True


def callable_kinds(tree):
    return [(type(n).__name__, getattr(n, "name", "<lambda>"),
             any(isinstance(c, (ast.Yield, ast.YieldFrom)) for c in ast.walk(n)))
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]


def symbol_signature(source, filename):
    """Include nested scope bindings and every available symbol classification."""
    def visit(table):
        symbols = []
        for symbol in sorted(table.get_symbols(), key=lambda item: item.get_name()):
            flags = tuple((name, getattr(symbol, name)()) for name in sorted(dir(symbol))
                          if name.startswith("is_") and callable(getattr(symbol, name)))
            symbols.append((symbol.get_name(), flags))
        return (str(table.get_type()), table.get_name(), tuple(symbols),
                tuple(visit(child) for child in table.get_children()))
    return visit(symtable.symtable(source, filename, "exec"))


def statement_layout(node):
    """Statement topology, preserving blocks but permitting expression changes."""
    if isinstance(node, ast.expr):
        return None
    children = []
    for name, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            layout = statement_layout(value)
            if layout is not None:
                children.append((name, layout))
        elif isinstance(value, list):
            layouts = tuple(layout for item in value if isinstance(item, ast.AST)
                            if (layout := statement_layout(item)) is not None)
            if layouts:
                children.append((name, layouts))
    return (type(node).__name__, tuple(children))


class StatementSelector(Selector):
    def __init__(self, *args, guarded=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.guarded = guarded

    def scope_fallback(self, baseline_selection, tree, changed, info):
        """Binding changes in nested functions can affect their enclosing callers."""
        result = set(baseline_selection)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if not any(node.lineno <= line <= node.end_lineno for line in changed):
                continue
            if any(node.lineno <= line <= node.end_lineno for line in info.get("collection_lines", [])):
                return self.all
            for line in range(node.lineno, node.end_lineno + 1):
                result.update(self.numbers(info.get("lines", {}).get(str(line), [])))
        return result

    def select(self, patch):
        baseline_selection = super().select(patch)
        selected = set(self.unmapped)
        for change in parse_patch(patch):
            path = change["path"]
            info = self.mapping.get("files", {}).get(path)
            if not path.endswith(".py") or path.startswith("tests/") or not info:
                return baseline_selection
            source = (self.source / path).read_text()
            updated, changed = apply_file(source, change)
            try:
                tree = compile(source.encode(), path, "exec", ast.PyCF_ONLY_AST)
                new_tree = compile(updated.encode(), path, "exec", ast.PyCF_ONLY_AST)
                if self.guarded:
                    # ONLY_AST permits some constructs rejected by actual compilation.
                    compile(updated.encode(), path, "exec")
            except SyntaxError:
                return self.all if self.guarded else baseline_selection
            selected.update(self.numbers(info.get("read_tests", [])))
            if ast.dump(tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False):
                continue
            if not pure_replacement(change) or callable_kinds(tree) != callable_kinds(new_tree):
                return baseline_selection
            if self.guarded and (symbol_signature(source, path) != symbol_signature(updated, path)
                                 or statement_layout(tree) != statement_layout(new_tree)):
                return self.scope_fallback(baseline_selection, tree, changed, info)
            lines = info.get("lines", {})
            functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
            for line in changed:
                enclosing = [n for n in functions if n.lineno <= line <= n.end_lineno]
                if not enclosing:
                    return baseline_selection
                function = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
                body_start = function.body[0].lineno if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) else function.lineno
                if line < body_start or any(body_start <= n <= function.end_lineno for n in info.get("collection_lines", [])):
                    return baseline_selection
                statements = [n for n in ast.walk(function) if isinstance(n, ast.stmt)
                              and n is not function and n.lineno <= line <= n.end_lineno]
                statement = min(statements, key=lambda n: (n.end_lineno - n.lineno, -n.col_offset)) if statements else function
                if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)) or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(function)):
                    statement = function
                hits = set()
                for number in range(statement.lineno, statement.end_lineno + 1):
                    hits.update(lines.get(str(number), []))
                if not hits:
                    for number in range(function.lineno, function.end_lineno + 1):
                        hits.update(lines.get(str(number), []))
                selected.update(self.numbers(hits))
        return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT.parent / ".work/sqlglot")
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--map", type=Path, default=ROOT / "maps/coverage.json.gz")
    parser.add_argument("--guarded", action="store_true", help="also test symbol/layout guards (still unsafe for shared mutable state)")
    args = parser.parse_args()
    tests = read_json(args.data / "tests.json")["tests"]
    mapping = read_json(args.map)
    outputs, selections, elapsed = {}, {}, {}
    modes = [("function", Selector), ("statement", StatementSelector)]
    if args.guarded:
        modes.append(("guarded_statement", StatementSelector))
    for label, cls in modes:
        start = time.process_time()
        engine = cls(tests, mapping, args.source, **({"guarded": True} if label == "guarded_statement" else {}))
        rows, chosen = [], {}
        for path in sorted((args.data / "commits").glob("*.patch")):
            selection = engine.select(path.read_text())
            chosen[path.stem] = selection
            rows.append({"commit": path.stem, "workers": split(t for t in tests if t["number"] in selection)})
        outputs[label] = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        selections[label] = chosen
        elapsed[label] = time.process_time() - start
    # This is the first access to labels, after every selection is fixed.
    answers = read_json(args.data / "answers.json")["commits"]
    report = {"warning": "Statement modes can miss later consumers of shared state; exploratory only."}
    for label, output in outputs.items():
        result = evaluate_run(output, tests, answers)
        result["local_cpu_seconds"] = round(elapsed[label], 6)
        result["total_selected_tests"] = sum(map(len, selections[label].values()))
        report[label] = result
    report["selection_reductions"] = [
        {"commit": key, "before": len(selections["function"][key]), "after": len(selected),
         **({"guarded_after": len(selections["guarded_statement"][key])} if args.guarded else {}),
         "missing_test_ids": [t["id"] for t in tests if t["number"] in set(answers[key]["failing"]) - selected]}
        for key, selected in selections["statement"].items() if selected != selections["function"][key]
    ]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
