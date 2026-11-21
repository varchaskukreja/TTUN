"""Adversarial cases for the opt-in granularity experiment, not label fitting."""
import difflib
import sys
import tempfile
import unittest
from pathlib import Path

from evaluate_granularity import StatementSelector, symbol_signature


class GranularityTests(unittest.TestCase):
    def trace_cases(self, source, calls, isolated=False):
        """Trace full calls, optionally rebuilding pristine module state per test."""
        namespace = {}
        code = compile(source, "m.py", "exec")
        exec(code, namespace)
        lines, values = {}, []
        for test_number, call in enumerate(calls):
            if isolated:
                namespace = {}
                exec(code, namespace)
            def trace(frame, event, arg):
                if event == 'line' and frame.f_code.co_filename == 'm.py':
                    lines.setdefault(str(frame.f_lineno), set()).add(test_number)
                return trace
            previous = sys.gettrace()
            sys.settrace(trace)
            try:
                values.append(call(namespace))
            finally:
                sys.settrace(previous)
        return {line: sorted(tests) for line, tests in lines.items()}, values

    def trace_calls(self, source, call):
        return self.trace_cases(source, [call, call])

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

    def test_known_unsoundness_shared_cache_write_misses_later_consumer(self):
        # Passing this test documents a counterexample, not algorithm correctness.
        before = 'cache = {}\ndef f(x):\n    if x not in cache:\n        cache[x] = x + 1\n    return cache[x]\n'
        after = before.replace('x + 1', 'x + 2')
        baseline_lines, expected = self.trace_calls(before, lambda ns: ns['f'](1))
        _, actual = self.trace_calls(after, lambda ns: ns['f'](1))
        self.assertEqual(baseline_lines['4'], [0])
        self.assertEqual(expected, [2, 2])
        self.assertEqual(actual, [3, 3])
        failing_tests = {i for i in (0, 1) if expected[i] != actual[i]}
        selected = self.select(before, after, baseline_lines)
        self.assertEqual(selected, {0})
        self.assertEqual(failing_tests - selected, {1}, 'Known unsafe narrowing must remain explicit')

    def test_known_unsoundness_lazy_global_registry_misses_later_consumer(self):
        before = 'registry = None\ndef resolve():\n    global registry\n    if registry is None:\n        registry = 1\n    return registry\n'
        after = before.replace('registry = 1', 'registry = 2')
        baseline_lines, expected = self.trace_calls(before, lambda ns: ns['resolve']())
        _, actual = self.trace_calls(after, lambda ns: ns['resolve']())
        self.assertEqual(baseline_lines['5'], [0])
        self.assertEqual(expected, [1, 1])
        self.assertEqual(actual, [2, 2])
        failing_tests = {i for i in (0, 1) if expected[i] != actual[i]}
        selected = self.select(before, after, baseline_lines)
        self.assertEqual(selected, {0})
        self.assertEqual(failing_tests - selected, {1}, 'Known unsafe narrowing must remain explicit')

    def test_isolated_coverage_fixes_sequential_mutable_cache_counterexample(self):
        before = 'cache = {}\ndef f(x):\n    if x not in cache:\n        cache[x] = x + 1\n    return cache[x]\n'
        after = before.replace('x + 1', 'x + 2')
        calls = [lambda ns: ns['f'](1)] * 2
        isolated_lines, expected = self.trace_cases(before, calls, isolated=True)
        _, actual = self.trace_cases(after, calls, isolated=True)
        self.assertEqual(isolated_lines['4'], [0, 1])
        self.assertEqual(expected, [2, 2])
        self.assertEqual(actual, [3, 3])
        self.assertEqual(self.select(before, after, isolated_lines), {0, 1})

    def test_isolated_coverage_fixes_sequential_lazy_registry_counterexample(self):
        before = 'registry = None\ndef resolve():\n    global registry\n    if registry is None:\n        registry = 1\n    return registry\n'
        after = before.replace('registry = 1', 'registry = 2')
        calls = [lambda ns: ns['resolve']()] * 2
        isolated_lines, expected = self.trace_cases(before, calls, isolated=True)
        _, actual = self.trace_cases(after, calls, isolated=True)
        self.assertEqual(isolated_lines['5'], [0, 1])
        self.assertEqual(expected, [1, 1])
        self.assertEqual(actual, [2, 2])
        self.assertEqual(self.select(before, after, isolated_lines), {0, 1})

    def test_known_unsoundness_isolated_factory_code_metadata_consumer(self):
        # Fresh test state does not make executable-line tracing track metadata.
        before = 'def factory():\n    def inner():\n        return 1\n    return inner\n'
        after = before.replace('return 1', 'return 2')
        calls = [lambda ns: ns['factory']()(), lambda ns: ns['factory']().__code__.co_consts]
        isolated_lines, expected = self.trace_cases(before, calls, isolated=True)
        _, actual = self.trace_cases(after, calls, isolated=True)
        self.assertEqual(isolated_lines['3'], [0])
        self.assertTrue(all(a != b for a, b in zip(expected, actual)))
        self.assertEqual(self.select(before, after, isolated_lines), {0})

    def test_known_unsoundness_warm_and_isolated_union_cross_test_state_producer(self):
        # Isolation cannot assign a producer's lines to a different test that
        # only observes its state when the evaluator runs tests together.
        before = 'state = 0\ndef initialize():\n    global state\n    state = 0\ndef read():\n    return state\n'
        after = before.replace('    state = 0', '    state = 1')
        calls = [lambda ns: (ns['initialize'](), ns['read']())[1], lambda ns: ns['read']()]
        warm_lines, expected = self.trace_cases(before, calls)
        cold_lines, _ = self.trace_cases(before, calls, isolated=True)
        _, actual = self.trace_cases(after, calls)
        union = {line: sorted(set(warm_lines.get(line, [])) | set(cold_lines.get(line, [])))
                 for line in warm_lines.keys() | cold_lines.keys()}
        self.assertEqual(expected, [0, 0])
        self.assertEqual(actual, [1, 1])
        self.assertEqual(union['4'], [0])
        self.assertEqual(self.select(before, after, union), {0})


if __name__ == "__main__":
    unittest.main()
