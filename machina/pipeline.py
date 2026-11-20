"""Run explicit mappings against a private corpus; retain a complete coverage ledger.

Outputs are development candidates, not automatically approved submissions.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from . import native
from . import neutral
from . import pdf_handler
from . import solidworks
from . import archives
from . import text_handler
from .identity import Registry


def transform(data: bytes, suffix: str, registry: Registry):
    if archives.kind('file'+suffix,data):
        def member(name, content):
            renamed=registry.path(name)
            if archives.kind(name,content):
                return renamed,content,{'status':'container_pending_recursion'}
            result,report=transform(content,Path(name).suffix.lower(),registry)
            return renamed,result,report
        return archives.transform(data,'file'+suffix,member,limits=archives.Limits(max_expanded_bytes=256*1024*1024,max_output_bytes=256*1024*1024))
    if suffix == '.xml':
        return text_handler.transform_xml(data,registry.replace)
    if suffix in {'.txt', '.csv', '.md'}:
        return registry.replace(data.decode('utf-8')).encode('utf-8'),{'status':'processed','limitations':['known_text_identities_only']}
    if solidworks.MARKER in data[:64]:
        return solidworks.rewrite_bytes(data,registry.mapping)
    if suffix in neutral.SUPPORTED:
        return neutral.transform(data,suffix,registry.replace)
    if suffix == '.pdf':
        return pdf_handler.transform(data,registry.mapping)
    if data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        with tempfile.TemporaryDirectory(prefix='machina-') as temp:
            source=Path(temp)/'source'; dest=Path(temp)/'result'
            source.write_bytes(data)
            report=native.rewrite_native(source,dest,registry.mapping)
            return (dest.read_bytes() if dest.exists() else data),report
    return data, {'status':'unsupported','complete_deidentification':False}


def run(source: Path, output: Path, registry: Registry, report_path: Path):
    source=source.resolve();output=output.resolve();report_path=report_path.resolve()
    if source==output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be separate non-nested directories')
    if output.exists(): raise ValueError('Output must be new; do not mutate an existing attempt')
    if report_path==output or output in report_path.parents:
        raise ValueError('Keep private run report outside output')
    files=[]; destinations={}
    for p in sorted(source.rglob('*')):
        if p.is_symlink():raise ValueError('Symlinks require explicit review')
        if not p.is_file():continue
        rel=registry.path(p.relative_to(source).as_posix())
        # Casefold collision check is conservative and portable.
        if rel.casefold() in destinations:raise ValueError('Pseudonym path collision')
        destinations[rel.casefold()]=p;files.append((p,rel))
    output.mkdir(parents=True)
    report_path.parent.mkdir(parents=True,exist_ok=True)
    totals={'files':0,'bytes':0,'errors':0,'ready_to_submit':False}
    with report_path.open('x') as ledger:
        report_path.chmod(0o600)
        for p,rel in files:
            target=output/rel;target.parent.mkdir(parents=True,exist_ok=True)
            row={'input':p.relative_to(source).as_posix(),'output':rel,'input_bytes':p.stat().st_size}
            try:
                if p.stat().st_size>256_000_000:
                    # Preserve source fidelity, but explicitly mark blocked coverage.
                    shutil.copyfile(p,target)
                    detail={'status':'deferred_large_file','complete_deidentification':False}
                else:
                    result,detail=transform(p.read_bytes(),p.suffix.lower(),registry)
                    target.write_bytes(result)
                row['coverage']=detail
                row['output_bytes']=target.stat().st_size
                with target.open('rb') as f:row['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
                totals['bytes']+=row['output_bytes']
            except Exception as e:
                row['error']=type(e).__name__+': '+str(e);totals['errors']+=1
                # Keep source defects private; never silently claim omission is sanitization.
            ledger.write(json.dumps(row)+'\n');ledger.flush();totals['files']+=1
    return totals


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--key-file',type=Path,required=True)
    p.add_argument('--private-report',type=Path,required=True)
    a=p.parse_args()
    registry=Registry(json.loads(a.registry.read_text()),a.key_file.read_bytes())
    print(json.dumps(run(a.input,a.output,registry,a.private_report),indent=2))

if __name__=='__main__':main()
