#!/usr/bin/env python3
"""Union independently observed baseline traces; retain primary run outcomes."""
import argparse
import gzip
import json
from pathlib import Path


def read(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def merge(primary, supplemental):
    if primary['revision'] != supplemental['revision'] or primary['test_ids'] != supplemental['test_ids']:
        raise ValueError('Maps must have identical pinned revisions and test-ID ordering')
    for path, extra in supplemental['files'].items():
        if path not in primary['files']:
            primary['files'][path] = extra
            continue
        entry = primary['files'][path]
        for key in ('tests', 'read_tests', 'collection_lines', 'class_collection'):
            entry[key] = sorted(set(entry.get(key, [])) | set(extra.get(key, [])))
        for key in ('collection', 'collection_read'):
            entry[key] = entry.get(key, False) or extra.get(key, False)
        for key in ('lines', 'class_consumers'):
            target = entry.setdefault(key, {})
            for name, consumers in extra.get(key, {}).items():
                target[name] = sorted(set(target.get(name, [])) | set(consumers))
        for key in ('functions', 'classes'):
            target = entry.setdefault(key, [])
            by_span = {(record['name'], record['start'], record['end']): record for record in target}
            for record in extra.get(key, []):
                identity = record['name'], record['start'], record['end']
                if identity in by_span:
                    current = by_span[identity]
                    current['tests'] = sorted(set(current['tests']) | set(record['tests']))
                else:
                    target.append(record)
    primary['scoped_setup_supplement'] = True
    return primary


def main():
    p = argparse.ArgumentParser()
    p.add_argument('primary')
    p.add_argument('supplemental')
    p.add_argument('output', type=Path)
    args = p.parse_args()
    result = merge(read(args.primary), read(args.supplemental))
    encoded = gzip.compress(json.dumps(result, separators=(',', ':'), sort_keys=True).encode(), mtime=0)
    temp = args.output.with_name(args.output.name + '.tmp')
    temp.write_bytes(encoded)
    temp.replace(args.output)
    print(f'Merged {len(result["files"])} baseline files into {args.output} ({len(encoded)} bytes); primary outcomes retained')

if __name__ == '__main__':
    main()
