#!/usr/bin/env python3
"""Offline analysis for one coordinated dual-LTE run directory."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from statistics import median
from typing import Any

import ltap_public_test as base

VERSION = "0.1.0"


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def norm_band(value: Any) -> str:
    m = re.search(r"\bB?([0-9]{1,3})\b", str(value or ""))
    return f"B{m.group(1)}" if m else ""


def ping_rtts(path: Path) -> list[float]:
    if not path.exists():
        return []
    return [float(x) for x in re.findall(r"time=([0-9.]+)\s*ms", path.read_text(encoding="utf-8", errors="replace"))]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[idx]


def numeric_samples(rows: list[dict[str, Any]], iface: str, key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        lte = (((row.get("interfaces") or {}).get(iface) or {}).get("lte") or {})
        val = base.numeric_radio(lte.get(key))
        if val is not None:
            out.append(val)
    return out


def events_v2(rows: list[dict[str, Any]], iface: str, path_name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    prev: dict[str, Any] = {}
    low_count = 0
    high_count = 0
    low_active = False

    def add(ts: str, event: str, **fields: Any) -> None:
        events.append({"timestamp_utc": ts, "path": path_name, "interface": iface, "event": event, **fields})

    for row in rows:
        ts = row.get("timestamp_utc")
        lte = (((row.get("interfaces") or {}).get(iface) or {}).get("lte") or {})
        if not ts or not lte:
            continue
        current = {
            "primary": norm_band(lte.get("primary-band")),
            "ca": norm_band(lte.get("ca-band")),
            "cell": lte.get("cell-id") or lte.get("phy-cellid") or "",
            "status": lte.get("status") or "",
        }
        if prev:
            if current["primary"] != prev.get("primary"):
                add(ts, "primary_band_changed", old=prev.get("primary"), new=current["primary"])
            if current["ca"] and not prev.get("ca"):
                add(ts, "ca_appeared", ca_band=current["ca"])
            if prev.get("ca") and not current["ca"]:
                add(ts, "ca_disappeared", old=prev.get("ca"))
            if current["ca"] and prev.get("ca") and current["ca"] != prev.get("ca"):
                add(ts, "ca_band_changed", old=prev.get("ca"), new=current["ca"])
            if current["cell"] and prev.get("cell") and current["cell"] != prev.get("cell"):
                add(ts, "cell_changed", old=prev.get("cell"), new=current["cell"])
            if current["status"] != prev.get("status"):
                event = "lte_registration_recovered" if current["status"] == "registered" else "lte_registration_changed"
                add(ts, event, old=prev.get("status"), new=current["status"])
        prev = current

        sinr = base.numeric_radio(lte.get("sinr"))
        if sinr is None:
            continue
        if sinr < 3:
            low_count += 1
            high_count = 0
        elif sinr > 5:
            high_count += 1
            low_count = 0
        if not low_active and low_count >= 3:
            low_active = True
            add(ts, "sinr_low_started", sinr=sinr)
        if low_active and high_count >= 3:
            low_active = False
            add(ts, "sinr_low_ended", sinr=sinr)
    return events


def summarize_path(run_dir: Path, path_name: str, iface: str, summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    rtts = ping_rtts(run_dir / f"{path_name}_ping.txt")
    states: dict[str, int] = {}
    ca_states: set[str] = set()
    for row in rows:
        lte = (((row.get("interfaces") or {}).get(iface) or {}).get("lte") or {})
        band = norm_band(lte.get("primary-band")) or "unknown"
        states[band] = states.get(band, 0) + 1
        ca = norm_band(lte.get("ca-band"))
        if ca:
            ca_states.add(ca)

    radio = (summary.get("radio") or {}).get(path_name) or {}
    iperf = (summary.get("iperf") or {}).get(path_name) or {}
    ping = (summary.get("ping") or {}).get(path_name) or {}
    return {
        "path": path_name,
        "telemetry_sample_count": len(rows),
        "radio_states": [{"primary_band": k, "sample_count": v, "approx_dwell_s": v} for k, v in sorted(states.items())],
        "ca_bands_seen": sorted(ca_states),
        "ping_reply_count": len(rtts),
        "ping_median_ms": median(rtts) if rtts else None,
        "ping_p95_ms": percentile(rtts, 95),
        "ping_max_ms": max(rtts) if rtts else None,
        "pct_rtt_gt_100ms": 100 * sum(1 for x in rtts if x > 100) / len(rtts) if rtts else None,
        "pct_rtt_gt_500ms": 100 * sum(1 for x in rtts if x > 500) / len(rtts) if rtts else None,
        "pct_rtt_gt_1000ms": 100 * sum(1 for x in rtts if x > 1000) / len(rtts) if rtts else None,
        "ping_loss_percent": ping.get("loss_percent"),
        "udp_receiver_mbps": iperf.get("mbps"),
        "udp_loss_percent": iperf.get("lost_percent"),
        "udp_jitter_ms": iperf.get("jitter_ms"),
        "rsrp_median": radio.get("rsrp_median"),
        "rsrq_median": radio.get("rsrq_median"),
        "sinr_median": radio.get("sinr_median"),
        "cqi_median": radio.get("cqi_median"),
        "ri_median": radio.get("ri_median"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()

    run_dir = args.run_dir
    summary = load_json(run_dir / "group_summary.json")
    rows = base.read_telemetry(run_dir / "telemetry.jsonl")
    paths = ((summary.get("test") or {}).get("paths") or {})
    out = {
        "analyzer_version": VERSION,
        "paths": {
            name: summarize_path(run_dir, name, cfg.get("lte_interface", name), summary, rows)
            for name, cfg in paths.items()
        },
    }
    (run_dir / "radio_state_summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with (run_dir / "radio_state_summary.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["path", "udp_receiver_mbps", "udp_loss_percent", "udp_jitter_ms", "ping_median_ms", "ping_p95_ms", "ping_max_ms", "ping_loss_percent", "pct_rtt_gt_100ms", "pct_rtt_gt_500ms", "pct_rtt_gt_1000ms", "rsrp_median", "rsrq_median", "sinr_median", "cqi_median", "ri_median", "ca_bands_seen"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in out["paths"].values():
            row = {k: item.get(k) for k in fields}
            row["ca_bands_seen"] = "|".join(item.get("ca_bands_seen") or [])
            writer.writerow(row)

    all_events: list[dict[str, Any]] = []
    for name, cfg in paths.items():
        all_events.extend(events_v2(rows, cfg.get("lte_interface", name), name))
    with (run_dir / "events_v2.jsonl").open("w", encoding="utf-8") as f:
        for event in all_events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
