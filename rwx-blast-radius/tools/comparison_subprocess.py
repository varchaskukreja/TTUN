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
            'argv_sha256': digest([str(resolved), *argv[1:]]), 'code_sha256': hashlib.sha256(argv[2].encode()).hexdigest(),
            'allowlisted_environment_sha256': environment_digest(environment), 'argument_count': len(argv)}


# Hook installation is opt-in from comparison_spawn; importing stays side-effect free.
def install_popen(context_getter, write_event):
    import inspect
    import subprocess
    import threading
    import uuid
    original_init = subprocess.Popen.__init__
    original_wait = subprocess.Popen._wait
    original_poll = subprocess.Popen._internal_poll
    signature = inspect.signature(original_init)
    local = threading.local()

    def record_exit(process):
        token = getattr(process, '_comparison_capture_token', None)
        if token and process.returncode is not None and not getattr(process, '_comparison_exit_recorded', False):
            process._comparison_exit_recorded = True
            write_event('popen_exit', token=token, child_pid=process.pid,
                        context=process._comparison_capture_context, returncode=process.returncode)

    def init(process, *args, **kwargs):
        bound = signature.bind(process, *args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        try:
            identity = classify_launch(arguments['args'], cwd=arguments.get('cwd'), env=arguments.get('env'),
                shell=arguments.get('shell', False), executable=arguments.get('executable'),
                preexec_fn=arguments.get('preexec_fn'))
        except (ValueError, TypeError, OSError):
            identity = {'supported': False, 'reason': 'unverifiable command'}
        context = context_getter()
        if not identity['supported']:
            write_event('untracked_activity', event='unsupported.Popen', context=context, reason=identity['reason'])
            return original_init(process, *args, **kwargs)
        token = uuid.uuid4().hex
        environment = dict(os.environ if arguments.get('env') is None else arguments['env'])
        environment.update(COMPARISON_POPEN_TOKEN=token, COMPARISON_POPEN_CONTEXT=context,
                           COMPARISON_POPEN_EXPECTED=json.dumps(identity, sort_keys=True))
        arguments['env'] = environment
        write_event('popen_launch', token=token, context=context, identity=identity)
        previous = getattr(local, 'launch', None)
        local.launch = (token, identity)
        try:
            original_init(*bound.args, **bound.kwargs)
        except BaseException:
            write_event('popen_failed', token=token, context=context)
            raise
        finally:
            local.launch = previous
        process._comparison_capture_token = token
        process._comparison_capture_context = context
        write_event('popen_started', token=token, child_pid=process.pid, context=context)
        record_exit(process)

    def wait(process, *args, **kwargs):
        result = original_wait(process, *args, **kwargs)
        record_exit(process)
        return result

    def poll(process, *args, **kwargs):
        result = original_poll(process, *args, **kwargs)
        record_exit(process)
        return result

    def tracked_audit(event, args):
        launch = getattr(local, 'launch', None)
        if launch is None or event not in ('subprocess.Popen', 'os.posix_spawn'):
            return False
        token, expected = launch
        try:
            if event == 'subprocess.Popen':
                executable, argv, cwd, environment = args
            else:
                executable, argv, environment = args
                cwd = expected['cwd']
            identity = classify_launch(argv, executable=executable, cwd=cwd, env=environment)
            return identity == expected and environment.get('COMPARISON_POPEN_TOKEN') == token
        except (ValueError, TypeError, OSError, AttributeError):
            return False

    subprocess.Popen.__init__ = init
    subprocess.Popen._wait = wait
    subprocess.Popen._internal_poll = poll

    def restore():
        subprocess.Popen.__init__ = original_init
        subprocess.Popen._wait = original_wait
        subprocess.Popen._internal_poll = original_poll
    return restore, tracked_audit


def consume_bootstrap_identity():
    """Consume launch token before user code can start nested children."""
    token = os.environ.pop('COMPARISON_POPEN_TOKEN', None)
    if token is None:
        return None
    context = os.environ.pop('COMPARISON_POPEN_CONTEXT', '')
    expected_raw = os.environ.pop('COMPARISON_POPEN_EXPECTED', '')
    try:
        expected = json.loads(expected_raw)
        # macOS framework launchers rewrite orig_argv[0]; bind executable
        # through sys.executable and preserve every actual command argument.
        actual = classify_launch([sys.executable, *sys.orig_argv[1:]])
        matched = actual == expected and actual.get('supported') is True
    except (ValueError, TypeError, OSError):
        actual, matched = {}, False
    return {'token': token, 'context': context, 'identity': actual, 'identity_matched': matched}
