import os
from pathlib import Path
import sys
import tempfile
import unittest

from comparison_subprocess import classify_launch, environment_digest


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.environment = dict(os.environ, COMPARISON_SPAWN_SOURCE='/baseline',
                                COMPARISON_SPAWN_DIR='/capture', PYTHONPATH='/bootstrap:/tools')

    def classify(self, argv=None, **kwargs):
        return classify_launch([sys.executable, '-c', 'import sqlglot'] if argv is None else argv,
                               inherited=self.environment, **kwargs)

    def test_python_command_preserves_identity(self):
        result = self.classify()
        self.assertTrue(result['supported'])
        self.assertEqual(result['executable'], str(Path(sys.executable).resolve()))
        self.assertNotEqual(result['code_sha256'], self.classify([sys.executable, '-c', 'import sys'])['code_sha256'])

    def test_unknown_or_sanitized_environment_rejected(self):
        self.assertFalse(self.classify(env={})['supported'])
        changed = dict(self.environment, PYTHONPATH='/other')
        self.assertFalse(self.classify(env=changed)['supported'])
        self.assertTrue(self.classify(env=dict(self.environment))['supported'])

    def test_nonpython_and_bootstrap_bypass_rejected(self):
        for argv in (['/bin/sh', '-c', 'echo ok'], [sys.executable, '-I', '-c', 'pass'],
                     [sys.executable, '-S', '-c', 'pass'], [sys.executable, '-m', 'sqlglot'],
                     [sys.executable, 'script.py'], 'python -c pass'):
            self.assertFalse(self.classify(argv)['supported'], argv)
        self.assertFalse(self.classify(shell=True)['supported'])
        self.assertFalse(self.classify(preexec_fn=lambda: None)['supported'])

    def test_executable_override_and_cwd_are_bound(self):
        self.assertFalse(self.classify(executable='/bin/sh')['supported'])
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.classify(cwd=directory)['cwd'], str(Path(directory).resolve()))

    def test_tokens_excluded_but_real_env_changes_bound(self):
        before = environment_digest(self.environment)
        tagged = dict(self.environment, COMPARISON_POPEN_TOKEN='unique', COMPARISON_POPEN_CONTEXT='test')
        self.assertEqual(before, environment_digest(tagged))
        tagged['UNRELATED_SECRET'] = 'never persist or hash publicly'
        self.assertEqual(before, environment_digest(tagged))
        tagged['PYTHONHASHSEED'] = '123'
        self.assertNotEqual(before, environment_digest(tagged))


if __name__ == '__main__':
    unittest.main()
