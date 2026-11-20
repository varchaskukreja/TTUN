"""Private corpus inventory and deterministic, bounded sample acquisition."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://machina-input.joinbuilders.org/'

def rows(manifest):
    for line in Path(manifest).read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        path, size, md5, url = line.split('\t')
        p = PurePosixPath(path)
        if p.is_absolute() or '..' in p.parts or '\\' in path:
            raise ValueError('Unsafe manifest path')
        if not url.startswith('corpus/') or '..' in PurePosixPath(url).parts:
            raise ValueError('Unsafe manifest URL')
        yield {'path':path, 'size':int(size), 'md5':md5, 'url':url}

def summarize(items):
    counts, sizes = Counter(), Counter()
    for r in items:
        ext = PurePosixPath(r['path']).suffix.lower()
        counts[ext] += 1
        sizes[ext] += r['size']
    return {'files':sum(counts.values()), 'bytes':sum(sizes.values()), 'formats':[
        {'extension':ext,'files':n,'bytes':sizes[ext]} for ext,n in counts.most_common()]}

def sample(items, per_format, max_file_bytes, budget):
    groups = defaultdict(list)
    for r in items:
        if 0 < r['size'] <= max_file_bytes:
            groups[PurePosixPath(r['path']).suffix.lower()].append(r)
    selected = []
    for group in groups.values():
        # Hash order avoids selecting only the alphabetically first source.
        group.sort(key=lambda r: hashlib.sha256(r['path'].encode()).digest())
    used = 0
    for rank in range(per_format):
        for ext in sorted(groups):
            group = groups[ext]
            if rank < len(group) and used + group[rank]['size'] <= budget:
                selected.append(group[rank]); used += group[rank]['size']
    return selected

def fetch(row, root):
    target = root / row['path']
    target.parent.mkdir(parents=True, exist_ok=True)
    def valid(p):
        if not p.is_file() or p.stat().st_size != row['size']: return False
        return not row['md5'] or hashlib.file_digest(p.open('rb'), 'md5').hexdigest() == row['md5']
    if valid(target): return
    partial = target.with_name(target.name + '.partial')
    subprocess.run(['curl','--fail','--silent','--show-error','--location','--retry','3', BASE+row['url'],'-o',str(partial)], check=True)
    if not valid(partial): raise ValueError('Downloaded file failed integrity check')
    partial.replace(target)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest'); p.add_argument('--sample-dir', type=Path)
    p.add_argument('--per-format',type=int,default=3)
    p.add_argument('--max-file-mb',type=int,default=2)
    p.add_argument('--budget-mb',type=int,default=100)
    a = p.parse_args(); items=list(rows(a.manifest))
    print(json.dumps(summarize(items), indent=2))
    if a.sample_dir:
        selected=sample(items,a.per_format,a.max_file_mb*1000000,a.budget_mb*1000000)
        a.sample_dir.mkdir(parents=True,exist_ok=True)
        # Never collapse case-sensitive names on a default macOS filesystem.
        probe=a.sample_dir/'.case-test-A'; lower=a.sample_dir/'.case-test-a'
        probe.touch()
        insensitive=lower.exists(); probe.unlink()
        if insensitive: raise SystemExit('Use a case-sensitive volume for corpus paths')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda row:fetch(row,a.sample_dir),selected))
        print(json.dumps({'sample_files':len(selected),'sample_bytes':sum(r['size'] for r in selected)}))

if __name__ == '__main__': main()
