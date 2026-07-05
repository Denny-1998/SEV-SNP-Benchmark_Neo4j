"""
LDBC SNB Neo4j Benchmark Analysis

Builds one complete table covering every (core count, variant, location, load)
combination, including failed/excluded runs (clearly marked, not hidden), with:
  - throughput (ops/sec)
  - p95 / p99 latency (approximate — see note below)
  - CPU / memory / disk utilization
  - status: CLEAN, AUDIT-FAIL, DATA-GAP, or MISSING

Also renders the table as an image and produces supporting charts.

NOTE on p95/p99: the LDBC driver reports percentiles per query type (26 types),
not one overall percentile across all operations. Rather than blending all
query types into one artificial number, this script computes a count-weighted
percentile per LDBC operation category (Short Read, Complex Read, Update) —
still an approximation, but one that stays within semantically similar
operations instead of mixing reads with writes.

Usage:
    python analyze_results.py ResultsRun1 [ResultsRun2 ResultsRun3 ...]
"""

import json
import re
import sys
import statistics
from pathlib import Path
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

CORES = [2, 4, 8, 16]
VARIANTS = ["baseline", "sev"]
LOADS = ["low", "medium", "high"]
LOCATIONS = ["local", "remote"]
AUDIT_THRESHOLD = 150
SAR_GAP_THRESHOLD_SEC = 600


def find_paths(run_dir, variant, cores, location, load):
    """Return dict of relevant file paths for one configuration (may not all exist)."""
    run_dir = Path(run_dir)
    local_base = run_dir / f"{variant}-{cores}core-local"
    if location == "local":
        result_base = local_base / "results" / f"{variant}-{cores}core-local-{load}"
        sar_txt = result_base / "sar.txt"
    else:
        result_base = run_dir / f"{variant}-{cores}core-remote-{load}"
        # Known bug: remote sar files land inside the local folder, misnamed.
        sar_txt = local_base / "results" / f"sar-remote-{load}.txt"
    return {
        "results": result_base / "LDBC-SNB-results.json",
        "validation": result_base / "LDBC-SNB-validation.json",
        "sar": sar_txt if sar_txt.exists() else None,
    }


def detect_sar_gap_sec(sar_txt_path):
    times = []
    with open(sar_txt_path) as f:
        for line in f:
            m = re.match(r"^(\d{2}:\d{2}:\d{2})\s+all\s+[\d.]+", line)
            if m:
                times.append(datetime.strptime(m.group(1), "%H:%M:%S"))
    max_gap = timedelta(0)
    for i in range(1, len(times)):
        gap = times[i] - times[i - 1]
        if gap.total_seconds() < 0:
            gap += timedelta(days=1)
        max_gap = max(max_gap, gap)
    return max_gap.total_seconds()


def parse_sar_averages(sar_txt_path):
    result = {"cpu_user_pct": None, "cpu_system_pct": None,
              "mem_used_pct": None, "disk_util_pct": None}
    with open(sar_txt_path) as f:
        for line in f:
            if not line.startswith("Average:"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            label = parts[1]
            if label == "all":
                result["cpu_user_pct"] = float(parts[2])
                result["cpu_system_pct"] = float(parts[4])
            elif label in ("sda", "nvme0n1"):
                result["disk_util_pct"] = float(parts[9])
            else:
                try:
                    float(label)
                    result["mem_used_pct"] = float(parts[4])
                except ValueError:
                    pass
    return result


def parse_sar_peaks(sar_txt_path):
    """Scans every individual sample row (not just the 'Average:' summary line)
    and returns the single highest value observed for each metric — i.e. the
    worst-case spike during the run, not an average."""
    peak = {"cpu_user_pct": None, "cpu_system_pct": None,
            "mem_used_pct": None, "disk_util_pct": None}
    time_re = re.compile(r"^\d{2}:\d{2}:\d{2}\s+(\S+)\s")
    with open(sar_txt_path) as f:
        for line in f:
            m = time_re.match(line)
            if not m:
                continue
            parts = line.split()
            label = parts[1]
            try:
                if label == "all":
                    user, system = float(parts[2]), float(parts[4])
                    if peak["cpu_user_pct"] is None or user > peak["cpu_user_pct"]:
                        peak["cpu_user_pct"] = user
                    if peak["cpu_system_pct"] is None or system > peak["cpu_system_pct"]:
                        peak["cpu_system_pct"] = system
                elif label in ("sda", "nvme0n1"):
                    util = float(parts[9])
                    if peak["disk_util_pct"] is None or util > peak["disk_util_pct"]:
                        peak["disk_util_pct"] = util
                else:
                    float(label)  # confirms this is a memory row (starts with kbmemfree)
                    memused = float(parts[4])
                    if peak["mem_used_pct"] is None or memused > peak["mem_used_pct"]:
                        peak["mem_used_pct"] = memused
            except (ValueError, IndexError):
                continue
    return peak


def categorize_query(name):
    """LDBC SNB's own operation categories — not something we invented.
    Complex Read (LdbcQuery1-14), Short Read (LdbcShortQuery1-7),
    Update (LdbcUpdate1-8)."""
    if name.startswith("LdbcShortQuery"):
        return "Short Read"
    if name.startswith("LdbcUpdate"):
        return "Update"
    if name.startswith("LdbcQuery"):
        return "Complex Read"
    return "Other"


def category_percentile(all_metrics, category, key):
    """Count-weighted average of one percentile field, but only across query
    types within the SAME LDBC-defined category — e.g. only the 7 short-read
    types, not blended with updates or complex reads. Still an approximation
    (percentiles don't combine perfectly linearly), but a defensible one,
    since it stays within one semantically coherent operation type rather
    than mixing reads with writes."""
    matching = [m for m in all_metrics if "run_time" in m and categorize_query(m["name"]) == category]
    total_count = sum(m["count"] for m in matching)
    if total_count == 0:
        return None
    weighted_sum = sum(m["run_time"][key] * m["count"] for m in matching)
    return weighted_sum / total_count


def export_per_query_percentiles(run_dirs, outfile="per_query_percentiles.csv"):
    """Writes the driver's own reported percentiles per query type, per
    configuration — no blending or aggregation across query types, since
    that would mix genuinely different operations into one meaningless number.
    Open this in a spreadsheet or filter it in your own script to pull out
    whichever specific query types matter for your paper."""
    import csv
    fieldnames = ["run", "cores", "variant", "location", "load", "query",
                  "count", "mean_ms", "p50_ms", "p90_ms", "p95_ms", "p99_ms"]
    with open(outfile, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for run_dir in run_dirs:
            for cores in CORES:
                for variant in VARIANTS:
                    for location in LOCATIONS:
                        for load in LOADS:
                            paths = find_paths(run_dir, variant, cores, location, load)
                            if not paths["results"].exists():
                                continue
                            with open(paths["results"]) as rf:
                                r = json.load(rf)
                            for m in r["all_metrics"]:
                                if "run_time" not in m:
                                    continue
                                rt = m["run_time"]
                                writer.writerow({
                                    "run": str(run_dir), "cores": cores, "variant": variant,
                                    "location": location, "load": load, "query": m["name"],
                                    "count": m["count"], "mean_ms": rt["mean"],
                                    "p50_ms": rt["50th_percentile"], "p90_ms": rt["90th_percentile"],
                                    "p95_ms": rt["95th_percentile"], "p99_ms": rt["99th_percentile"],
                                })
    print(f"Saved: {outfile} (every query type's own reported percentiles, no blending)")


def load_all_rows(run_dirs):
    """Returns a flat list of row-dicts, one per (run, cores, variant, location, load)."""
    rows = []
    for run_dir in run_dirs:
        for cores in CORES:
            for variant in VARIANTS:
                for location in LOCATIONS:
                    for load in LOADS:
                        paths = find_paths(run_dir, variant, cores, location, load)
                        row = {
                            "run": str(run_dir), "cores": cores, "variant": variant,
                            "location": location, "load": load,
                            "status": "MISSING", "throughput": None,
                            "cpu_user_pct": None, "cpu_system_pct": None,
                            "mem_used_pct": None, "disk_util_pct": None,
                            "peak_cpu_user_pct": None, "peak_cpu_system_pct": None,
                            "peak_mem_used_pct": None, "peak_disk_util_pct": None,
                            "excessive_delay_count": None,
                            "complex_read_p99": None, "short_read_p99": None, "update_p99": None,
                        }
                        if paths["results"].exists() and paths["validation"].exists():
                            with open(paths["results"]) as f:
                                r = json.load(f)
                            with open(paths["validation"]) as f:
                                v = json.load(f)
                            row["throughput"] = r["throughput"]
                            row["excessive_delay_count"] = v["excessive_delay_count"]
                            row["complex_read_p99"] = category_percentile(r["all_metrics"], "Complex Read", "99th_percentile")
                            row["short_read_p99"] = category_percentile(r["all_metrics"], "Short Read", "99th_percentile")
                            row["update_p99"] = category_percentile(r["all_metrics"], "Update", "99th_percentile")

                            gap_sec = None
                            if paths["sar"] is not None:
                                gap_sec = detect_sar_gap_sec(paths["sar"])
                                row.update(parse_sar_averages(paths["sar"]))
                                peaks = parse_sar_peaks(paths["sar"])
                                row["peak_cpu_user_pct"] = peaks["cpu_user_pct"]
                                row["peak_cpu_system_pct"] = peaks["cpu_system_pct"]
                                row["peak_mem_used_pct"] = peaks["mem_used_pct"]
                                row["peak_disk_util_pct"] = peaks["disk_util_pct"]

                            if gap_sec is not None and gap_sec > SAR_GAP_THRESHOLD_SEC:
                                row["status"] = "DATA-GAP"
                            elif v["excessive_delay_count"] > AUDIT_THRESHOLD:
                                row["status"] = "AUDIT-FAIL"
                            else:
                                row["status"] = "CLEAN"
                        rows.append(row)
    return rows


def get_cell(rows, cores, variant, load, location):
    """Finds all rows matching one configuration and picks which ones to use:
    prefers CLEAN runs if any exist, otherwise falls back to whatever exists
    (AUDIT-FAIL / DATA-GAP) so nothing is silently hidden. Returns None if the
    configuration has no data at all.
    Used by every table and chart so they all agree on the same numbers."""
    matching = [r for r in rows if r["cores"] == cores and r["variant"] == variant
                and r["load"] == load and r["location"] == location]
    if not matching:
        return None
    clean = [r for r in matching if r["status"] == "CLEAN"]
    if clean:
        status = "CLEAN" if len(clean) == len(matching) else f"CLEAN({len(clean)}/{len(matching)})"
        source = clean
    else:
        status = matching[0]["status"]
        source = matching
    return {"status": status, "rows": source}


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def fmt(value, width, decimals=2):
    if value is None:
        return "-".rjust(width)
    return f"{value:.{decimals}f}".rjust(width)


def cohens_d(sample_a, sample_b):
    """Standardized effect size between two independent samples — the difference
    between their means, expressed in units of pooled standard deviation. Unlike
    a p-value, it stays meaningful at small n, which is why it's usable here with
    only a handful of repeated runs per configuration.
    Returns None if either sample has fewer than 2 points, or if pooled variance
    is exactly zero (no meaningful scale to standardize against).
    Convention: |d|<0.2 negligible, 0.2-0.5 small, 0.5-0.8 medium, >0.8 large."""
    a = [v for v in sample_a if v is not None]
    b = [v for v in sample_b if v is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    mean_diff = statistics.mean(a) - statistics.mean(b)
    pooled_var = (((len(a) - 1) * statistics.variance(a) + (len(b) - 1) * statistics.variance(b))
                  / (len(a) + len(b) - 2))
    pooled_std = pooled_var ** 0.5
    return mean_diff / pooled_std if pooled_std != 0 else None


def interpret_d(d):
    if d is None:
        return "n/a"
    ad = abs(d)
    label = "negligible" if ad < 0.2 else "small" if ad < 0.5 else "medium" if ad < 0.8 else "large"
    return f"{d:+.2f} ({label})"


def print_master_table(rows):
    print("=" * 130)
    print("MASTER TABLE — every configuration, sorted by core count / variant / load / location")
    print("=" * 130)
    header = (f"{'cores':>5} {'variant':>8} {'load':>7} {'location':>8} {'status':>10} "
              f"{'ops/sec':>8} {'cpu_usr%':>8} {'cpu_sys%':>8} "
              f"{'mem%':>6} {'disk%':>6}")
    print(header)
    print("-" * len(header))
    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                for location in LOCATIONS:
                    cell = get_cell(rows, cores, variant, load, location)
                    if cell is None:
                        continue
                    src = cell["rows"]
                    print(f"{cores:5d} {variant:>8} {load:>7} {location:>8} {cell['status']:>10} "
                          f"{fmt(mean([r['throughput'] for r in src]),8)} "
                          f"{fmt(mean([r['cpu_user_pct'] for r in src]),8)} "
                          f"{fmt(mean([r['cpu_system_pct'] for r in src]),8)} "
                          f"{fmt(mean([r['mem_used_pct'] for r in src]),6)} "
                          f"{fmt(mean([r['disk_util_pct'] for r in src]),6)}")
    print()


def render_master_table_image(rows, outfile="master_table.png"):
    """Renders the same master table as a PNG so it can be viewed/embedded directly."""
    table_rows = []
    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                for location in LOCATIONS:
                    cell = get_cell(rows, cores, variant, load, location)
                    if cell is None:
                        continue
                    src = cell["rows"]
                    table_rows.append([
                        str(cores), variant, load, location, cell["status"],
                        fmt(mean([r["throughput"] for r in src]), 0).strip(),
                        fmt(mean([r["cpu_user_pct"] for r in src]), 0).strip(),
                        fmt(mean([r["cpu_system_pct"] for r in src]), 0).strip(),
                        fmt(mean([r["mem_used_pct"] for r in src]), 0).strip(),
                        fmt(mean([r["disk_util_pct"] for r in src]), 0).strip(),
                    ])

    columns = ["cores", "variant", "load", "location", "status", "ops/sec",
               "cpu_usr%", "cpu_sys%", "mem%", "disk%"]

    fig, ax = plt.subplots(figsize=(14, 0.35 * len(table_rows) + 1))
    ax.axis("off")
    tbl = ax.table(cellText=table_rows, colLabels=columns, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8)
    tbl.scale(1, 1.3)

    status_col_idx = columns.index("status")
    for i, row in enumerate(table_rows):
        status = row[status_col_idx]
        color = "#d4edda" if status == "CLEAN" else ("#fff3cd" if status.startswith("CLEAN(") else "#f8d7da")
        tbl[(i + 1, status_col_idx)].set_facecolor(color)

    fig.tight_layout()
    fig.savefig(outfile, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {outfile}")


def plot_grouped_bars(rows, metric, ylabel, title, outfile, location="remote"):
    """Grouped bar chart: one group per core count, bars per (variant, load).
    Shows EVERY run, not just clean ones — bars for AUDIT-FAIL (or DATA-GAP,
    should it occur) configurations are hatched and outlined so you can see
    them, but also see at a glance that they're not trustworthy numbers."""
    fig, ax = plt.subplots(figsize=(9, 5))
    bar_width = 0.12
    combos = [(v, l) for v in VARIANTS for l in LOADS]
    x_base = range(len(CORES))
    statuses_seen = set()

    for i, (variant, load) in enumerate(combos):
        values, statuses = [], []
        for cores in CORES:
            cell = get_cell(rows, cores, variant, load, location)
            if cell is None:
                values.append(None)
                statuses.append(None)
                continue
            values.append(mean([r[metric] for r in cell["rows"]]))
            statuses.append(cell["status"])

        offset = (i - len(combos) / 2) * bar_width
        positions = [x + offset for x in x_base]
        color = "#1f77b4" if variant == "baseline" else "#ff7f0e"
        alpha = {"low": 0.4, "medium": 0.7, "high": 1.0}[load]

        plot_positions = [p for p, v in zip(positions, values) if v is not None]
        plot_values = [v for v in values if v is not None]
        plot_statuses = [s for v, s in zip(values, statuses) if v is not None]

        bars = ax.bar(plot_positions, plot_values, width=bar_width, color=color,
                       alpha=alpha, label=f"{variant}-{load}")
        for patch, status in zip(bars.patches, plot_statuses):
            base_status = status.split("(")[0]  # e.g. "CLEAN(3/4)" -> "CLEAN"
            statuses_seen.add(base_status)
            if base_status == "AUDIT-FAIL":
                patch.set_hatch("//")
                patch.set_edgecolor("darkred")
                patch.set_linewidth(1)
            elif base_status == "DATA-GAP":
                patch.set_hatch("xx")
                patch.set_edgecolor("dimgray")
                patch.set_linewidth(1)

    ax.set_xticks(list(x_base))
    ax.set_xticklabels([str(c) for c in CORES])
    ax.set_xlabel("Core count")
    ax.set_ylabel(ylabel)
    ax.set_title(f"{title} ({location})")

    main_legend = ax.legend(fontsize=7, ncol=2, loc="upper left")
    ax.add_artist(main_legend)
    # Only include a hatch-legend entry for a status if at least one plotted
    # bar actually used it — otherwise the legend shows a pattern nothing in
    # the chart has, which is confusing rather than informative.
    hatch_options = {
        "AUDIT-FAIL": Patch(facecolor="white", edgecolor="darkred", hatch="//", label="AUDIT-FAIL"),
        "DATA-GAP": Patch(facecolor="white", edgecolor="dimgray", hatch="xx", label="DATA-GAP"),
    }
    hatch_legend = [h for status, h in hatch_options.items() if status in statuses_seen]
    if hatch_legend:
        ax.legend(handles=hatch_legend, fontsize=7, loc="upper right", title="Hatched = unreliable")

    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    print(f"Saved: {outfile}")



def print_peak_table(rows):
    """Worst-case peak (not average) resource utilization, local runs only.
    Shows every run regardless of status — the AUDIT-FAIL configurations are
    exactly the ones whose peaks explain why they failed."""
    print("=" * 110)
    print("PEAK RESOURCE UTILIZATION (local runs only — worst single sample per configuration)")
    print("=" * 110)
    header = (f"{'cores':>5} {'variant':>8} {'load':>7} {'status':>10} {'peak_cpu_usr%':>13} "
              f"{'peak_cpu_sys%':>13} {'peak_mem%':>9} {'peak_disk%':>10}")
    print(header)
    print("-" * len(header))
    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                cell = get_cell(rows, cores, variant, load, "local")
                if cell is None:
                    continue
                src = cell["rows"]
                peak_cpu_u = max((r["peak_cpu_user_pct"] for r in src if r["peak_cpu_user_pct"] is not None), default=None)
                peak_cpu_s = max((r["peak_cpu_system_pct"] for r in src if r["peak_cpu_system_pct"] is not None), default=None)
                peak_mem = max((r["peak_mem_used_pct"] for r in src if r["peak_mem_used_pct"] is not None), default=None)
                peak_disk = max((r["peak_disk_util_pct"] for r in src if r["peak_disk_util_pct"] is not None), default=None)
                print(f"{cores:5d} {variant:>8} {load:>7} {cell['status']:>10} {fmt(peak_cpu_u,13)} "
                      f"{fmt(peak_cpu_s,13)} {fmt(peak_mem,9)} {fmt(peak_disk,10)}")
    print()


def print_variability_table(rows, metric, label, location):
    """Mean, median, and range (min-max) across repeated runs for one metric —
    matches Section 5.3's stated methodology (mean, median, range)."""
    print("=" * 100)
    print(f"CROSS-RUN VARIABILITY — {label} ({location})")
    print("=" * 100)
    header = f"{'cores':>5} {'variant':>8} {'load':>7} {'status':>10} {'n':>3} {'mean':>8} {'median':>8} {'min':>8} {'max':>8}"
    print(header)
    print("-" * len(header))
    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                cell = get_cell(rows, cores, variant, load, location)
                if cell is None:
                    continue
                vals = [r[metric] for r in cell["rows"] if r[metric] is not None]
                if not vals:
                    continue
                print(f"{cores:5d} {variant:>8} {load:>7} {cell['status']:>10} {len(vals):3d} "
                      f"{fmt(statistics.mean(vals),8)} {fmt(statistics.median(vals),8)} "
                      f"{fmt(min(vals),8)} {fmt(max(vals),8)}")
    print()


def print_effect_size_table(rows, metric, label):
    """Cohen's d comparing baseline vs sev for one metric, per (cores, load, location),
    using each individual CLEAN run as one data point. Needs >=2 clean runs on both
    sides to compute — otherwise reports 'n/a'."""
    print("=" * 100)
    print(f"EFFECT SIZE (Cohen's d) — baseline vs sev, {label}")
    print("=" * 100)
    print("|d| < 0.2 negligible | 0.2-0.5 small | 0.5-0.8 medium | > 0.8 large")
    print("Positive d = baseline higher than sev. Only computed from CLEAN runs.")
    print()
    header = f"{'cores':>5} {'load':>7} {'location':>8} {'n_base':>6} {'n_sev':>6} {'cohens_d':>16}"
    print(header)
    print("-" * len(header))
    for cores in CORES:
        for load in LOADS:
            for location in LOCATIONS:
                b_cell = get_cell(rows, cores, "baseline", load, location)
                s_cell = get_cell(rows, cores, "sev", load, location)
                if b_cell is None or s_cell is None:
                    continue
                b_vals = [r[metric] for r in b_cell["rows"] if r["status"] == "CLEAN" and r[metric] is not None]
                s_vals = [r[metric] for r in s_cell["rows"] if r["status"] == "CLEAN" and r[metric] is not None]
                d = cohens_d(b_vals, s_vals)
                if not b_vals and not s_vals:
                    note = "  (no CLEAN runs on either side — audit failed every time)"
                elif not b_vals:
                    note = "  (no CLEAN baseline runs — audit failed every time)"
                elif not s_vals:
                    note = "  (no CLEAN sev runs — audit failed every time)"
                else:
                    note = ""
                print(f"{cores:5d} {load:>7} {location:>8} {len(b_vals):6d} {len(s_vals):6d} "
                      f"{interpret_d(d):>16}{note}")
    print()


def parse_sar_timeseries(sar_txt_path):
    """Parses every individual sample row (not just the Average summary) into
    a time series: elapsed minutes since the first sample, alongside CPU
    user/system %, memory %, and disk %util at each point.
    This is genuine data — sar sampled every 5 seconds throughout the run,
    so we can see how utilization actually evolved, not just its average."""
    samples = []  # list of (elapsed_min, cpu_user, cpu_system, mem_pct, disk_pct)
    pending = {}
    first_time = None
    time_re = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(\S+)\s")
    with open(sar_txt_path) as f:
        for line in f:
            m = time_re.match(line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), "%H:%M:%S")
            label = m.group(2)
            parts = line.split()
            try:
                if label == "all":
                    pending["t"] = t
                    pending["cpu_user"] = float(parts[2])
                    pending["cpu_system"] = float(parts[4])
                elif label in ("sda", "nvme0n1"):
                    pending["disk"] = float(parts[9])
                else:
                    float(label)  # memory row check
                    pending["mem"] = float(parts[4])
            except (ValueError, IndexError):
                continue
            # Once we have a CPU sample with a timestamp, record a point
            # (memory/disk update independently at their own sample times,
            # carried forward from whatever was last seen).
            if "t" in pending and "cpu_user" in pending:
                if first_time is None:
                    first_time = pending["t"]
                elapsed_min = (pending["t"] - first_time).total_seconds() / 60
                if elapsed_min < 0:
                    elapsed_min += 24 * 60  # midnight rollover
                samples.append((
                    elapsed_min, pending["cpu_user"], pending["cpu_system"],
                    pending.get("mem"), pending.get("disk"),
                ))
    return samples


def plot_utilization_over_time(sar_txt_path, title, outfile):
    """Plots CPU user/system % over elapsed time for one specific run.
    Directly shows whether utilization is a sustained plateau or a series of
    spikes — the average alone can't distinguish these, but this can."""
    samples = parse_sar_timeseries(sar_txt_path)
    if not samples:
        print(f"No sar samples found in {sar_txt_path}, skipping.")
        return

    elapsed = [s[0] for s in samples]
    cpu_user = [s[1] for s in samples]
    cpu_system = [s[2] for s in samples]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(elapsed, cpu_user, label="CPU user %", color="#1f77b4")
    ax.plot(elapsed, cpu_system, label="CPU system %", color="#d62728")
    ax.set_xlabel("Elapsed time (minutes)")
    ax.set_ylabel("CPU %")
    ax.set_ylim(0, 100)
    ax.set_title(title)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    print(f"Saved: {outfile}")


def plot_utilization_comparison(run_dir, cores, load, location, outfile):
    """Overlays baseline vs sev CPU user % as two lines on the same axes, for
    one (cores, load, location) configuration. Direct visual comparison
    instead of two separate images — makes it immediately visible whether
    the two variants track each other closely or diverge over the run."""
    paths_base = find_paths(run_dir, "baseline", cores, location, load)
    paths_sev = find_paths(run_dir, "sev", cores, location, load)
    if paths_base["sar"] is None or paths_sev["sar"] is None:
        print(f"Missing sar file for {cores}-core {load} {location}, skipping comparison plot.")
        return

    samples_base = parse_sar_timeseries(paths_base["sar"])
    samples_sev = parse_sar_timeseries(paths_sev["sar"])
    if not samples_base or not samples_sev:
        print(f"No sar samples for {cores}-core {load} {location}, skipping comparison plot.")
        return

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot([s[0] for s in samples_base], [s[1] for s in samples_base],
            label="baseline — CPU user %", color="#1f77b4")
    ax.plot([s[0] for s in samples_sev], [s[1] for s in samples_sev],
            label="sev — CPU user %", color="#ff7f0e")
    ax.set_xlabel("Elapsed time (minutes)")
    ax.set_ylabel("CPU user %")
    ax.set_ylim(0, 100)
    ax.set_title(f"CPU Utilization Over Time — {cores}-Core, {load.capitalize()} Load ({location.capitalize()})")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    print(f"Saved: {outfile}")


def plot_cpu_vs_latency(rows, outfile="cpu_vs_latency.png"):
    """Scatter plot: local CPU utilization vs local short-read p99 latency,
    one point per (cores, variant, load) configuration. Directly shows
    whether higher CPU utilization correlates with higher latency — this is
    the RQ4 question (does resource utilization explain the overhead/failures)
    made visible in one picture. Point shape marks audit status."""
    fig, ax = plt.subplots(figsize=(8, 6))
    markers = {"CLEAN": "o", "AUDIT-FAIL": "X", "DATA-GAP": "s"}
    colors = {"baseline": "#1f77b4", "sev": "#ff7f0e"}
    plotted_labels = set()
    statuses_seen = set()

    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                cell = get_cell(rows, cores, variant, load, "local")
                if cell is None:
                    continue
                src = cell["rows"]
                cpu = mean([r["cpu_user_pct"] for r in src])
                lat = mean([r["short_read_p99"] for r in src])
                if cpu is None or lat is None:
                    continue
                status = cell["status"]
                base_status = status.split("(")[0]
                statuses_seen.add(base_status)
                marker = markers.get(base_status, "o")
                label = variant if variant not in plotted_labels else None
                plotted_labels.add(variant)
                ax.scatter(cpu, lat, color=colors[variant], marker=marker, s=70,
                           edgecolor="black", linewidth=0.5, label=label)

    ax.set_xlabel("CPU user % (local, mean)")
    ax.set_ylabel("Short-read p99 latency (ms, local, mean)")
    ax.set_title("CPU Utilization vs. Short-Read p99 Latency (Local Execution)")
    marker_options = {
        "CLEAN": plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="gray", markersize=9, label="CLEAN"),
        "AUDIT-FAIL": plt.Line2D([0], [0], marker="X", color="w", markerfacecolor="gray", markersize=9, label="AUDIT-FAIL"),
        "DATA-GAP": plt.Line2D([0], [0], marker="s", color="w", markerfacecolor="gray", markersize=9, label="DATA-GAP"),
    }
    marker_legend = [h for status, h in marker_options.items() if status in statuses_seen]
    color_legend = ax.legend(loc="upper left", fontsize=8, title="Variant")
    ax.add_artist(color_legend)
    if marker_legend:
        ax.legend(handles=marker_legend, loc="lower right", fontsize=8, title="Status (shape)")
    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    print(f"Saved: {outfile}")


def plot_local_vs_remote(rows, metric, ylabel, title, outfile, load="high"):
    """Grouped bar chart comparing local vs remote directly, for one load
    level, one bar pair per variant per core count. This is the RQ3 answer
    made visible: the latency/throughput tradeoff between the two locations."""
    fig, ax = plt.subplots(figsize=(9, 5))
    bar_width = 0.18
    combos = [(v, loc) for v in VARIANTS for loc in LOCATIONS]
    x_base = range(len(CORES))
    statuses_seen = set()

    for i, (variant, location) in enumerate(combos):
        values, statuses = [], []
        for cores in CORES:
            cell = get_cell(rows, cores, variant, load, location)
            if cell is None:
                values.append(None)
                statuses.append(None)
                continue
            values.append(mean([r[metric] for r in cell["rows"]]))
            statuses.append(cell["status"])

        offset = (i - len(combos) / 2) * bar_width
        positions = [x + offset for x in x_base]
        color = "#1f77b4" if variant == "baseline" else "#ff7f0e"
        alpha = 0.5 if location == "local" else 1.0

        plot_positions = [p for p, v in zip(positions, values) if v is not None]
        plot_values = [v for v in values if v is not None]
        plot_statuses = [s for v, s in zip(values, statuses) if v is not None]

        bars = ax.bar(plot_positions, plot_values, width=bar_width, color=color,
                       alpha=alpha, label=f"{variant}-{location}")
        for patch, status in zip(bars.patches, plot_statuses):
            base_status = status.split("(")[0]
            statuses_seen.add(base_status)
            if base_status == "AUDIT-FAIL":
                patch.set_hatch("//")
                patch.set_edgecolor("darkred")
                patch.set_linewidth(1)
            elif base_status == "DATA-GAP":
                patch.set_hatch("xx")
                patch.set_edgecolor("dimgray")
                patch.set_linewidth(1)

    ax.set_xticks(list(x_base))
    ax.set_xticklabels([str(c) for c in CORES])
    ax.set_xlabel("Core count")
    ax.set_ylabel(ylabel)
    ax.set_title(f"{title} — {load} load")

    # Built explicitly rather than from ax.legend(handles=bars): if we let
    # matplotlib pick a representative bar patch per label, it may grab one
    # that happens to be hatched (AUDIT-FAIL) while most bars in that series
    # aren't, producing a legend swatch that doesn't match most of the bars.
    color_legend_handles = [
        Patch(facecolor=color, alpha=alpha, edgecolor="black", label=f"{variant}-{location}")
        for variant, color in (("baseline", "#1f77b4"), ("sev", "#ff7f0e"))
        for location, alpha in (("local", 0.5), ("remote", 1.0))
    ]
    main_legend = ax.legend(handles=color_legend_handles, fontsize=8, ncol=2, loc="upper left")
    ax.add_artist(main_legend)
    # Only show a hatch-legend entry for a status if it actually occurs among
    # the plotted bars for this specific chart (load level).
    hatch_options = {
        "AUDIT-FAIL": Patch(facecolor="white", edgecolor="darkred", hatch="//", label="AUDIT-FAIL"),
        "DATA-GAP": Patch(facecolor="white", edgecolor="dimgray", hatch="xx", label="DATA-GAP"),
    }
    hatch_legend = [h for status, h in hatch_options.items() if status in statuses_seen]
    if hatch_legend:
        ax.legend(handles=hatch_legend, fontsize=7, loc="upper right", title="Hatched = unreliable")

    fig.tight_layout()
    fig.savefig(outfile, dpi=150)
    plt.close(fig)
    print(f"Saved: {outfile}")


def print_category_latency_table(rows):
    """Prints short/complex/update p99 per configuration so these values are
    actually visible for manual sanity-checking, not just consumed silently
    by the charts."""
    print("=" * 100)
    print("CATEGORY LATENCY (p99, ms) — per LDBC operation category")
    print("=" * 100)
    header = (f"{'cores':>5} {'variant':>8} {'load':>7} {'location':>8} {'status':>10} "
              f"{'short_p99':>10} {'complex_p99':>12} {'update_p99':>11}")
    print(header)
    print("-" * len(header))
    for cores in CORES:
        for variant in VARIANTS:
            for load in LOADS:
                for location in LOCATIONS:
                    cell = get_cell(rows, cores, variant, load, location)
                    if cell is None:
                        continue
                    src = cell["rows"]
                    print(f"{cores:5d} {variant:>8} {load:>7} {location:>8} {cell['status']:>10} "
                          f"{fmt(mean([r['short_read_p99'] for r in src]),10)} "
                          f"{fmt(mean([r['complex_read_p99'] for r in src]),12)} "
                          f"{fmt(mean([r['update_p99'] for r in src]),11)}")
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_results.py <run1_dir> [<run2_dir> ...]")
        sys.exit(1)

    run_dirs = sys.argv[1:]
    rows = load_all_rows(run_dirs)

    print_master_table(rows)
    render_master_table_image(rows)
    print_peak_table(rows)
    print_category_latency_table(rows)

    # Matches Section 5.3's stated methodology (mean, median, range) and
    # Section 7.1's Cohen's d claim on CPU system time.
    print_variability_table(rows, "throughput", "Throughput (ops/sec)", location="remote")
    print_variability_table(rows, "cpu_system_pct", "CPU system %", location="local")
    print_effect_size_table(rows, "throughput", "throughput (remote)")
    print_effect_size_table(rows, "cpu_system_pct", "CPU system % (local)")

    # No fabricated overall p95/p99 — the driver only reports percentiles per
    # query type, and blending 26 different operation types into one number
    # isn't a real percentile. This exports every query type's own numbers.
    export_per_query_percentiles(run_dirs)

    plot_grouped_bars(rows, "throughput", "Throughput (ops/sec)",
                       "Throughput by core count", "throughput_by_cores_remote.png", location="remote")
    plot_grouped_bars(rows, "throughput", "Throughput (ops/sec)",
                       "Throughput by core count", "throughput_by_cores_local.png", location="local")
    plot_grouped_bars(rows, "cpu_user_pct", "CPU user %",
                       "CPU Utilization by Core Count",
                       "cpu_by_cores_local.png", location="local")

    # RQ1/RQ2 — latency by LDBC's own operation categories (not blended across
    # incompatible types), baseline vs sev, across core counts and loads.
    plot_grouped_bars(rows, "short_read_p99", "Short-read p99 (ms)",
                       "Short-read latency by core count", "latency_short_read_remote.png", location="remote")
    plot_grouped_bars(rows, "complex_read_p99", "Complex-read p99 (ms)",
                       "Complex-read latency by core count", "latency_complex_read_remote.png", location="remote")
    plot_grouped_bars(rows, "update_p99", "Update p99 (ms)",
                       "Update latency by core count", "latency_update_remote.png", location="remote")

    # RQ4 — does CPU utilization actually explain the latency/failures?
    plot_cpu_vs_latency(rows)

    # RQ3 — local vs remote, directly compared, at high load (where it matters most)
    plot_local_vs_remote(rows, "throughput", "Throughput (ops/sec)",
                          "Local vs remote throughput", "local_vs_remote_throughput.png", load="high")
    plot_local_vs_remote(rows, "short_read_p99", "Short-read p99 (ms)",
                          "Local vs remote latency", "local_vs_remote_latency.png", load="high")

    # Overlapping baseline-vs-sev CPU utilization over time, at 2-core (where
    # the contrast is clearest), for both a passing (low) and failing (high)
    # load level — symmetric coverage, one figure per load level, each
    # showing both variants directly overlaid for comparison.
    plot_utilization_comparison(run_dirs[0], 2, "low", "local",
                                 "utilization_over_time_2core_low_local.png")
    plot_utilization_comparison(run_dirs[0], 2, "high", "local",
                                 "utilization_over_time_2core_high_local.png")


if __name__ == "__main__":
    main()
