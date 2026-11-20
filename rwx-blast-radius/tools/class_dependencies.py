"""Observe class consumers in an unmodified baseline run.

This is supplementary dependency evidence, not proof that an unobserved class
cannot affect a test. Module initialization, free functions reading class
attributes, monkeypatching, and external/native code need conservative handling.

Usage::

    collector = ClassDependencies(source_root, lambda: current_pytest_nodeid)
    collector.start()
    # Run baseline tests, switching the context returned by the callback.
    collector.stop()
    json.dump(collector.snapshot(), stream)

Spawned workers should construct their own collector with a constant inherited
test context; their snapshots can be merged with ``merge_snapshots``.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import defaultdict


class ClassDependencies:
    def __init__(self, source_root, context_getter):
        self.root = os.path.abspath(source_root)
        self.prefix = self.root + os.sep + 'sqlglot' + os.sep
        self.context_getter = context_getter
        self._arguments = {}
        self._classes = {}
        self._seen = set()
        self._contexts = defaultdict(set)
        self._previous = None
        self._previous_thread = None
        self._started = False
        self._tool_id = None

    def _class_keys(self, cls):
        cached = self._classes.get(cls)
        if cached is not None:
            return cached
        keys = []
        for ancestor in type.__getattribute__(cls, '__mro__'):
            namespace = type.__getattribute__(ancestor, '__dict__')
            module_name = namespace.get('__module__', '')
            if not isinstance(module_name, str) or (module_name != 'sqlglot' and not module_name.startswith('sqlglot.')):
                continue
            module = sys.modules.get(module_name)
            filename = vars(module).get('__file__') if module is not None else None
            if not filename:
                continue
            filename = os.path.abspath(filename)
            if not filename.startswith(self.prefix):
                continue
            # Qualified names distinguish nested Tokenizer/Parser classes.
            qualname = type.__getattribute__(ancestor, '__qualname__')
            keys.append((os.path.relpath(filename, self.root), qualname))
        cached = tuple(keys)
        self._classes[cls] = cached
        return cached

    def _profile(self, frame, event, arg):
        if event != 'call':
            return
        code = frame.f_code
        key = id(code)
        cached = self._arguments.get(key)
        if cached is None:
            first = code.co_varnames[0] if code.co_argcount else None
            argument = (
                first if code.co_filename.startswith(self.prefix)
                and first in ('self', 'cls', 'mcs', 'mcls') else None
            )
            # Code-object hashing traverses constants and can be expensive.
            # Retain the object so its identity cannot be recycled in this map.
            self._arguments[key] = (code, argument)
        else:
            argument = cached[1]
        if argument is None:
            return
        self._record(frame, argument)

    def _record(self, frame, argument):
        receiver = frame.f_locals.get(argument)
        cls = receiver if isinstance(receiver, type) else type(receiver)
        context = self.context_getter()
        identity = (context, cls)
        if identity in self._seen:
            return
        self._seen.add(identity)
        for key in self._class_keys(cls):
            self._contexts[key].add(context)

    def _monitor(self, code, offset):
        key = id(code)
        cached = self._arguments.get(key)
        if cached is None:
            first = code.co_varnames[0] if code.co_argcount else None
            argument = (
                first if code.co_filename.startswith(self.prefix)
                and first in ('self', 'cls', 'mcs', 'mcls') else None
            )
            self._arguments[key] = (code, argument)
        else:
            argument = cached[1]
        if argument is None:
            return sys.monitoring.DISABLE
        self._record(sys._getframe(1), argument)

    def start(self):
        if self._started:
            raise RuntimeError('class dependency collector is already running')
        if hasattr(sys, 'monitoring'):
            for tool_id in (2, 3, 4, 5):
                if sys.monitoring.get_tool(tool_id) is None:
                    sys.monitoring.use_tool_id(tool_id, 'blast-class-dependencies')
                    self._tool_id = tool_id
                    sys.monitoring.register_callback(tool_id, sys.monitoring.events.PY_START, self._monitor)
                    sys.monitoring.set_events(tool_id, sys.monitoring.events.PY_START)
                    self._started = True
                    return
        self._previous = sys.getprofile()
        self._previous_thread = threading.getprofile()
        if self._previous is not None or self._previous_thread is not None:
            raise RuntimeError('refusing to replace an existing profiling hook')
        self._started = True
        threading.setprofile(self._profile)
        sys.setprofile(self._profile)

    def stop(self):
        if not self._started:
            return
        if self._tool_id is not None:
            sys.monitoring.set_events(self._tool_id, 0)
            sys.monitoring.register_callback(self._tool_id, sys.monitoring.events.PY_START, None)
            sys.monitoring.free_tool_id(self._tool_id)
            self._tool_id = None
            self._started = False
            return
        sys.setprofile(self._previous)
        threading.setprofile(self._previous_thread)
        self._started = False

    def snapshot(self):
        result = {}
        for (path, name), contexts in sorted(self._contexts.items()):
            result.setdefault(path, {})[name] = sorted(contexts)
        return result


def merge_snapshots(*snapshots):
    merged = defaultdict(lambda: defaultdict(set))
    for snapshot in snapshots:
        for path, classes in snapshot.items():
            for name, contexts in classes.items():
                merged[path][name].update(contexts)
    return {path: {name: sorted(contexts) for name, contexts in sorted(classes.items())}
            for path, classes in sorted(merged.items())}
