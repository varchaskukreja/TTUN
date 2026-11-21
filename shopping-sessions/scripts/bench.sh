#!/bin/sh
# bench.sh DATA [RUNS] [BIN]: runs the binary RUNS times and prints the fastest run's phase log.
set -eu
DATA=$1
RUNS=${2:-5}
BIN=${3:-./target/release/ss}
OUT=$(mktemp -d)
best=999999
for i in $(seq "$RUNS"); do
  rm -rf "$OUT"/*
  log=$(DATA_DIR="$DATA" OUTPUT_DIR="$OUT" "$BIN" 2>&1 >/dev/null)
  t=$(printf '%s\n' "$log" | tail -1 | sed -E 's/^\[ *([0-9.]+)s\].*/\1/')
  if [ "$(echo "$t < $best" | bc)" = 1 ]; then best=$t; bestlog=$log; fi
done
printf '%s\n' "$bestlog" | sed -E 's/^\[ *([0-9.]+)s\] (.*)/\1  \2/' | tr '\n' '|'
echo
echo "best of $RUNS: ${best}s"
rm -rf "$OUT"
