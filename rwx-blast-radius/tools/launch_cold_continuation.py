#!/usr/bin/env python3
"""Read only our reviewed live driver's environment; never serialize its values."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

ALLOWED_PID = 17334


def parse_procargs(payload):
    if len(payload) < 5:
        raise ValueError('invalid process argument payload')
    argc = struct.unpack_from('=i', payload)[0]
    if not 1 <= argc <= 10000:
        raise ValueError('invalid process argument count')
    end = payload.find(b'\0', 4)
    if end < 0:
        raise ValueError('missing executable terminator')
    offset = end + 1
    while offset < len(payload) and payload[offset] == 0:
        offset += 1
    args = []
    for _ in range(argc):
        end = payload.find(b'\0', offset)
        if end < 0:
            raise ValueError('truncated process arguments')
        args.append(os.fsdecode(payload[offset:end])); offset = end + 1
    environment = {}
    for entry in payload[offset:].split(b'\0'):
        if not entry:
            break  # Environment terminator; later Darwin apple-vector entries are not env.
        key, separator, value = entry.partition(b'=')
        if not separator or not key or os.fsdecode(key) in environment:
            raise ValueError('invalid or duplicate environment entry')
        environment[os.fsdecode(key)] = os.fsdecode(value)
    return args, environment


def read_owned_environment(pid):
    if pid != ALLOWED_PID or sys.platform != 'darwin':
        raise ValueError('only the reviewed local macOS driver PID is allowed')
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value > 2**22:
        raise ValueError('cannot read reviewed process argument size')
    data = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
        raise ValueError('cannot read reviewed process arguments')
    args, environment = parse_procargs(data.raw[:size.value])
    if not any(arg.endswith('/validate_comparison_robustness.py') for arg in args):
        raise ValueError('reviewed PID no longer names the expected driver')
    return environment


def verify_environment(environment, expected):
    environment = dict(environment)
    # Reviewed CPython macOS bootstrap removal, verified against the full frozen hash.
    environment.pop('__PYVENV_LAUNCHER__', None)
    # Sole explicit environment mutation in the frozen driver.
    environment['PYTHON_CPU_COUNT'] = '2'
    overridden = {'PYTEST_ADDOPTS', 'PYTHONPATH', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONDONTWRITEBYTECODE'}
    inherited = {k:v for k,v in environment.items() if not k.startswith(('BLAST_', 'HOLDOUT_')) and k not in overridden}
    digest = hashlib.sha256(json.dumps(inherited, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != expected:
        raise ValueError('exact inherited environment hash mismatch; refusing launch')
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('prior', 'plan', 'source', 'catalog', 'output', 'launch-review'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    prior = args.prior.resolve(); freeze_path = prior/'freeze.json'; summary_path = prior/'cold/baseline/summary.json'
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    freeze = json.loads(freeze_path.read_text()); summary = json.loads(summary_path.read_text())
    review = json.loads(args.launch_review.read_text())
    if (review.get('reviewed') is not True or review.get('exact_environment_launch') is not True
        or review.get('freeze_sha256') != sha(freeze_path) or review.get('baseline_summary_sha256') != sha(summary_path)
        or summary.get('complete') is not True or summary.get('observed_count') != 1620):
        raise ValueError('complete baseline and exact launch review required')
    environment = verify_environment(read_owned_environment(ALLOWED_PID), freeze['runtime']['inherited_environment_sha256'])
    command = [sys.executable, '-u', str(Path(__file__).with_name('continue_comparison_cold.py'))]
    for name in ('prior','plan','source','catalog','output'):
        command += ['--'+name, str(getattr(args, name).resolve())]
    # Values remain in memory and go only to the reviewed local child process.
    return subprocess.call(command, env=environment)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError):
        print('Continuation launch refused: prerequisite or exact environment verification failed.', file=sys.stderr)
        raise SystemExit(1)
