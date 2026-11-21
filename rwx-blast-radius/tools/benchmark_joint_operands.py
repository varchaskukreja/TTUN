#!/usr/bin/env python3
"""Fresh joint operand descriptor experiment; no persistent type/member cache."""
import argparse
import enum
import json
from pathlib import Path
import statistics
import sys
import time
import unittest

from comparison_types import describe_operand, describe_type, namespace, effective_slot, TYPE_MRO, describe_operands




def reference(left, right):
    return describe_operand(left), describe_operand(right)


class JointTests(unittest.TestCase):
    def assert_same(self, left, right):
        self.assertEqual(reference(left, right), describe_operands(left, right))

    def test_types_aliases_forged_and_different_types(self):
        class Word(str, enum.Enum):
            A='a'; ALIAS='a'; B='b'
        class Number(int, enum.Enum):
            A=1; B=2
        class Pure(enum.Enum):
            A=1; B=2
        for pair in [(None,None),(object(),object()),(Word.A,Word.A),(Word.A,Word.ALIAS),(Word.A,Word.B),(Word.A,'a'),(Word.A,str.__new__(Word,'a')),(Number.A,Number.B),(Pure.A,Pure.B)]:
            self.assert_same(*pair)

    def test_mutation_between_observations(self):
        class Word(str, enum.Enum): A='a'; B='b'
        self.assert_same(Word.A,Word.B)
        Word._member_map_['FORGED']=str.__new__(Word,'a')
        self.assert_same(Word.A,Word.B)
        del Word._member_map_['FORGED']
        Word.__eq__=lambda self,other: True
        self.assert_same(Word.A,Word.B)
        Word.__ne__=lambda self,other: False
        self.assert_same(Word.A,Word.B)

    def test_distinct_members_same_underlying_value_and_early_break(self):
        class Weird(str,enum.Enum):
            def __new__(cls,label):
                item=str.__new__(cls,'same');item._value_=label;return item
            A='a';B='b';C='c'
        for left in Weird:
            for right in Weird:
                self.assert_same(left,right)

    def test_custom_metaclass_and_conversion_hooks(self):
        class Meta(enum.EnumType): pass
        class Custom(str,enum.Enum,metaclass=Meta): A='a'
        class Word(str,enum.Enum):
            A='a';B='b'
            def __str__(self): raise AssertionError('hook called')
        self.assert_same(Custom.A,Custom.A)
        self.assert_same(Word.A,Word.B)

    def test_descriptors_are_independent_and_mutation_not_cached(self):
        class Number(int,enum.Enum): A=1;B=2
        left,right=describe_operands(Number.A,Number.B)
        left['canonical_member']=False
        self.assertTrue(right['canonical_member'])
        self.assertTrue(describe_operands(Number.A,Number.B)[0]['canonical_member'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--iterations',type=int,default=2000);args=parser.parse_args()
    result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(JointTests))
    if not result.wasSuccessful(): raise SystemExit(1)
    sys.path.insert(0,str(args.source.resolve()))
    from sqlglot.tokens import TokenType
    from sqlglot.dialects.dialect import NormalizationStrategy
    rows=[]
    for name,pair in [('token_enum',(TokenType.SELECT,TokenType.FROM)),('normalization_enum',(NormalizationStrategy.LOWERCASE,NormalizationStrategy.UPPERCASE)),('none',(None,None)),('mixed',(TokenType.SELECT,None))]:
        if reference(*pair)!=describe_operands(*pair):raise AssertionError('descriptor mismatch')
        measurements={name:[] for name in ('reference','joint')}
        for repeat in range(3):
            order=[('reference',reference),('joint',describe_operands)]
            if repeat%2:order.reverse()
            for label,fn in order:
                start=time.process_time()
                for _ in range(args.iterations):fn(*pair)
                measurements[label].append(time.process_time()-start)
        medians={name:statistics.median(values) for name,values in measurements.items()}
        rows.append({'workload':name,'cpu_seconds':measurements,'medians':medians,'speedup':medians['reference']/medians['joint'],'descriptors_equal':True})
    report={'format':1,'tests_passed':result.testsRun,'iterations':args.iterations,'python':sys.version,'results':rows,'persistent_cache':False,'limitations':['Concurrent member/type mutation inside one observation is outside the deterministic baseline assumption.','Synthetic speedup does not measure fullsuite speedup.'],'capture_modified':False}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(rows,indent=2))

if __name__=='__main__':main()
