#!/usr/bin/env bash
# Benchmark an image inside the Docker VM (Linux) against data in the `ssdata` volume.
# Usage: dbench.sh IMAGE [RUNS] [DATASET]
set -euo pipefail
img=$1
runs=${2:-5}
ds=${3:-p20}
best=999
for _ in $(seq "$runs"); do
  t0=$(python3 -c 'import time; print(time.time())')
  log=$(docker run --rm --network none --read-only --tmpfs /output:uid=1000 \
    -v ssdata:/vol:ro -e DATA_DIR=/vol/$ds -e OUTPUT_DIR=/output "$img" 2>&1 | tr '\n' '|')
  t1=$(python3 -c 'import time; print(time.time())')
  w=$(python3 -c "print(round($t1-$t0,3))")
  echo "wall ${w}s  $log"
  best=$(python3 -c "print(min($best,$w))")
done
echo "best wall: ${best}s"
