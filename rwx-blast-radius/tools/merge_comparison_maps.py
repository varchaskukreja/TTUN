#!/usr/bin/env python3
"""Strict research-only comparison capture merger. Never produces selected tests."""
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from comparison_spawn import audit_receipts

SITE_FIELDS = {'path', 'line', 'column', 'end_line', 'end_column', 'operator', 'source_sha256'}
ENUM_FIELDS = {'backed_enum_kind', 'canonical_member', 'builtin_values_bijective'}
TYPE_FIELDS = {'module', 'qualname', 'eq', 'ne', 'none_type', 'standard_enum_type', 'supported_metaclass'}
CODE_FIELDS = {'path', 'name', 'qualname', 'first_line', 'span_first_line', 'span_last_line'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key {key}')
        result[key] = value
    return result


def read_json(path):
    def reject(value):
        raise ValueError(f'nonstandard JSON constant {value}')
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object, parse_constant=reject)


def safe_path(path):
    require(type(path) is str and bool(path), 'invalid path')
    p = PurePosixPath(path)
    require(not p.is_absolute() and '..' not in p.parts and p.suffix == '.py', f'invalid Python path {path}')


def code_metadata(record, key=False):
    require(type(record) is dict and set(record) == CODE_FIELDS | ({'key'} if key else set()), 'invalid code metadata fields')
    safe_path(record['path'])
    for field in ('name', 'qualname'):
        require(type(record[field]) is str, f'invalid code {field}')
    for field in ('first_line', 'span_first_line', 'span_last_line'):
        require(type(record[field]) is int and record[field] >= 0, f'invalid code {field}')
    if key:
        require(record['key'] == f"{record['path']}:{record['first_line']}:{record['qualname']}", 'inconsistent code key')


def merge_snapshots(snapshots, catalog, worker_contexts):
    """Return union and errors; every failed shard makes the entire capture incomplete."""
    catalog = set(catalog)
    errors = []
    merged = {'format': 1, 'kind': 'baseline_operand_types', 'sites': {}, 'preexisting_modules': {},
              'ineligible_files': [], 'limitations': [], 'read_dependencies': {
                  'source': {}, 'code': {}, 'code_objects': {}, 'code_object_granularity': True, 'coverage_complete': False}}
    hashes = {}

    def contexts(values):
        require(type(values) is list and all(type(v) is str for v in values), 'invalid context list')
        expanded = set()
        for value in values:
            if value.startswith('@worker-unassigned:'):
                pid = value.split(':', 1)[1]
                require(pid in worker_contexts and worker_contexts[pid], f'unknown worker context {value}')
                for context in worker_contexts[pid]:
                    require(context in catalog or context == '', f'unknown worker job context {context}')
                expanded.update(worker_contexts[pid])
            else:
                require(value in catalog or value == '', f'unknown context {value}')
                expanded.add(value)
        return sorted(expanded)

    def context_union(target, key, values):
        target[key] = sorted(set(target.get(key, [])) | set(contexts(values)))

    def merge_codes(target, source, execution=False):
        require(type(source) is dict, 'invalid code mapping')
        for key, record in source.items():
            require(type(key) is str and type(record) is dict, 'invalid code entry')
            values = contexts(record.get('contexts'))
            metadata = {k: v for k, v in record.items() if k != 'contexts'}
            if execution:
                require(set(metadata) == {'ancestry'} and type(metadata['ancestry']) is list and metadata['ancestry'], 'invalid execution ancestry')
                for ancestor in metadata['ancestry']:
                    code_metadata(ancestor, key=True)
                require(metadata['ancestry'][-1]['key'] == key, 'execution owner/ancestry mismatch')
            else:
                code_metadata(metadata)
                require(key == f"{metadata['path']}:{metadata['first_line']}:{metadata['qualname']}", 'reader code key mismatch')
            if key in target:
                require({k: v for k, v in target[key].items() if k != 'contexts'} == metadata, f'conflicting code metadata {key}')
                values = sorted(set(values) | set(target[key]['contexts']))
            target[key] = dict(copy.deepcopy(metadata), contexts=values)

    for label, mapping in snapshots:
        try:
            require(type(mapping) is dict and mapping.get('format') == 1 and mapping.get('kind') == 'baseline_operand_types', 'invalid mapping format')
            require(type(mapping.get('sites')) is dict, 'missing sites')
            for site, record in mapping['sites'].items():
                require(type(record) is dict and set(record) == SITE_FIELDS | {'observations', 'execution_codes'}, 'invalid site schema')
                metadata = {k: record[k] for k in SITE_FIELDS}
                safe_path(record['path'])
                for field in ('line', 'column', 'end_line', 'end_column'):
                    require(type(record[field]) is int and record[field] >= 0, f'invalid {field}')
                require(site == f"{record['path']}:{record['line']}:{record['column']}:{record['end_line']}:{record['end_column']}", 'site key mismatch')
                require(record['operator'] in ('Is', 'IsNot', 'Eq', 'NotEq'), 'unsupported operator')
                require(type(record['source_sha256']) is str and re.fullmatch('[0-9a-f]{64}', record['source_sha256']), 'invalid source hash')
                previous = hashes.setdefault(record['path'], record['source_sha256'])
                require(previous == record['source_sha256'], f'conflicting file hash {record["path"]}')
                if site in merged['sites']:
                    require({k: merged['sites'][site][k] for k in SITE_FIELDS} == metadata, f'conflicting site metadata {site}')
                target = merged['sites'].setdefault(site, dict(metadata, observations=[], execution_codes={}))
                require(type(record['observations']) is list, 'invalid observations')
                pairs = {json.dumps({k: v for k, v in p.items() if k != 'contexts'}, sort_keys=True): p for p in target['observations']}
                for pair in record['observations']:
                    require(type(pair) is dict and set(pair) == {'left', 'right', 'same_type', 'contexts'}, 'invalid pair schema')
                    require(type(pair['same_type']) is bool, 'invalid same_type')
                    for side in ('left', 'right'):
                        value = pair[side]
                        require(type(value) is dict and set(value) in (TYPE_FIELDS, TYPE_FIELDS | ENUM_FIELDS), 'invalid type descriptor')
                        for field in ('module', 'qualname'):
                            require(type(value[field]) is str, 'invalid type name')
                        for field in ('eq', 'ne'):
                            require(value[field] in ('object_identity', 'none_identity', 'overridden'), 'invalid slot classification')
                        if 'backed_enum_kind' in value:
                            require(value['backed_enum_kind'] in ('', 'str', 'int'), 'invalid backed Enum kind')
                            for field in ('canonical_member', 'builtin_values_bijective'):
                                require(type(value[field]) is bool, 'invalid backed Enum flag')
                        for field in ('none_type', 'standard_enum_type', 'supported_metaclass'):
                            require(type(value[field]) is bool, 'invalid type flag')
                    descriptor = {k: v for k, v in pair.items() if k != 'contexts'}
                    key = json.dumps(descriptor, sort_keys=True)
                    existing = pairs.setdefault(key, dict(copy.deepcopy(descriptor), contexts=[]))
                    existing['contexts'] = sorted(set(existing['contexts']) | set(contexts(pair['contexts'])))
                target['observations'] = [pairs[key] for key in sorted(pairs)]
                merge_codes(target['execution_codes'], record['execution_codes'], execution=True)
            readers = mapping.get('read_dependencies')
            require(type(readers) is dict and set(readers) == {'source', 'code', 'code_objects', 'code_object_granularity', 'coverage_complete'}, 'invalid readers schema')
            require(readers['code_object_granularity'] is True and readers['coverage_complete'] is False, 'unsupported reader completeness claim')
            for field in ('source', 'code'):
                require(type(readers[field]) is dict, 'invalid reader paths')
                for path, values in readers[field].items():
                    safe_path(path)
                    context_union(merged['read_dependencies'][field], path, values)
            merge_codes(merged['read_dependencies']['code_objects'], readers['code_objects'])
            require(type(mapping.get('preexisting_modules')) is dict, 'missing preexisting module metadata')
            for name, path in mapping['preexisting_modules'].items():
                require(type(name) is str and name, 'invalid module name')
                safe_path(path)
                previous = merged['preexisting_modules'].setdefault(name, path)
                require(previous == path, f'conflicting preexisting module {name}')
            require(type(mapping.get('ineligible_files')) is list, 'missing ineligible file metadata')
            for path in mapping['ineligible_files']:
                safe_path(path)
            merged['ineligible_files'] = sorted(set(merged['ineligible_files']) | set(mapping['ineligible_files']) | set(merged['preexisting_modules'].values()))
            require(type(mapping.get('limitations')) is list and all(type(v) is str for v in mapping['limitations']), 'invalid limitations')
            merged['limitations'] = sorted(set(merged['limitations']) | set(mapping['limitations']))
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f'{label}: {exc}')
    merged['capture_complete'] = False
    merged['narrowing_authorized'] = False
    merged['merge_errors'] = errors
    return merged


def merge_capture(parent, workers, catalog, outcomes, required_count=1620, instrumentation_freeze=None):
    errors, snapshots = [], []
    try:
        nodes = [test['id'] for test in read_json(catalog)['tests']]
        require(len(nodes) == len(set(nodes)) and len(nodes) == required_count, 'catalog size/uniqueness mismatch')
        require(all(type(node) is str and node for node in nodes), 'invalid catalog nodes')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        nodes = []
        errors.append(f'catalog: {exc}')
    try:
        receipt = audit_receipts(workers)
        require(Path(workers).is_dir() and receipt.get('submitted', 0) > 0, 'missing tracked spawn submissions')
        require(not receipt['errors'] and not receipt['must_retain'], f'receipt gaps: {receipt}')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        receipt = {'worker_contexts': {}, 'errors': [str(exc)]}
        errors.append(f'receipts: {exc}')
    known_pids = set()
    for path in Path(workers).glob('events-*.jsonl'):
        try:
            for line in path.read_text().splitlines():
                event = json.loads(line, object_pairs_hook=unique_object)
                require(type(event) is dict and type(event.get('pid')) is int, 'invalid receipt event')
                known_pids.add(str(event['pid']))
                if 'context' in event:
                    context = event['context']
                    require(context in nodes or context == '' or context == f'@worker-unassigned:{event["pid"]}', f'unknown receipt context {context}')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f'{path.name}: {exc}')
    paths = [Path(parent), *sorted(Path(workers).glob('observations-*.json'))]
    for path in paths:
        try:
            if path != Path(parent):
                require(path.stem.removeprefix('observations-') in known_pids, f'unknown observation shard {path.name}')
            snapshots.append((path.name, read_json(path)))
        except (OSError, ValueError) as exc:
            errors.append(f'{path.name}: {exc}')
    for pid in receipt['worker_contexts']:
        if not (Path(workers) / f'observations-{pid}.json').is_file():
            errors.append(f'missing observation shard for PID {pid}')
    merged = merge_snapshots(snapshots, nodes, receipt['worker_contexts'])
    errors.extend(merged.pop('merge_errors'))
    try:
        report = read_json(outcomes)
        require(report['exitstatus'] == 0 and type(report['exitstatus']) is int, 'pytest exit status not zero')
        for field in ('selected', 'collected', 'finished'):
            require(type(report[field]) is list and set(report[field]) == set(nodes) and len(report[field]) == len(nodes), f'outcome {field} catalog mismatch')
        require(report['missing'] == [] and report['collection_errors'] == [], 'missing tests or collection errors')
        require(set(report['details']) == set(nodes), 'phase details catalog mismatch')
        for node, detail in report['details'].items():
            require(detail['phases'] == {'setup': 'passed', 'call': 'passed', 'teardown': 'passed'}, f'incomplete phases: {node}')
            require(detail['failures'] == [], f'phase failures: {node}')
            require(type(detail['subtests']) is dict and all(k == 'passed' and type(v) is int and v >= 0 for k, v in detail['subtests'].items()), f'nonpassed subtests: {node}')
        require(set(report['outcomes']) == set(nodes) and all(v == 'passed' for v in report['outcomes'].values()), 'not every catalog test passed')
        all_passed = True
    except (OSError, ValueError, TypeError, KeyError) as exc:
        all_passed = False
        errors.append(f'outcomes: {exc}')
    version = report.get('configuration', {}).get('python', '') if all_passed else ''
    merged['python_version'] = '.'.join(version.split()[0].split('.')[:2]) if version else None
    merged['capture_complete'] = not errors and all_passed and len(nodes) == required_count
    merged['capture_report'] = {'errors': errors, 'python_version': merged['python_version'], 'catalog_count': len(nodes), 'all_catalog_passed': all_passed,
        'catalog_node_ids_sha256': hashlib.sha256(json.dumps(sorted(nodes), separators=(',', ':')).encode()).hexdigest(),
        'baseline_outcomes_sha256': hashlib.sha256(json.dumps(report.get('outcomes', {}), sort_keys=True, separators=(',', ':')).encode()).hexdigest() if all_passed else None,
        'receipt_audit': receipt, 'source_reader_coverage_complete': False, 'code_reader_coverage_complete': False,
        'untracked_activity_detection_exhaustive': False, 'narrowing_authorized': False}
    input_files = [('parent', Path(parent)), ('catalog', Path(catalog)), ('outcomes', Path(outcomes))]
    input_files.extend((f'workers/{path.name}', path) for path in sorted(Path(workers).glob('*')) if path.is_file())
    merged['capture_report']['input_sha256'] = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in input_files if path.is_file()}
    tools = Path(__file__).resolve().parent
    instrumenters = ('comparison_types.py', 'comparison_spawn.py', 'comparison_subprocess.py', 'comparison_bootstrap/sitecustomize.py')
    merged['capture_report']['instrumentation_files_at_merge_sha256'] = {name: hashlib.sha256((tools / name).read_bytes()).hexdigest() for name in instrumenters}
    merged['capture_report']['instrumentation_files_at_merge_mtime_ns'] = {name: (tools / name).stat().st_mtime_ns for name in instrumenters}
    merged['capture_report']['per_worker_instrumentation_attested'] = False
    if instrumentation_freeze is not None:
        try:
            freeze = read_json(instrumentation_freeze)
            require(set(freeze['files']) == set(instrumenters), 'freeze file set mismatch')
            for name in instrumenters:
                require(freeze['files'][name]['sha256'] == merged['capture_report']['instrumentation_files_at_merge_sha256'][name], f'instrumenter changed: {name}')
                require(freeze['files'][name]['mtime_ns'] == (tools / name).stat().st_mtime_ns, f'instrumenter mtime changed: {name}')
            merged['capture_report']['operational_instrumentation_freeze'] = dict(freeze, end_match=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            merged['capture_report']['errors'].append(f'instrumentation freeze: {exc}')
            merged['capture_complete'] = False
    return merged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, required=True)
    parser.add_argument('--workers', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--outcomes', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--instrumentation-freeze', type=Path)
    args = parser.parse_args()
    merged = merge_capture(args.parent, args.workers, args.catalog, args.outcomes, instrumentation_freeze=args.instrumentation_freeze)
    args.output.write_text(json.dumps(merged, sort_keys=True, separators=(',', ':')))
    print(json.dumps(merged['capture_report'], sort_keys=True))


if __name__ == '__main__':
    main()
