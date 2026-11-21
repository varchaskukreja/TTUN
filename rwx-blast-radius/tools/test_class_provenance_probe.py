import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ProvenanceProbeTests(unittest.TestCase):
    def test_import_initializer_and_later_receiver_are_distinct(self):
        if not hasattr(sys, 'monitoring'):
            self.skipTest('Python monitoring required')
        tools = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = root / 'sqlglot'
            package.mkdir()
            (package/'__init__.py').write_text('')
            (package/'synthetic.py').write_text('class Base:\n    def __init_subclass__(cls): pass\nclass Target(Base):\n    def ping(self): return 1\n')
            output = root/'output'
            env = dict(os.environ, CLASS_PROVENANCE_SOURCE=str(root), CLASS_PROVENANCE_OUTPUT=str(output),
                       CLASS_PROVENANCE_MODULE='sqlglot.synthetic', CLASS_PROVENANCE_CLASS='Target',
                       CLASS_PROVENANCE_NODE='synthetic', PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=os.pathsep.join([str(tools/'class_provenance_bootstrap'),str(tools),str(root)]))
            process = subprocess.run([sys.executable,'-c','from sqlglot.synthetic import Target; Target().ping()'],
                                     env=env,text=True,capture_output=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            events = [event for path in output.glob('class-calls-*.json') for event in json.loads(path.read_text())['events']]
            self.assertTrue(any(e['function']=='Base.__init_subclass__' and e['lifecycle']=='class_initialization' for e in events))
            self.assertTrue(any(e['function']=='Target.ping' and e['lifecycle']=='receiver_use' for e in events))


if __name__=='__main__':unittest.main()
