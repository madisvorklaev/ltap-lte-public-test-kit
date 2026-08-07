#!/usr/bin/env python3
"""Generate a compact sanitized overnight report from public summary files."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

PUBLIC_ROOT = Path("results-public/overnight-2026-08-07-7.24rc3")


def fnum(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def med(values: list[float]) -> float | None:
    return median(values) if values else None


def main() -> int:
    rows: list[dict[str, str]] = []
    summary_path = PUBLIC_ROOT / "overnight_summary.csv"
    if summary_path.exists():
        rows = list(csv.DictReader(summary_path.open(encoding="utf-8")))

    status: dict[str, Any] = {}
    status_path = PUBLIC_ROOT / "RUN_STATUS.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))

    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("matrix_id", ""), row.get("path", ""))].append(row)

    matrix_summary: dict[str, Any] = {"generated_from_rows": len(rows), "by_matrix_path": []}
    for (matrix_id, path), items in sorted(groups.items()):
        matrix_summary["by_matrix_path"].append(
            {
                "matrix_id": matrix_id,
                "path": path,
                "samples": len(items),
                "median_receiver_mbps": med([x for x in (fnum(i.get("receiver_mbps")) for i in items) if x is not None]),
                "median_udp_loss_percent": med([x for x in (fnum(i.get("udp_loss_percent")) for i in items) if x is not None]),
                "median_ping_p95_ms": med([x for x in (fnum(i.get("ping_p95_ms")) for i in items) if x is not None]),
                "verdicts": sorted(set(i.get("dual_path_verification", "") for i in items)),
            }
        )
    (PUBLIC_ROOT / "matrix_summary.json").write_text(json.dumps(matrix_summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Overnight LtAP LTE7 Band Matrix Report",
        "",
        f"Status: `{status.get('state', 'unknown')}`",
        f"Last completed: `{status.get('last_completed_configuration', '')}`",
        f"Bands restored: `{status.get('bands_restored', '')}`",
        "",
        "## Median Results By Matrix And Path",
        "",
        "| Matrix | Path | Samples | Rx Mbps | UDP loss % | Ping p95 ms | Verdicts |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in matrix_summary["by_matrix_path"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["matrix_id"],
                    item["path"],
                    str(item["samples"]),
                    fmt(item["median_receiver_mbps"]),
                    fmt(item["median_udp_loss_percent"]),
                    fmt(item["median_ping_p95_ms"]),
                    ",".join(item["verdicts"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Raw local artifacts remain under `results-raw/` on the test machine. This public report is generated from sanitized `results-public/` artifacts.",
        ]
    )
    (PUBLIC_ROOT / "OVERNIGHT_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
