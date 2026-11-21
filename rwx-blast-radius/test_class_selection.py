"""Conservative eligibility tests for class literal dependency narrowing."""
import ast
import unittest

from class_selection import class_candidates


class ClassSelectionTests(unittest.TestCase):
    def candidates(self, before, after, line, consumers=None):
        info = {'class_consumers': {'Example': [1, 3]} if consumers is None else consumers}
        return class_candidates(ast.parse(before), ast.parse(after), line, info)

    def test_nested_tokenizer_literal_mapping(self):
        old = 'class Snowflake:\n    class Tokenizer:\n        NUMERIC_ESCAPES = {"x": (16, 2, 2, 255), "u": (16, 4, 4, 65535)}\n'
        self.assertEqual(self.candidates(old, old.replace('(16, 4', '(17, 4'), 3,
                                         {'Snowflake': [0], 'Snowflake.Tokenizer': [2, 4]}), {2, 4})

    def test_pure_integer_and_boolean(self):
        for old_value, new_value in [('1', '2'), ('True', 'False'), ('-1', '-2')]:
            with self.subTest(value=old_value):
                old = f'class Example:\n    VALUE = {old_value}\n'
                self.assertEqual(self.candidates(old, old.replace(old_value, new_value), 2), {1, 3})

    def test_registry_reader_can_change_without_calling_class_methods(self):
        old = 'class Example:\n    NAMES = ["old"]\n    LOCAL = 1\nregistry = {name: Example for name in Example.NAMES}\n'
        new = old.replace('"old"', '"new"')
        before, after = {}, {}
        exec(old, before)
        exec(new, after)
        self.assertIn('old', before['registry'])
        self.assertNotIn('old', after['registry'])
        info = {'class_consumers': {'Example': [1]},
                'class_attribute_global_exports': {'Example': ['NAMES']}}
        self.assertIsNone(class_candidates(ast.parse(old), ast.parse(new), 2, info))
        # Attribute-level evidence need not disable unrelated local settings.
        self.assertEqual(class_candidates(ast.parse(old), ast.parse(old.replace('LOCAL = 1', 'LOCAL = 2')), 3, info), {1})

    def test_unresolved_global_export_rejects_every_attribute(self):
        old = 'class Example:\n    VALUE = 1\n'
        info = {'class_consumers': {'Example': [1]},
                'class_attribute_global_exports': {'Example': ['*']}}
        self.assertIsNone(class_candidates(ast.parse(old), ast.parse(old.replace('1', '2')), 2, info))

    def test_annotated_name_assignment(self):
        old = 'class Example:\n    VALUE: int = 1\n'
        self.assertEqual(self.candidates(old, old.replace('= 1', '= 2'), 2), {1, 3})

    def test_chained_name_assignments(self):
        old = 'class Example:\n    FIRST = SECOND = 1\n'
        self.assertEqual(self.candidates(old, old.replace('= 1', '= 2'), 2), {1, 3})

    def test_allowed_nested_literal_nodes(self):
        old = 'class Example:\n    VALUE = [{"a": (~1, +2, not False)}, {3, 4}]\n'
        self.assertEqual(self.candidates(old, old.replace('~1', '~2'), 2), {1, 3})

    def test_call_name_attribute_and_binary_values_rejected(self):
        for value in ['make(1)', '[known, 1]', '(obj.value, 1)', '1 + 2']:
            with self.subTest(value=value):
                old = f'class Example:\n    VALUE = {value}\n'
                self.assertIsNone(self.candidates(old, old.replace('1', '3'), 2))

    def test_external_assignment_rejected(self):
        old = 'class Example:\n    VALUE = 1\nExample.VALUE = 2\n'
        self.assertIsNone(self.candidates(old, old.replace('VALUE = 2', 'VALUE = 3'), 3))

    def test_class_header_rejected(self):
        old = 'class Example(factory(1)):\n    VALUE = 2\n'
        self.assertIsNone(self.candidates(old, old.replace('factory(1)', 'factory(3)'), 1))

    def test_symbol_change_rejected(self):
        old = 'class Example:\n    VALUE = 1\n'
        self.assertIsNone(self.candidates(old, old.replace('VALUE', 'OTHER').replace('1', '2'), 2))

    def test_container_structure_change_rejected(self):
        old = 'class Example:\n    VALUE = [1, 2]\n'
        self.assertIsNone(self.candidates(old, old.replace('[1, 2]', '(1, 3)'), 2))
        self.assertIsNone(self.candidates(old, old.replace('[1, 2]', '[1, 2, 3]'), 2))

    def test_constant_type_changes_rejected(self):
        old = 'class Example:\n    VALUE = 1\n'
        for value in ['True', '1.0', '"1"', 'None']:
            with self.subTest(value=value):
                self.assertIsNone(self.candidates(old, old.replace('= 1', '= ' + value), 2))

    def test_dunder_attribute_rejected(self):
        old = 'class Example:\n    __slots__ = ("first",)\n'
        self.assertIsNone(self.candidates(old, old.replace('first', 'second'), 2))

    def test_unpacking_or_attribute_target_rejected(self):
        for assignment in ['A, B = (1, 2)', 'other.A = 1', 'A = __special__ = 1']:
            old = f'class Example:\n    {assignment}\n'
            self.assertIsNone(self.candidates(old, old.replace('1', '3'), 2))

    def test_function_local_class_rejected(self):
        old = 'def factory():\n    class Example:\n        VALUE = 1\n    return Example\n'
        self.assertIsNone(self.candidates(old, old.replace('1', '2'), 3))

    def test_conditional_class_body_assignment_rejected(self):
        old = 'class Example:\n    if True:\n        VALUE = 1\n'
        self.assertIsNone(self.candidates(old, old.replace('= 1', '= 2'), 3))

    def test_absent_empty_or_wrong_qualified_consumers_rejected(self):
        old = 'class Example:\n    VALUE = 1\n'
        for consumers in [{}, {'Example': []}, {'Other': [1]}]:
            self.assertIsNone(self.candidates(old, old.replace('1', '2'), 2, consumers))

    def test_line_must_be_inside_assignment_value(self):
        old = 'class Example:\n    VALUE = [\n        1,\n    ]\n'
        self.assertIsNone(self.candidates(old, old.replace('1', '2'), 1))
        self.assertEqual(self.candidates(old, old.replace('1', '2'), 3), {1, 3})

    def test_comment_only_ast_or_unrelated_constant_change_rejected(self):
        old = 'class Example:\n    VALUE = 1\nother = 2\n'
        self.assertIsNone(self.candidates(old, old + '# comment\n', 2))
        self.assertIsNone(self.candidates(old, old.replace('other = 2', 'other = 3'), 2))

    def test_shared_line_cannot_hide_ineligible_change(self):
        old = 'class Example:\n    VALUE = 1; OTHER = factory(2)\n'
        self.assertIsNone(self.candidates(old, old.replace('1', '3').replace('2', '4'), 2))
        old = 'class Example:\n    VALUE: factory(1) = 2\n'
        self.assertIsNone(self.candidates(old, old.replace('1', '3').replace('2', '4'), 2))


if __name__ == '__main__':
    unittest.main()
