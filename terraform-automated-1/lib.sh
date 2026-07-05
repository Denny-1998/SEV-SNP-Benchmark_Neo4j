#!/bin/bash
# lib.sh — shared helpers for the benchmark orchestrator
# Source this from other scripts: source "$(dirname "$0")/lib.sh"

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR_LIB/config.sh" ]; then
  source "$SCRIPT_DIR_LIB/config.sh"
else
  echo "ERROR: config.sh not found. Copy config.sh.example to config.sh and fill in your values." >&2
  exit 1
fi

: "${SSH_USER:?SSH_USER not set in config.sh}"
: "${RESULTS_ROOT:?RESULTS_ROOT not set in config.sh}"

# Wait until /opt/ldbc-snb/setup-and-run.sh exists on the remote VM.
# SSH is reachable long before the startup-script has finished cloning,
# building, and writing that file — this replaces the manual
# "ssh ... cat vm-info.txt" polling step.
wait_for_startup() {
  local ip=$1
  echo "[$ip] waiting for startup script to finish..."
  until ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
      "${SSH_USER}@${ip}" 'test -f /opt/ldbc-snb/setup-and-run.sh' 2>/dev/null; do
    echo -n "."
    sleep 15
  done
  echo " [$ip] ready."
}

# Copy the dataset to a VM. Safe to background (call with `&`).
copy_data() {
  local ip=$1
  scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -r \
    ~/ldbc-snb/ldbc_snb_datagen_hadoop/social_network/ \
    ~/ldbc-snb/ldbc_snb_datagen_hadoop/substitution_parameters/ \
    "${SSH_USER}@${ip}:/opt/ldbc-snb/"
}

# Run the local benchmark suite on a VM.
# IMPORTANT: uses `ssh -t` — without a TTY, start-neo4j.sh's readiness
# check (`docker exec --tty`) fails forever and the run hangs.
run_local_benchmarks() {
  local ip=$1
  ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${SSH_USER}@${ip}" \
    '/opt/ldbc-snb/setup-and-run.sh'
}

# Run one remote (network-mediated) benchmark load level against a VM.
# Opens its own tunnel, runs the benchmark, tears the tunnel down —
# no manual tunnel babysitting or IP/variant double-checking needed.
run_remote_load() {
  local ip=$1 cores=$2 variant=$3 load=$4

  ssh -f -N -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ExitOnForwardFailure=yes \
    -L 7687:localhost:7687 "${SSH_USER}@${ip}"
  local tunnel_pid
  tunnel_pid=$(pgrep -f "L 7687:localhost:7687.*${ip}" | head -1)

  echo "[$ip] tunnel up (pid $tunnel_pid) — running $variant $cores-core $load"
  ./run-remote-benchmark.sh "$ip" "$cores" "$variant" "$load"

  kill "$tunnel_pid" 2>/dev/null || true
  sleep 2
}

run_all_remote_loads() {
  local ip=$1 cores=$2 variant=$3
  for load in low medium high; do
    run_remote_load "$ip" "$cores" "$variant" "$load"
  done
}

# Pull local-run results down from a VM.
fetch_local_results() {
  local ip=$1 variant=$2 cores=$3
  mkdir -p "${RESULTS_ROOT}/${variant}-${cores}core-local"
  scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -r \
    "${SSH_USER}@${ip}:/opt/ldbc-snb/results/" \
    "${RESULTS_ROOT}/${variant}-${cores}core-local/"
}

# Verify the 8 expected result folders exist for a VM pair. Exits non-zero
# (and prints what's missing) if anything is absent — call this before
# any `terraform destroy` so you never lose a run's data.
verify_results() {
  local cores=$1
  local ok=1
  for variant in baseline sev; do
    for suffix in local remote-low remote-medium remote-high; do
      local dir="${RESULTS_ROOT}/${variant}-${cores}core-${suffix}"
      if [ ! -d "$dir" ] || [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
        echo "MISSING: $dir"
        ok=0
      fi
    done
  done
  [ "$ok" -eq 1 ]
}
