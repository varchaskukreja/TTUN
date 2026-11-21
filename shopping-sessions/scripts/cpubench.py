#!/usr/bin/env python3
"""Min CPU time (user+sys) and min wall over N runs for each binary.

Usage: cpubench.py DATA RUNS BIN [BIN...]   (runs are interleaved across binaries)
"""
import os
import resource
import shutil
import subprocess
import sys
import time

data, runs, bins = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
best = {b: (1e9, 1e9) for b in bins}
for _ in range(runs):
    for b in bins:
        out = "/tmp/ss/cpubench-out"
        shutil.rmtree(out, ignore_errors=True)
        env = dict(os.environ, DATA_DIR=data, OUTPUT_DIR=out, SS_QUIET="1")
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.time()
        subprocess.run([b], env=env, check=True)
        wall = time.time() - t0
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
        c, w = best[b]
        best[b] = (min(c, cpu), min(w, wall))
for b in bins:
    c, w = best[b]
    print(f"{b}: cpu {c:.3f}s  wall {w:.3f}s")
