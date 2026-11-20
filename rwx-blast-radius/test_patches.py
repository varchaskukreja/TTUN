"""Boundary cases for applying an unseen unified diff to pinned source."""
import difflib
import unittest

from patches import apply_file, parse_patch


def diff(before, after, context=3):
    return "diff --git a/example.py b/example.py\n" + "".join(
        difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                             fromfile="a/example.py", tofile="b/example.py", n=context)
    )


class PatchTests(unittest.TestCase):
    def apply(self, before, after, context=3):
        changes = parse_patch(diff(before, after, context))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["path"], "example.py")
        result, changed = apply_file(before, changes[0])
        self.assertEqual(result, after)
        return changed

    def test_zero_count_insertions_at_all_boundaries(self):
        for before, after in [("", "first\n"), ("a\nb\n", "first\na\nb\n"),
                              ("a\nb\n", "a\ninsert\nb\n"), ("a\nb\n", "a\nb\nlast\n")]:
            with self.subTest(before=before, after=after):
                self.apply(before, after, 0)

    def test_delete_at_eof(self):
        self.assertEqual(self.apply("a\nb\nc\n", "a\nb\n", 0), {3})

    def test_delete_entire_file(self):
        self.assertEqual(self.apply("a\nb\n", "", 0), {1, 2})

    def test_multiline_replacement(self):
        before = "def f():\n    value = (\n        1\n        + 2\n    )\n    return value\n"
        after = "def f():\n    value = (\n        7\n        - 3\n        + 9\n    )\n    return value\n"
        changed = self.apply(before, after)
        self.assertTrue({3, 4}.issubset(changed))

    def test_multiple_hunks_keep_original_coordinates(self):
        before = "".join(f"line{i}\n" for i in range(20))
        after = before.replace("line1\n", "replacement\nextra\n").replace("line18\n", "")
        self.apply(before, after, 1)

    def test_no_newline_at_eof(self):
        patch = "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        output, changed = apply_file("old", parse_patch(patch)[0])
        self.assertEqual(output, "new")
        self.assertEqual(changed, {1})

    def test_reject_mismatching_deleted_text(self):
        change = parse_patch(diff("a\nb\n", "a\nc\n"))[0]
        with self.assertRaises(ValueError):
            apply_file("a\nwrong\n", change)

    def test_reject_mismatching_context(self):
        change = parse_patch(diff("a\nb\n", "a\nc\n"))[0]
        with self.assertRaises(ValueError):
            apply_file("wrong\nb\n", change)

    def test_reject_overlapping_hunks(self):
        change = parse_patch(diff("a\nb\n", "a\nc\n", 0))[0]
        change["hunks"].append(change["hunks"][0])
        with self.assertRaises(ValueError):
            apply_file("a\nb\n", change)

    def test_reject_wrong_old_count(self):
        change = parse_patch(diff("a\nb\n", "a\nc\n").replace("@@ -1,2 +1,2 @@", "@@ -1,3 +1,2 @@"))[0]
        with self.assertRaises(ValueError):
            apply_file("a\nb\n", change)

    def test_reject_wrong_new_count(self):
        change = parse_patch(diff("a\nb\n", "a\nc\n").replace("@@ -1,2 +1,2 @@", "@@ -1,2 +1,3 @@"))[0]
        with self.assertRaises(ValueError):
            apply_file("a\nb\n", change)

    def test_reject_insertion_past_eof(self):
        patch = "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n@@ -9,0 +10 @@\n+bad\n"
        with self.assertRaises(ValueError):
            apply_file("only line\n", parse_patch(patch)[0])


if __name__ == "__main__":
    unittest.main()
