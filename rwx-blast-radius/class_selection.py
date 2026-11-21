"""Recognize narrowly scoped class-literal edits using baseline consumers.

This helper does not establish import safety. Its caller must validate patched
collection before trusting class consumers. None requests conservative fallback.
"""
from __future__ import annotations

import ast


_LITERAL_NODES = (
    ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.UnaryOp,
    ast.UAdd, ast.USub, ast.Invert, ast.Not, ast.Load,
)


def _constant_changes(old, new, changed):
    """Check every AST field except Constant.value, preserving exact value types."""
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        if type(old.value) is not type(new.value) or old.kind != new.kind:
            return False
        if old.value != new.value:
            changed.add(id(old))
        return True
    if isinstance(old, ast.AST):
        return all(_constant_changes(getattr(old, name), getattr(new, name), changed)
                   for name in old._fields)
    if isinstance(old, list):
        return len(old) == len(new) and all(_constant_changes(a, b, changed) for a, b in zip(old, new))
    return old == new


def class_candidates(tree, new_tree, line, info):
    """Return baseline map test indices for a direct class literal-value edit.

    Both trees must have identical non-constant structure. Qualified names use
    lexical class nesting; classes inside functions are deliberately unsupported.
    No test IDs, patch IDs, sample labels, or mutation kinds are consulted.
    """
    changed = set()
    if not _constant_changes(tree, new_tree, changed) or not changed:
        return None
    candidates = []
    accepted_constants = set()

    def visit(node, classes=(), in_function=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            in_function = True
        if isinstance(node, ast.ClassDef):
            classes = (*classes, node.name)
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
                    if not targets or any(not isinstance(target, ast.Name)
                                          or (target.id.startswith('__') and target.id.endswith('__'))
                                          for target in targets):
                        continue
                    value_nodes = tuple(ast.walk(value))
                    if not all(isinstance(child, _LITERAL_NODES) for child in value_nodes):
                        continue
                    if not any(id(child) in changed for child in value_nodes):
                        continue
                    consumers = info.get('class_consumers', {}).get('.'.join(classes))
                    if consumers:
                        candidates.append(set(consumers))
                        accepted_constants.update(id(child) for child in value_nodes if isinstance(child, ast.Constant))
        for child in ast.iter_child_nodes(node):
            visit(child, classes, in_function)

    visit(tree)
    if any(id(node) in changed and id(node) not in accepted_constants
           and node.lineno <= line <= node.end_lineno for node in ast.walk(tree)
           if isinstance(node, ast.Constant)):
        return None
    # A shared physical line can contain multiple assignments. Combine each
    # applicable class's consumers instead of relying on traversal order.
    return set().union(*candidates) if candidates else None
