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
context = ''
reads = {}
results = {}
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
            reads.setdefault(rel, set()).add(context)


def pytest_configure(config):
    sys.addaudithook(audit)
    cov.start()


def pytest_collection_modifyitems(session, config, items):
    chosen = [item for item in items if item.nodeid in IDS]
    found = {item.nodeid for item in chosen}
    (OUT / 'missing_tests.json').write_text(json.dumps(sorted(IDS - found)))
    items[:] = chosen


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global context
    context = item.nodeid
    cov.switch_context(context)
    yield
    cov.switch_context('')
    context = ''


def pytest_runtest_logreport(report):
    if report.when == 'call' or report.failed:
        results[report.nodeid] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    cov.stop()
    cov.save()
    (OUT / 'reads.json').write_text(json.dumps({k: sorted(v) for k, v in reads.items()}))
    (OUT / 'results.json').write_text(json.dumps(results, sort_keys=True))
