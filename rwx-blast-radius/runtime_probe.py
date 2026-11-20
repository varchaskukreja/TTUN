#!/usr/bin/env python3
"""Optional cold-process pytest probe with an in-memory baseline patch overlay.

Example:
  python runtime_probe.py --source /opt/sqlglot --patch /data/commits/x.patch \
      --nodes selected-nodeids.json

``--nodes -`` reads a JSON list of exact pytest node IDs from stdin. The caller
must enforce an external timeout and treat absent/invalid JSON or complete=false
as inconclusive. This is not a sandbox: it runs challenge tests. Child processes
are deliberately unsupported because they would not inherit this overlay.
``--collect-only`` imports and collects every requested ID without running tests
or fixtures. Success proves collection only, not runtime behavior or dependencies.
With ``--import-patches``, explicitly import patched sqlglot Python modules before
collection as well. This guards lazy imports, but changes their import order.

Dependency: pytest 9.2.0 (includes unittest/subtest support), plus dependencies of
the selected baseline tests. No coverage plugin, sample labels or source writes.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import importlib.abc
import importlib.machinery
import io
import json
import linecache
import multiprocessing.process
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch as mock_patch

from patches import apply_file, parse_patch


class UnsupportedProbe(RuntimeError):
    pass


class OverlayLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, filename, content):
        super().__init__(fullname, filename)
        self.content = content

    def get_code(self, fullname):
        # Compile bytes to preserve encoding-cookie semantics and never consult
        # a baseline .pyc, whose timestamp could otherwise hide the patch.
        return self.source_to_code(self.content, self.path)

    def get_data(self, path):
        if os.path.abspath(path) == self.path:
            return self.content
        return super().get_data(path)


class OverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files):
        self.files = files

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            filename = os.path.abspath(spec.origin)
            if filename in self.files:
                spec.loader = OverlayLoader(fullname, filename, self.files[filename])
                return spec
        return None


class Overlay:
    def __init__(self, files):
        self.files = files
        self.unsupported = set()
        self._active = False

    def refuse(self, reason):
        self.unsupported.add(reason)
        raise UnsupportedProbe(reason)

    def filename(self, filename):
        if isinstance(filename, int):
            return None
        return os.path.abspath(os.fsdecode(os.fspath(filename)))

    @contextlib.contextmanager
    def install(self):
        original_open = builtins.open
        original_io_open = io.open
        original_os_open = os.open
        overlay = self

        class VirtualBytes(io.BytesIO):
            def fileno(self):
                return overlay.refuse('file-descriptor access to an overlaid file')

        def virtual_open(original, filename, mode='r', buffering=-1, encoding=None,
                         errors=None, newline=None, closefd=True, opener=None):
            key = self.filename(filename)
            if key not in self.files:
                return original(filename, mode, buffering, encoding, errors, newline, closefd, opener)
            if mode not in ('r', 'rt', 'tr', 'rb', 'br') or opener is not None or not closefd:
                return self.refuse(f'unsupported open mode/options for {key}')
            stream = VirtualBytes(self.files[key])
            stream.name = os.fsdecode(os.fspath(filename))
            stream.mode = mode
            if 'b' in mode:
                if encoding is not None or errors is not None or newline is not None:
                    raise ValueError('binary mode does not take encoding/errors/newline')
                return stream
            if buffering == 0:
                raise ValueError("can't have unbuffered text I/O")
            text = io.TextIOWrapper(stream, encoding=encoding, errors=errors, newline=newline)
            text.mode = mode
            return text

        def raw_open(filename, *args, **kwargs):
            if self.filename(filename) in self.files:
                return self.refuse('os.open on an overlaid file')
            # A relative open with dir_fd cannot be resolved from cwd reliably.
            if kwargs.get('dir_fd') is not None:
                return self.refuse('os.open with dir_fd')
            return original_os_open(filename, *args, **kwargs)

        def no_process(*args, **kwargs):
            return self.refuse('child process creation cannot inherit the in-memory overlay')

        finder = OverlayFinder(self.files)
        def audit(event, args):
            # Covers cached original open(), direct FileIO, and extension code
            # that would otherwise bypass the virtual streams silently.
            if self._active and event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
                if self.filename(args[0]) in self.files:
                    self.refuse('unintercepted filesystem access to an overlaid file')

        sys.addaudithook(audit)
        self._active = True
        sys.meta_path.insert(0, finder)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock_patch('builtins.open', lambda *a, **k: virtual_open(original_open, *a, **k)))
                stack.enter_context(mock_patch('io.open', lambda *a, **k: virtual_open(original_io_open, *a, **k)))
                stack.enter_context(mock_patch('os.open', raw_open))
                stack.enter_context(mock_patch.object(subprocess, 'Popen', no_process))
                stack.enter_context(mock_patch.object(multiprocessing.process.BaseProcess, 'start', no_process))
                for name in ('fork', 'forkpty', 'posix_spawn', 'posix_spawnp', 'system'):
                    if hasattr(os, name):
                        stack.enter_context(mock_patch.object(os, name, no_process))
                yield
        finally:
            self._active = False
            sys.meta_path.remove(finder)


class ProbeReports:
    def __init__(self, nodes):
        self.nodes = set(nodes)
        self.collected = set()
        self.finished = set()
        self.failed = set()
        self.collection_failed = set()
        self.collection_errors = set()
        self.interrupted = False

    def pytest_collection_modifyitems(self, session, config, items):
        chosen = [item for item in items if item.nodeid in self.nodes]
        deselected = [item for item in items if item.nodeid not in self.nodes]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = chosen
        self.collected.update(item.nodeid for item in chosen)

    def pytest_runtest_logreport(self, report):
        if report.nodeid in self.nodes:
            if report.failed:
                self.failed.add(report.nodeid)
            if report.when == 'teardown':
                self.finished.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.add(report.nodeid)
            filename = report.nodeid.split('::')[0]
            affected = {node for node in self.nodes if node.split('::')[0] == filename}
            self.collection_failed.update(affected)
            self.failed.update(affected)

    def pytest_internalerror(self, excrepr, excinfo):
        self.interrupted = True

    def pytest_keyboard_interrupt(self, excinfo):
        self.interrupted = True


@contextlib.contextmanager
def diagnostics_to_stderr():
    """Keep even native writes and pytest output off the result JSON channel."""
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def run(source, patch_path, nodes, collect_only=False, import_patches=False):
    source = source.resolve()
    if import_patches and not collect_only:
        raise ValueError('--import-patches requires --collect-only')
    if not isinstance(nodes, list) or any(not isinstance(node, str) for node in nodes):
        raise ValueError('nodes must be a JSON list of exact pytest node ID strings')
    if len(set(nodes)) != len(nodes):
        raise ValueError('duplicate node IDs')
    for node in nodes:
        filename = PurePosixPath(node.split('::')[0])
        if not filename.parts or filename.parts[0] != 'tests' or '..' in filename.parts or filename.suffix != '.py':
            raise ValueError(f'invalid test node ID: {node}')
    if not nodes:
        if import_patches:
            raise ValueError('--import-patches requires a nonempty catalog of test node IDs')
        return {'complete': True, 'failing': [], 'requested': 0, 'collected': 0,
                'finished': 0, 'collect_only': collect_only, 'reasons': []}
    files = {}
    patch_modules = []
    for change in parse_patch(patch_path.read_text()):
        filename = (source / change['path']).resolve()
        if not filename.is_relative_to(source) or not filename.is_file():
            raise ValueError('probe only supports existing files inside the baseline')
        updated, _ = apply_file(filename.read_text(), change)
        files[str(filename)] = updated.encode('utf-8')
        if filename.suffix == '.py':
            linecache.cache[str(filename)] = (len(files[str(filename)]), None,
                                             updated.splitlines(keepends=True), str(filename))
            if import_patches:
                relative = filename.relative_to(source)
                parts = list(relative.with_suffix('').parts)
                if parts[-1] == '__init__':
                    parts.pop()
                if len(relative.parts) < 2 or relative.parts[0] != 'sqlglot' or not parts or any(not part.isidentifier() for part in parts):
                    raise ValueError('--import-patches only supports Python modules inside the sqlglot package')
                patch_modules.append('.'.join(parts))
    if any(name == 'sqlglot' or name.startswith('sqlglot.') or name == 'tests' or name.startswith('tests.')
           for name in sys.modules):
        raise ValueError('probe requires a fresh process without imported baseline modules')

    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ.pop('PYTEST_ADDOPTS', None)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    os.chdir(source)
    import pytest

    reports = ProbeReports(nodes)
    overlay = Overlay(files)
    import_errors = []
    explicit_imports = []
    exitcode = None
    with tempfile.TemporaryDirectory(prefix='blast-probe-') as temporary:
        with overlay.install():
            for module_name in sorted(set(patch_modules)):
                try:
                    importlib.import_module(module_name)
                    explicit_imports.append(module_name)
                except BaseException as exc:
                    import_errors.append(f'{module_name}: {type(exc).__name__}: {exc}')
                    break  # Failed import may leave partial global initialization.
            if not import_errors:
                exitcode = pytest.main([
                    '-c', '/dev/null', '--rootdir', str(source), '--confcutdir', str(source),
                    '--basetemp', temporary, '-p', 'no:cacheprovider', '--assert=plain',
                    '-q', '--tb=no', '-rN', *(['--collect-only'] if collect_only else []), *nodes,
                ], plugins=[reports])
    unresolved = (reports.nodes - reports.collected if collect_only else
                  reports.nodes - reports.finished - reports.collection_failed)
    reasons = sorted(overlay.unsupported)
    if import_errors:
        reasons.extend(f'patched module import failed: {error}' for error in import_errors)
    if unresolved:
        reasons.append(f'{len(unresolved)} requested tests were not collected' if collect_only else
                       f'{len(unresolved)} requested tests did not finish or report import failure')
    if collect_only and reports.collection_errors:
        reasons.append(f'{len(reports.collection_errors)} collection errors')
    if reports.interrupted or exitcode not in ((0,) if collect_only else (0, 1, 2)):
        reasons.append(f'pytest incomplete session (exit {exitcode})')
    loaded_paths = set()
    for module in tuple(sys.modules.values()):
        if module is None:
            continue
        filename = getattr(module, '__dict__', {}).get('__file__')
        if isinstance(filename, str):
            loaded_paths.add(os.path.abspath(filename))
    python_patches = {path for path in files if path.endswith('.py')}
    if import_patches and python_patches - loaded_paths:
        reasons.append('some patched Python modules were not loaded')
    return {'complete': not reasons, 'failing': sorted(reports.failed), 'requested': len(nodes),
            'collected': len(reports.collected), 'collect_only': collect_only,
            'finished': len(reports.finished), 'collection_failed': sorted(reports.collection_failed),
            'collection_errors': sorted(reports.collection_errors),
            'explicit_imports': explicit_imports, 'patch_import_errors': import_errors,
            'python_patches_loaded': sorted(os.path.relpath(path, source) for path in python_patches & loaded_paths),
            'python_patches_not_loaded': sorted(os.path.relpath(path, source) for path in python_patches - loaded_paths),
            'pytest_exitcode': int(exitcode) if exitcode is not None else None, 'reasons': reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--patch', type=Path, required=True)
    parser.add_argument('--nodes', required=True, help='JSON node-ID list file, or - for stdin')
    parser.add_argument('--collect-only', action='store_true', help='require complete successful collection; do not execute tests')
    parser.add_argument('--import-patches', action='store_true', help='with --collect-only, first import patched sqlglot Python modules')
    args = parser.parse_args()
    start = time.process_time()
    try:
        nodes = json.load(sys.stdin) if args.nodes == '-' else json.loads(Path(args.nodes).read_text())
        with diagnostics_to_stderr():
            result = run(args.source, args.patch.resolve(), nodes, collect_only=args.collect_only,
                         import_patches=args.import_patches)
    except Exception as exc:
        result = {'complete': False, 'failing': [], 'reasons': [f'{type(exc).__name__}: {exc}']}
    # process_time is CPU since this process started, so include interpreter and
    # module-import startup rather than reporting only the pytest portion.
    result['cpu_seconds'] = round(time.process_time(), 6)
    result['execution_cpu_seconds'] = round(time.process_time() - start, 6)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
