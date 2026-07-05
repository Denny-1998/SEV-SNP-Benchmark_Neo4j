#!/bin/bash
set -euo pipefail

# Usage:
#   ./orchestrate-pair.sh SIZE              # both baseline + sev in parallel (2/4/8-core)
#   ./orchestrate-pair.sh SIZE VARIANT      # single variant only (16-core, one at a time)
#
# Requires: lib.sh in the same directory, terraform applied config with
# outputs matching main.tf, and run-remote-benchmark.sh in the CWD.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

SIZE=${1:?Usage: $0 SIZE [VARIANT]}
ONLY_VARIANT=${2:-}

get_ip() {
  # Reads the IP for a given "SIZE-VARIANT" key straight from terraform output.
  terraform output -json vms | python3 -c "
import json,sys
data = json.load(sys.stdin)
print(data['$1']['ip'])
"
}

run_pair_member() {
  local variant=$1
  local key="${SIZE}-${variant}"
  local ip
  ip=$(get_ip "$key")
  echo "=== [$key] IP=$ip ==="

  wait_for_startup "$ip"
  copy_data "$ip"
  run_local_benchmarks "$ip"
  run_all_remote_loads "$ip" "$SIZE" "$variant"
  fetch_local_results "$ip" "$variant" "$SIZE"

  echo "=== [$key] done ==="
}

echo ">>> Applying Terraform for size $SIZE..."
if [ -n "$ONLY_VARIANT" ]; then
  terraform apply -auto-approve -target="google_compute_instance.vm[\"${SIZE}-${ONLY_VARIANT}\"]"
  run_pair_member "$ONLY_VARIANT"
else
  terraform apply -auto-approve \
    -target="google_compute_instance.vm[\"${SIZE}-baseline\"]" \
    -target="google_compute_instance.vm[\"${SIZE}-sev\"]"


  # --- Sequential (safe default) ---
  run_pair_member "baseline"
  run_pair_member "sev"

fi

echo ">>> Verifying results for size $SIZE..."
if [ -n "$ONLY_VARIANT" ]; then
  echo "(single-variant run — skipping full pair verification, check manually)"
else
  if ! verify_results "$SIZE"; then
    echo "!!! Some result folders are missing for size $SIZE — NOT destroying VMs."
    echo "!!! Investigate before running terraform destroy manually."
    exit 1
  fi
  echo "All 8 result folders present for size $SIZE."
fi

echo ">>> Destroying VMs for size $SIZE..."
if [ -n "$ONLY_VARIANT" ]; then
  terraform destroy -auto-approve -target="google_compute_instance.vm[\"${SIZE}-${ONLY_VARIANT}\"]"
else
  terraform destroy -auto-approve \
    -target="google_compute_instance.vm[\"${SIZE}-baseline\"]" \
    -target="google_compute_instance.vm[\"${SIZE}-sev\"]"
fi

echo ">>> Size $SIZE complete."
