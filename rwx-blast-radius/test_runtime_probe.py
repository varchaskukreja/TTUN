"""Cold-process tests for overlay execution and import-only completeness."""

import difflib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('pytest'), 'pytest is required for runtime probe tests')
class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for package in ('sqlglot', 'tests'):
            (self.root / package).mkdir()
            (self.root / package / '__init__.py').write_text('')
        self.old = 'class Config:\n    VALUE = 1\n'
        self.new = 'class Config:\n    VALUE = 2\n'
        self.module = self.root / 'sqlglot/config.py'
        self.module.write_text(self.old)
        self.runner = Path(__file__).with_name('runtime_probe.py')

    def invoke(self, test_source, *, collect_only=False, new=None, nodes=None,
               target='sqlglot/config.py', old=None, import_patches=False):
        (self.root / 'tests/test_case.py').write_text(test_source)
        old = self.old if old is None else old
        new = self.new if new is None else new
        (self.root / target).write_text(old)
        patch = self.root / 'change.patch'
        patch.write_text(f'diff --git a/{target} b/{target}\n' + ''.join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile='a/' + target, tofile='b/' + target)))
        command = [sys.executable, str(self.runner), '--source', str(self.root),
                   '--patch', str(patch), '--nodes', '-']
        if collect_only:
            command.append('--collect-only')
        if import_patches:
            command.append('--import-patches')
        result = subprocess.run(command, input=json.dumps(nodes or ['tests/test_case.py::test_one']),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)  # Any leaked pytest output breaks this.
        self.assertEqual((self.root / target).read_text(), old, 'source tree was modified')
        return output

    def test_collect_only_imports_patched_constant_without_running_fixtures_or_test(self):
        result = self.invoke('''
import pytest
from sqlglot.config import Config
assert Config.VALUE == 2
@pytest.fixture(autouse=True)
def forbidden_fixture():
    raise AssertionError('fixture must not execute')
def test_one():
    raise AssertionError('test must not execute')
''', collect_only=True)
        self.assertTrue(result['complete'], result)
        self.assertEqual((result['requested'], result['collected'], result['finished']), (1, 1, 0))
        self.assertEqual(result['failing'], [])
        self.assertEqual(result['python_patches_loaded'], ['sqlglot/config.py'])
        self.assertEqual(result['python_patches_not_loaded'], [])

    def test_collect_only_rejects_import_time_failure(self):
        result = self.invoke('from sqlglot.config import Config\ndef test_one(): pass\n',
                             new='class Config:\n    VALUE = 1 / 0\n', collect_only=True)
        self.assertFalse(result['complete'])
        self.assertTrue(result['collection_errors'])
        self.assertEqual(result['finished'], 0)

    def test_collect_only_requires_every_requested_node(self):
        result = self.invoke('def test_one(): pass\n', collect_only=True,
                             nodes=['tests/test_case.py::test_one', 'tests/test_case.py::test_missing'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['finished'], 0)

    def test_collect_only_rejects_child_process_during_import(self):
        result = self.invoke('import subprocess\nsubprocess.run(["true"])\ndef test_one(): pass\n',
                             collect_only=True)
        self.assertFalse(result['complete'])
        self.assertTrue(any('child process' in reason for reason in result['reasons']))

    def test_collection_covers_multiple_requested_ids(self):
        result = self.invoke('def test_one(): pass\ndef test_two(): pass\n', collect_only=True,
                             nodes=['tests/test_case.py::test_one', 'tests/test_case.py::test_two'])
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['collected'], 2)
        self.assertEqual(result['finished'], 0)
        # Collection can succeed while a lazy changed module was never imported.
        # Consumers must not mistake collection success for that import check.
        self.assertEqual(result['python_patches_not_loaded'], ['sqlglot/config.py'])

    def test_execution_still_detects_patched_failure(self):
        result = self.invoke('from sqlglot.config import Config\ndef test_one():\n    assert Config.VALUE == 1\n')
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['failing'], ['tests/test_case.py::test_one'])
        self.assertEqual(result['finished'], 1)

    def test_explicit_import_loads_otherwise_lazy_patched_module(self):
        result = self.invoke('def test_one(): pass\n', collect_only=True, import_patches=True)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['collected'], 1)
        self.assertEqual(result['finished'], 0)
        self.assertEqual(result['explicit_imports'], ['sqlglot.config'])
        self.assertEqual(result['python_patches_not_loaded'], [])

    def test_explicit_import_rejects_invalid_class_constant_before_collection(self):
        result = self.invoke('def test_one(): pass\n', collect_only=True, import_patches=True,
                             new='class Config:\n    VALUE = 1 / 0\n')
        self.assertFalse(result['complete'])
        self.assertEqual(result['collected'], 0)
        self.assertEqual(result['finished'], 0)
        self.assertTrue(any('ZeroDivisionError' in error for error in result['patch_import_errors']))

    def test_explicit_import_rejects_python_outside_sqlglot_package(self):
        result = self.invoke('def test_one(): pass\n', collect_only=True, import_patches=True,
                             target='tests/helper.py')
        self.assertFalse(result['complete'])
        self.assertTrue(any('inside the sqlglot package' in reason for reason in result['reasons']))

    def test_explicit_import_requires_collection_mode(self):
        result = self.invoke('def test_one(): pass\n', import_patches=True)
        self.assertFalse(result['complete'])
        self.assertTrue(any('requires --collect-only' in reason for reason in result['reasons']))

    def test_explicit_import_recognizes_package_init(self):
        result = self.invoke('def test_one(): pass\n', collect_only=True, import_patches=True,
                             target='sqlglot/__init__.py')
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['explicit_imports'], ['sqlglot'])
        self.assertEqual(result['python_patches_loaded'], ['sqlglot/__init__.py'])

    def test_fixture_overlay_applies_during_collection(self):
        result = self.invoke('''
from pathlib import Path
assert Path(__file__).with_name('input.txt').read_text() == 'new\\n'
def test_one(): pass
''', collect_only=True, target='tests/input.txt', old='old\n', new='new\n')
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['collected'], 1)

    def test_unmanaged_fileio_cannot_silently_read_original_fixture(self):
        result = self.invoke('''
import io
from pathlib import Path
assert io.FileIO(Path(__file__).with_name('input.txt')).read() == b'old\\n'
def test_one(): pass
''', collect_only=True, target='tests/input.txt', old='old\n', new='new\n')
        self.assertFalse(result['complete'])
        self.assertTrue(any('unintercepted' in reason for reason in result['reasons']))


if __name__ == '__main__':
    unittest.main()
