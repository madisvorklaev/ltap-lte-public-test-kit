#!/usr/bin/env python3
"""Generate RESULTS_INDEX.md from results/*/summary.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def compact_bands(values: list[str] | None) -> str:
    if not values:
        return ""
    out: list[str] = []
    for value in values:
        first = value.split()[0] if value else ""
        if first and first not in out:
            out.append(first)
    return ",".join(out)


def main() -> int:
    rows: list[dict[str, Any]] = []
    results_dir = ROOT / "results"
    if not results_dir.exists():
        results_dir.mkdir()

    for result_dir in sorted(results_dir.iterdir()):
        if not result_dir.is_dir():
            continue
        summary = load_json(result_dir / "summary.json")
        if not summary:
            continue

        test = summary.get("test") or {}
        iperf = summary.get("iperf") or {}
        ping = summary.get("ping") or {}
        radio = summary.get("radio_target") or {}

        rows.append(
            {
                "folder": result_dir.name,
                "tag": test.get("tag"),
                "path": test.get("path"),
                "duration": test.get("duration_s"),
                "port": test.get("server_port"),
                "rx_mbps": iperf.get("receiver_mbps")
                if iperf.get("receiver_mbps") is not None
                else iperf.get("mbps"),
                "loss": iperf.get("lost_percent"),
                "jitter": iperf.get("jitter_ms"),
                "ping_avg": ping.get("avg_ms"),
                "ping_p95": ping.get("p95_ms"),
                "ping_loss": ping.get("loss_percent"),
                "verdict": summary.get("path_verification"),
                "bands": compact_bands(radio.get("primary_bands_seen")),
                "ca": compact_bands(radio.get("ca_bands_seen")),
            }
        )

    lines = [
        "# LtAP Result Index",
        "",
        "Generated from committed `results/*/summary.json` files. Raw data remains in each result folder.",
        "",
        "| Folder | Tag | Path | Duration | Port | Rx Mbps | UDP loss % | Jitter ms | Ping avg/p95/loss | Verdict | Bands | CA |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        ping = f"{fmt(row['ping_avg'])}/{fmt(row['ping_p95'])}/{fmt(row['ping_loss'])}%"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`results/{row['folder']}`",
                    fmt(row["tag"]),
                    fmt(row["path"]),
                    fmt(row["duration"]),
                    fmt(row["port"]),
                    fmt(row["rx_mbps"]),
                    fmt(row["loss"]),
                    fmt(row["jitter"]),
                    ping,
                    fmt(row["verdict"]),
                    fmt(row["bands"]),
                    fmt(row["ca"]),
                ]
            )
            + " |"
        )
    lines.extend(["", f"Total result folders indexed: {len(rows)}"])
    (ROOT / "RESULTS_INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

