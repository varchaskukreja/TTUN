#!/usr/bin/env python3
"""Union clean-baseline traces and repair only explicitly reverified outcomes.

The original outcome and both platforms remain recorded in artifact provenance.
This never consumes a patch or a patch's expected failures.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
from merge_maps import merge, read


def main():
    p = argparse.ArgumentParser()
    p.add_argument('primary')
    p.add_argument('supplemental')
    p.add_argument('output', type=Path)
    p.add_argument('--test', required=True, action='append')
    args = p.parse_args()
    primary, supplemental = read(args.primary), read(args.supplemental)
    requested = sorted(set(args.test))
    for test in requested:
        if supplemental.get('baseline_outcomes', {}).get(test) != 'passed':
            raise SystemExit(f'Refusing repair: aggregate test/subtest outcome is not passed: {test}')
        if supplemental.get('call_outcomes', {}).get(test) != 'passed':
            raise SystemExit(f'Refusing repair: main test call did not complete successfully: {test}')
        if test in supplemental.get('missing_tests', []):
            raise SystemExit(f'Refusing repair: test is missing: {test}')
    previous = {test: primary['baseline_outcomes'].get(test) for test in requested}
    result = merge(primary, supplemental)
    for test in requested:
        result['baseline_outcomes'][test] = 'passed'
    result.setdefault('baseline_outcome_repairs', []).append({
        'test_ids': requested,
        'previous_outcomes': previous,
        'verified_outcomes': {test: supplemental['baseline_outcomes'][test] for test in requested},
        'original_environment': primary.get('environment'),
        'verified_environment': supplemental.get('environment'),
        'supplemental_sha256': hashlib.sha256(Path(args.supplemental).read_bytes()).hexdigest(),
        'method': 'Union of clean pinned-baseline coverage; explicit passing test and subtest reports on a second platform.',
    })
    encoded = gzip.compress(json.dumps(result, separators=(',', ':'), sort_keys=True).encode(), mtime=0)
    temporary = args.output.with_name(args.output.name + '.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(args.output)
    print(f'Repaired {len(requested)} explicitly verified baseline outcomes; wrote {args.output} ({len(encoded)} bytes)')

if __name__ == '__main__':
    main()
