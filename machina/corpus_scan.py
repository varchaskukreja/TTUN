"""Resumable private identity discovery over manifest-listed, downloaded files."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from .inventory import rows
from .probe import inspect_bytes
from .discovery import contacts,provenance
from . import native_reference,solidworks


def _inspect(task):
    root,row,max_bytes=task
    path=Path(root)/row['path'];before=path.stat()
    result={'path':row['path'],'size':before.st_size,'mtime_ns':before.st_mtime_ns,'identity_candidates':[]}
    try:
        if before.st_size>max_bytes:
            result.update(status='deferred_large_file',reason='scan_byte_budget')
        else:
            data=path.read_bytes()
            if row['md5'] and hashlib.md5(data).hexdigest()!=row['md5']:
                result.update(status='integrity_pending')
                return result
            record=inspect_bytes(data,path.suffix.lower(),row['path'])
            result['status']=record['status']
            result['identity_candidates']=record['identity_candidates']
            result['issues']=record.get('issues',[])
            # Discover contact/account evidence in other native streams read-only.
            if result['status']=='solidworks_metadata':
                streams,_=solidworks.read_streams(data)
                for stream in streams:
                    for field in native_reference.text_candidates(stream.payload,limit=2000):
                        result['identity_candidates'].extend(c for c in contacts(field.text,row['path']) if c)
            if result['status']=='ole':
                report=native_reference.inspect_ole_streams(path,max_stream_bytes=max_bytes,limit_per_stream=2000)
                result['issues'].extend(report['issues'])
                for stream in report['streams']:
                    for field in stream['candidates']:
                        result['identity_candidates'].extend(c for c in contacts(field['text'],row['path']) if c)
        result['identity_candidates'].extend(c for c in provenance(row['path']) if c)
    except Exception as error:
        result.update(status='error',error=type(error).__name__+': '+str(error))
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        result.update(status='input_changed',identity_candidates=[])
    return result


def scan(root,manifest,database,*,workers=4,max_bytes=32_000_000,limit=None):
    root=Path(root).resolve();database=Path(database)
    if not 1<=workers<=16 or max_bytes<1:raise ValueError('Invalid scan resource limits')
    database.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(database);database.chmod(0o600)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,status TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS candidates(path TEXT,identity TEXT,kind TEXT,field TEXT,confidence TEXT,PRIMARY KEY(path,identity,kind,field));''')
    existing={r[0]:r[1:] for r in conn.execute('SELECT path,size,mtime_ns,status FROM files')}
    tasks=[];missing=0
    for row in rows(manifest):
        path=root/row['path']
        if not path.is_file() or path.stat().st_size!=row['size']:
            missing+=1;continue
        if not path.resolve().is_relative_to(root) or path.is_symlink():raise ValueError('Unsafe corpus path')
        stat=path.stat();previous=existing.get(row['path'])
        if previous and previous[:2]==(stat.st_size,stat.st_mtime_ns) and previous[2] not in {'error','input_changed','integrity_pending','deferred_large_file'}:continue
        tasks.append((str(root),row,max_bytes))
        if limit and len(tasks)>=limit:break
    completed=0;started=time.monotonic()
    def save(result):
        nonlocal completed
        path=result['path'];candidates=result.pop('identity_candidates',[])
        conn.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)',(path,result['size'],result['mtime_ns'],result['status'],json.dumps(result)))
        conn.execute('DELETE FROM candidates WHERE path=?',(path,))
        conn.executemany('INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?)',[(path,c['identity'],c['kind'],c['field'],c['confidence']) for c in candidates])
        completed+=1
        if completed%100==0:
            conn.commit()
            print(json.dumps({'scanned_this_run':completed,'scheduled':len(tasks),'elapsed_seconds':round(time.monotonic()-started)}),flush=True)
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            iterator=iter(tasks);pending=set()
            def fill():
                while len(pending)<workers*2:
                    task=next(iterator,None)
                    if task is None:break
                    pending.add(pool.submit(_inspect,task))
            fill()
            while pending:
                done,left=wait(pending,return_when=FIRST_COMPLETED);pending=set(left)
                for future in done:save(future.result())
                fill()
        conn.commit()
        return {'scanned_this_run':completed,'not_downloaded':missing,'total_recorded':conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],
                'statuses':dict(conn.execute('SELECT status,COUNT(*) FROM files GROUP BY status')),'candidate_evidence':conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]}
    finally:conn.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('input',type=Path);p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--private-db',type=Path,required=True);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--max-file-mb',type=int,default=32);p.add_argument('--limit',type=int)
    a=p.parse_args()
    print(json.dumps(scan(a.input,a.manifest,a.private_db,workers=a.workers,max_bytes=a.max_file_mb*1_000_000,limit=a.limit),indent=2))

if __name__=='__main__':main()
