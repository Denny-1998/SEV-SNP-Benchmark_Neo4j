#!/bin/bash
set -euo pipefail

# Usage: ./run-remote-benchmark.sh <vm_ip> <cores> <variant> <load>
#
# All paths are configured via environment variables with sensible defaults.
# Override by setting them before running, or edit the defaults below.

VM_IP=${1:?Usage: $0 <vm_ip> <cores> <variant> <load>}
CORES=${2:?}
VARIANT=${3:?}
LOAD=${4:?}

# Load personal config (gitignored) — see config.sh.example
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/config.sh" ]; then
  source "$SCRIPT_DIR/config.sh"
else
  echo "ERROR: config.sh not found. Copy config.sh.example to config.sh and fill in your values." >&2
  exit 1
fi
LOCAL_RESULTS="${LOCAL_RESULTS:-$RESULTS_ROOT}"

case $LOAD in
  low)    TCR=0.1   ;;
  medium) TCR=0.01  ;;
  high)   TCR=0.001 ;;
  *) echo "Unknown load: $LOAD. Use low, medium, or high."; exit 1 ;;
esac

RESULTS_DIR=$LOCAL_RESULTS/${VARIANT}-${CORES}core-remote-${LOAD}
mkdir -p $RESULTS_DIR

echo "========================================"
echo "Starting REMOTE benchmark"
echo "  VM:       $VARIANT $CORES cores ($VM_IP)"
echo "  Load:     $LOAD (TCR=$TCR)"
echo "  Results:  $RESULTS_DIR"
echo "========================================"

# Restore clean DB on VM
ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${SSH_USER}@$VM_IP \
  "cd /opt/ldbc-snb/ldbc_snb_interactive_v1_impls/cypher && source scripts/vars.sh && scripts/restore-database.sh"

# Wait for Neo4j to be reachable via tunnel
echo "Waiting for Neo4j to be reachable..."
until nc -z localhost 7687 2>/dev/null; do
  echo -n "."
  sleep 2
done
echo " Ready."
sleep 5  

# Update local benchmark.properties
PROPS=$LOCAL_IMPLS/cypher/driver/benchmark.properties
sed -i "s/^time_compression_ratio=.*/time_compression_ratio=$TCR/" $PROPS
sed -i "s|^ldbc.snb.interactive.updates_dir=.*|ldbc.snb.interactive.updates_dir=$LOCAL_SOCIAL/|" $PROPS
sed -i "s|^ldbc.snb.interactive.parameters_dir=.*|ldbc.snb.interactive.parameters_dir=$LOCAL_PARAMS/|" $PROPS
sed -i "s/^thread_count=.*/thread_count=$CORES/" $PROPS

# Start monitoring on VM
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${SSH_USER}@$VM_IP \
  "sar -o /opt/ldbc-snb/results/sar-remote-${LOAD}.bin 5 > /dev/null 2>&1 &"

# Run benchmark locally (connects via tunnel on localhost:7687)
cd $LOCAL_IMPLS/cypher
driver/benchmark.sh 2>&1 | tee $RESULTS_DIR/benchmark.log

# Stop monitoring and collect from VM
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ${SSH_USER}@$VM_IP \
  "pkill sar 2>/dev/null || true; sleep 2; sar -f /opt/ldbc-snb/results/sar-remote-${LOAD}.bin -u -r -d > /opt/ldbc-snb/results/sar-remote-${LOAD}.txt 2>/dev/null || true"

# Save results
cp $LOCAL_IMPLS/cypher/results/LDBC-SNB-results.json $RESULTS_DIR/
cp $LOCAL_IMPLS/cypher/results/LDBC-SNB-validation.json $RESULTS_DIR/ 2>/dev/null || true
cp $PROPS $RESULTS_DIR/benchmark.properties

scp ${SSH_USER}@$VM_IP:/opt/ldbc-snb/results/sar-remote-${LOAD}.txt \
  $RESULTS_DIR/sar-vm.txt 2>/dev/null || true

echo "========================================"
echo "Done. Results saved to $RESULTS_DIR"
echo "========================================"
