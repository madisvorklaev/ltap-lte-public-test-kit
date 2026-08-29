#!/usr/bin/env python3
"""Run one condition block of ELMO Experiment 2b stationary LTE tests."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import ltap_public_test as base

VERSION = "0.1.0"
DEFAULT_CAMPAIGN_DIR = Path("results/exp2b-stationary-20260813")


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_cmd(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(cmd), flush=True)
    cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if cp.stdout:
        print(cp.stdout)
    if cp.stderr:
        print(cp.stderr, file=sys.stderr)
    return cp


def git_sha() -> str:
    cp = run_cmd(["git", "rev-parse", "HEAD"])
    return cp.stdout.strip() if cp.returncode == 0 else "unknown"


def idle_sample(cfg: dict[str, Any], out_dir: Path, condition: str, seconds: int) -> None:
    router = base.RouterSSH(cfg["router"])
    try:
        interfaces = [p["lte_interface"] for p in cfg["paths"].values()]
        stop = threading.Event()
        t = threading.Thread(
            target=base.telemetry_loop,
            args=(router, interfaces, 1.0, out_dir / f"idle_{condition}.telemetry.jsonl", stop, base.EXP2B_QUEUE_NAMES, True),
            daemon=True,
        )
        t.start()
        time.sleep(seconds)
        stop.set()
        t.join(timeout=10)
    finally:
        router.close()


def run_single(condition: str, repeat: int, path: str, bitrate: str, duration: int, campaign_file: str, out_dir: Path) -> dict[str, Any]:
    tag = f"exp2b_{condition}_{path}_{bitrate}_r{repeat}"
    cmd = [
        "python3", "ltap_public_test.py",
        "--config", "config.json",
        "run",
        "--campaign", campaign_file,
        "--path", path,
        "--protocol", "udp",
        "--bitrate", bitrate,
        "--packet-length", "1200",
        "--duration", str(duration),
        "--warmup", "5",
        "--cooldown", "10",
        "--telemetry-interval", "1",
        "--ping-interval", "0.2",
        "--tag", tag,
        "--output", str(out_dir / "runs"),
        "--exp2b-condition", condition,
    ]
    before = set(p for p in (out_dir / "runs").glob("*") if p.is_dir())
    cp = run_cmd(cmd, timeout=duration + 120)
    after = set(p for p in (out_dir / "runs").glob("*") if p.is_dir())
    new_dirs = sorted(after - before, key=lambda p: p.stat().st_mtime)
    rc = cp.returncode
    if new_dirs:
        try:
            summary = json.loads((new_dirs[-1] / "summary.json").read_text(encoding="utf-8"))
            if summary.get("path_verification") != "PASS" or (summary.get("iperf") or {}).get("error"):
                rc = rc or 3
        except Exception:
            rc = rc or 3
    return {"condition": condition, "repeat": repeat, "kind": "single", "path": path, "bitrate": bitrate, "duration_s": duration, "rc": rc, "run_dir": str(new_dirs[-1]) if new_dirs else None}


def run_dual(condition: str, repeat: int, bitrate: str, duration: int, out_dir: Path) -> dict[str, Any]:
    group_id = f"exp2b_{condition}_dual_{bitrate}_r{repeat}"
    cmd = [
        "python3", "ltap_dual_run.py",
        "--config", "config.json",
        "--campaign-lte1", "campaign-exp2b-lte1.json",
        "--campaign-lte2", "campaign-exp2b-lte2.json",
        "--group-id", group_id,
        "--output", str(out_dir / "runs"),
        "--bitrate", bitrate,
        "--packet-length", "1200",
        "--duration", str(duration),
        "--preload", "10",
        "--postload", "10",
        "--telemetry-interval", "1",
        "--ping-interval", "0.2",
        "--exp2b-condition", condition,
    ]
    before = set(p for p in (out_dir / "runs").glob("*") if p.is_dir())
    cp = run_cmd(cmd, timeout=duration + 150)
    after = set(p for p in (out_dir / "runs").glob("*") if p.is_dir())
    new_dirs = sorted(after - before, key=lambda p: p.stat().st_mtime)
    rc = cp.returncode
    if new_dirs:
        try:
            summary = json.loads((new_dirs[-1] / "group_summary.json").read_text(encoding="utf-8"))
            if summary.get("group_status") != "PASS_DUAL" or any((summary.get("iperf") or {}).get(p, {}).get("error") for p in ("lte1", "lte2")):
                rc = rc or 3
        except Exception:
            rc = rc or 3
    return {"condition": condition, "repeat": repeat, "kind": "dual", "path": "lte1+lte2", "bitrate": bitrate, "duration_s": duration, "rc": rc, "run_dir": str(new_dirs[-1]) if new_dirs else None}


def collect_manifest(cfg: dict[str, Any], out_dir: Path, condition: str) -> dict[str, Any]:
    campaign_lte1 = base.load_json(Path("campaign-exp2b-lte1.json"))
    campaign_lte2 = base.load_json(Path("campaign-exp2b-lte2.json"))
    router = base.RouterSSH(cfg["router"])
    try:
        metadata = base.collect_router_metadata(router, [p["lte_interface"] for p in cfg["paths"].values()])
        condition_check = base.verify_exp2b_condition(router, condition)
        status, problems = base.exp2b_condition_status(condition_check)
        condition_check["status"] = status
        condition_check["problems"] = problems
    finally:
        router.close()
    manifest = {
        "schema": "elmo-exp2b-stationary-v1",
        "collector_version": base.VERSION,
        "runner_version": VERSION,
        "git_commit": git_sha(),
        "campaign_dir": str(out_dir),
        "condition": condition,
        "created_local": base.now_local(),
        "server": {
            "hostname": campaign_lte1.get("server_hostname"),
            "ipv4": campaign_lte1.get("server_ipv4"),
            "lte1_ports": campaign_lte1.get("ports"),
            "lte2_ports": campaign_lte2.get("ports"),
        },
        "fixed_cap": "5M for B/C only",
        "test_rates": {"single": "6M", "dual_main": "6M per path", "dual_control": "5M per path"},
        "durations": {"single_s": 120, "dual_main_s": 300, "dual_control_s": 300},
        "router_metadata": metadata,
        "exp2b_condition_check": condition_check,
        "physical_notes": "Stationary lab. Antenna/router position must not be moved during A/B/C.",
    }
    save_json(out_dir / "experiment_manifest.json", manifest)
    return manifest


def append_run_rows(out_dir: Path, rows: list[dict[str, Any]]) -> None:
    path = out_dir / "comparison_runs.csv"
    fields = ["condition", "repeat", "kind", "path", "bitrate", "duration_s", "rc", "run_dir"]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def write_report(out_dir: Path, condition: str, rows: list[dict[str, Any]], decision: str | None = None) -> None:
    lines = [
        "# ELMO Experiment 2b Stationary Report",
        "",
        f"Updated: {base.now_local()}",
        f"Latest condition block: {condition}",
        "",
        "This is network queue/latency evidence only. It is not actual production video frame-age evidence.",
        "",
        "## Runs",
        "",
    ]
    for row in rows:
        lines.append(f"- {row['condition']} {row['kind']} {row['path']} {row['bitrate']} r{row['repeat']}: rc={row['rc']} {row.get('run_dir') or ''}")
    if decision:
        lines += ["", "## Baseline Decision", "", decision]
    (out_dir / "EXP2B_STATIONARY_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    save_json(out_dir / "comparison_summary.json", {"updated_local": base.now_local(), "latest_condition": condition, "decision": decision, "runs": rows})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--condition", choices=["A", "B", "C"], required=True)
    ap.add_argument("--output", default=str(DEFAULT_CAMPAIGN_DIR))
    ap.add_argument("--idle-seconds", type=int, default=60)
    ap.add_argument("--rest-seconds", type=int, default=45)
    ap.add_argument("--smoke", action="store_true", help="Use short durations/repeats for validation only")
    args = ap.parse_args()

    out_dir = Path(args.output)
    cfg = base.load_json(Path("config.json"))
    collect_manifest(cfg, out_dir, args.condition)
    pre = run_cmd([
        "python3", "ltap_public_test.py", "--config", "config.json", "preflight",
        "--campaign", "campaign-exp2b-lte1.json", "--exp2b-condition", args.condition, "--output", str(out_dir),
    ], timeout=60)
    if pre.returncode != 0:
        return pre.returncode

    idle_sample(cfg, out_dir, args.condition, min(args.idle_seconds, 5) if args.smoke else args.idle_seconds)

    single_repeats = 1 if args.smoke else 3
    dual_main_repeats = 1 if args.smoke else 3
    dual_control_repeats = 1 if args.smoke else 2
    single_duration = 15 if args.smoke else 120
    dual_duration = 20 if args.smoke else 300
    rest = 2 if args.smoke else args.rest_seconds
    rows: list[dict[str, Any]] = []

    for repeat in range(1, single_repeats + 1):
        rows.append(run_single(args.condition, repeat, "lte1", "6M", single_duration, "campaign-exp2b-lte1.json", out_dir))
        append_run_rows(out_dir, rows[-1:])
        if rows[-1]["rc"] != 0:
            write_report(out_dir, args.condition, rows, "STOPPED_INVALID_RUN")
            return int(rows[-1]["rc"])
        time.sleep(rest)
    for repeat in range(1, single_repeats + 1):
        rows.append(run_single(args.condition, repeat, "lte2", "6M", single_duration, "campaign-exp2b-lte2.json", out_dir))
        append_run_rows(out_dir, rows[-1:])
        if rows[-1]["rc"] != 0:
            write_report(out_dir, args.condition, rows, "STOPPED_INVALID_RUN")
            return int(rows[-1]["rc"])
        time.sleep(rest)
    for repeat in range(1, dual_main_repeats + 1):
        rows.append(run_dual(args.condition, repeat, "6M", dual_duration, out_dir))
        append_run_rows(out_dir, rows[-1:])
        if rows[-1]["rc"] != 0:
            write_report(out_dir, args.condition, rows, "STOPPED_INVALID_RUN")
            return int(rows[-1]["rc"])
        time.sleep(rest)
    for repeat in range(1, dual_control_repeats + 1):
        rows.append(run_dual(args.condition, repeat, "5M", dual_duration, out_dir))
        append_run_rows(out_dir, rows[-1:])
        if rows[-1]["rc"] != 0:
            write_report(out_dir, args.condition, rows, "STOPPED_INVALID_RUN")
            return int(rows[-1]["rc"])
        time.sleep(rest)

    decision = None
    if args.condition == "A":
        decision = "BASELINE_COMPLETE_PENDING_MANUAL_GATE. Review comparison_runs.csv and per-run summaries before importing the 5M PFIFO shaper."
    write_report(out_dir, args.condition, rows, decision)
    return 0 if all(row["rc"] == 0 for row in rows) else 3


if __name__ == "__main__":
    raise SystemExit(main())
