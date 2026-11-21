"""Run as a script in a fresh Python interpreter; spawn must re-import this file."""
from concurrent.futures import ProcessPoolExecutor
import json
from multiprocessing import get_context
import os
from pathlib import Path
import sys
import tempfile

from comparison_spawn import install_parent, audit_receipts


def task(value):
    from sqlglot import parse_one
    return os.getpid(), parse_one(value).sql()


def baseline(source):
    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
    node = 'tests/test_optimizer.py::TestOptimizer::test_eliminate_subqueries'
    sys.path.insert(0, str(source))
    with tempfile.TemporaryDirectory(prefix='comparison-baseline-') as directory:
        os.environ.update(COMPARISON_SOURCE=str(source), COMPARISON_NODES=json.dumps([node]),
                          COMPARISON_OUTPUT=directory + '/parent.json', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        import comparison_types
        import pytest
        restore = install_parent(lambda: comparison_types._context, source, directory)
        try:
            os.chdir(source)
            code = pytest.main([node, '-p', 'comparison_types', '-q', '-p', 'no:cacheprovider', '--assert=plain'])
            report = audit_receipts(directory)
            report.update(pytest_exitcode=int(code), outcomes=comparison_types._reports,
                          observation_shards=len(list(Path(directory).glob('observations-*.json'))))
            assert code == 0 and report['submitted'] and not report['must_retain'] and not report['errors'], report
            print('SPAWN_REPORT=' + json.dumps(report, sort_keys=True))
        finally:
            restore()


def main():
    source = Path(sys.argv[1]).resolve()
    if '--baseline' in sys.argv:
        return baseline(source)
    sys.path.insert(0, str(source))
    with tempfile.TemporaryDirectory(prefix='comparison-spawn-') as directory:
        context = ['first']
        restore = install_parent(lambda: context[0], source, directory)
        try:
            with ProcessPoolExecutor(max_workers=1, mp_context=get_context('spawn')) as pool:
                first = pool.submit(task, 'SELECT 1').result(timeout=60)
                context[0] = 'second'
                second = pool.submit(task, 'SELECT NULL').result(timeout=60)
            assert first[0] == second[0], (first, second)
            report = audit_receipts(directory)
            assert report['submitted'] == 2, report
            assert not report['must_retain'] and not report['errors'], report
            assert report['worker_contexts'] == {str(first[0]): ['first', 'second']}, report
            snapshot = json.loads((Path(directory) / f'observations-{first[0]}.json').read_text())
            observed = {c for site in snapshot['sites'].values() for pair in site['observations'] for c in pair['contexts']}
            assert {'first', 'second'} <= observed, observed
            # Removing a receipt must conservatively retain its context.
            receipt = Path(directory) / f'events-{first[0]}.jsonl'
            lines = [json.loads(line) for line in receipt.read_text().splitlines()]
            receipt.write_text(''.join(json.dumps(e)+'\n' for e in lines if not (e['kind'] == 'job_end' and e['context'] == 'second')))
            assert 'second' in audit_receipts(directory)['must_retain']
            with receipt.open('a') as stream:
                stream.write(json.dumps({'kind': 'untracked_activity', 'pid': first[0], 'context': 'first', 'event': 'subprocess.Popen'})+'\n')
            assert {'first', 'second'} <= set(audit_receipts(directory)['must_retain'])
            print(json.dumps({'reuse_passed': True, 'missing_receipt_retained': True, 'untracked_activity_retained': True, 'report': report,
                              'observed_contexts': sorted(observed), 'sites': len(snapshot['sites'])}, indent=2))
        finally:
            restore()


if __name__ == '__main__':
    main()
