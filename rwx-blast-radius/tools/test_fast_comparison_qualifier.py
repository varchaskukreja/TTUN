import difflib
from pathlib import Path
import tempfile
import unittest

from comparison_types import Recorder, qualify_patch as legacy
from fast_comparison_qualifier import qualify_patch


def patch_text(before, after):
    return 'diff --git a/module.py b/module.py\n' + ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/module.py', tofile='b/module.py'))


class QualifierTests(unittest.TestCase):
    def test_existing_acceptance_preserved_and_whole_patch_rejections(self):
        before = 'def f(x):\n    return x is None\n'
        recorder = Recorder(lambda: 'test')
        recorder.execute(before, 'module.py')['f'](None)
        mapping = recorder.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'module.py').write_text(before)
            for after in [before.replace(' is ', ' == '), before.replace(' is ', ' == ')+'# comment\n',
                          before.replace(' is ', ' == ').replace('return ', 'return  '),
                          before.replace(' is ', ' != ')]:
                change = patch_text(before, after)
                self.assertEqual(legacy(mapping, root, change), qualify_patch(mapping, root, change))
            change = patch_text(before, before.replace(' is ', ' == '))
            for prefix in ('new mode 100755\n', 'rename from elsewhere\n', 'GIT binary patch\n'):
                self.assertEqual(qualify_patch(mapping, root, prefix+change)['reason'], 'unsupported whole patch')
            self.assertEqual(qualify_patch(mapping, root, change+change)['reason'], 'unsupported whole patch')

    def test_repetitive_source_false_rejection_recovered(self):
        before = ''.join(f'v{i % 3} = x is None\n' for i in range(18))
        positions = [i for i in range(len(before)) if before.startswith(' is ', i)]
        offset = positions[3] + 1
        after = before[:offset] + '==' + before[offset+2:]
        recorder = Recorder(lambda: 'test')
        recorder.execute(before, 'module.py', {'x': None})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'module.py').write_text(before)
            change = patch_text(before, after)
            self.assertFalse(legacy(recorder.snapshot(), root, change)['equivalent_for_observed_types'])
            result = qualify_patch(recorder.snapshot(), root, change)
            self.assertTrue(result['equivalent_for_observed_types'])
            self.assertFalse(result['narrowing_authorized'])


if __name__ == '__main__':
    unittest.main()
