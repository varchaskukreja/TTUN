import enum
import difflib
import types
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

from comparison_types import Recorder, ReaderAudit, Finder, describe_type, observed_certificate, single_operator_edit, qualify_patch


class ComparisonTypesTests(unittest.TestCase):
    def certificate(self, value, other=None, before='result = value is other', after='result = value == other'):
        recorder = Recorder(lambda: 'test')
        recorder.execute(before, globals_={'value': value, 'other': other})
        return observed_certificate(recorder.snapshot(), 'synthetic.py', before, after)

    def test_identity_none_is_conditional_only(self):
        for value, other in [(None, None), (object(), None), (None, object())]:
            certificate = self.certificate(value, other)
            self.assertTrue(certificate['equivalent_for_observed_types'])
            self.assertFalse(certificate['unconditional'])
            self.assertFalse(certificate['narrowing_authorized'])

    def test_operands_once_subclass_dispatch_and_nonbool_result(self):
        events = []
        class Marker:
            def __bool__(self):
                raise AssertionError('must not coerce rich result')
        marker = Marker()
        class Parent:
            def __eq__(self, other):
                events.append('parent.eq')
                return NotImplemented
        class Child(Parent):
            def __eq__(self, other):
                events.append('child.eq')
                return marker
        def left():
            events.append('left')
            return Parent()
        def right():
            events.append('right')
            return Child()
        result = Recorder().execute('result = left() == right()', globals_={'left': left, 'right': right})['result']
        self.assertIs(result, marker)
        self.assertEqual(events, ['left', 'right', 'child.eq'])

    def test_is_does_not_invoke_custom_equality_and_certificate_rejects(self):
        calls = []
        class Custom:
            def __eq__(self, other):
                calls.append(other)
                return True
        self.assertFalse(self.certificate(Custom())['equivalent_for_observed_types'])
        self.assertEqual(calls, [])

    def test_independent_ne_override_rejected(self):
        class Custom:
            def __ne__(self, other):
                return False
        self.assertTrue(self.certificate(Custom())['equivalent_for_observed_types'])
        self.assertFalse(self.certificate(Custom(), before='result = value is not other', after='result = value != other')['equivalent_for_observed_types'])

    def test_identity_not_none(self):
        self.assertTrue(self.certificate(object(), before='result = value is not other', after='result = value != other')['equivalent_for_observed_types'])

    def test_regular_enum_same_exact_type_only(self):
        class Color(enum.Enum):
            RED = 1
            BLUE = 2
        class Other(enum.Enum):
            RED = 1
        self.assertTrue(self.certificate(Color.RED, Color.BLUE)['equivalent_for_observed_types'])
        self.assertFalse(self.certificate(Color.RED, Other.RED)['equivalent_for_observed_types'])
        class Number(enum.IntEnum):
            ONE = 1
        class Word(enum.StrEnum):
            ONE = 'one'
        for value in (Number.ONE, Word.ONE):
            self.assertTrue(self.certificate(value, value)['equivalent_for_observed_types'])

    def test_custom_enum_equality_rejected(self):
        class Custom(enum.Enum):
            ONE = 1
            def __eq__(self, other):
                return True
        self.assertFalse(self.certificate(Custom.ONE, Custom.ONE)['equivalent_for_observed_types'])

    def test_cross_test_slot_mutation_is_not_cached(self):
        class Dynamic:
            pass
        context = ['first']
        recorder = Recorder(lambda: context[0])
        before = 'def f(value):\n    return value is None\n'
        after = before.replace(' is ', ' == ')
        f = recorder.execute(before)['f']
        f(Dynamic())
        self.assertTrue(observed_certificate(recorder.snapshot(), 'synthetic.py', before, after)['equivalent_for_observed_types'])
        Dynamic.__eq__ = lambda self, other: True
        context[0] = 'second'
        self.assertFalse(f(Dynamic()))
        self.assertFalse(observed_certificate(recorder.snapshot(), 'synthetic.py', before, after)['equivalent_for_observed_types'])
        changed = {}
        exec(after, changed)
        self.assertTrue(changed['f'](Dynamic()))

    def test_unobserved_context_is_not_authorized(self):
        certificate = self.certificate(object())
        self.assertEqual(certificate['contexts'], ['test'])
        self.assertFalse(certificate['narrowing_authorized'])
        self.assertIn('retain all source/code-object/bytecode readers and unobserved contexts', certificate['requirements'])

    def test_hostile_metaclass_is_not_executed(self):
        class Meta(type):
            def __getattribute__(self, name):
                if name in ('__mro__', '__dict__', '__qualname__'):
                    raise AssertionError('custom metadata hook invoked')
                return super().__getattribute__(name)
            def __hash__(self):
                raise AssertionError('custom class hash invoked')
            def __eq__(self, other):
                raise AssertionError('custom class equality invoked')
        class Hostile(metaclass=Meta):
            pass
        self.assertFalse(describe_type(Hostile)['supported_metaclass'])
        self.assertFalse(self.certificate(Hostile())['equivalent_for_observed_types'])

    def test_chains_preserve_short_circuit_and_are_not_instrumented(self):
        calls = []
        def make(value):
            calls.append(value)
            return value
        recorder = Recorder()
        self.assertFalse(recorder.execute('result = make(1) == make(2) == make(3)', globals_={'make': make})['result'])
        self.assertEqual(calls, [1, 2])
        self.assertEqual(recorder.sites, {})
        self.assertIsNone(single_operator_edit('s.py', 'x = a is b is c', 'x = a == b is c'))

    def test_exact_whole_module_edit_and_operator_singletons(self):
        before = 'a = x is None\nb = y is None\n'
        after = 'a = x is None\nb = y == None\n'
        self.assertIsNotNone(single_operator_edit('s.py', before, after))
        for changed in [before.replace(' is ', ' == '), after + 'z = 1\n', before.replace(' is ', ' != ', 1)]:
            self.assertIsNone(single_operator_edit('s.py', before, changed))
        self.assertIsNone(single_operator_edit('s.py', before, after, changed_files=2))

    def test_helper_exception_binding_collision(self):
        source = 'def f():\n    try:\n        raise ValueError()\n    except ValueError as _blast_comparison_observe:\n        pass\n    return None is None\nresult = f()\n'
        self.assertTrue(Recorder().execute(source)['result'])

    def test_equality_exception_preserved(self):
        class Error:
            def __eq__(self, other):
                raise ValueError('same error')
        with self.assertRaisesRegex(ValueError, 'same error'):
            Recorder().execute('result = value == None', globals_={'value': Error()})

    def test_import_hook_and_code_source_reader_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = 'def f(value):\n    return value is None\n'
            path = root / 'comparison_synthetic_module.py'
            path.write_text(source)
            context = ['execute']
            recorder = Recorder(lambda: context[0], root=root)
            recorder.readers.start()
            sys.path.insert(0, directory)
            try:
                with Finder(root, recorder).installed():
                    module = importlib.import_module('comparison_synthetic_module')
                    self.assertTrue(module.f(None))
                    self.assertFalse(module.f(object()))
                    context[0] = 'code_reader'
                    code = module.f.__code__
                    _ = code.co_code, code.co_consts
                    context[0] = 'source_reader'
                    _ = path.read_text()
                certificate = observed_certificate(recorder.snapshot(), path.name, source, source.replace(' is ', ' == '))
                self.assertTrue(certificate['equivalent_for_observed_types'])
                self.assertIn('code_reader', certificate['must_retain_observed_code_readers'])
                self.assertIn('source_reader', certificate['must_retain_observed_source_readers'])
                self.assertFalse(certificate['narrowing_authorized'])
                self.assertEqual(module.f.__code__.co_filename, str(path))
            finally:
                recorder.readers.stop()
                sys.path.remove(directory)
                sys.modules.pop('comparison_synthetic_module', None)

    def test_bytecode_counterexample_and_cached_code_audit_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = str(Path(directory) / 'module.py')
            before = 'def f(value):\n    return value is None\n'
            after = before.replace(' is ', ' == ')
            a, b = {}, {}
            exec(compile(before, filename, 'exec'), a)
            exec(compile(after, filename, 'exec'), b)
            cached = a['f'].__code__  # Acquired before audit: later reads invisible.
            audit = ReaderAudit(directory, lambda: 'reflect')
            audit.start()
            try:
                self.assertNotEqual(cached.co_code, b['f'].__code__.co_code)
                self.assertEqual(a['f'](None), b['f'](None))
                value = object()
                self.assertEqual(a['f'](value), b['f'](value))
                self.assertIn('module.py', audit.snapshot()['code'])
                audit.code.clear()
                _ = cached.co_code, cached.co_consts
                self.assertEqual(audit.snapshot()['code'], {})
                self.assertFalse(audit.snapshot()['coverage_complete'])
            finally:
                audit.stop()

    def test_collection_code_acquisition_forces_all_tests_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'def f(value):\n    return value is None\n'
            context = ['test']
            recorder = Recorder(lambda: context[0], root=root)
            code, helper = recorder.compile(before, 'module.py', filename=str(root / 'module.py'))
            namespace = {helper: recorder.compare}
            exec(code, namespace)
            namespace['f'](None)
            recorder.readers.start()
            try:
                context[0] = ''
                _ = namespace['f'].__code__
                certificate = observed_certificate(recorder.snapshot(), 'module.py', before, before.replace(' is ', ' == '))
                self.assertTrue(certificate['collection_escape_requires_all_tests'])
            finally:
                recorder.readers.stop()

    def test_unrelated_function_collection_read_does_not_taint_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'def f(value):\n    return value is None\ndef other():\n    return 7\n'
            context = ['test']
            recorder = Recorder(lambda: context[0], root=root)
            code, helper = recorder.compile(before, 'module.py', filename=str(root / 'module.py'))
            namespace = {helper: recorder.compare}
            exec(code, namespace)
            namespace['f'](None)
            recorder.readers.start()
            try:
                context[0] = ''
                _ = namespace['other'].__code__
                certificate = observed_certificate(recorder.snapshot(), 'module.py', before, before.replace(' is ', ' == '))
                self.assertTrue(certificate['equivalent_for_observed_types'])
                self.assertFalse(certificate['collection_escape_requires_all_tests'])
                self.assertEqual(certificate['must_retain_observed_code_readers'], [])
                self.assertFalse(certificate['narrowing_authorized'])
            finally:
                recorder.readers.stop()

    def test_outer_code_acquisition_retains_nested_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'def outer():\n    def inner(value):\n        return value is None\n    return inner\n'
            context = ['test']
            recorder = Recorder(lambda: context[0], root=root)
            code, helper = recorder.compile(before, 'module.py', filename=str(root / 'module.py'))
            namespace = {helper: recorder.compare}
            exec(code, namespace)
            namespace['outer']()(None)
            recorder.readers.start()
            try:
                context[0] = ''
                _ = namespace['outer'].__code__
                snapshot = recorder.snapshot()
                certificate = observed_certificate(snapshot, 'module.py', before, before.replace(' is ', ' == '))
                self.assertTrue(certificate['collection_escape_requires_all_tests'])
                site = next(iter(snapshot['sites'].values()))
                execution = next(iter(site['execution_codes'].values()))
                self.assertEqual([scope['qualname'] for scope in execution['ancestry']], ['<module>', 'outer', 'outer.<locals>.inner'])
            finally:
                recorder.readers.stop()

    def test_default_expression_executes_in_module_not_function(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'def f(value=object() is None):\n    return value\n'
            recorder = Recorder(lambda: 'test', root=root)
            code, helper = recorder.compile(before, 'module.py', filename=str(root / 'module.py'))
            namespace = {helper: recorder.compare}
            exec(code, namespace)
            recorder.readers.start()
            try:
                _ = namespace['f'].__code__
                snapshot = recorder.snapshot()
                site = next(iter(snapshot['sites'].values()))
                execution = next(iter(site['execution_codes'].values()))
                self.assertEqual([scope['qualname'] for scope in execution['ancestry']], ['<module>'])
                certificate = observed_certificate(snapshot, 'module.py', before, before.replace(' is ', ' == '))
                self.assertEqual(certificate['must_retain_observed_code_readers'], [])
            finally:
                recorder.readers.stop()

    def test_preexisting_module_remains_ineligible_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = types.ModuleType('_preloaded_comparison_test')
            module.__file__ = str(root / 'module.py')
            sys.modules[module.__name__] = module
            try:
                recorder = Recorder(lambda: 'test', root=root)
                with Finder(root, recorder).installed():
                    before = 'value = object() is None\n'
                    recorder.execute(before, 'module.py')
                snapshot = recorder.snapshot()
                self.assertEqual(snapshot['preexisting_modules'], {module.__name__: 'module.py'})
                certificate = observed_certificate(snapshot, 'module.py', before, before.replace(' is ', ' == '))
                self.assertFalse(certificate['equivalent_for_observed_types'])
                self.assertIn('before instrumentation', certificate['reason'])
            finally:
                del sys.modules[module.__name__]

    def test_whole_patch_rejects_extra_file_comment_and_operand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'value = object() is None  # unchanged\n'
            (root / 'module.py').write_text(before)
            recorder = Recorder(lambda: 'test')
            recorder.execute(before, 'module.py')
            def patch(after):
                return 'diff --git a/module.py b/module.py\n' + ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/module.py', tofile='b/module.py'))
            after = before.replace(' is ', ' == ')
            self.assertTrue(qualify_patch(recorder.snapshot(), root, patch(after))['equivalent_for_observed_types'])
            for text in (patch(after) + patch(after), patch(after.replace('unchanged', 'changed')), patch(after.replace('object()', 'None')), patch(after.replace('value =', 'value  =')), patch(after.replace(' == ', '  == ')), 'new mode 100755\n' + patch(after)):
                self.assertFalse(qualify_patch(recorder.snapshot(), root, text)['equivalent_for_observed_types'])

    def test_unsafe_producer_context_disables_safe_consumer_certificate(self):
        state = []
        class Producer:
            def __eq__(self, other):
                state.append('changed')
                return False
        context = ['producer']
        recorder = Recorder(lambda: context[0])
        before = 'def compare(value):\n    return value is None\n'
        target = recorder.execute(before)
        target['compare'](Producer())
        context[0] = 'consumer'
        target['compare'](object())
        self.assertEqual(state, [])
        # Changing the producer comparison to == mutates state subsequently
        # read by the otherwise-safe consumer test. Per-context proof is unsafe.
        certificate = observed_certificate(recorder.snapshot(), 'synthetic.py', before, before.replace(' is ', ' == '))
        self.assertFalse(certificate['equivalent_for_observed_types'])
        self.assertEqual(certificate['reason'], 'unproved operand type pair')
        mutated = {}
        exec(before.replace(' is ', ' == '), mutated)
        mutated['compare'](Producer())
        self.assertEqual(state, ['changed'])

    def test_whole_patch_rejects_width_change_affecting_other_code_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = 'f = lambda x: x is not None; g = lambda y: y\n'
            after = before.replace('is not', '!=')
            (root / 'module.py').write_text(before)
            recorder = Recorder(lambda: 'test')
            target = recorder.execute(before, 'module.py')
            target['f'](object())
            patch = 'diff --git a/module.py b/module.py\n' + ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/module.py', tofile='b/module.py'))
            self.assertFalse(qualify_patch(recorder.snapshot(), root, patch)['equivalent_for_observed_types'])

    def test_backed_enum_canonical_members_aliases_and_forgery(self):
        class Word(str, enum.Enum):
            A = 'a'
            ALIAS = 'a'
            B = 'b'
        for left, right in ((Word.A, Word.B), (Word.A, Word.ALIAS)):
            self.assertTrue(self.certificate(left, right)['equivalent_for_observed_types'])
        self.assertFalse(self.certificate(Word.A, 'a')['equivalent_for_observed_types'])
        forged = str.__new__(Word, 'a')
        self.assertFalse(self.certificate(Word.A, forged)['equivalent_for_observed_types'])
        class Number(int, enum.Enum):
            A = 1
            B = 2
        self.assertTrue(self.certificate(Number.A, Number.B)['equivalent_for_observed_types'])
        self.assertFalse(self.certificate(Number.A, int.__new__(Number, 1))['equivalent_for_observed_types'])

    def test_backed_enum_duplicate_underlying_value_rejected(self):
        class Weird(str, enum.Enum):
            def __new__(cls, label):
                obj = str.__new__(cls, 'same')
                obj._value_ = label
                return obj
            A = 'a'
            B = 'b'
        self.assertIsNot(Weird.A, Weird.B)
        self.assertEqual(Weird.A, Weird.B)
        self.assertFalse(self.certificate(Weird.A, Weird.B)['equivalent_for_observed_types'])

    def test_backed_enum_member_map_mutation_refreshes_flags(self):
        class Word(str, enum.Enum):
            A = 'a'
        recorder = Recorder(lambda: 'test')
        before = 'def compare(a,b):\n    return a is b\n'
        function = recorder.execute(before)['compare']
        function(Word.A, Word.A)
        Word._member_map_['BAD'] = str.__new__(Word, 'a')
        function(Word.A, Word.A)
        cert = observed_certificate(recorder.snapshot(), 'synthetic.py', before, before.replace(' is ', ' == '))
        self.assertFalse(cert['equivalent_for_observed_types'])
        self.assertEqual(len(recorder.serialization_cache), 2)

    def test_serialization_cache_refreshes_slots_and_is_bounded(self):
        recorder = Recorder(lambda: 'test')
        before = 'def compare(value):\n    return value is None\n'
        function = recorder.execute(before)['compare']
        class Value:
            pass
        function(Value())
        Value.__eq__ = lambda self, other: True
        function(Value())
        self.assertEqual(len(recorder.serialization_cache), 2)
        self.assertFalse(observed_certificate(recorder.snapshot(), 'synthetic.py', before, before.replace(' is ', ' == '))['equivalent_for_observed_types'])
        recorder.serialization_cache = {('dummy', i): 'unused' for i in range(4096)}
        function(None)
        self.assertEqual(len(recorder.serialization_cache), 1)

    def test_backed_enum_custom_slots_and_metaclasses_rejected(self):
        class CustomEq(str, enum.Enum):
            A = 'a'
            def __eq__(self, other):
                return True
        class CustomNe(str, enum.Enum):
            A = 'a'
            def __ne__(self, other):
                return True
        class Meta(enum.EnumType):
            pass
        class CustomMeta(str, enum.Enum, metaclass=Meta):
            A = 'a'
        for value in (CustomEq.A, CustomNe.A, CustomMeta.A):
            self.assertFalse(self.certificate(value, value)['equivalent_for_observed_types'])

    def test_backed_enum_custom_conversion_hooks_not_invoked(self):
        class Word(str, enum.Enum):
            A = 'a'
            B = 'b'
            def __str__(self):
                raise AssertionError('custom str called')
        class Number(int, enum.Enum):
            A = 1
            B = 2
            def __int__(self):
                raise AssertionError('custom int called')
        for left, right in ((Word.A, Word.B), (Number.A, Number.B)):
            self.assertTrue(self.certificate(left, right)['equivalent_for_observed_types'])


if __name__ == '__main__':
    unittest.main()
