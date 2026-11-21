import difflib
import io
import tokenize
import unittest

from comparison_types import single_operator_edit
from linear_operator_edit import exact_operator_edit


def reference(path, before, after, changed_files=1):
    """Exact current qualify_patch gate, before observed-certificate lookup."""
    try:
        edit = single_operator_edit(path, before, after, changed_files)
        if not edit:
            return None
        def tokens(text):
            return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)]
        old_tokens, new_tokens = tokens(before), tokens(after)
        differences = [(old_tokens[a:b], new_tokens[c:d]) for tag, a, b, c, d in difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False).get_opcodes() if tag != 'equal']
        names = {'Is': ['is'], 'IsNot': ['is', 'not'], 'Eq': ['=='], 'NotEq': ['!=']}
        if len(differences) != 1 or [[word for _, word in side] for side in differences[0]] != [names[edit['old']], names[edit['new']]]:
            return None
        if {edit['old'], edit['new']} != {'Is', 'Eq'}:
            return None
        text_edits = [(before[a:b], after[c:d]) for tag, a, b, c, d in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes() if tag != 'equal']
        return edit if text_edits == [(names[edit['old']][0], names[edit['new']][0])] else None
    except (ValueError, SyntaxError, tokenize.TokenError):
        return None


class ExactEditTests(unittest.TestCase):
    def compare(self, before, after, expected, changed_files=1):
        old = reference('synthetic.py', before, after, changed_files)
        new = exact_operator_edit('synthetic.py', before, after, changed_files)
        self.assertEqual(old, new)
        self.assertEqual(new is not None, expected)

    def test_valid_layouts_unicode_and_nested(self):
        for before in ('x = a is None\n', 'x = (a is None)\n', 'x = a is (b is None)\n',
                       'π = "☃"; x = a is None\n', 'x = a is None\r\n', 'x = a is \\\n None\n',
                       'x = f"{a is None}"\n', 'x = (\n a is None\n)\n'):
            with self.subTest(before=before):
                after = before.replace(' is ', ' == ', 1)
                self.compare(before, after, True)
                self.compare(after, before, True)

    def test_comments_strings_whitespace_and_chains_rejected(self):
        for before, after in [
            ('x = "is"\n', 'x = "=="\n'), ('# is\nx=1\n', '# ==\nx=1\n'),
            ('x = a is None\n', 'x = a == None # changed\n'),
            ('x = a is None\n', 'x  = a == None\n'),
            ('x = a is b is c\n', 'x = a == b is c\n'),
            ('x = a is not b\n', 'x = a != b\n'),
            ('x = a is b; y = c is d\n', 'x = a == b; y = c == d\n'),
            ('x = f"{a is None=}"\n', 'x = f"{a == None=}"\n'),
            ('x = a is None\n', 'x = a is None\n'),
            ('x = a is None\n', 'x = a == None\n\n')]:
            with self.subTest(before=before, after=after):
                self.compare(before, after, False)

    def test_whole_patch_multiple_files_rejected(self):
        self.compare('x = a is b\n', 'x = a == b\n', False, changed_files=2)

    def test_repeated_context_exposes_legacy_false_rejections(self):
        before = ''.join(f'v{i % 3} = a is None\n' for i in range(18))
        legacy_rejections = 0
        for index in range(18):
            positions = [i for i in range(len(before)) if before.startswith(' is ', i)]
            pos = positions[index]+1
            after = before[:pos]+'=='+before[pos+2:]
            proposed = exact_operator_edit('synthetic.py', before, after)
            legacy = reference('synthetic.py', before, after)
            self.assertIsNotNone(proposed)
            if legacy is None:
                legacy_rejections += 1
            else:
                self.assertEqual(legacy, proposed)
        self.assertGreater(legacy_rejections, 0)


if __name__ == '__main__':
    unittest.main()
