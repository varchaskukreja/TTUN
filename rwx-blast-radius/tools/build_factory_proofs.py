#!/usr/bin/env python3
"""Build baseline-only syntactic proofs of closure-factory invocation purity.

A proof means the factory invocation only creates and returns a closure; it does
not prove the returned callable pure, dependency-complete, or safe to execute.
No patches, labels, test catalog, or runtime imports of sqlglot are inspected.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_proof_format import REVISION, SCHEMA, PROOF_KIND, ast_hash, python_version


def _has_defaults(args):
    return bool(args.defaults) or any(value is not None for value in args.kw_defaults)


def _has_annotations(function):
    args = function.args
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    all_args += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return function.returns is not None or any(arg.annotation is not None for arg in all_args)


def is_closure_factory(node, future_annotations=False):
    if not isinstance(node, ast.FunctionDef) or node.decorator_list or getattr(node, 'type_params', []):
        return False
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body.pop(0)
    if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        return not _has_defaults(body[0].value.args)
    if len(body) != 2 or not isinstance(body[0], ast.FunctionDef) or not isinstance(body[1], ast.Return):
        return False
    inner, returned = body[0], body[1].value
    return (not inner.decorator_list and not getattr(inner, 'type_params', [])
            and not _has_defaults(inner.args)
            and (future_annotations or not _has_annotations(inner))
            and isinstance(returned, ast.Name) and returned.id == inner.name)


class ModuleBindings(ast.NodeVisitor):
    """Conservative module-name rebinding inventory, without entering local scopes."""
    def __init__(self):
        self.counts = Counter()
        self.opaque_bindings = False

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.counts[node.id] += 1

    def visit_FunctionDef(self, node):
        self.counts[node.name] += 1
        # These expressions execute while the function is defined.
        for expr in [*node.decorator_list, *node.args.defaults,
                     *(value for value in node.args.kw_defaults if value is not None),
                     *(arg.annotation for arg in [*node.args.posonlyargs, *node.args.args,
                       *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []),
                       *([node.args.kwarg] if node.args.kwarg else [])] if arg.annotation is not None),
                     *([node.returns] if node.returns is not None else [])]:
            self.visit(expr)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for expr in [*node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)]:
            self.visit(expr)

    def visit_ClassDef(self, node):
        self.counts[node.name] += 1
        for expr in [*node.decorator_list, *node.bases, *(item.value for item in node.keywords)]:
            self.visit(expr)

    def visit_Import(self, node):
        for item in node.names:
            self.counts[item.asname or item.name.split('.')[0]] += 1

    def visit_ImportFrom(self, node):
        for item in node.names:
            if item.name == '*':
                self.opaque_bindings = True
            else:
                self.counts[item.asname or item.name] += 1

    def visit_ExceptHandler(self, node):
        if node.name:
            self.counts[node.name] += 1
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.counts[node.name] += 1
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.counts[node.name] += 1

    def visit_MatchMapping(self, node):
        if node.rest:
            self.counts[node.rest] += 1
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in {'exec', 'eval', 'globals', 'locals'}:
            self.opaque_bindings = True
        self.generic_visit(node)


def has_namespace_mutation(tree):
    """Reject visible module/function namespace aliases and mutation channels.

    This is deliberately syntactic, not whole-program Python effect analysis.
    Ordinary attribute writes to local objects do not invalidate unrelated
    module bindings. Opaque calls can still require a baseline/runtime audit.
    """
    nodes = list(ast.walk(tree))
    sys_aliases = {item.asname or 'sys' for node in nodes if isinstance(node, ast.Import)
                   for item in node.names if item.name == 'sys'}
    imported = {item.asname or item.name.split('.')[0] for node in nodes if isinstance(node, ast.Import)
                for item in node.names}
    imported.update(item.asname or item.name for node in nodes if isinstance(node, ast.ImportFrom)
                    for item in node.names if item.name != '*')
    tracked = imported | {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    mutation_calls = {'setattr', 'delattr'}
    vars_calls = {'vars'}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.module == 'sys' and any(item.name == 'modules' for item in node.names):
                return True
            if node.module == 'builtins':
                mutation_calls.update(item.asname or item.name for item in node.names if item.name in {'setattr', 'delattr'})
                vars_calls.update(item.asname or item.name for item in node.names if item.name == 'vars')
    # Follow transparent aliases such as module_alias = imported_module and
    # function_alias = factory; later stores through either are rebindings.
    progress = True
    while progress:
        progress = False
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name) and node.value.id in tracked:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tracked:
                        tracked.add(target.id)
                        progress = True
                    if isinstance(target, ast.Name) and node.value.id in sys_aliases and target.id not in sys_aliases:
                        sys_aliases.add(target.id)
                        progress = True
    for node in nodes:
        if isinstance(node, ast.Attribute):
            if node.attr == 'modules' and isinstance(node.value, ast.Name) and node.value.id in sys_aliases:
                return True
            if isinstance(node.value, ast.Name) and node.value.id in tracked:
                if node.attr == '__dict__' or isinstance(node.ctx, (ast.Store, ast.Del)):
                    return True
        if isinstance(node, ast.Call):
            call_name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            first_is_tracked = bool(node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in tracked)
            if call_name in mutation_calls | vars_calls and first_is_tracked:
                return True
            if call_name == 'getattr' and first_is_tracked and len(node.args) > 1:
                if not isinstance(node.args[1], ast.Constant) or node.args[1].value in {'__dict__', 'modules', 'setattr', 'delattr'}:
                    return True
    return False


def _module_name(path):
    parts = list(Path(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _import_module(path, node):
    if not node.level:
        return node.module
    module = _module_name(path).split('.')
    package = module if Path(path).name == '__init__.py' else module[:-1]
    if node.level > len(package):
        return None
    base = package[:len(package) - node.level + 1]
    return '.'.join(base + (node.module.split('.') if node.module else []))


def build_proofs(source, paths=None):
    source = Path(source)
    paths = sorted(paths if paths is not None else (str(path.relative_to(source)) for path in (source / 'sqlglot').rglob('*.py')))
    modules, parsed, bindings = {}, {}, {}
    for path in paths:
        code = (source / path).read_bytes()
        compile(code, path, 'exec')
        tree = compile(code, path, 'exec', ast.PyCF_ONLY_AST)
        parsed[path] = tree
        modules[_module_name(path)] = path
        inventory = ModuleBindings()
        inventory.visit(tree)
        if has_namespace_mutation(tree):
            inventory.opaque_bindings = True
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id in {'exec', 'eval', 'globals', 'locals'} for node in ast.walk(tree)):
            inventory.opaque_bindings = True
        # Even writes performed later by local functions invalidate a stable alias.
        global_names = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
        bindings[path] = (inventory, global_names)
    files, unresolved = {}, []
    for path, tree in parsed.items():
        inventory, global_names = bindings[path]
        if inventory.opaque_bindings:
            continue
        stable = lambda name: inventory.counts[name] == 1 and name not in global_names
        future_annotations = any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
                                 and any(item.name == 'annotations' for item in node.names) for node in tree.body)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and stable(node.name) and is_closure_factory(node, future_annotations):
                files.setdefault(path, {})[node.name] = {
                    'factory_file': path, 'factory_name': node.name, 'factory_ast_sha256': ast_hash(node),
                    'binding': 'definition', 'import_chain': [],
                }
            elif isinstance(node, ast.ImportFrom):
                target_path = modules.get(_import_module(path, node))
                if target_path:
                    for item in node.names:
                        local = item.asname or item.name
                        if item.name != '*' and stable(local):
                            unresolved.append((path, local, target_path, item.name, ast_hash(node)))
    # Resolve direct aliases and reexports to the original proved definition.
    while unresolved:
        remaining, progress = [], False
        for path, local, target, imported, import_hash in unresolved:
            proof = files.get(target, {}).get(imported)
            if proof is None:
                remaining.append((path, local, target, imported, import_hash))
                continue
            record = dict(proof, binding='import')
            record['import_chain'] = [
                {'file': path, 'local_name': local, 'from_file': target,
                 'imported_name': imported, 'import_ast_sha256': import_hash}, *proof['import_chain']]
            files.setdefault(path, {})[local] = record
            progress = True
        if not progress:
            break
        unresolved = remaining
    return {'schema': SCHEMA, 'baseline_revision': REVISION, 'python_version': python_version(),
            'proof_kind': PROOF_KIND, 'files': files,
            'source_ast_sha256': {path: ast_hash(parsed[path]) for path in files}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='default: print JSON to stdout')
    args = parser.parse_args()
    source = args.source.resolve()
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVISION or subprocess.check_output(['git', '-C', str(source), 'diff', '--name-only', 'HEAD'], text=True).strip():
        raise SystemExit('factory proofs require the clean pinned baseline')
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', 'sqlglot'], text=True).splitlines()
    result = build_proofs(source, [path for path in tracked if path.endswith('.py')])
    encoded = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end='')
    definitions = sum(record['binding'] == 'definition' for names in result['files'].values() for record in names.values())
    total = sum(map(len, result['files'].values()))
    print(f'{definitions} factory definitions; {total} proved local names in {len(result["files"])} files', file=sys.stderr)


if __name__ == '__main__':
    main()
