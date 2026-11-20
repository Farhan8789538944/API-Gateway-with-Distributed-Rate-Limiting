"""Summarize a benchmark run directory into a Markdown table.

    .\\.venv\\Scripts\\python.exe scripts\\summarize_benchmarks.py load_tests\\results\\<run-id>

Reads <scenario>.json (written by load_tests/locustfile.py) and
<scenario>.docker-stats.tsv, prints Markdown and writes SUMMARY.md next to them.
Successful-response percentiles never include rejected or failed requests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

INTERESTING = ("nginx", "gateway-1", "gateway-2", "product-1", "product-2", "orders", "redis",
               "postgres", "locust")


def stats_line(path: Path) -> str:
    if not path.exists():
        return "n/a"
    parts = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        name = cols[0]
        short = next((s for s in INTERESTING if s in name), None)
        if short:
            parts.append(f"{short} {cols[1]}")
    return ", ".join(parts) or "n/a"


def main(run_dir: str) -> int:
    directory = Path(run_dir)
    meta_path = directory / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
    rows = [
        "| Scenario | Users / wait | Requests | Achieved req/s (all / success) | Success "
        "| 429 quota | 503 capacity | Other errors | Success P50 / P95 / P99 ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    cpu_rows = ["| Scenario | CPU usage sampled mid-run (docker stats) |", "|---|---|"]
    for summary_path in sorted(directory.glob("*.json")):
        if summary_path.name == "meta.json":
            continue
        name = summary_path.stem
        s = json.loads(summary_path.read_text(encoding="utf-8"))
        o = s["outcomes"]
        errors = sum(v for k, v in o.items() if k.startswith("error"))
        lat = s["success_latency_ms"]
        rows.append(
            f"| {name} | {s['users']} / {s['wait_mode']} | {s['total_requests']} | "
            f"{s['achieved_rps_total']} / {s['achieved_rps_success']} | {o.get('success', 0)} | "
            f"{o.get('quota_rejected', 0)} | {o.get('capacity_rejected', 0)} | {errors} | "
            f"{lat['p50']} / {lat['p95']} / {lat['p99']} |"
        )
        cpu_rows.append(f"| {name} | {stats_line(directory / f'{name}.docker-stats.tsv')} |")
    text = "\n".join([
        f"# Benchmark run {meta.get('run_id', directory.name)}",
        "",
        f"- Docker: {meta.get('docker', 'n/a')}",
        f"- Host CPU: {meta.get('host_cpu', 'n/a')}; "
        f"host memory: {meta.get('host_memory_gb', 'n/a')} GB",
        f"- Warm-up {meta.get('warmup_s', '?')} s (discarded), "
        f"measured {meta.get('measured_s', '?')} s",
        "",
        *rows, "", *cpu_rows, "",
    ])
    (directory / "SUMMARY.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
