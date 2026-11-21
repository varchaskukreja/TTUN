#!/usr/bin/env python3
"""Bound remaining scheduling gains for fixed selections without reading answers."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scheduler import milliseconds


def audit(output, catalog):
    durations = {test['number']: milliseconds(test['seconds'])
                 for test in json.loads(catalog.read_text())['tests']}
    rows = []
    for line in output.read_text().splitlines():
        entry = json.loads(line)
        workers = [worker for worker in entry['workers'] if worker]
        numbers = [number for worker in workers for number in worker]
        if len(numbers) != len(set(numbers)) or len(workers) > 8:
            raise ValueError('invalid partition')
        jobs = [durations[number] for number in numbers]
        actual = max((sum(durations[n] for n in worker) for worker in workers), default=0) + 2000 * len(workers)
        bound = min((max(max(jobs), (sum(jobs) + count - 1) // count) + 2000 * count
                     for count in range(1, min(8, len(jobs)) + 1)), default=0)
        rows.append({'commit': entry['commit'], 'actual_ms': actual,
                     'lower_bound_ms': bound, 'maximum_possible_gain_ms': actual - bound})
    return {'kind': 'scheduling_lower_bound_audit',
            'input_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'catalog_sha256': hashlib.sha256(catalog.read_bytes()).hexdigest(),
            'actual_ms': sum(row['actual_ms'] for row in rows),
            'lower_bound_ms': sum(row['lower_bound_ms'] for row in rows),
            'rows': rows,
            'note': 'Lower bound need not be achievable. No sample answers consulted; selection held fixed.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('catalog', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.output, args.catalog), indent=2))
