#!/bin/bash
set -euo pipefail

# Usage: ./run-benchmark.sh <load> <location>
#   load:     low | medium | high
#   location: local | remote

LOAD=${1:-medium}
LOCATION=${2:-local}

CYPHER_DIR=/opt/ldbc-snb/ldbc_snb_interactive_v1_impls/cypher
PROPS=$CYPHER_DIR/driver/benchmark.properties
VM_INFO=/opt/ldbc-snb/vm-info.txt

CORES=$(grep cores $VM_INFO | cut -d= -f2)
VARIANT=$(grep variant $VM_INFO | cut -d= -f2)

case $LOAD in
  low)    TCR=0.1   ;;
  medium) TCR=0.01  ;;
  high)   TCR=0.001 ;;
  *) echo "Unknown load: $LOAD. Use low, medium, or high."; exit 1 ;;
esac

RESULTS_DIR=/opt/ldbc-snb/results/${VARIANT}-${CORES}core-${LOCATION}-${LOAD}
mkdir -p $RESULTS_DIR

echo "========================================"
echo "Starting benchmark"
echo "  VM:       $VARIANT $CORES cores"
echo "  Load:     $LOAD (TCR=$TCR)"
echo "  Location: $LOCATION"
echo "  Results:  $RESULTS_DIR"
echo "========================================"

sed -i "s/^time_compression_ratio=.*/time_compression_ratio=$TCR/" $PROPS

cd $CYPHER_DIR
source scripts/vars.sh
scripts/restore-database.sh

sar -o $RESULTS_DIR/sar.bin 5 &
SAR_PID=$!

driver/benchmark.sh 2>&1 | tee $RESULTS_DIR/benchmark.log

kill $SAR_PID 2>/dev/null || true
sleep 2
sar -f $RESULTS_DIR/sar.bin -u -r -d > $RESULTS_DIR/sar.txt 2>/dev/null || true

cp $CYPHER_DIR/results/LDBC-SNB-results.json $RESULTS_DIR/
cp $CYPHER_DIR/results/LDBC-SNB-validation.json $RESULTS_DIR/ 2>/dev/null || true
cp $PROPS $RESULTS_DIR/benchmark.properties

echo "========================================"
echo "Done. Results saved to $RESULTS_DIR"
echo "========================================"
