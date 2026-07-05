#!/bin/bash
set -euo pipefail

# Runs the full benchmark suite for every VM size, unattended.
# 16-core is quota-constrained and handled one variant at a time.
#
# Usage (foreground):
#   ./run-all.sh
#
# Usage (overnight, survives terminal close):
#   nohup ./run-all.sh > run-all.log 2>&1 &
#   disown
#   # check progress any time with: tail -f run-all.log

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for SIZE in 2 4 8; do
  echo "########################################"
  echo "### Starting size $SIZE"
  echo "########################################"
  "$SCRIPT_DIR/orchestrate-pair.sh" "$SIZE"
done

echo "########################################"
echo "### Starting size 16 (baseline, then sev — quota-separated)"
echo "########################################"
"$SCRIPT_DIR/orchestrate-pair.sh" 16 baseline
"$SCRIPT_DIR/orchestrate-pair.sh" 16 sev

echo "########################################"
echo "### ALL SIZES COMPLETE"
echo "########################################"
