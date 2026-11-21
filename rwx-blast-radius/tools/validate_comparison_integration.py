#!/usr/bin/env python3
"""Experimental all-site policy equivalence; no mutation answer inputs."""
import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from comparison_selection import ExperimentalComparisons
from comparison_types import observed_certificate,site_key
from analyze_comparison_gain import conditional_selection
from selector import read_json

OPS={'Is':('is','=='),'Eq':('==','is'),'IsNot':('is not','!='),'NotEq':('!=','is not')}
def normalized(mapping,key):
    site=mapping['sites'][key];path=site['path'];old,new=OPS[site['operator']]
    before=f'left {old} right\n';after=f'left {new} right\n'
    normalized_key=site_key(path,ast.parse(before).body[0].value)
    value=dict(site,source_sha256=hashlib.sha256(before.encode()).hexdigest())
    result=observed_certificate(dict(mapping,sites={normalized_key:value}),path,before,after)
    if 'site' in result:result['site']=key
    result['path']=path
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    tests=read_json(ROOT/'sample/tests.json')['tests'];production=read_json(ROOT/'maps/coverage.json.gz')
    runtime=ExperimentalComparisons(tests,production,a.source,ROOT/'maps/comparison-experimental.json.gz',ROOT/'maps/comparison-experimental-manifest.json',enabled=True)
    if runtime.mapping is None:raise ValueError(runtime.issue)
    mapping=runtime.mapping;all_tests={t['number'] for t in tests};rows=[];deltas=[]
    for key in sorted(mapping['sites']):
        certificate=normalized(mapping,key)
        with patch('analyze_comparison_gain.qualify_patch',return_value=certificate):
            legacy,detail=conditional_selection(mapping,Path(a.source),'normalized',all_tests,tests,production)
        with patch('comparison_selection.qualify',return_value=certificate):
            candidate=runtime.propose('normalized',all_tests,{'complete':True,'reason':'dependency_selection'})
        expected=legacy|runtime.unstable
        if candidate!=expected:raise ValueError('unexpected decision difference: '+key)
        if candidate!=legacy:deltas.append({'site':key,'added_unstable':sorted(candidate-legacy)})
        # Complete dependency selection may legitimately include every test.
        # An identical test set from fallback must never be narrowed.
        with patch('comparison_selection.qualify',return_value=certificate):
            for reason in ('unknown','invalid_syntax','global_effect_without_guard','class_import_guard_failed'):
                retained=runtime.propose('normalized',all_tests,{'complete':False,'reason':reason})
                if retained!=all_tests:raise ValueError('fallback narrowed: '+key)
        rows.append({'site':key,'legacy_decision':detail['decision'],'selection_digest':hashlib.sha256(json.dumps(sorted(candidate)).encode()).hexdigest()})
    # Verify extracted proof and capture gates exactly match reviewed function ASTs.
    for origin,target,names in [('tools/comparison_types.py','comparison_proof.py',{'site_key','single_operator_edit','observed_certificate'}),('tools/analyze_comparison_gain.py','comparison_audit.py',{'digest','capture_issues','_capture_issues'})]:
        defs=[]
        for name in (origin,target):
            tree=ast.parse((ROOT/name).read_text());defs.append({n.name:ast.dump(n,include_attributes=False) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names})
        if defs[0]!=defs[1]:raise ValueError('extracted function differs')
    report={'kind':'isolated_opt_in_comparison_integration_validation','production_authorized':False,'sites':len(rows),'equivalent_modulo_required_unstable_retention':True,'unstable_retention_differences':deltas,'fallback_policy_checks':4*len(rows),'proof_and_capture_functions_ast_identical':True,'decisions':rows,'answers_read':False,'artifact_sha256':hashlib.sha256((ROOT/'maps/comparison-experimental.json.gz').read_bytes()).hexdigest()}
    Path(a.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k not in ('decisions','unstable_retention_differences')}));print('unstable-delta sites',len(deltas))

if __name__=='__main__':main()
