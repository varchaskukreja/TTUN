"""Fork each baseline test from a parent that has performed collection only.

Loaded alongside coverage_plugin by run_isolated_map.py. The parent never runs
a test or fixture. Every child starts with the same collection-time state, gets
a fresh tracer, executes one full pytest protocol, saves, then exits explicitly.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import traceback

import coverage
import pytest


def _write(path, payload):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, sort_keys=True))
    temporary.replace(path)


def _child(item, parent):
    import coverage_plugin as traced

    pid = os.getpid()
    os.setsid()  # Parent can terminate this test and its spawned descendants.
    sys.stdout.flush()
    sys.stderr.flush()
    log = os.open(str(traced.OUT / f'isolated-log-{pid}.txt'), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(log)
    traced.cov.stop()
    if traced.classes is not None:
        traced.classes.stop()
        traced.classes = None
    traced.context = ''
    traced.reads = {}
    traced.results = {}
    traced.call_outcomes = {}
    os.environ.pop('BLAST_CONTEXT', None)
    traced.cov = coverage.Coverage(
        data_file=str(traced.OUT / 'baseline.coverage'), data_suffix=True,
        source=[str(traced.ROOT / 'sqlglot'), str(traced.ROOT / 'tests')],
    )
    if os.environ.get('BLAST_CLASSES'):
        from class_dependencies import ClassDependencies
        traced.classes = ClassDependencies(traced.ROOT, lambda: traced.context)
        traced.classes.start()
    traced.cov.start()
    started = time.monotonic()
    errors = []
    try:
        # Existing coverage_plugin protocol and fixture wrappers switch contexts
        # and propagate them through the ProcessPoolExecutor submit wrapper.
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
    except BaseException as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
    finally:
        try:
            item.session._setupstate.teardown_exact(None)
        except BaseException as exc:
            errors.append(f'teardown: {type(exc).__name__}: {exc}')
            traceback.print_exc()
        # Normal fixture teardown must close its pools. Do not leave live test
        # processes behind while marking an incomplete trace as successful.
        for process in multiprocessing.active_children():
            process.join(timeout=1)
            if process.is_alive():
                errors.append(f'process {process.pid} survived fixture teardown')
                process.terminate()
                process.join(timeout=1)
        if errors or item.nodeid not in traced.results:
            traced.results[item.nodeid] = 'failed'
        try:
            if traced.classes is not None:
                traced.classes.stop()
                _write(traced.OUT / f'child-classes-{pid}.json', traced.classes.snapshot())
            traced.cov.stop()
            traced.cov.save()
            _write(traced.OUT / f'child-reads-{pid}.json', {path: sorted(contexts) for path, contexts in traced.reads.items()})
            _write(traced.OUT / f'isolated-result-{pid}.json', {
                'nodeid': item.nodeid, 'parent_pid': parent, 'pid': pid,
                'results': traced.results, 'call_outcomes': traced.call_outcomes,
                'errors': errors, 'wall_seconds': time.monotonic() - started,
            })
            sys.stdout.flush()
            sys.stderr.flush()
        except BaseException:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(2)
    os._exit(0 if not errors and traced.results.get(item.nodeid) in ('passed', 'skipped') else 1)


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    import coverage_plugin as traced

    if session.config.option.collectonly:
        return None
    if not hasattr(os, 'fork'):
        raise pytest.UsageError('isolated map generation requires Unix fork')
    if session.testsfailed:
        raise pytest.UsageError('refusing isolated tests after collection errors')
    concurrency = int(os.environ.get('BLAST_ISOLATED_JOBS', '2'))
    if concurrency not in (1, 2):
        raise pytest.UsageError('isolated map concurrency must be 1 or 2')
    timeout = float(os.environ.get('BLAST_ISOLATED_TIMEOUT', '300'))
    # Freeze the trace and state inherited by every child. Only the collection
    # trace will be written by the original coverage_plugin sessionfinish hook.
    traced.cov.stop()
    traced.cov.save()
    if traced.classes is not None:
        traced.classes.stop()
    pending = iter(session.items)
    live = {}
    exhausted = False
    manifest = []
    parent_pid = os.getpid()
    try:
        while live or not exhausted:
            while len(live) < concurrency and not exhausted:
                try:
                    item = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                sys.stdout.flush()
                sys.stderr.flush()
                pid = os.fork()
                if pid == 0:
                    # Even setup failures before _child's tracing try/finally
                    # must never fall back into the inherited parent loop and
                    # terminate a sibling's process group.
                    try:
                        _child(item, parent_pid)
                    except BaseException as exc:
                        try:
                            _write(traced.OUT / f'isolated-result-{os.getpid()}.json', {
                                'nodeid': item.nodeid, 'parent_pid': parent_pid,
                                'pid': os.getpid(), 'results': {item.nodeid: 'failed'},
                                'call_outcomes': {}, 'wall_seconds': 0,
                                'errors': [f'child initialization: {type(exc).__name__}: {exc}'],
                            })
                        except BaseException:
                            pass
                    finally:
                        os._exit(2)
                live[pid] = {'nodeid': item.nodeid, 'started': time.monotonic(), 'timed_out': False}
            for pid, info in list(live.items()):
                if not info['timed_out'] and time.monotonic() - info['started'] > timeout:
                    info['timed_out'] = True
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                finished, status = os.waitpid(pid, os.WNOHANG)
                if not finished:
                    continue
                path = traced.OUT / f'isolated-result-{pid}.json'
                if path.exists():
                    result = json.loads(path.read_text())
                else:
                    result = {'nodeid': info['nodeid'], 'pid': pid, 'parent_pid': parent_pid,
                              'results': {info['nodeid']: 'failed'}, 'call_outcomes': {},
                              'errors': ['timeout' if info['timed_out'] else 'child exited without saved result']}
                result['wait_exitcode'] = os.waitstatus_to_exitcode(status)
                result['timed_out'] = info['timed_out']
                if info['timed_out'] or result['wait_exitcode'] not in (0, 1):
                    result['results'][info['nodeid']] = 'failed'
                traced.results.update(result['results'])
                traced.call_outcomes.update(result['call_outcomes'])
                if result['results'].get(info['nodeid']) == 'failed':
                    session.testsfailed += 1
                manifest.append(result)
                del live[pid]
                # Clean up helper descendants such as multiprocessing's resource
                # tracker after the child has explicitly saved its trace.
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    # The test process is already reaped; some macOS runtimes
                    # reject signalling its now-empty group with EPERM rather
                    # than ESRCH. Preserve this cleanup observation without
                    # discarding the explicitly saved completed test trace.
                    result['cleanup_warning'] = str(exc)
                print(f"isolated {len(manifest)}/{len(session.items)} {info['nodeid']} "
                      f"{result['results'].get(info['nodeid'])}", file=sys.stderr, flush=True)
            if live:
                time.sleep(0.025)
    finally:
        for pid in live:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        _write(traced.OUT / 'isolated-manifest.json', {
            'mode': 'fork-from-collection-only-parent', 'parent_pid': parent_pid,
            'requested': [item.nodeid for item in session.items],
            'completed': manifest, 'jobs': concurrency,
        })
    return True
