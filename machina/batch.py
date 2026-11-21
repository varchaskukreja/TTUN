"""Manifest-scoped, bounded multiprocessing for private corpus candidates."""
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from .inventory import rows


def _case_sensitive(directory):
    while not directory.exists():directory=directory.parent
    with tempfile.NamedTemporaryFile(prefix='MachinaCaseProbe-',dir=directory) as probe:
        p=Path(probe.name)
        return not p.with_name(p.name.swapcase()).exists()


def plan_inputs(source,output,registry,manifest=None):
    source=Path(source).resolve();output=Path(output).resolve()
    if not source.is_dir():raise ValueError('Input directory does not exist')
    if source==output or source in output.parents or output in source.parents:
        raise ValueError('Input and output must be separate non-nested directories')
    if output.exists():raise ValueError('Output must be new; do not mutate an existing attempt')
    if manifest is None and (source/'.get-input').exists():
        raise ValueError('Downloader input requires --manifest to exclude private operational files')
    sensitive=_case_sensitive(output.parent)
    if manifest is not None:
        entries=((source/r['path'],r['size']) for r in rows(manifest))
    else:
        entries=((p,None) for p in sorted(source.rglob('*')) if p.is_file() or p.is_symlink())
    planned=[];destinations=set()
    for p,size in entries:
        if not p.resolve().is_relative_to(source):raise ValueError('Input path escapes corpus')
        if any(part.is_symlink() for part in [p,*p.parents] if part.is_relative_to(source)):
            raise ValueError('Symlinks require explicit review')
        if not p.is_file():raise ValueError('Manifest file missing')
        if size is not None and p.stat().st_size!=size:raise ValueError('Manifest size mismatch')
        relative=p.relative_to(source).as_posix()
        renamed=registry.path(relative)
        key=renamed if sensitive else renamed.casefold()
        if key in destinations:raise ValueError('Pseudonym path collision')
        destinations.add(key)
        planned.append((relative,renamed))
    for key in destinations:
        if any(str(parent) in destinations for parent in Path(key).parents):
            raise ValueError('Pseudonym file/directory collision')
    if not planned:raise ValueError('Input corpus is empty')
    return planned


def _process(item,source,output,registry,ocr):
    from .pipeline import transform
    original,renamed=item;p=source/original;target=output/renamed
    target.parent.mkdir(parents=True,exist_ok=True)
    record={'input':original,'output':renamed,'input_bytes':p.stat().st_size}
    partial=target.with_name(target.name+'.machina-partial')
    try:
        if p.stat().st_size>256_000_000:
            # Development output retains large originals but flags them unprocessed.
            # Submission validation must reject unresolved records.
            shutil.copyfile(p,partial)
            detail={'status':'deferred_large_file','complete_deidentification':False}
        else:
            result,detail=transform(p.read_bytes(),p.suffix.lower(),registry,ocr=ocr)
            partial.write_bytes(result)
        partial.replace(target)
        record['coverage']=detail
        record['output_bytes']=target.stat().st_size
        with target.open('rb') as f:record['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    except Exception as error:
        if partial.exists():partial.unlink()
        record['error']=type(error).__name__+': '+str(error)
    return record

_CONTEXT=None

def _init(source,output,registry,ocr):
    global _CONTEXT
    _CONTEXT=(source,output,registry,ocr)

def _worker(item):return _process(item,*_CONTEXT)


def run_pipeline(source,output,registry,report_path,*,ocr=False,manifest=None,workers=1):
    source=Path(source).resolve();output=Path(output).resolve();report_path=Path(report_path).resolve()
    if not 1<=workers<=32:raise ValueError('Workers must be between 1 and 32')
    if report_path==output or output in report_path.parents:
        raise ValueError('Keep private run report outside output')
    if report_path.exists():raise ValueError('Private report must be new')
    plan=plan_inputs(source,output,registry,manifest)
    output.mkdir(parents=True,mode=0o700)
    report_path.parent.mkdir(parents=True,exist_ok=True)
    totals={'files':0,'expected_files':len(plan),'bytes':0,'errors':0,'ready_to_submit':False}
    with report_path.open('x') as ledger:
        report_path.chmod(0o600)
        def record(row):
            ledger.write(json.dumps(row)+'\n');ledger.flush()
            totals['files']+=1;totals['bytes']+=row.get('output_bytes',0)
            totals['errors']+='error' in row
        if workers==1:
            for item in plan:record(_process(item,source,output,registry,ocr))
        else:
            items=iter(plan)
            with ProcessPoolExecutor(max_workers=workers,initializer=_init,initargs=(source,output,registry,ocr)) as pool:
                pending=set()
                def fill():
                    while len(pending)<workers*2:
                        item=next(items,None)
                        if item is None:break
                        pending.add(pool.submit(_worker,item))
                fill()
                while pending:
                    done,pending_left=wait(pending,return_when=FIRST_COMPLETED)
                    pending=set(pending_left)
                    for future in done:record(future.result())
                    fill()
    return totals
