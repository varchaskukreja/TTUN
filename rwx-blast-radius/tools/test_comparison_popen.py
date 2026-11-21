"""Cold CLI integration test for ordinary/nested Python subprocess capture."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from comparison_spawn import install_parent, audit_receipts


def main():
    source = Path(sys.argv[1]).resolve()
    os.chdir(source)
    with tempfile.TemporaryDirectory(prefix='comparison-popen-') as directory:
        context = ['ordinary']
        restore = install_parent(lambda: context[0], source, directory)
        try:
            code = 'from sqlglot import parse_one; assert parse_one("SELECT 1").sql() == "SELECT 1"'
            subprocess.run([sys.executable, '-c', code], check=True, timeout=60)
            context[0] = 'nested'
            nested = 'import subprocess,sys; subprocess.run([sys.executable,"-c",' + repr(code) + '],check=True)'
            subprocess.run([sys.executable, '-c', nested], check=True, timeout=60)
            report = audit_receipts(directory)
            if report['errors']:
                for path in Path(directory).glob('events-*.jsonl'):
                    print(path.read_text(), file=sys.stderr)
            assert report['python_subprocesses'] == 3 and not report['errors'], report
            assert sorted(c for cs in report['worker_contexts'].values() for c in cs) == ['nested', 'nested', 'ordinary'], report
            # Audit parsing scales with PIDs, not job/event counts.
            original = Path.read_text
            reads = []
            def counted(path, *args, **kwargs):
                if path.name.startswith('observations-'):
                    reads.append(path.name)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', counted):
                audit_receipts(directory)
            assert len(reads) == len(set(reads)) == 3, reads
            # Removing one child completion invalidates the entire capture.
            receipt = next(p for p in Path(directory).glob('events-*.jsonl') if 'popen_child_end' in p.read_text())
            before = receipt.read_text()
            receipt.write_text('\n'.join(line for line in before.splitlines() if json.loads(line)['kind'] != 'popen_child_end')+'\n')
            assert audit_receipts(directory)['errors']
            receipt.write_text(before)
            context[0] = 'unsupported'
            subprocess.run([sys.executable, '-S', '-c', 'pass'], check=True)
            assert 'unsupported' in audit_receipts(directory)['must_retain']
            print(json.dumps({'ordinary_nested_passed': True, 'missing_end_rejected': True,
                              'unsupported_flags_retained': True, 'one_parse_per_pid': True,
                              'successful_capture': report}, indent=2))
        finally:
            restore()


if __name__ == '__main__':
    main()
