import copy
import gzip
import hashlib
import tempfile
import subprocess
import sys
from pathlib import Path
import json
import unittest
from compact_comparison_evidence import pack,unpack,load_plain

class CompactEvidenceTests(unittest.TestCase):
    def test_all_json_types_and_repeated_containers(self):
        value={'x':[None,False,True,0,1,1.5,'é'],'repeat':[['a'],['a']]}
        restored=unpack(json.loads(json.dumps(pack(value))))
        self.assertEqual(value,restored)
        restored['repeat'][0].append('different')
        self.assertEqual(restored['repeat'][1],['a'])

    def test_unknown_context_and_flags_preserved(self):
        value={'contexts':['','@unknown:12'],'complete':False,'narrowing_authorized':False,'baseline_unstable_tests':['test']}
        self.assertEqual(unpack(pack(value)),value)

    def test_corruption_and_forward_reference_rejected(self):
        p=pack({'x':['a']})
        p['nodes'][p['root']]=('l',(p['root'],))
        with self.assertRaises(ValueError):unpack(p)
        p=pack({'x':1});p['expanded_sha256']='bad'
        with self.assertRaises(ValueError):unpack(p)

    def test_boolean_integer_and_float_not_coalesced(self):
        values=[False,0,0.0,True,1,1.0]
        self.assertEqual([type(v) for v in unpack(pack(values))],[type(v) for v in values])

    def test_plain_digest_validation(self):
        raw=b'{"unknown": ["@worker:12"], "complete": false}'
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'map.gz';path.write_bytes(gzip.compress(raw))
            self.assertEqual(load_plain(path,hashlib.sha256(raw).hexdigest()),json.loads(raw))
            with self.assertRaises(ValueError):load_plain(path,'incorrect')

    def test_plain_cli_noncanonical_json_digest(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'input.json';out=root/'out.gz';report=root/'report.json'
            raw=b'{ "sites" : {}, "extra" : [1, 2] }\n';source.write_bytes(raw)
            subprocess.run([sys.executable,str(Path(__file__).with_name('compact_comparison_evidence.py')),'--input',str(source),'--output',str(out),'--report',str(report),'--encoding','plain'],check=True,capture_output=True)
            meta=json.loads(report.read_text())
            self.assertEqual(meta['expanded_sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(load_plain(out,meta['expanded_sha256']),json.loads(raw))

if __name__=='__main__':unittest.main()
