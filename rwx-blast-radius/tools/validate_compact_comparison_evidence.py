#!/usr/bin/env python3
"""All-site representation equivalence; no mutation labels or selected output.

Site algebra/reader-policy checks use a normalized one-comparison source, since
full mapping equality separately proves original source/span/hash preservation.
Actual source-bound whole-patch qualification is additionally timed per file.
"""
import argparse
import ast
import difflib
import gzip
import hashlib
import json
import time
import tokenize
import io
from pathlib import Path
from unittest.mock import patch
from compact_comparison_evidence import load,load_plain,canonical
from comparison_types import observed_certificate,site_key
from analyze_comparison_gain import conditional_selection,capture_issues
from fast_comparison_qualifier import qualify_patch,qualifier_hashes

OPS={'Is':('is','=='),'Eq':('==','is'),'IsNot':('is not','!='),'NotEq':('!=','is not')}

def certificate(mapping,key):
    site=mapping['sites'][key];path=site['path'];old,new=OPS[site['operator']]
    before=f'left {old} right\n';after=f'left {new} right\n'
    normalized=site_key(path,ast.parse(before).body[0].value)
    copy=dict(site,source_sha256=hashlib.sha256(before.encode()).hexdigest())
    view=dict(mapping,sites={normalized:copy})
    result=observed_certificate(view,path,before,after)
    if 'site' in result:result['site']=key
    result['path']=path
    return result


def actual_patch(source,site):
    if site['operator'] not in ('Is','Eq'):return None
    path=site['path'];before=(source/path).read_text();lines=before.splitlines(keepends=True)
    def char(line,col):return len(lines[line-1].encode()[:col].decode())
    start=(site['line'],char(site['line'],site['column']));end=(site['end_line'],char(site['end_line'],site['end_column']))
    tokens=list(tokenize.generate_tokens(io.StringIO(before).readline));old,new=OPS[site['operator']]
    # Operand subexpressions may contain other comparisons: only accept a
    # candidate when the existing whole-patch qualifier identifies this site.
    for t in tokens:
        if t.string==old and start<=t.start<t.end<=end:
            offset=sum(len(x) for x in lines[:t.start[0]-1])+t.start[1]
            after=before[:offset]+new+before[offset+len(old):]
            yield f'diff --git a/{path} b/{path}\n' + ''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/'+path,tofile='b/'+path))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--full',required=True);p.add_argument('--compact',required=True);p.add_argument('--encoding',choices=('dag','plain'),default='dag');p.add_argument('--catalog',required=True);p.add_argument('--production-map',required=True);p.add_argument('--source',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    start=time.process_time();full=json.loads(Path(a.full).read_bytes());full_load=time.process_time()-start
    start=time.process_time();compact=load(a.compact) if a.encoding=='dag' else load_plain(a.compact,hashlib.sha256(Path(a.full).read_bytes()).hexdigest());compact_load=time.process_time()-start
    if compact!=full:raise ValueError('not lossless')
    tests=json.loads(Path(a.catalog).read_text())['tests'];production=json.loads(gzip.decompress(Path(a.production_map).read_bytes()));original={t['number'] for t in tests}
    issues=capture_issues(full,tests)
    if issues!=capture_issues(compact,tests):raise ValueError('capture gate differs')
    rows=[];start=time.process_time()
    for key in sorted(full['sites']):
        left,right=certificate(full,key),certificate(compact,key)
        if left!=right:raise ValueError('certificate differs: '+key)
        results=[]
        for mapping,cert in ((full,left),(compact,right)):
            with patch('analyze_comparison_gain.qualify_patch',return_value=cert):
                selected,detail=conditional_selection(mapping,Path(a.source),'normalized certificate',original,tests,production)
            results.append((sorted(selected),detail))
        if results[0]!=results[1]:raise ValueError('retention policy differs: '+key)
        rows.append({'site':key,'certificate_sha256':hashlib.sha256(canonical(left)).hexdigest(),'decision':results[0][1]['decision'],'retention_sha256':hashlib.sha256(canonical(results[0])).hexdigest()})
    policy_cpu=time.process_time()-start
    # One actual equal-width supported edit per source file, no answer inputs.
    benchmarks=[];seen=set();start=time.process_time()
    for key,site in sorted(full['sites'].items()):
        if site['path'] in seen or site['operator'] not in ('Is','Eq'):continue
        for text in actual_patch(Path(a.source),site):
            t=time.process_time();left=qualify_patch(full,a.source,text);elapsed=time.process_time()-t
            t=time.process_time();right=qualify_patch(compact,a.source,text);elapsed_compact=time.process_time()-t
            if left!=right:raise ValueError('actual qualification differs')
            if left.get('site') not in (None,key):continue
            benchmarks.append({'path':site['path'],'site':key,'full_cpu':elapsed,'compact_cpu':elapsed_compact,'equivalent':left.get('equivalent_for_observed_types') is True});seen.add(site['path']);break
    report={'kind':'all_site_lossless_representation_validation','runtime_narrowing_authorized':False,'site_count':len(rows),'all_site_certificate_and_retention_equal':True,'all_site_normalization':'source/span/hash preserved by exact whole-map equality; policy exercised through synthetic one-comparison sources retaining real observation/read/context metadata','capture_issues':issues,'input_sha256':hashlib.sha256(Path(a.full).read_bytes()).hexdigest(),'artifact_sha256':hashlib.sha256(Path(a.compact).read_bytes()).hexdigest(),'qualifier_hashes':qualifier_hashes(),'full_load_cpu':full_load,'compact_load_verify_cpu':compact_load,'all_site_policy_cpu':policy_cpu,'decisions':rows,'actual_source_qualification':benchmarks,'actual_qualification_total_cpu':time.process_time()-start,'baseline_unstable_policy_gap':'Existing analyzer does not inspect baseline_unstable_tests; an integration must add those protections independently.'}
    Path(a.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in report.items() if k not in ('decisions','actual_source_qualification')}))

if __name__=='__main__':main()
