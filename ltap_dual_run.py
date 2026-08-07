#!/usr/bin/env python3
"""Coordinated dual-LTE run wrapper for source-IP lab mode."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import ltap_public_test as base

VERSION = "0.1.0"


def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def start_ping(source_ip: str, target: str, interval: float, out_dir: Path, name: str) -> subprocess.Popen[str]:
    out = (out_dir / f"{name}_ping.txt").open("w", encoding="utf-8")
    err = (out_dir / f"{name}_ping_stderr.txt").open("w", encoding="utf-8")
    cmd = ["ping", "-n", "-D", "-O", "-I", source_ip, "-i", str(interval), target]
    (out_dir / f"{name}_ping_command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True)
    proc._ltap_files = (out, err)  # type: ignore[attr-defined]
    return proc


def stop_ping(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
    for f in getattr(proc, "_ltap_files", ()):
        try:
            f.close()
        except Exception:
            pass


def load_iperf(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"could not parse iperf JSON: {exc}"}


def choose(campaign_path: Path, source_ip: str) -> tuple[dict[str, Any], int, dict[str, Any]]:
    campaign = base.load_json(campaign_path)
    port, probe = base.choose_port(campaign["server_ipv4"], source_ip, [int(x) for x in campaign["ports"]])
    return campaign, port, probe


def path_verification(
    path_name: str,
    iface_summary: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    iperf_rc: int,
) -> dict[str, Any]:
    source_bytes = base.counter_delta(before, after, "bytes") or 0
    source_packets = base.counter_delta(before, after, "packets") or 0
    lte_tx = iface_summary.get("tx-byte_delta", 0) or 0
    status = "PASS_DUAL"
    if source_bytes < 1_000_000 or source_packets <= 0:
        status = f"FAIL_{path_name.upper()}_SOURCE_RULE"
    elif lte_tx < max(1_000_000, source_bytes * 0.50):
        status = f"FAIL_{path_name.upper()}_INTERFACE"
    elif iperf_rc != 0:
        status = f"FAIL_IPERF_{path_name.upper()}"
    return {
        "status": status,
        "source_rule_bytes_delta": source_bytes,
        "source_rule_packets_delta": source_packets,
        "lte_tx_bytes_delta": lte_tx,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--campaign-lte1", default="campaign-dual-lte1.json")
    ap.add_argument("--campaign-lte2", default="campaign-dual-lte2.json")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--output", default="results-raw/overnight-2026-08-07-7.24rc3")
    ap.add_argument("--bitrate", default="6M")
    ap.add_argument("--packet-length", type=int, default=1200)
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--preload", type=int, default=10)
    ap.add_argument("--postload", type=int, default=10)
    ap.add_argument("--telemetry-interval", type=float, default=1.0)
    ap.add_argument("--ping-interval", type=float, default=0.2)
    ap.add_argument("--require-pass", action="store_true")
    args = ap.parse_args()

    base.require_commands(["ssh", "iperf3", "ping", "ip"])
    cfg = base.load_json(Path(args.config))
    paths = cfg["paths"]
    lte1 = paths["lte1"]
    lte2 = paths["lte2"]
    linux_if = cfg["linux_interface"]
    for p in (lte1, lte2):
        if not base.ip_present(linux_if, p["source_ip"]):
            raise SystemExit(f"Source IP {p['source_ip']} is not assigned to {linux_if}")

    campaign1, port1, probe1 = choose(Path(args.campaign_lte1), lte1["source_ip"])
    campaign2, port2, probe2 = choose(Path(args.campaign_lte2), lte2["source_ip"])
    if campaign1["server_ipv4"] != campaign2["server_ipv4"]:
        raise SystemExit("Dual campaigns must use the same pinned server IPv4")

    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output) / f"{stamp}_{base.slug(args.group_id)}"
    out_dir.mkdir(parents=True, exist_ok=False)

    router = base.RouterSSH(cfg["router"])
    all_lte = [lte1["lte_interface"], lte2["lte_interface"]]
    comments = {
        "lte1": base.source_rule_comment("lte1", lte1),
        "lte2": base.source_rule_comment("lte2", lte2),
    }
    meta = {
        "dual_wrapper_version": VERSION,
        "collector_version": base.VERSION,
        "created_local": base.now_local(),
        "created_utc": base.now_utc(),
        "group_id": args.group_id,
        "server_hostname": campaign1.get("server_hostname"),
        "server_ipv4": campaign1["server_ipv4"],
        "paths": {
            "lte1": {"source_ip": lte1["source_ip"], "lte_interface": lte1["lte_interface"], "server_port": port1, "port_probe": probe1},
            "lte2": {"source_ip": lte2["source_ip"], "lte_interface": lte2["lte_interface"], "server_port": port2, "port_probe": probe2},
        },
        "protocol": "udp",
        "target_bitrate": args.bitrate,
        "packet_length": args.packet_length,
        "duration_s": args.duration,
        "preload_s": args.preload,
        "postload_s": args.postload,
    }
    save_json(out_dir / "test_group.json", meta)
    save_json(out_dir / "router_metadata.json", base.collect_router_metadata(router, all_lte))

    before = {name: base.mangle_rule_counters(router, comments[name]) for name in ("lte1", "lte2")}

    stop = threading.Event()
    telemetry_path = out_dir / "telemetry.jsonl"
    telemetry = threading.Thread(
        target=base.telemetry_loop,
        args=(router, all_lte, args.telemetry_interval, telemetry_path, stop),
        daemon=True,
    )
    telemetry.start()

    ping_target = cfg.get("ping_target", "1.1.1.1")
    pings: list[subprocess.Popen[str]] = []
    procs: dict[str, subprocess.Popen[str]] = {}
    iperf_cmds = {
        "lte1": base.build_iperf(campaign1["server_ipv4"], port1, lte1["source_ip"], "udp", args.duration, args.bitrate, args.packet_length, False),
        "lte2": base.build_iperf(campaign2["server_ipv4"], port2, lte2["source_ip"], "udp", args.duration, args.bitrate, args.packet_length, False),
    }
    rc: dict[str, int] = {"lte1": 99, "lte2": 99}
    start_utc = ""
    end_utc = ""
    try:
        pings.append(start_ping(lte1["source_ip"], ping_target, args.ping_interval, out_dir, "lte1"))
        pings.append(start_ping(lte2["source_ip"], ping_target, args.ping_interval, out_dir, "lte2"))
        time.sleep(args.preload)
        start_utc = base.now_utc()
        for name, cmd in iperf_cmds.items():
            (out_dir / f"{name}_iperf_command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
            stdout = (out_dir / f"{name}_iperf.json").open("w", encoding="utf-8")
            stderr = (out_dir / f"{name}_iperf_stderr.txt").open("w", encoding="utf-8")
            proc = subprocess.Popen(cmd, text=True, stdout=stdout, stderr=stderr)
            proc._ltap_files = (stdout, stderr)  # type: ignore[attr-defined]
            procs[name] = proc
        for name, proc in procs.items():
            rc[name] = proc.wait()
            for f in getattr(proc, "_ltap_files", ()):
                f.close()
        end_utc = base.now_utc()
        time.sleep(args.postload)
    finally:
        for proc in pings:
            stop_ping(proc)
        stop.set()
        telemetry.join(timeout=15)
        after = {name: base.mangle_rule_counters(router, comments[name]) for name in ("lte1", "lte2")}
        router.close()

    for name, value in rc.items():
        (out_dir / f"{name}_iperf_exit_code.txt").write_text(str(value) + "\n", encoding="utf-8")

    telemetry_rows = base.read_telemetry(telemetry_path)
    radio = {
        "lte1": base.summarize_radio(telemetry_rows, lte1["lte_interface"]),
        "lte2": base.summarize_radio(telemetry_rows, lte2["lte_interface"]),
    }
    iperf = {
        "lte1": base.summarize_iperf(load_iperf(out_dir / "lte1_iperf.json"), "udp", False),
        "lte2": base.summarize_iperf(load_iperf(out_dir / "lte2_iperf.json"), "udp", False),
    }
    ping = {
        "lte1": base.parse_ping(out_dir / "lte1_ping.txt"),
        "lte2": base.parse_ping(out_dir / "lte2_ping.txt"),
    }
    verification = {
        "lte1": path_verification("lte1", radio["lte1"], before["lte1"], after["lte1"], rc["lte1"]),
        "lte2": path_verification("lte2", radio["lte2"], before["lte2"], after["lte2"], rc["lte2"]),
    }
    group_status = "PASS_DUAL" if all(v["status"] == "PASS_DUAL" for v in verification.values()) else "FAIL_DUAL"
    summary = {
        "test": meta,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "iperf_exit_codes": rc,
        "iperf": iperf,
        "ping": ping,
        "radio": radio,
        "source_rule": {"before": before, "after": after},
        "path_verification": verification,
        "group_status": group_status,
    }
    save_json(out_dir / "group_summary.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nSaved: {out_dir}")
    if args.require_pass and group_status != "PASS_DUAL":
        return 3
    return 0 if any(value == 0 for value in rc.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
