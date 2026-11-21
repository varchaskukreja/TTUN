"""Optional class factory candidate policy; production default remains strict.

Requires interpreter-matched proofs derived only from the pinned baseline and
an external whole-commit ImportGuard. Proofs establish construction purity, not
class-consumer completeness. Changes to factory provenance are rejected.
"""
from __future__ import annotations

import ast
from pathlib import Path

import class_selection
from factory_proof_format import REVISION, SCHEMA, PROOF_KIND, ast_hash, python_version

STRICT_CANDIDATES = class_selection.class_candidates


def _shadowed(classes, name):
    # Deliberately overapproximate: reject the name anywhere in any enclosing
    # class's binding syntax, even if a method's local would not shadow it.
    for owner in classes:
        for node in ast.walk(owner):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == name:
                return True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                return True
            if isinstance(node, ast.arg) and node.arg == name:
                return True
            if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == name:
                return True
            if isinstance(node, ast.MatchMapping) and node.rest == name:
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'exec', 'eval', 'locals', 'globals'}:
                return True
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if any(item.name == '*' or (item.asname or item.name.split('.')[0]) == name for item in node.names):
                    return True
    return False


def _valid_proof(name, proofs, path, changed_paths, tree, validator):
    proof = proofs.get('files', {}).get(path, {}).get(name)
    if not proof or not validator.validate_provenance(name, proof, path, tree):
        return False
    if proof['factory_file'] in changed_paths:
        return False
    for step in proof['import_chain']:
        if step['from_file'] in changed_paths:
            return False
        if step['file'] in changed_paths and step['file'] != path:
            return False
        if step['file'] == path:
            if not any(isinstance(node, ast.ImportFrom) and validator.tree_hash(node) == step['import_ast_sha256']
                       for node in tree.body):
                return False
    return True


def factory_candidates(tree, new_tree, line, info, proofs, path, changed_paths, validator=None):
    strict = STRICT_CANDIDATES(tree, new_tree, line, info)
    if strict is not None:
        return strict
    if validator is None or not validator.valid:
        return None
    changed = set()
    if not class_selection._constant_changes(tree, new_tree, changed) or not changed:
        return None
    selected, accepted = set(), set()

    def visit(node, classes=(), in_function=False):
        nonlocal selected
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            in_function = True
        if isinstance(node, ast.ClassDef):
            classes = (*classes, node)
            if not in_function:
                for statement in node.body:
                    if isinstance(statement, ast.Assign):
                        targets, value = statement.targets, statement.value
                    elif isinstance(statement, ast.AnnAssign):
                        targets, value = [statement.target], statement.value
                    else:
                        continue
                    if value is None or not value.lineno <= line <= value.end_lineno:
                        continue
                    if any(not isinstance(target, ast.Name) or (target.id.startswith('__') and target.id.endswith('__')) for target in targets):
                        continue
                    consumers = class_selection.assignment_consumers(
                        info, '.'.join(owner.name for owner in classes),
                        [target.id for target in targets])
                    if not consumers:
                        continue
                    changed_paths_in_value = []

                    def paths_to_changes(current, ancestors=()):
                        route = (*ancestors, current)
                        if id(current) in changed and current.lineno <= line <= current.end_lineno:
                            changed_paths_in_value.append(route)
                        for child in ast.iter_child_nodes(current):
                            paths_to_changes(child, route)

                    paths_to_changes(value)
                    if not changed_paths_in_value:
                        continue
                    valid = True
                    for route in changed_paths_in_value:
                        calls = [item for item in route if isinstance(item, ast.Call)]
                        if not calls or any(not isinstance(item, (*class_selection._LITERAL_NODES, ast.Call, ast.keyword)) for item in route):
                            valid = False
                            break
                        for call in calls:
                            if (not isinstance(call.func, ast.Name) or _shadowed(classes, call.func.id)
                                    or not _valid_proof(call.func.id, proofs, path, changed_paths, tree, validator)
                                    or any(keyword.arg is None for keyword in call.keywords)):
                                valid = False
                                break
                            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
                            if any(not isinstance(child, class_selection._LITERAL_NODES)
                                   for argument in arguments for child in ast.walk(argument)):
                                valid = False
                                break
                        if not valid:
                            break
                    if valid:
                        selected.update(consumers)
                        accepted.update(id(route[-1]) for route in changed_paths_in_value)
        for child in ast.iter_child_nodes(node):
            visit(child, classes, in_function)

    visit(tree)
    changed_here = {id(node) for node in ast.walk(tree) if id(node) in changed and node.lineno <= line <= node.end_lineno}
    return selected if selected and changed_here <= accepted else None


def _module_name(path):
    parts = list(Path(path).with_suffix('').parts)
    if parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts)


def _import_module(path, node):
    if not node.level:
        return node.module
    parts = _module_name(path).split('.')
    package = parts if Path(path).name == '__init__.py' else parts[:-1]
    if node.level > len(package):
        return None
    return '.'.join(package[:len(package) - node.level + 1] + (node.module.split('.') if node.module else []))


class FactoryCandidates:
    """Opt-in Selector callback with cached baseline provenance verification."""
    def __init__(self, source, proofs):
        self.source = Path(source)
        self.proofs = proofs
        self.cache = {}
        self.hash_cache = {}
        self.valid = (isinstance(proofs, dict) and type(proofs.get('schema')) is int
                      and proofs['schema'] == SCHEMA and proofs.get('proof_kind') == PROOF_KIND
                      and proofs.get('baseline_revision') == REVISION
                      and type(proofs.get('python_version')) is list
                      and all(type(value) is int for value in proofs['python_version'])
                      and proofs.get('python_version') == python_version()
                      and isinstance(proofs.get('files'), dict)
                      and isinstance(proofs.get('source_ast_sha256'), dict))

    def tree_hash(self, tree):
        # Baseline trees are retained and never mutated. Keeping the object in
        # the cache also prevents Python from reusing its identity for another.
        cached = self.hash_cache.get(id(tree))
        if cached is None or cached[0] is not tree:
            cached = (tree, ast_hash(tree))
            self.hash_cache[id(tree)] = cached
        return cached[1]

    def baseline_tree(self, path, current_path, current_tree):
        relative = Path(path)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'sqlglot':
            raise ValueError('invalid proof source path')
        if path == current_path:
            tree = current_tree
        else:
            if path not in self.cache:
                self.cache[path] = compile((self.source / relative).read_bytes(), path, 'exec', ast.PyCF_ONLY_AST)
            tree = self.cache[path]
        if self.tree_hash(tree) != self.proofs['source_ast_sha256'].get(path):
            raise ValueError('stale baseline module proof')
        return tree

    def validate_provenance(self, name, proof, path, tree):
        factory_tree = self.baseline_tree(proof['factory_file'], path, tree)
        factory = next((node for node in factory_tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == proof['factory_name']), None)
        if factory is None or self.tree_hash(factory) != proof['factory_ast_sha256']:
            return False
        self.baseline_tree(path, path, tree)
        expected_file, expected_name = path, name
        for step in proof['import_chain']:
            if step['file'] != expected_file or step['local_name'] != expected_name:
                return False
            imported_tree = self.baseline_tree(step['file'], path, tree)
            self.baseline_tree(step['from_file'], path, tree)
            found = any(isinstance(node, ast.ImportFrom)
                        and self.tree_hash(node) == step['import_ast_sha256']
                        and _import_module(step['file'], node) == _module_name(step['from_file'])
                        and any(item.name == step['imported_name'] and (item.asname or item.name) == step['local_name']
                                for item in node.names)
                        for node in imported_tree.body)
            if not found:
                return False
            expected_file, expected_name = step['from_file'], step['imported_name']
        return expected_file == proof['factory_file'] and expected_name == proof['factory_name']

    def __call__(self, tree, new_tree, line, info, path, changed_paths):
        try:
            return factory_candidates(tree, new_tree, line, info, self.proofs, path, changed_paths, self)
        except (KeyError, TypeError, ValueError, OSError, SyntaxError, AttributeError):
            return None

