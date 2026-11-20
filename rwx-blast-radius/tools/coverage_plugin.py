"""Baseline-only instrumentation; never reads patches or expected failures."""
import json
import os
import sys
from pathlib import Path

import coverage
import pytest

ROOT = Path(os.environ['BLAST_SOURCE']).resolve()
OUT = Path(os.environ['BLAST_WORK']).resolve()
CATALOG = json.loads(Path(os.environ['BLAST_CATALOG']).read_text())['tests']
IDS = {t['id'] for t in CATALOG}
RETRY = os.environ.get('BLAST_RETRY')
if RETRY:
    IDS = set(json.loads(RETRY))
context = ''
reads = {}
results = {}
classes = None
call_outcomes = {}
if RETRY:
    reads = {k: set(v) for k, v in json.loads((OUT / 'reads.json').read_text()).items()}
    results = {k: v for k, v in json.loads((OUT / 'results.json').read_text()).items() if k not in IDS}
    if (OUT / 'call_outcomes.json').exists():
        call_outcomes = {k: v for k, v in json.loads((OUT / 'call_outcomes.json').read_text()).items() if k not in IDS}
cov = coverage.Coverage(data_file=str(OUT / 'baseline.coverage'), source=[str(ROOT / 'sqlglot'), str(ROOT / 'tests')])


def audit(event, args):
    if event != 'open' or not args or not isinstance(args[0], (str, bytes)):
        return
    path = os.fsdecode(args[0])
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if path.startswith(str(ROOT) + os.sep):
        rel = os.path.relpath(path, ROOT)
        if not rel.startswith('.git/') and not rel.endswith(('.pyc', '.coverage')):
            consumers = {context}
            if not rel.endswith('.py') and context:
                frame = sys._getframe(1)
                while frame is not None:
                    if frame.f_code.co_name == 'setUpClass':
                        prefix = context.rsplit('::', 1)[0] + '::'
                        consumers.update(t['id'] for t in CATALOG if t['id'].startswith(prefix))
                        break
                    if frame.f_code.co_name == 'setUpModule':
                        prefix = context.split('::', 1)[0] + '::'
                        consumers.update(t['id'] for t in CATALOG if t['id'].startswith(prefix))
                        break
                    request = frame.f_locals.get('request')
                    if isinstance(request, pytest.FixtureRequest) and request.scope != 'function':
                        if request.scope == 'class':
                            prefix = context.rsplit('::', 1)[0] + '::'
                        elif request.scope == 'module':
                            prefix = context.split('::', 1)[0] + '::'
                        else:
                            prefix = ''
                        consumers.update(t['id'] for t in CATALOG if t['id'].startswith(prefix))
                        break
                    frame = frame.f_back
            reads.setdefault(rel, set()).update(consumers)


def pytest_configure(config):
    global classes
    if os.environ.get('BLAST_CLASSES'):
        from class_dependencies import ClassDependencies
        classes = ClassDependencies(ROOT, lambda: context)
        classes.start()
    from concurrent.futures import ProcessPoolExecutor
    import coverage_worker
    original_submit = ProcessPoolExecutor.submit
    def submit(pool, function, *args, **kwargs):
        return original_submit(pool, coverage_worker.run, context, function, args, kwargs)
    ProcessPoolExecutor.submit = submit
    sys.addaudithook(audit)
    if RETRY:
        cov.load()
    cov.start()


def pytest_collection_modifyitems(session, config, items):
    chosen = [item for item in items if item.nodeid in IDS]
    found = {item.nodeid for item in chosen}
    if not RETRY:
        (OUT / 'missing_tests.json').write_text(json.dumps(sorted(IDS - found)))
    items[:] = chosen


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global context
    context = item.nodeid
    os.environ['BLAST_CONTEXT'] = context
    cov.switch_context(context)
    yield
    cov.switch_context('')
    context = ''
    os.environ.pop('BLAST_CONTEXT', None)


def pytest_runtest_logreport(report):
    if report.when == 'call' and type(report).__name__ != 'SubtestReport':
        call_outcomes[report.nodeid] = report.outcome
    if report.when == 'call' or report.failed or report.skipped:
        if results.get(report.nodeid) != 'failed':
            results[report.nodeid] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    if classes is not None:
        classes.stop()
        (OUT / 'classes.json').write_text(json.dumps(classes.snapshot()))
    cov.stop()
    cov.save()
    (OUT / 'reads.json').write_text(json.dumps({k: sorted(v) for k, v in reads.items()}))
    (OUT / 'results.json').write_text(json.dumps(results, sort_keys=True))
    (OUT / 'call_outcomes.json').write_text(json.dumps(call_outcomes, sort_keys=True))
