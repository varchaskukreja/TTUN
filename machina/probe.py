"""Probe bounded, stratified remote samples without retaining the raw corpus.

Only a private candidate/coverage ledger is persisted. This is discovery, not a
substitute for processing and submitting the complete corpus.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from .inventory import rows,sample,BASE
from . import neutral,native,solidworks,pdf_handler
from .discovery import step_metadata,contacts,xml_metadata,scan_candidates

SUPPORTED=neutral.SUPPORTED | {'.pdf','.sldprt','.sldasm','.slddrw','.ipt','.iam','.idw'}


def inspect_bytes(data,suffix,path):
    record={'path':path,'bytes':len(data),'candidates':[]}
    typed=[]
    if suffix in {'.sldprt','.sldasm','.slddrw','.ipt','.iam','.idw'} and len(data)<8:
        record.update(status='source_defect',reason='native_file_too_short_for_supported_container_header')
    elif solidworks.MARKER in data[:64]:
        streams,issues=solidworks.read_streams(data)
        record.update(status='solidworks_metadata',issues=issues,candidates=[s.payload.decode('utf-8') for s in streams if s.name.startswith('docProps/') and s.name.endswith('.xml')])
    elif suffix in neutral.SUPPORTED:
        record.update(status='neutral_metadata',candidates=neutral.extract(data,suffix))
        if suffix in {'.step','.stp'}:typed.extend(step_metadata(data,path))
    elif suffix=='.pdf':
        record.update(status='pdf_text',candidates=pdf_handler.extract(data))
    elif data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        with tempfile.TemporaryDirectory(prefix='machina-probe-') as folder:
            p=Path(folder)/'native';p.write_bytes(data)
            report=native.inspect_native(p)
        record.update(status=report['format'],native=report,candidates=[p['text'] for p in report['properties']])
    else:record['status']='unsupported'
    typed.extend(scan_candidates(record))
    record['identity_candidates']=[c for c in typed if c]
    return record


def fetch_probe(row,max_file_bytes):
    path=row['path']
    try:
        result=subprocess.run(['curl','--fail','--silent','--show-error','--location','--max-time','45','--max-filesize',str(max_file_bytes),BASE+row['url']],capture_output=True,check=True,timeout=50)
        data=result.stdout
        if len(data)!=row['size']:raise ValueError('size_mismatch')
        if row['md5'] and hashlib.md5(data).hexdigest()!=row['md5']:raise ValueError('md5_mismatch')
        return inspect_bytes(data,Path(path).suffix.lower(),path)
    except Exception as error:
        # Avoid stderr URLs and raw binary in normal console logs.
        return {'path':path,'status':'error','error':type(error).__name__+': '+str(error)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest',type=Path);p.add_argument('--private-report',type=Path,required=True)
    p.add_argument('--per-format',type=int,default=5);p.add_argument('--max-file-mb',type=int,default=1)
    p.add_argument('--budget-mb',type=int,default=30);p.add_argument('--jobs',type=int,default=2)
    a=p.parse_args()
    if not 1<=a.jobs<=8 or not 1<=a.max_file_mb<=8 or not 1<=a.budget_mb<=500 or not 1<=a.per_format<=100:
        p.error('Probe resource limits out of range')
    selected=sample([r for r in rows(a.manifest) if Path(r['path']).suffix.lower() in SUPPORTED],a.per_format,a.max_file_mb*1_000_000,a.budget_mb*1_000_000)
    a.private_report.parent.mkdir(parents=True,exist_ok=True)
    counts=Counter();identities=0
    with a.private_report.open('x') as output:
        a.private_report.chmod(0o600)
        with ThreadPoolExecutor(max_workers=a.jobs) as pool:
            for record in pool.map(lambda row:fetch_probe(row,a.max_file_mb*1_000_000),selected):
                output.write(json.dumps(record)+'\n');output.flush()
                counts[record['status']]+=1;identities+=len(record.get('identity_candidates',[]))
    print(json.dumps({'files':len(selected),'input_bytes':sum(r['size'] for r in selected),'statuses':counts,'identity_evidence':identities},indent=2))

if __name__=='__main__':main()
