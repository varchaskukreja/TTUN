import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comparison_spawn import audit_receipts


class AuditIndexTests(unittest.TestCase):
    def test_many_jobs_parse_reused_worker_once(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            parent = [{'kind': 'process_start', 'pid': 1, 'child_pid': 2, 'context': 'test0', 'tracked': True}]
            child = [{'kind': 'worker_start', 'pid': 2, 'argv': []}, {'kind': 'worker_end', 'pid': 2, 'contexts': []}]
            for index in range(200):
                job, context = f'job{index}', f'test{index}'
                parent.append({'kind': 'submitted', 'pid': 1, 'job': job, 'context': context, 'supported': True})
                child.extend([{'kind': 'job_start', 'pid': 2, 'job': job, 'context': context},
                              {'kind': 'job_end', 'pid': 2, 'job': job, 'context': context, 'returned': True}])
            for pid, events in ((1, parent), (2, child)):
                (directory / f'events-{pid}.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            (directory / 'observations-2.json').write_text(json.dumps({'sites': {}, 'read_dependencies': {}}))
            original = Path.read_text
            reads = []
            def counted(path, *args, **kwargs):
                if path.name.startswith('observations-'):
                    reads.append(path.name)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', counted):
                report = audit_receipts(directory)
            self.assertEqual(reads, ['observations-2.json'])
            self.assertEqual(report['submitted'], 200)
            self.assertEqual(report['errors'], [])
            self.assertEqual(len(report['worker_contexts']['2']), 200)
            (directory / 'observations-2.json').write_text('{invalid')
            report = audit_receipts(directory)
            self.assertTrue(report['errors'])
            self.assertEqual(len(report['must_retain']), 200)


if __name__ == '__main__':
    unittest.main()
