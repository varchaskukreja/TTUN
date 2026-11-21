"""Pure launch validation for a future baseline-only Python subprocess capture.

This file does not install hooks. The running v1 experiment remains untouched.
Only the generic ``python -c CODE [args...]`` form is currently supported;
module/script launch paths can bypass Finder.exec_module and remain conservative.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

CAPTURE_KEYS = ('COMPARISON_SPAWN_SOURCE', 'COMPARISON_SPAWN_DIR', 'PYTHONPATH')
PROVENANCE_ENV_KEYS = (*CAPTURE_KEYS, 'PYTHON_CPU_COUNT', 'PYTHONHASHSEED',
                       'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                       'NUMEXPR_NUM_THREADS', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD')
TOKEN_KEYS = ('COMPARISON_POPEN_TOKEN', 'COMPARISON_POPEN_CONTEXT', 'COMPARISON_POPEN_EXPECTED')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def environment_digest(environment):
    # Public provenance deliberately excludes arbitrary host environment/secrets.
    return digest({key: environment[key] for key in PROVENANCE_ENV_KEYS if key in environment})


def classify_launch(argv, *, cwd=None, env=None, shell=False, executable=None,
                    preexec_fn=None, inherited=None, interpreter=None):
    """Validate without launching or rewriting a command; unsupported means retain."""
    inherited = dict(os.environ) if inherited is None else inherited
    interpreter = sys.executable if interpreter is None else interpreter
    result = {'supported': False}
    if shell or preexec_fn is not None:
        return dict(result, reason='shell or preexec hook unsupported')
    if type(argv) not in (list, tuple) or not argv or any(type(arg) is not str or '\x00' in arg for arg in argv):
        return dict(result, reason='only explicit string argv supported')
    if len(argv) < 3 or argv[1] != '-c':
        return dict(result, reason='only direct python -c bootstrap supported')
    environment = inherited if env is None else env
    if type(environment) is not dict or any(type(k) is not str or type(v) is not str for k, v in environment.items()):
        return dict(result, reason='unknown environment mapping')
    if any(not inherited.get(key) or environment.get(key) != inherited[key] for key in CAPTURE_KEYS):
        return dict(result, reason='capture environment removed or changed')
    directory = Path(os.getcwd() if cwd is None else cwd).resolve()
    command = argv[0] if executable is None else executable
    if type(command) is not str or '\x00' in command:
        return dict(result, reason='unknown executable')
    if os.sep in command:
        resolved = (directory / command).resolve()
    else:
        # Relative PATH entries are interpreted in the child's working directory.
        paths = os.pathsep.join(str((directory / entry).resolve()) for entry in environment.get('PATH', os.defpath).split(os.pathsep))
        match = shutil.which(command, path=paths)
        if match is None:
            return dict(result, reason='executable not found')
        resolved = Path(match).resolve()
    if resolved != Path(interpreter).resolve():
        return dict(result, reason='different or non-Python interpreter')
    if not directory.is_dir():
        return dict(result, reason='invalid child working directory')
    return {'supported': True, 'executable': str(resolved), 'cwd': str(directory),
            'argv_sha256': digest(list(argv)), 'code_sha256': hashlib.sha256(argv[2].encode()).hexdigest(),
            'allowlisted_environment_sha256': environment_digest(environment), 'argument_count': len(argv)}
