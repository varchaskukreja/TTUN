"""Research-only linear text/token gate for exact equal-width comparison edits.

Capture instrumentation is unchanged; this module is postprocessing only. AST
construction and comparison remain linear in input/tree size; no SequenceMatcher is used.
"""
import ast
import io
import tokenize


def exact_operator_edit(path, before, after, changed_files=1):
    """Accept one Is↔Eq token, identical remaining characters, and one AST edit.

    Inputs are decoded Python source strings, as read by qualify_patch. Invalid
    syntax/token streams and unsupported changes return None.
    """
    if changed_files != 1 or type(before) is not str or type(after) is not str or len(before) != len(after):
        return None
    offset = next((i for i, (left, right) in enumerate(zip(before, after)) if left != right), None)
    if offset is None:
        return None
    old_text, new_text = before[offset:offset + 2], after[offset:offset + 2]
    if (old_text, new_text) not in (('is', '=='), ('==', 'is')) or before[offset + 2:] != after[offset + 2:]:
        return None
    try:
        old_tokens = list(tokenize.generate_tokens(io.StringIO(before).readline))
        new_tokens = list(tokenize.generate_tokens(io.StringIO(after).readline))
        if len(old_tokens) != len(new_tokens):
            return None
        changes = [(left, right) for left, right in zip(old_tokens, new_tokens)
                   if (left.type, left.string) != (right.type, right.string)]
        if len(changes) != 1:
            return None
        old_token, new_token = changes[0]
        expected = {'is': tokenize.NAME, '==': tokenize.OP}
        if (old_token.string, new_token.string) != (old_text, new_text) or old_token.type != expected[old_text] or new_token.type != expected[new_text]:
            return None
        # Bind the one differing token to the one differing source span.
        line = before.count('\n', 0, offset) + 1
        column = offset - (before.rfind('\n', 0, offset) + 1)
        if old_token.start != (line, column) or new_token.start != (line, column):
            return None
        old = compile(before.encode(), path, 'exec', ast.PyCF_ONLY_AST)
        new = compile(after.encode(), path, 'exec', ast.PyCF_ONLY_AST)
        old_comparisons = [node for node in ast.walk(old) if isinstance(node, ast.Compare)]
        new_comparisons = [node for node in ast.walk(new) if isinstance(node, ast.Compare)]
        if len(old_comparisons) != len(new_comparisons):
            return None
        edits = [(left, right) for left, right in zip(old_comparisons, new_comparisons)
                 if [type(op) for op in left.ops] != [type(op) for op in right.ops]]
        if len(edits) != 1:
            return None
        comparison, changed = edits[0]
        if len(comparison.ops) != 1 or len(changed.ops) != 1:
            return None
        left_op, right_op = comparison.ops[0], changed.ops[0]
        if {type(left_op), type(right_op)} != {ast.Is, ast.Eq}:
            return None
        comparison.ops[0] = type(right_op)()
        equivalent = ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False)
        comparison.ops[0] = left_op
        if not equivalent:
            return None
        return {'site': f'{path}:{comparison.lineno}:{comparison.col_offset}:{comparison.end_lineno}:{comparison.end_col_offset}',
                'old': type(left_op).__name__, 'new': type(right_op).__name__}
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None
