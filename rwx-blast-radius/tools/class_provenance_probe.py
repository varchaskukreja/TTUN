"""Independent baseline target-class call provenance; never selects tests.

A narrow one-test probe. Collection, fixture setup and spawned-worker imports
are included; no class links are removed based on these observations.
"""
import atexit
import json
import os
from pathlib import Path
import sys

_started = False
_events = {}
_root = ''
_tool = None


def monitor(code, offset):
    if not code.co_filename.startswith(_root + os.sep + 'sqlglot' + os.sep):
        return sys.monitoring.DISABLE
    first = code.co_varnames[0] if code.co_argcount else None
    if first not in ('self', 'cls', 'mcs', 'mcls'):
        return sys.monitoring.DISABLE
    frame = sys._getframe(1)
    receiver = frame.f_locals.get(first)
    cls = receiver if isinstance(receiver, type) else type(receiver)
    namespace = type.__getattribute__(cls, '__dict__')
    if namespace.get('__module__') != os.environ['CLASS_PROVENANCE_MODULE'] or type.__getattribute__(cls, '__qualname__') != os.environ['CLASS_PROVENANCE_CLASS']:
        return
    key = (code.co_filename, code.co_qualname, code.co_firstlineno)
    if key in _events:
        _events[key]['count'] += 1
        return
    stack = []
    caller = frame.f_back
    for _ in range(25):
        if caller is None: break
        filename = caller.f_code.co_filename
        stack.append({'file': os.path.relpath(filename, _root) if filename.startswith(_root) else filename,
                      'function': caller.f_code.co_qualname, 'line': caller.f_lineno})
        caller = caller.f_back
    _events[key] = {'file': os.path.relpath(code.co_filename, _root), 'function': code.co_qualname,
                    'line': code.co_firstlineno, 'count': 1, 'stack': stack,
                    'lifecycle': 'class_initialization' if code.co_name == '__init_subclass__' else 'receiver_use'}


def save():
    directory = Path(os.environ['CLASS_PROVENANCE_OUTPUT'])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'class-calls-{os.getpid()}.json'
    path.write_text(json.dumps({'pid': os.getpid(), 'requested_test': os.environ['CLASS_PROVENANCE_NODE'],
        'class': os.environ['CLASS_PROVENANCE_MODULE']+'.'+os.environ['CLASS_PROVENANCE_CLASS'],
        'events': list(_events.values()), 'proves_unused': False}, indent=2)+'\n')


def start():
    global _started, _root, _tool
    if _started: return
    _started = True
    _root = str(Path(os.environ['CLASS_PROVENANCE_SOURCE']).resolve())
    for tool in (2, 3, 4, 5):
        if sys.monitoring.get_tool(tool) is None:
            _tool = tool
            break
    if _tool is None: raise RuntimeError('No monitoring slot available')
    sys.monitoring.use_tool_id(_tool, 'class-provenance-probe')
    sys.monitoring.register_callback(_tool, sys.monitoring.events.PY_START, monitor)
    sys.monitoring.set_events(_tool, sys.monitoring.events.PY_START)
    atexit.register(save)
    from multiprocessing.util import Finalize
    Finalize(None, save, exitpriority=-sys.maxsize)
