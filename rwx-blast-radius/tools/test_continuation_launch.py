import contextlib
import hashlib
import io
import json
import struct
import unittest
from unittest.mock import patch
import launch_cold_continuation as launch

class LaunchTests(unittest.TestCase):
    def test_parse_null_delimited_values_without_logging(self):
        payload = struct.pack('=i', 2)+b'/python\0\0python\0/script\0TOKEN=secret value=x\0X=y\0\0apple_vector=not_environment\0'
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            args, env = launch.parse_procargs(payload)
        self.assertEqual(args, ['python','/script'])
        self.assertEqual(env['TOKEN'], 'secret value=x')
        self.assertNotIn('apple_vector', env)
        self.assertEqual(output.getvalue(), '')

    def test_hash_exact_and_only_known_mutation(self):
        env = {'TOKEN':'secret', 'PYTHON_CPU_COUNT':'10', '__PYVENV_LAUNCHER__':'bootstrap'}
        expected = hashlib.sha256(json.dumps({'TOKEN':'secret','PYTHON_CPU_COUNT':'2'}, sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(launch.verify_environment(env, expected)['PYTHON_CPU_COUNT'], '2')
        self.assertEqual(env['PYTHON_CPU_COUNT'], '10')
        self.assertNotIn('__PYVENV_LAUNCHER__', launch.verify_environment(env, expected))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            launch.verify_environment({'TOKEN':'different'}, expected)

    def test_invalid_payloads_do_not_expose_values(self):
        for payload in (b'secret', struct.pack('=i',1)+b'/x\0\0arg\0secret\0',
                        struct.pack('=i',1)+b'/x\0\0arg\0X=secret\0X=other\0'):
            with self.assertRaises(ValueError) as error:
                launch.parse_procargs(payload)
            self.assertNotIn('secret', str(error.exception))

    def test_unrelated_pid_forbidden_before_syscall(self):
        with patch.object(launch.ctypes, 'CDLL') as syscall:
            with self.assertRaises(ValueError): launch.read_owned_environment(999)
            syscall.assert_not_called()

if __name__ == '__main__': unittest.main()
