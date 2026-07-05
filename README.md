# LDBC SNB Neo4j Confidential Computing Benchmark

Benchmarks the performance overhead of AMD SEV-SNP confidential VMs vs.
baseline VMs on GCP, running Neo4j with the LDBC SNB Interactive v1
workload at 3 load levels, executed both locally (on the VM) and remotely
(from this laptop), across 4 core counts (2, 4, 8, 16).

## One-time setup

1. **Fill in personal config** (gitignored, never committed):
   ```bash
   cp config.sh.example config.sh
   ```
   Edit `config.sh` with your GCP OS Login username and local paths.

2. **Fill in Terraform variables**:
   Edit `terraform.tfvars` — set your `project_id`, `zone`, and which
   `core_counts` you want to run this session (comment/uncomment as needed).

3. **Generate the dataset once** (see project notes for the datagen
   command) so `social_network/` and `substitution_parameters/` exist at
   the paths set in `config.sh`.

4. **Make scripts executable**:
   ```bash
   chmod +x orchestrate-pair.sh run-all.sh run-remote-benchmark.sh setup-and-run.sh
   ```

## Running benchmarks

**Single size** (e.g. just the 2-core pair):
```bash
terraform init   # first time only
./orchestrate-pair.sh 2
```

**16-core** (quota-constrained — one variant at a time):
```bash
./orchestrate-pair.sh 16 baseline
./orchestrate-pair.sh 16 sev
```

**Everything, unattended** (edit `core_counts` in `terraform.tfvars` to
include all sizes you want first):
```bash
nohup ./run-all.sh > run-all.log 2>&1 &
disown
tail -f run-all.log   # check progress any time
```

`orchestrate-pair.sh` handles, per size, fully automatically:
apply → wait for startup → copy data → run local benchmarks (3 loads) →
run remote benchmarks (3 loads, tunnel opened/closed automatically) →
fetch results → **verify all 8 result folders exist** → destroy VMs.
It refuses to destroy if verification fails, so a partial/failed run
never silently loses data.

## After a run

Results land in `RESULTS_ROOT` (set in `config.sh`), one folder per
variant/core-count/location/load, e.g.:
```
sev-8core-remote-high/
baseline-8core-local/
```

Confirm no VMs are left running (cost/quota hygiene):
```bash
gcloud compute instances list
```

## Analyzing results

`analyze_results.py` aggregates one or more result runs into tables,
descriptive statistics, effect sizes, and figures. It takes one folder
argument per repetition, e.g. for five repetitions stored as
`ResultsRun1` … `ResultsRun5`:

```bash
python3 analyze_results.py ResultsRun1 ResultsRun2 ResultsRun3 ResultsRun4 ResultsRun5
```

Run it from the directory containing those result folders (or pass full
paths). Output is written to the current working directory.

### What it prints

- **Master table** — every (core count, variant, load, location)
  configuration, averaged across all provided runs, with throughput,
  CPU/memory/disk usage, and a `status` column (`CLEAN`, `AUDIT-FAIL`, or
  a partial marker like `CLEAN(3/5)` if some but not all repetitions
  passed the timeliness audit for that configuration).
- **Peak resource utilization** — worst single sample per configuration
  (local runs only), useful for spotting saturation the averaged table
  smooths over.
- **Category latency (p99)** — short-read, complex-read, and update
  percentiles reported separately per LDBC's own operation categories,
  rather than blended into one artificial "overall" percentile.
- **Cross-run variability** — mean, median, min, max across repetitions,
  for throughput (remote) and CPU system % (local).
- **Effect size (Cohen's d)** — baseline vs. SEV, for throughput (remote)
  and CPU system % (local), computed only from `CLEAN` runs. Shows `n/a`
  with an explanation where one or both sides have no clean runs to
  compare (e.g. a configuration that failed the audit in every
  repetition).


### Notes

- Figures/tables described above assume the standard 4-core-count,
  3-load-level, 2-variant, 2-location matrix. If you change the
  experimental matrix, some plotting functions may need their hardcoded
  `CORES`/`LOADS`/`VARIANTS`/`LOCATIONS` lists updated at the top of the
  script.
- Re-run the script any time after adding a new repetition folder — all
  tables and figures regenerate from whatever set of run folders you pass
  in, nothing is cached between runs.

## Known issues already fixed in these scripts

- **TTY hang on first load**: `start-neo4j.sh` (external LDBC repo) polls
  readiness via `docker exec --tty`, which fails silently over a non-TTY
  SSH session and loops forever. Fixed by using `ssh -t` in
  `run_local_benchmarks()` (`lib.sh`).
- **Duplicate index creation**: `load-in-one-step.sh` already runs
  `create-indices.sh` internally; `setup-and-run.sh` also runs it
  afterward and hits existing constraints — harmless, caught by `|| true`.

## Files

| File | Purpose |
|---|---|
| `main.tf` | VM + firewall definitions |
| `terraform.tfvars` | Your project/zone/size selection (not personal SSH/paths — see `config.sh`) |
| `config.sh` | Personal SSH username + local paths (gitignored) |
| `config.sh.example` | Template for the above (committed) |
| `startup-script.tpl` | Runs on VM boot: installs Docker, builds Neo4j impl, writes `benchmark.properties` |
| `run-benchmark.sh` | Runs on the VM: one benchmark scenario (load × location) |
| `setup-and-run.sh` | Runs on the VM: orchestrates load + local benchmarks |
| `run-remote-benchmark.sh` | Runs on this laptop: one remote benchmark scenario |
| `lib.sh` | Shared shell functions for the orchestrator |
| `orchestrate-pair.sh` | Full pipeline for one VM size |
| `run-all.sh` | Loops all sizes unattended |
| `analyze_results.py` | Aggregates one or more result runs into tables, statistics, and figures (see "Analyzing results" above) |
