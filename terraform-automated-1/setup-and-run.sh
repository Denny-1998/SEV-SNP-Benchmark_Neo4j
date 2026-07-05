#!/bin/bash
set -euo pipefail

CYPHER_DIR=/opt/ldbc-snb/ldbc_snb_interactive_v1_impls/cypher
VM_INFO=/opt/ldbc-snb/vm-info.txt
RESULTS_DIR=/opt/ldbc-snb/results

echo "========================================"
echo "Checking startup script status..."
until [ -f "$VM_INFO" ]; do
  echo "Startup script still running, waiting 30 seconds..."
  sleep 30
done

CORES=$(grep cores $VM_INFO | cut -d= -f2)
VARIANT=$(grep variant $VM_INFO | cut -d= -f2)
echo "VM ready: $VARIANT $CORES cores"

echo "========================================"
echo "Checking data..."
if [ -z "$(ls -A /opt/ldbc-snb/social_network 2>/dev/null)" ]; then
  echo "ERROR: /opt/ldbc-snb/social_network is empty."
  echo "Please copy data first:"
  echo "  scp -r social_network/ substitution_parameters/ <YOUR_SSH_USER>@VM_IP:/opt/ldbc-snb/"
  exit 1
fi
echo "Data found."

echo "========================================"
echo "Loading database (this takes ~10 min)..."
cd $CYPHER_DIR
source scripts/vars.sh
export NEO4J_VANILLA_CSV_DIR=/opt/ldbc-snb/social_network
export NEO4J_CONVERTED_CSV_DIR=/opt/ldbc-snb/social_network_converted
scripts/load-in-one-step.sh

echo "========================================"
echo "Creating indices and backup..."
source scripts/vars.sh
scripts/create-indices.sh || true
scripts/backup-database.sh

for LOAD in low medium high; do
  echo "========================================"
  echo "Running LOCAL benchmark: $LOAD load"
  /opt/ldbc-snb/run-benchmark.sh $LOAD local
done

VM_IP=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" -H "Metadata-Flavor: Google")

echo ""
echo "========================================"
echo "All local benchmarks complete!"
echo "Results saved to: $RESULTS_DIR"
echo ""
echo "To run REMOTE benchmarks from your laptop:"
echo "  1. Open SSH tunnel:"
echo "     ssh -L 7687:localhost:7687 <YOUR_SSH_USER>@$VM_IP"
echo ""
echo "  2. In another terminal, run:"
echo "     ./run-remote-benchmark.sh $VM_IP $CORES $VARIANT low"
echo "     ./run-remote-benchmark.sh $VM_IP $CORES $VARIANT medium"
echo "     ./run-remote-benchmark.sh $VM_IP $CORES $VARIANT high"
echo ""
echo "  3. Grab all results:"
echo "     scp -r <YOUR_SSH_USER>@$VM_IP:/opt/ldbc-snb/results/ ./results-$VARIANT-${CORES}core/"
echo "========================================"
