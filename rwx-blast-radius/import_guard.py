"""Bounded cold-process collection checks for opt-in class narrowing.

Success establishes patched import and collection only. It is not evidence that
the patch is harmless or that class-consumer dependencies are complete.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
MAX_CAPTURE_BYTES = 2 * 1024 * 1024


def _reject_constant(value):
    raise ValueError(f'nonstandard JSON constant: {value}')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON field: {key}')
        result[key] = value
    return result


def _valid_report(report, count):
    if not isinstance(report, dict) or count < 1:
        return False
    if report.get('collect_only') is not True or report.get('complete') is not True:
        return False
    for key, expected in [('requested', count), ('collected', count), ('finished', 0), ('pytest_exitcode', 0)]:
        if type(report.get(key)) is not int or report[key] != expected:
            return False
    for key in ('failing', 'collection_failed', 'collection_errors', 'patch_import_errors',
                'python_patches_not_loaded', 'reasons'):
        if type(report.get(key)) is not list or report[key] != []:
            return False
    for key in ('python_patches_loaded', 'explicit_imports'):
        entries = report.get(key)
        if type(entries) is not list or not entries or any(type(entry) is not str or not entry for entry in entries):
            return False
    return True


class ImportGuard:
    def __init__(self, source, tests, budget=120, probe_timeout=8):
        self.source = Path(source)
        node_ids = [test['id'] for test in tests]
        self.nodes = json.dumps(node_ids)
        self.count = len(node_ids)
        self.catalog_valid = bool(node_ids) and all(isinstance(node, str) and node for node in node_ids) and len(set(node_ids)) == len(node_ids)
        self.budget = float(budget)
        self.probe_timeout = min(8.0, float(probe_timeout))
        self.wall_seconds = 0.0
        self.calls = []

    def __call__(self, patch):
        remaining = self.budget - self.wall_seconds
        if not self.catalog_valid or remaining < 1 or self.probe_timeout <= 0:
            self.calls.append({'valid': False, 'reason': 'invalid_catalog' if not self.catalog_valid else 'budget_exhausted'})
            return False
        start = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix='blast-import-') as directory:
                path = Path(directory) / 'change.patch'
                path.write_text(patch)
                process = subprocess.run([
                    sys.executable, str(ROOT / 'runtime_probe.py'), '--source', str(self.source),
                    '--patch', str(path), '--nodes', '-', '--collect-only', '--import-patches',
                ], input=self.nodes, text=True, capture_output=True,
                    timeout=min(self.probe_timeout, remaining))
                if process.returncode != 0:
                    self.calls.append({'valid': False, 'reason': 'nonzero_exit'})
                    return False
                if (len(process.stdout.encode('utf-8')) + len(process.stderr.encode('utf-8'))) > MAX_CAPTURE_BYTES:
                    self.calls.append({'valid': False, 'reason': 'output_limit'})
                    return False
                report = json.loads(process.stdout, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
                valid = _valid_report(report, self.count)
                self.calls.append({'valid': valid,
                                   'cpu_seconds': report.get('cpu_seconds') if isinstance(report, dict) else None,
                                   'reasons': report.get('reasons', []) if isinstance(report, dict) else ['invalid report object']})
                return valid
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            self.calls.append({'valid': False, 'reason': type(exc).__name__})
            return False
        finally:
            # Charge successful, failed, and timed-out attempts to the same
            # cumulative wall budget. Selection time between calls is excluded.
            self.wall_seconds += max(0.0, time.monotonic() - start)
