"""Research-only append-only phase journal for bounded registry diagnostics."""
import json
import os
from pathlib import Path


def pytest_runtest_logreport(report):
    target=os.environ.get('REGISTRY_PROBE_JOURNAL')
    if not target:return
    row={'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,
         'subtest':type(report).__name__=='SubtestReport'}
    if report.failed:row['error']=str(report.longrepr)[:4000]
    with Path(target).open('a') as stream:
        stream.write(json.dumps(row,sort_keys=True)+'\n')
