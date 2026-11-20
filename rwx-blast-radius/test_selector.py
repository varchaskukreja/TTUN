"""Selection invariants on synthetic source, independent of sample labels."""
import contextlib
import difflib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import selector


def make_patch(path, old, new):
    return f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tests = [{"id": f"test::{i}", "number": 10 + i, "seconds": .1} for i in range(4)]
        self.mapping = {"test_ids": [t["id"] for t in self.tests], "files": {}}

    def add_source(self, path, text, **info):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.mapping["files"][path] = info
        return text

    def select(self, path, old, new):
        return selector.Selector(self.tests, self.mapping, self.root).select(make_patch(path, old, new))

    def test_function_edit_selects_all_function_callers(self):
        old = self.add_source("m.py", "def f(x):\n    value = 1\n    return value + x\n", lines={"2": [0], "3": [1]})
        self.assertEqual(self.select("m.py", old, old.replace("value = 1", "value = 2")), {10, 11})

    def test_fixture_consumers_are_selected(self):
        old = self.add_source("fixtures/data.sql", "SELECT 1;\n", tests=[2], read_tests=[2])
        self.assertEqual(self.select("fixtures/data.sql", old, "SELECT 2;\n"), {12})

    def test_source_reader_selected_without_execution(self):
        old = self.add_source("m.py", "def f():\n    value = 1\n    return value\n", lines={"2": [0]}, read_tests=[3])
        self.assertEqual(self.select("m.py", old, old.replace("value = 1", "value = 2")), {10, 13})

    def test_ast_equivalent_comment_without_readers_selects_nothing(self):
        old = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0, 1]})
        self.assertEqual(self.select("m.py", old, "# explanation\n" + old), set())

    def test_ast_equivalent_comment_retains_source_readers(self):
        old = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0, 1]}, read_tests=[3])
        self.assertEqual(self.select("m.py", old, old.replace("return 1", "return 1  # note")), {13})

    def test_ast_equivalent_comment_retains_missing_and_failed_baseline_tests(self):
        self.mapping["missing_tests"] = ["test::1"]
        self.mapping["baseline_outcomes"] = {"test::2": "failed"}
        old = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0]})
        self.assertEqual(self.select("m.py", old, "# note\n" + old), {11, 12})

    def test_encoding_cookie_comment_cannot_hide_import_failure(self):
        old = self.add_source("m.py", 'def f():\n    return "café"\n', lines={"2": [0]})
        self.assertEqual(self.select("m.py", old, "# coding: ascii\n" + old), {10, 11, 12, 13})

    def test_unknown_encoding_comment_cannot_hide_import_failure(self):
        old = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0]})
        self.assertEqual(self.select("m.py", old, "# coding: not-a-codec\n" + old), {10, 11, 12, 13})

    def test_comment_file_does_not_mask_changed_fixture(self):
        old_py = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0]}, read_tests=[3])
        old_sql = self.add_source("fixtures/data.sql", "SELECT 1;\n", tests=[1])
        comment_patch = make_patch("m.py", old_py, "# note\n" + old_py)
        fixture_patch = make_patch("fixtures/data.sql", old_sql, "SELECT FROM;\n")
        for combined in (comment_patch + fixture_patch, fixture_patch + comment_patch):
            with self.subTest(comment_first=combined.startswith(comment_patch)):
                self.assertEqual(selector.Selector(self.tests, self.mapping, self.root).select(combined), {11, 13})

    def test_constant_requires_conservative_fallback(self):
        old = self.add_source("m.py", "CONSTANT = 1\ndef f():\n    return CONSTANT\n", lines={"1": [0]})
        self.assertEqual(self.select("m.py", old, old.replace("CONSTANT = 1", "CONSTANT = 2")), {10, 11, 12, 13})

    def test_syntax_error_requires_conservative_fallback(self):
        old = self.add_source("m.py", "def f():\n    return 1\n", lines={"2": [0]})
        self.assertEqual(self.select("m.py", old, "def f(:\n    return 1\n"), {10, 11, 12, 13})

    def test_unknown_catalog_ids_cannot_leak_into_selection(self):
        self.mapping["test_ids"].append("test::removed")
        old = self.add_source("m.py", "def f():\n    value = 1\n    return value\n", lines={"2": [0, 4, 999]})
        self.assertEqual(self.select("m.py", old, old.replace("value = 1", "value = 2")), {10})

    def test_new_catalog_test_is_always_selected(self):
        self.mapping["test_ids"] = self.mapping["test_ids"][:3]
        old = self.add_source("m.py", "def f():\n    value = 1\n    return value\n", lines={"2": [0]})
        self.assertEqual(self.select("m.py", old, old.replace("value = 1", "value = 2")), {10, 13})

    def test_missing_baseline_tests_are_always_selected(self):
        self.mapping["missing_tests"] = ["test::2", "test::not-in-catalog"]
        old = self.add_source("fixtures/f.sql", "SELECT 1;\n", tests=[0])
        self.assertEqual(self.select("fixtures/f.sql", old, "SELECT 2;\n"), {10, 12})

    def test_baseline_failures_errors_skips_are_always_selected(self):
        for status in ("failed", "error", "skipped"):
            with self.subTest(status=status):
                self.mapping["baseline_outcomes"] = {"test::2": status, "test::1": "passed", "unknown": status}
                old = self.add_source("fixtures/f.sql", "SELECT 1;\n", tests=[0])
                self.assertEqual(self.select("fixtures/f.sql", old, "SELECT 2;\n"), {10, 12})

    def test_unknown_source_or_dependency_map_falls_back(self):
        self.assertEqual(self.select("missing.py", "x = 1\n", "x = 2\n"), {10, 11, 12, 13})
        (self.root / "missing.py").write_text("x = 1\n")
        self.assertEqual(self.select("missing.py", "x = 1\n", "x = 2\n"), {10, 11, 12, 13})

    def test_multiple_files_union_their_consumers(self):
        old_a = self.add_source("fixtures/a.sql", "SELECT 1;\n", tests=[0])
        old_b = self.add_source("fixtures/b.sql", "SELECT 2;\n", tests=[1])
        combined = make_patch("fixtures/a.sql", old_a, "SELECT 3;\n") + make_patch("fixtures/b.sql", old_b, "SELECT 4;\n")
        self.assertEqual(selector.Selector(self.tests, self.mapping, self.root).select(combined), {10, 11})

    def test_replacement_at_function_end_does_not_select_next_function(self):
        old = self.add_source("m.py", "def f():\n    return 1\ndef g():\n    return 3\n", lines={"2": [0], "4": [1]})
        self.assertEqual(self.select("m.py", old, old.replace("return 1", "return 2")), {10})

    def test_replacement_before_blank_line_does_not_fall_back(self):
        old = self.add_source("m.py", "def f():\n    return 1\n\ndef g():\n    return 3\n", lines={"2": [0], "5": [1]})
        self.assertEqual(self.select("m.py", old, old.replace("return 1", "return 2")), {10})

    def test_cli_never_reads_answers(self):
        old = self.add_source("fixtures/data.sql", "SELECT 1;\n", tests=[0])
        data = self.root / "input"
        (data / "commits").mkdir(parents=True)
        (data / "tests.json").write_text(json.dumps({"tests": self.tests}))
        (data / "answers.json").write_text("INVALID AND MUST NEVER BE READ")
        (data / "commits" / "x.patch").write_text(make_patch("fixtures/data.sql", old, "SELECT 2;\n"))
        map_path = self.root / "map.json"
        map_path.write_text(json.dumps(self.mapping))
        real_read = Path.read_text
        def guarded_read(path, *args, **kwargs):
            self.assertNotEqual(path.name, "answers.json", "Runtime accessed answer labels")
            return real_read(path, *args, **kwargs)
        stdout = io.StringIO()
        with patch.object(Path, "read_text", guarded_read), patch("sys.argv", ["selector.py", "--source", str(self.root), "--data", str(data), "--map", str(map_path)]), contextlib.redirect_stdout(stdout):
            selector.main()
        self.assertEqual(json.loads(stdout.getvalue()), {"commit": "x", "workers": [[10]]})


if __name__ == "__main__":
    unittest.main()
