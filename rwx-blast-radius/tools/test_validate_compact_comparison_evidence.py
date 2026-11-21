import tempfile
import unittest
from pathlib import Path
from comparison_types import Recorder
from fast_comparison_qualifier import qualify_patch
from validate_compact_comparison_evidence import actual_patch,certificate
from compact_comparison_evidence import pack,unpack

class ValidationTests(unittest.TestCase):
    def test_source_bound_and_normalized_acceptance_not_trivial_rejection(self):
        source='def f(x):\n    return x is None\n'
        recorder=Recorder(lambda:'test');recorder.execute(source,'module.py')['f'](None)
        full=recorder.snapshot();compact=unpack(pack(full));key=next(iter(full['sites']))
        self.assertTrue(certificate(full,key)['equivalent_for_observed_types'])
        self.assertEqual(certificate(full,key)['path'],'module.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'module.py').write_text(source)
            change=next(actual_patch(root,full['sites'][key]))
            left=qualify_patch(full,root,change);right=qualify_patch(compact,root,change)
            self.assertTrue(left['equivalent_for_observed_types'])
            self.assertEqual(left,right)

if __name__=='__main__':unittest.main()
