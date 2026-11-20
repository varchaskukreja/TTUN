"""Adversarial cases for the opt-in granularity experiment, not label fitting."""
import difflib
import tempfile
import unittest
from pathlib import Path

from evaluate_granularity import StatementSelector, symbol_signature


class GranularityTests(unittest.TestCase):
    def select(self, before, after, lines, guarded=True):
        tests = [{"id": str(i), "number": i, "seconds": .1} for i in range(3)]
        mapping = {"test_ids": ["0", "1", "2"], "files": {"m.py": {"lines": lines}}}
        patch = "diff --git a/m.py b/m.py\n" + "".join(difflib.unified_diff(
            before.splitlines(True), after.splitlines(True), fromfile="a/m.py", tofile="b/m.py"))
        with tempfile.TemporaryDirectory() as root:
            Path(root, "m.py").write_text(before)
            return StatementSelector(tests, mapping, Path(root), guarded=guarded).select(patch)

    def test_local_binding_change_retains_early_caller(self):
        before = "x = 1\ndef f(flag):\n    if flag:\n        return x\n    y = 2\n    return y\n"
        after = before.replace("y = 2", "x = 2")
        lines = {"3": [0, 1], "4": [0], "5": [1], "6": [1]}
        self.assertEqual(self.select(before, after, lines, guarded=False), {1})
        self.assertEqual(self.select(before, after, lines), {0, 1})
        old_ns, new_ns = {}, {}
        exec(before, old_ns)
        exec(after, new_ns)
        self.assertEqual(old_ns["f"](True), 1)
        with self.assertRaises(UnboundLocalError):
            new_ns["f"](True)

    def test_nested_closure_change_retains_outer_factory_caller(self):
        before = "x = 1\ndef outer():\n    y = 2\n    def inner():\n        return x\n    return inner\n"
        after = before.replace("return x", "return y")
        lines = {"3": [0, 1], "4": [0, 1], "5": [1], "6": [0, 1]}
        self.assertEqual(self.select(before, after, lines, guarded=False), {1})
        self.assertEqual(self.select(before, after, lines), {0, 1})
        old_ns, new_ns = {}, {}
        exec(before, old_ns)
        exec(after, new_ns)
        self.assertIsNone(old_ns["outer"]().__closure__)
        self.assertIsNotNone(new_ns["outer"]().__closure__)

    def test_expression_only_change_can_narrow(self):
        before = "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        after = before.replace("return 2", "return 3")
        self.assertEqual(self.select(before, after, {"2": [0, 1], "3": [0], "4": [1]}), {1})

    def test_changed_condition_includes_both_baseline_branches(self):
        before = "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        after = before.replace("if flag:", "if not flag:")
        self.assertEqual(self.select(before, after, {"2": [0, 1], "3": [0], "4": [1]}), {0, 1})

    def test_statement_topology_change_uses_function_fallback(self):
        before = "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        after = before.replace("    return 2", "    raise ValueError()")
        self.assertEqual(self.select(before, after, {"2": [0, 1], "3": [0], "4": [1]}), {0, 1})

    def test_indentation_change_uses_function_fallback(self):
        before = "def f(flag):\n    if flag:\n        value = 1\n    return 2\n"
        after = before.replace("    return 2", "        return 2")
        self.assertEqual(self.select(before, after, {"2": [0, 1], "3": [0], "4": [1]}), {0, 1})

    def test_invalid_ast_syntax_selects_every_test(self):
        before = "def f():\n    return 1\n"
        self.assertEqual(self.select(before, "def f(:\n    return 1\n", {"2": [0]}), {0, 1, 2})

    def test_compilation_error_after_successful_ast_parse_selects_every_test(self):
        before = "def f():\n    return 1\n"
        self.assertEqual(self.select(before, "def f():\n    break\n", {"2": [0]}), {0, 1, 2})

    def test_insertion_keeps_function_fallback(self):
        before = "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        after = before.replace("    return 2", "    flag = False\n    return 2")
        self.assertEqual(self.select(before, after, {"2": [0, 1], "3": [0], "4": [1]}), {0, 1})

    def test_recursive_symbol_signature_detects_nested_free_variables(self):
        before = "x = 1\ndef outer():\n    y = 2\n    def inner():\n        return x\n    return inner\n"
        self.assertNotEqual(symbol_signature(before, "m.py"), symbol_signature(before.replace("return x", "return y"), "m.py"))


if __name__ == "__main__":
    unittest.main()
