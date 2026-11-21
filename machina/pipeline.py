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
from . import freecad
from . import text_handler
from . import source_formats
from . import raster
from .identity import Registry


def transform(data: bytes, suffix: str, registry: Registry, *, ocr=False):
    if archives.kind('file'+suffix,data):
        def member(name, content):
            renamed=registry.path(name)
            if name.endswith('/'):
                if content:
                    raise ValueError('Directory contains unexpected bytes')
                return renamed+'/',b'',{'status':'processed','directory':True}
            if archives.kind(name,content):
                return renamed,content,{'status':'container_pending_recursion'}
            result,report=transform(content,Path(name).suffix.lower(),registry,ocr=ocr)
            return renamed,result,report

        def factory(container_name, container_bytes):
            if not container_name.lower().endswith(('.fcstd', '.fcstd1')):
                return member
            types=freecad.member_types(container_bytes)
            protected={'Document.xml','GuiDocument.xml'}
            def rename_internal(name):
                return name if name in protected else registry.path(name)
            def native_member(name, content):
                if name.endswith('/') or archives.kind(name,content):
                    return member(name,content)
                result,report=freecad.transform_member(name,content,registry.replace,
                    info=types.get(name),rename=rename_internal)
                if report.get('status')=='unsupported':
                    renamed,result,report=member(name,content)
                    return rename_internal(name),result,report
                return rename_internal(name),result,report
            return native_member

        return archives.transform(data,'file'+suffix,member,
            limits=archives.Limits(max_expanded_bytes=256*1024*1024,max_output_bytes=256*1024*1024),
            callback_factory=factory)
    if suffix in source_formats.SUPPORTED:
        return source_formats.transform(data,suffix,registry.replace)
    if suffix == '.xml':
        return text_handler.transform_xml(data,registry.replace)
    if suffix in {'.txt', '.csv', '.md'}:
        return registry.replace(data.decode('utf-8')).encode('utf-8'),{'status':'processed','limitations':['known_text_identities_only']}
    if solidworks.MARKER in data[:64]:
        return solidworks.rewrite_bytes(data,registry.mapping)
    if suffix in neutral.SUPPORTED:
        return neutral.transform(data,suffix,registry.replace)
    if suffix == '.pdf':
        output,report=pdf_handler.transform(data,registry.mapping)
        if ocr:
            output,report['raster']=raster.transform_pdf(output,registry.mapping)
        return output,report
    if suffix in {'.png','.jpg','.jpeg','.tif','.tiff'} and ocr:
        return raster.transform_image(data,registry.mapping)
    if data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        with tempfile.TemporaryDirectory(prefix='machina-') as temp:
            source=Path(temp)/'source'; dest=Path(temp)/'result'
            source.write_bytes(data)
            report=native.rewrite_native(source,dest,registry.mapping)
            return (dest.read_bytes() if dest.exists() else data),report
    return data, {'status':'unsupported','complete_deidentification':False}


def run(source: Path, output: Path, registry: Registry, report_path: Path, *, ocr=False,manifest=None,workers=1):
    from .batch import run_pipeline
    return run_pipeline(source,output,registry,report_path,ocr=ocr,manifest=manifest,workers=workers)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--key-file',type=Path,required=True)
    p.add_argument('--private-report',type=Path,required=True)
    p.add_argument('--ocr',action='store_true',help='Enable bounded known-identity raster edits; all edited regions require review')
    p.add_argument('--manifest',type=Path,help='Select exactly these input paths; required for get-input.sh output')
    p.add_argument('--workers',type=int,default=1)
    a=p.parse_args()
    registry=Registry(json.loads(a.registry.read_text()),a.key_file.read_bytes())
    print(json.dumps(run(a.input,a.output,registry,a.private_report,ocr=a.ocr,manifest=a.manifest,workers=a.workers),indent=2))

if __name__=='__main__':main()
