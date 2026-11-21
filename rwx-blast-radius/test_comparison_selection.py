import copy
import gzip
import zlib
import hashlib
import json
import difflib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from comparison_selection import ExperimentalComparisons,qualify
from selector import Selector
import sys
sys.path.insert(0,str(Path(__file__).parent/'tools'))
from comparison_types import Recorder

SOURCE='def f(x):\n    return x is None\n'
def change(after):
    return 'diff --git a/module.py b/module.py\n'+''.join(difflib.unified_diff(SOURCE.splitlines(True),after.splitlines(True),fromfile='a/module.py',tofile='b/module.py'))
OK={'complete':True,'reason':'dependency_selection'}

class ComparisonSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'module.py').write_text(SOURCE)
        self.tests=[{'id':x,'number':i+1} for i,x in enumerate(['a','b','reader','unstable'])]
        context=['a'];recorder=Recorder(lambda:context[0]);f=recorder.execute(SOURCE,'module.py')['f'];f(None);context[0]='b';f(None)
        self.evidence=recorder.snapshot();self.evidence.setdefault('ineligible_files',[]);self.evidence.setdefault('preexisting_modules',{})
        self.production={'test_ids':[t['id'] for t in self.tests],'files':{'module.py':{'lines':{'2':[0,1,2,3]},'read_tests':[]}},'baseline_unstable_tests':[]}
        self.patch=change(SOURCE.replace(' is ',' == '))
    def candidate(self):
        result=ExperimentalComparisons(self.tests,self.production,self.root)
        result.enabled=True;result.mapping=self.evidence;result.issue=None
        return result
    def test_eligible_removes_only_exact_observed(self):
        result=self.candidate().propose(self.patch,{1,2,3,4},OK)
        self.assertEqual(result,{3,4})
    def test_unstable_source_and_production_readers_retained_and_added(self):
        self.production['baseline_unstable_tests']=['unstable']
        self.production['files']['module.py']['read_tests']=[1]
        self.evidence['read_dependencies']['source']={'module.py':['reader']}
        result=self.candidate().propose(self.patch,{1,2},OK)
        self.assertEqual(result,{2,3,4})
    def test_all_base_fallback_reasons_block(self):
        for status in ({},None,{'complete':False,'reason':'invalid_syntax'},{'complete':False,'reason':'global_effect_without_guard'},{'complete':False,'reason':'unknown_or_exception'}):
            self.assertEqual(self.candidate().propose(self.patch,{1,2,3,4},status),{1,2,3,4})
    def test_collection_and_unknown_contexts_block(self):
        for context in ('','unknown'):
            self.evidence['read_dependencies']['source']={'module.py':[context]}
            self.assertEqual(self.candidate().propose(self.patch,{1,2,3,4},OK),{1,2,3,4})
    def test_unknown_indices_and_baseline_metadata_block(self):
        self.production['files']['module.py']['lines']['2']=[999]
        self.assertEqual(self.candidate().propose(self.patch,{1,2},OK),{1,2})
        self.production['baseline_unstable_tests']=['not-in-catalog']
        self.assertEqual(self.candidate().propose(self.patch,{1},OK),{1,2,3,4})
    def test_whole_patch_and_source_hash_gate(self):
        self.assertEqual(self.candidate().propose(change(SOURCE.replace(' is ',' == ')+'# comment\n'),{1,2},OK),{1,2})
        (self.root/'module.py').write_text('# changed\n'+SOURCE)
        self.assertEqual(self.candidate().propose(self.patch,{1,2},OK),{1,2})
    def test_default_disabled_and_invalid_manifest_retain(self):
        disabled=ExperimentalComparisons(self.tests,self.production,self.root)
        self.assertEqual(disabled.propose(self.patch,{1,2},OK),{1,2})
        invalid=ExperimentalComparisons(self.tests,self.production,self.root,'missing','missing',enabled=True)
        self.assertIsNone(invalid.mapping);self.assertEqual(invalid.propose(self.patch,{1},OK),{1})
    def test_selector_success_all_is_distinct_from_fallback_all(self):
        s=Selector(self.tests,self.production,self.root)
        self.assertEqual(s.select(self.patch),{1,2,3,4});self.assertEqual(s.last_selection_status,OK)
        invalid=change('def f(x):\n    return (\n')
        self.assertEqual(s.select(invalid),{1,2,3,4});self.assertFalse(s.last_selection_status['complete'])
        globalchange=change('VALUE = x == None\n')
        self.assertEqual(s.select(globalchange),{1,2,3,4});self.assertFalse(s.last_selection_status['complete'])
    def test_failed_import_guard_not_overridden(self):
        (self.root/'module.py').write_text('class C:\n    X=True\n')
        text='diff --git a/module.py b/module.py\n--- a/module.py\n+++ b/module.py\n@@ -1,2 +1,2 @@\n class C:\n-    X=True\n+    X=False\n'
        s=Selector(self.tests,self.production,self.root,class_guard=lambda _:False,class_candidate=lambda *args:[0])
        self.assertEqual(s.select(text),{1,2,3,4});self.assertEqual(s.last_selection_status['reason'],'class_import_guard_failed')

    def test_code_reader_is_protected(self):
        site=next(iter(self.evidence['sites'].values()))
        key=next(iter(site['execution_codes']))
        self.evidence['read_dependencies']['code_object_granularity']=True
        self.evidence['read_dependencies']['code_objects']={key:{'contexts':['b']}}
        self.assertEqual(self.candidate().propose(self.patch,{1,2},OK),{2})
    def test_unknown_execution_context_is_not_dropped(self):
        site=next(iter(self.evidence['sites'].values()))
        next(iter(site['execution_codes'].values()))['contexts'].append('@untracked:12')
        self.assertEqual(self.candidate().propose(self.patch,{1,2},OK),{1,2})
    def test_missing_and_nonpassing_baseline_retained(self):
        self.production['missing_tests']=['a']
        self.production['baseline_outcomes']={'b':'failed'}
        self.assertEqual(self.candidate().propose(self.patch,{1,2},OK),{1,2})

    def test_corrupt_deflate_retains_base_even_with_matching_artifact_hash(self):
        corrupt=bytes.fromhex('1f8b0800000000000203')+b'\x07'+bytes(8)
        with self.assertRaises(zlib.error):gzip.decompress(corrupt)
        directory=Path(__file__).parent
        meta=json.loads((directory/'maps/comparison-experimental-manifest.json').read_text())
        meta['runtime_hashes']={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in meta['runtime_hashes']}
        meta['compressed_sha256']=hashlib.sha256(corrupt).hexdigest()
        evidence=self.root/'corrupt.gz';evidence.write_bytes(corrupt)
        manifest=self.root/'manifest.json';manifest.write_text(json.dumps(meta))
        self.production['baseline_unstable_tests']=['unstable']
        result=ExperimentalComparisons(self.tests,self.production,self.root,evidence,manifest,enabled=True)
        self.assertIsNone(result.mapping)
        self.assertIn('invalid block type',result.issue)
        self.assertEqual(result.propose(self.patch,{1,2},OK),{1,2,4})

    def test_persistent_class_state_fallback_never_narrowed(self):
        before='class C:\n    @classmethod\n    def f(cls, x):\n        cls.cache = x is None\n        return cls.cache\n'
        after=before.replace(' is ',' == ')
        (self.root/'module.py').write_text(before)
        text='diff --git a/module.py b/module.py\n'+''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/module.py',tofile='b/module.py'))
        selector=Selector(self.tests,self.production,self.root)
        original=selector.select(text)
        self.assertEqual(original,{1,2,3,4})
        self.assertEqual(selector.last_selection_status,{'complete':False,'reason':'persistent_class_state'})
        with patch('comparison_selection.qualify',side_effect=AssertionError('must not qualify fallback')):
            self.assertEqual(self.candidate().propose(text,original,selector.last_selection_status),original)

if __name__=='__main__':unittest.main()
