import tempfile
import unittest
from pathlib import Path
from field_effect_certificate import analyze, edit_gate

BASE = '''class C:
    flags = {"x": True, "y": False}
    def __init_subclass__(cls):
        cls.needed = {k for k, v in cls.flags.items() if v}
'''

class FieldEffectsTests(unittest.TestCase):
    def run_analysis(self, extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.py').write_text(BASE + extra)
            return analyze(root, 'a.py', (BASE + extra).replace('"x": True', '"x": False'))

    def test_value_edit_and_unicode_offsets(self):
        for prefix in ('', '# café\n'):
            a = prefix + BASE
            self.assertTrue(edit_gate(a, a.replace('"x": True', '"x": False'))['accepted'])

    def test_reject_other_changes_and_non_bool(self):
        for new in (BASE.replace('True', '1'), BASE.replace('"x"', '"z"'), BASE.replace('True', 'False')+'# changed\n', BASE.replace('True', 'False').replace('items()', 'values()'), BASE.replace('"y"', '"x"')):
            self.assertFalse(edit_gate(BASE, new)['accepted'])

    def test_reject_duplicate_and_spread_literals(self):
        for source in ('class C:\n f={"x": True, "x": False}\n', 'class C:\n f={**other, "x": True}\n'):
            self.assertFalse(edit_gate(source, source.replace('True','False'))['accepted'])

    def test_local_summary_is_not_global_certificate(self):
        result = self.run_analysis()
        self.assertEqual(result['local_derivations'][0]['derived_field'], 'needed')
        self.assertFalse(result['closed_world_complete'])
        self.assertFalse(result['narrowing_authorized'])

    def test_alias_spread_registry_and_derived_exports_propagate(self):
        result = self.run_analysis('''alias = C.flags
class D:
    copied = {**alias}
REGISTRY = C.needed
''')
        self.assertTrue({'flags','alias','copied','needed','REGISTRY'} <= set(result['may_tainted_names']))
        self.assertFalse(result['closed_world_complete'])

    def test_reflection_is_explicit_unknown(self):
        result = self.run_analysis('x = getattr(C, dynamic_name)\ny = vars(C)\nz = C.__dict__\n')
        kinds = {x['kind'] for x in result['unresolved']}
        self.assertIn('reflection_or_dynamic_call', kinds)
        self.assertIn('namespace_escape', kinds)

    def test_derived_membership_never_called_keys_only(self):
        result = self.run_analysis('x = len(C.flags)\ny = len(C.needed)\n')
        records = {x['field']: x['kind'] for x in result['observations'] if x['line'] >= 5}
        self.assertEqual(records['flags'], 'keys_only_if_exact_dict_and_builtin')
        self.assertEqual(records['needed'], 'derived_or_alias_observation_unresolved')

    def test_wrong_receiver_or_filter_not_local_summary(self):
        for old, new in [('cls.needed', 'other.needed'), ('if v}', 'if not v}')]:
            with tempfile.TemporaryDirectory() as tmp:
                source = BASE.replace(old,new)
                Path(tmp,'a.py').write_text(source)
                result = analyze(tmp,'a.py',source.replace('"x": True','"x": False'))
                self.assertEqual(result['local_derivations'], [])

if __name__ == '__main__':
    unittest.main()
