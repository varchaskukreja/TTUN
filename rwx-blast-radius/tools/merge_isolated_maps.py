#!/usr/bin/env python3
"""Reproducibly union baseline maps, with cold-test evidence and provenance.

Example (paths are local generation artifacts, never patch answers)::

  python tools/merge_isolated_maps.py --primary warm.json.gz \
    --supplement cold.json.gz --evidence-work cold-work \
    --provenance-coverage cold-work/baseline.coverage \
    --output candidate.json.gz --report validation.json

Repeat supplement/evidence-work arguments for interrupted-run recovery and
explicit platform repairs. Evidence work directories are ordered: a later saved
test result supersedes an earlier one, and all such differences are reported.
Primary baseline outcomes are never overwritten. Failed/incomplete final traces
are added to missing_tests so the selector conservatively retains those tests.
Generation needs coverage.py; selection does not. No sample labels are read.
"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

try:
    from .merge_maps import merge
except ImportError:
    from merge_maps import merge


def read(path):
    return json.loads(gzip.decompress(Path(path).read_bytes()))


def dependency_payload(mapping):
    """Every map field except the isolated-run explanatory metadata."""
    return {key: value for key, value in mapping.items() if key != 'isolated_baseline_supplement'}


def context_indices(context, ids):
    """Same scope expansion as build_map; collection ('') contributes no tests."""
    if context in ids:
        return {ids[context]}
    if context.startswith('@scope|'):
        _, scope, node = context.split('|', 2)
        if scope not in ('class', 'module', 'package', 'session'):
            raise ValueError(f'unknown fixture scope: {scope}')
        prefix = node.rstrip('/') + ('/' if scope == 'package' else '::')
        return {number for name, number in ids.items() if scope == 'session' or name.startswith(prefix)}
    if context:
        raise ValueError(f'unknown test context: {context}')
    return set()


def successful(row):
    node = row['nodeid']
    return (row.get('results', {}).get(node) == 'passed'
            and row.get('call_outcomes', {}).get(node) == 'passed'
            and not row.get('errors') and not row.get('timed_out')
            and row.get('wait_exitcode', 0) == 0)


def audit_work(path, ids):
    from coverage import CoverageData

    path = Path(path)
    rows = {}
    for filename in sorted(path.glob('isolated-result-*.json')):
        row = json.loads(filename.read_text())
        if row['nodeid'] not in ids or row['nodeid'] in rows:
            raise ValueError('unknown or duplicate saved test result')
        rows[row['nodeid']] = row
    manifest = path / 'isolated-manifest.json'
    if manifest.exists():
        for row in json.loads(manifest.read_text())['completed']:
            if row['nodeid'] not in rows:
                raise ValueError('manifest result has no saved child report')
            rows[row['nodeid']].update(row)
    observed = set()
    databases = shards = 0
    for filename in sorted(path.glob('baseline.coverage*')):
        if filename.name.endswith(('-journal', '-wal', '-shm')):
            continue
        with sqlite3.connect(filename.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            if list(connection.execute('PRAGMA integrity_check')) != [('ok',)]:
                raise ValueError(f'corrupt coverage database: {filename.name}')
        data = CoverageData(basename=str(filename))
        data.read()
        contexts = set(data.measured_contexts())
        for context in contexts:
            context_indices(context, ids)
        actual = contexts & ids.keys()
        if filename.name != 'baseline.coverage':
            if len(actual) > 1:
                raise ValueError(f'cross-test context leakage in {filename.name}')
            shards += 1
            observed.update(actual)
        databases += 1
    return rows, observed, {'work': path.as_posix(), 'databases': databases,
                            'single_test_shards': shards, 'saved_results': len(rows)}


def added_links(primary, union):
    result = {}
    for path, info in union['files'].items():
        old = primary['files'].get(path, {}).get('lines', {})
        for line, numbers in info.get('lines', {}).items():
            extra = set(numbers) - set(old.get(line, []))
            if extra:
                result[path, int(line)] = extra
    return result


def provenance(added, coverage_paths, ids):
    from coverage import CoverageData

    direct, scoped = {}, {}
    contexts_cache = {}
    relative_paths = sorted({path for path, _ in added}, key=len, reverse=True)
    for filename in coverage_paths:
        data = CoverageData(basename=str(filename))
        data.read()
        for absolute in data.measured_files():
            relative = next((path for path in relative_paths if absolute.endswith('/' + path)), None)
            if relative is None:
                continue
            for line, contexts in data.contexts_by_lineno(absolute).items():
                key = relative, line
                if key not in added:
                    continue
                for context in contexts:
                    if context not in contexts_cache:
                        contexts_cache[context] = context_indices(context, ids)
                    hits = contexts_cache[context] & added[key]
                    if hits:
                        target = scoped if context.startswith('@scope|') else direct
                        target.setdefault(key, set()).update(hits)
    totals = {'direct_only': 0, 'scope_only': 0, 'both': 0, 'unexplained': 0}
    by_file = {}
    examples = []
    names = {value: key for key, value in ids.items()}
    for key, extra in added.items():
        d, s = direct.get(key, set()), scoped.get(key, set())
        counts = {'direct_only': len(d - s), 'scope_only': len(s - d),
                  'both': len(d & s), 'unexplained': len(extra - d - s)}
        row = by_file.setdefault(key[0], {**dict.fromkeys(counts, 0), 'new_links': 0})
        row['new_links'] += len(extra)
        for kind, count in counts.items():
            totals[kind] += count
            row[kind] += count
        if len(examples) < 12 and key[0].startswith('sqlglot/'):
            number = min(extra)
            examples.append({'path': key[0], 'line': key[1], 'test': names[number],
                             'direct': number in d, 'fixture_scope': number in s})
    if totals['unexplained']:
        raise ValueError(f'{totals["unexplained"]} added links lack raw context evidence')
    return {'counts': totals, 'by_file': by_file, 'examples': examples,
            'collection_context_expands_to_tests': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--primary', type=Path, required=True)
    parser.add_argument('--supplement', type=Path, action='append', required=True)
    parser.add_argument('--evidence-work', type=Path, action='append', required=True)
    parser.add_argument('--provenance-coverage', type=Path, action='append', required=True,
                        help='combined raw CoverageData file supporting supplementary links')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--verify-map', type=Path, help='require identical dependency payload to a retained production map')
    args = parser.parse_args()
    primary = read(args.primary)
    union = json.loads(json.dumps(primary))
    for supplement in args.supplement:
        union = merge(union, read(supplement))
    ids = {name: number for number, name in enumerate(primary['test_ids'])}
    if len(ids) != len(primary['test_ids']):
        raise ValueError('duplicate catalog test IDs')
    final, observed, audits, history = {}, set(), [], {}
    for work in args.evidence_work:
        rows, contexts, audit = audit_work(work, ids)
        observed.update(contexts)
        audits.append(audit)
        for node, row in rows.items():
            history.setdefault(node, []).append({'work': work.as_posix(), 'successful': successful(row),
                                                 'outcome': row.get('results', {}).get(node),
                                                 'errors': row.get('errors', [])})
        final.update(rows)
    missing = set(ids) - final.keys()
    incomplete = missing | (set(ids) - observed) | {node for node, row in final.items() if not successful(row)}
    differences = {node: rows for node, rows in history.items() if any(not row['successful'] for row in rows)}
    added = added_links(primary, union)
    evidence = provenance(added, args.provenance_coverage, ids)
    new_links = sum(map(len, added.values()))
    metadata = {'mode': 'isolated-baseline-union', 'catalog_tests': len(ids),
                'observed_test_contexts': len(observed), 'final_saved_results': len(final),
                'incomplete_tests': sorted(incomplete), 'attempt_differences': differences,
                'new_line_test_links': new_links,
                'new_source_line_test_links': sum(len(hits) for (path, _), hits in added.items() if path.startswith('sqlglot/')),
                'primary_outcomes_preserved': union['baseline_outcomes'] == primary['baseline_outcomes']}
    union['isolated_baseline_supplement'] = metadata
    union['missing_tests'] = sorted(set(union.get('missing_tests', [])) | incomplete)
    encoded = gzip.compress(json.dumps(union, separators=(',', ':'), sort_keys=True).encode(), mtime=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + '.tmp')
    temporary.write_bytes(encoded)
    temporary.replace(args.output)
    report = {**metadata, 'audits': audits, 'provenance': evidence,
              'primary_sha256': hashlib.sha256(args.primary.read_bytes()).hexdigest(),
              'candidate_sha256': hashlib.sha256(encoded).hexdigest(), 'candidate_bytes': len(encoded),
              'supplements': [{'path': path.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                              for path in args.supplement]}
    payload = dependency_payload(union)
    report['dependency_payload_sha256'] = hashlib.sha256(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()).hexdigest()
    if args.verify_map:
        if payload != dependency_payload(read(args.verify_map)):
            raise ValueError('reproduced dependency payload differs from retained map')
        report['retained_map_equivalent'] = {
            'path': args.verify_map.as_posix(),
            'sha256': hashlib.sha256(args.verify_map.read_bytes()).hexdigest(),
            'only_excluded_field': 'isolated_baseline_supplement',
        }
    report['reproduction_argv'] = ['python3', *sys.argv]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps({**metadata, 'provenance_counts': evidence['counts'],
                      'candidate_sha256': report['candidate_sha256']}, indent=2))


if __name__ == '__main__':
    main()
