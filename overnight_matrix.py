#!/usr/bin/env python3
"""Run the RouterOS 7.24rc3 dual-LTE overnight band matrix."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import ltap_public_test as base

VERSION = "0.1.0"
PUBLIC_ROOT = Path("results-public/overnight-2026-08-07-7.24rc3")
RAW_ROOT = Path("results-raw/overnight-2026-08-07-7.24rc3")

MATRIX = [
    ("M0", "AUTO-AUTO", "", ""),
    ("M1", "B3-B3", "3", "3"),
    ("M2", "B7-B7", "7", "7"),
    ("M3", "B3-B7", "3", "7"),
    ("M4", "B7-B3", "7", "3"),
    ("M5", "FDD-NO-B38", "1,3,7,8,20,28", "1,3,7,8,20,28"),
    ("M6", "B38-B3", "38", "3"),
    ("M7", "B3-B38", "3", "38"),
    ("M8", "B1-B3", "1", "3"),
    ("M9", "B3-B1", "3", "1"),
]


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(cmd: list[str], timeout: float | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(cmd), flush=True)
    cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if cp.stdout:
        print(cp.stdout)
    if cp.stderr:
        print(cp.stderr, file=sys.stderr)
    if check and cp.returncode != 0:
        raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)
    return cp


def git_sha() -> str:
    return run(["git", "rev-parse", "HEAD"], check=True).stdout.strip()


def router_cmd(router: base.RouterSSH, command: str, timeout: float = 10) -> str:
    cp = router.call(command, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"Router command failed: {command}: {cp.stderr.strip()}")
    return cp.stdout


def get_lte_band(router: base.RouterSSH, iface: str) -> str:
    return router_cmd(router, f':put [/interface/lte/get [find name="{iface}"] band]').strip()


def set_lte_band(router: base.RouterSSH, iface: str, bands: str) -> None:
    escaped = bands.replace('"', '\\"')
    router_cmd(router, f'/interface/lte/set [find name="{iface}"] band="{escaped}"', timeout=20)


def monitor(router: base.RouterSSH, iface: str) -> dict[str, str]:
    return base.parse_lte_monitor(router_cmd(router, f"/interface/lte/monitor {iface} once", timeout=10))


def registered(status: str) -> bool:
    return status.lower() in {"registered", "connected", "link-ok"}


def wait_registered(router: base.RouterSSH, ifaces: list[str], timeout_s: int = 120, stable_s: int = 30) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    stable_since: float | None = None
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = {iface: monitor(router, iface) for iface in ifaces}
        ok = all(registered((last.get(iface) or {}).get("status", "")) for iface in ifaces)
        if ok:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= stable_s:
                return True, last
        else:
            stable_since = None
        time.sleep(5)
    return False, last


def verify_versions(router: base.RouterSSH) -> dict[str, Any]:
    resource = router_cmd(router, "/system/resource/print", timeout=10)
    routerboard = router_cmd(router, "/system/routerboard/print", timeout=10)
    if "7.24rc3" not in resource or "current-firmware: 7.24rc3" not in routerboard:
        raise SystemExit("RouterOS/RouterBOARD firmware is not fixed at 7.24rc3; stopping matrix.")
    return {"resource": resource, "routerboard": routerboard}


def verify_linux(cfg: dict[str, Any], server_ip: str) -> None:
    for name, path in cfg["paths"].items():
        if not base.ip_present(cfg["linux_interface"], path["source_ip"]):
            raise SystemExit(f"{name} source IP missing: {path['source_ip']}")
        cp = run(["ip", "route", "get", server_ip, "from", path["source_ip"]], check=True)
        if "192.168.101.254" not in cp.stdout or cfg["linux_interface"] not in cp.stdout:
            raise SystemExit(f"{name} route does not use LtAP gateway: {cp.stdout.strip()}")


def verify_router_rules(router: base.RouterSSH) -> None:
    checks = {
        "lte1 source": '/ip/firewall/mangle/print detail where comment="ELMO TEST: source via lte1"',
        "lte2 source": '/ip/firewall/mangle/print detail where comment="ELMO TEST: source via lte2"',
        "video lte1": '/ip/firewall/mangle/print detail where comment~"video via lte1"',
        "video lte2": '/ip/firewall/mangle/print detail where comment~"video via lte2"',
        "tables": "/routing/table/print detail",
        "routes": "/ip/route/print detail",
        "nat": "/ip/firewall/nat/print detail",
    }
    observed: dict[str, str] = {}
    for name, command in checks.items():
        observed[name] = router_cmd(router, command, timeout=12)
    text = "\n".join(observed.values())
    required = ["192.168.101.201", "192.168.101.202", "to-lte1", "to-lte2", "lte1", "lte2"]
    missing = [item for item in required if item not in text]
    if missing:
        raise SystemExit(f"Router production/lab checks missing: {missing}")
    save_json(PUBLIC_ROOT / "router_rule_check.json", observed)


def update_status(**fields: Any) -> None:
    current = {}
    path = PUBLIC_ROOT / "RUN_STATUS.json"
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(fields)
    current["updated_utc"] = base.now_utc()
    save_json(path, current)


def append_summary(group_id: str, round_name: str, matrix_id: str, lte1_bands: str, lte2_bands: str, raw_dir: Path) -> None:
    summary = json.loads((raw_dir / "group_summary.json").read_text(encoding="utf-8"))
    analysis = json.loads((raw_dir / "radio_state_summary.json").read_text(encoding="utf-8"))
    path = PUBLIC_ROOT / "overnight_summary.csv"
    fields = [
        "schema_version", "round", "matrix_id", "group_id", "path",
        "requested_lte1_bands", "requested_lte2_bands", "actual_primary_bands_seen",
        "ca_bands_seen", "server_ip", "server_port", "target_mbps",
        "receiver_mbps", "udp_loss_percent", "udp_jitter_ms",
        "ping_median_ms", "ping_p95_ms", "ping_max_ms", "ping_loss_percent",
        "pct_rtt_gt_100ms", "pct_rtt_gt_500ms", "pct_rtt_gt_1000ms",
        "rsrp_median", "rsrq_median", "sinr_median", "cqi_median", "ri_median",
        "source_rule_bytes_delta", "lte_tx_bytes_delta", "dual_path_verification",
        "start_time", "end_time", "git_commit",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for path_name, item in analysis["paths"].items():
            test_path = summary["test"]["paths"][path_name]
            verify = summary["path_verification"][path_name]
            row = {
                "schema_version": 1,
                "round": round_name,
                "matrix_id": matrix_id,
                "group_id": group_id,
                "path": path_name,
                "requested_lte1_bands": lte1_bands or "AUTO",
                "requested_lte2_bands": lte2_bands or "AUTO",
                "actual_primary_bands_seen": "|".join(x["primary_band"] for x in item["radio_states"]),
                "ca_bands_seen": "|".join(item.get("ca_bands_seen") or []),
                "server_ip": summary["test"]["server_ipv4"],
                "server_port": test_path["server_port"],
                "target_mbps": 6,
                "receiver_mbps": item.get("udp_receiver_mbps"),
                "udp_loss_percent": item.get("udp_loss_percent"),
                "udp_jitter_ms": item.get("udp_jitter_ms"),
                "ping_median_ms": item.get("ping_median_ms"),
                "ping_p95_ms": item.get("ping_p95_ms"),
                "ping_max_ms": item.get("ping_max_ms"),
                "ping_loss_percent": item.get("ping_loss_percent"),
                "pct_rtt_gt_100ms": item.get("pct_rtt_gt_100ms"),
                "pct_rtt_gt_500ms": item.get("pct_rtt_gt_500ms"),
                "pct_rtt_gt_1000ms": item.get("pct_rtt_gt_1000ms"),
                "rsrp_median": item.get("rsrp_median"),
                "rsrq_median": item.get("rsrq_median"),
                "sinr_median": item.get("sinr_median"),
                "cqi_median": item.get("cqi_median"),
                "ri_median": item.get("ri_median"),
                "source_rule_bytes_delta": verify.get("source_rule_bytes_delta"),
                "lte_tx_bytes_delta": verify.get("lte_tx_bytes_delta"),
                "dual_path_verification": verify.get("status"),
                "start_time": summary.get("start_utc"),
                "end_time": summary.get("end_utc"),
                "git_commit": git_sha(),
            }
            writer.writerow(row)


def sanitize_and_push(group_id: str, commit_message: str) -> None:
    run(["python3", "scripts/update_results_index.py"], check=True)
    scan = run(["bash", "-lc", "git diff --cached --name-only >/dev/null; true"])
    run(["git", "add", "ltap_dual_run.py", "analyze_run.py", "sanitize_public_results.py", "overnight_matrix.py", "README.md", "RESULTS_INDEX.md", "scripts/update_results_index.py"], check=True)
    run(["git", "add", "results-public"], check=True)
    staged = run(["bash", "-lc", "git diff --cached -U0 | rg -i 'imsi|imei|iccid|uicc|serial-number|software-id|private key' || true"])
    if staged.stdout.strip():
        raise RuntimeError("Sensitive string found in staged diff; refusing to commit.")
    if run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    run(["git", "commit", "-m", commit_message], check=True)
    push = run(["git", "push"], check=False)
    if push.returncode != 0:
        update_status(push_failed_for=group_id, push_stderr=push.stderr[-2000:])


def run_group(group_id: str, round_name: str, matrix_id: str, lte1_bands: str, lte2_bands: str, duration: int, bitrate: str = "6M") -> tuple[str, Path | None]:
    cmd = [
        "python3", "ltap_dual_run.py",
        "--config", "config.json",
        "--campaign-lte1", "campaign-dual-lte1.json",
        "--campaign-lte2", "campaign-dual-lte2.json",
        "--group-id", group_id,
        "--output", str(RAW_ROOT),
        "--bitrate", bitrate,
        "--packet-length", "1200",
        "--duration", str(duration),
        "--preload", "10",
        "--postload", "10",
    ]
    before = set(RAW_ROOT.glob(f"*_{base.slug(group_id)}"))
    cp = run(cmd, timeout=duration + 180, check=False)
    after = set(RAW_ROOT.glob(f"*_{base.slug(group_id)}"))
    new_dirs = sorted(after - before, key=lambda p: p.stat().st_mtime)
    run_dir = new_dirs[-1] if new_dirs else None
    if run_dir:
        run(["python3", "analyze_run.py", str(run_dir)], check=True)
        run(["python3", "sanitize_public_results.py", str(run_dir)], check=True)
        append_summary(group_id, round_name, matrix_id, lte1_bands, lte2_bands, run_dir)
    return ("RUN_OK" if cp.returncode == 0 else f"RUN_RC_{cp.returncode}"), run_dir


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--duration", type=int, default=300)
    ap.add_argument("--smoke-only", action="store_true")
    args = ap.parse_args()

    cfg = base.load_json(Path(args.config))
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    update_status(state="starting", matrix_version=VERSION, git_commit=git_sha())

    router = base.RouterSSH(cfg["router"])
    original: dict[str, str] = {}
    try:
        verify_versions(router)
        original = {"lte1": get_lte_band(router, "lte1"), "lte2": get_lte_band(router, "lte2")}
        save_json(PUBLIC_ROOT / "original_bands.json", original)
        update_status(original_band_settings=original, bands_restored=False)
        verify_router_rules(router)
        campaign = base.load_json(Path("campaign-dual-lte1.json"))
        verify_linux(cfg, campaign["server_ipv4"])

        update_status(state="smoke", next_configuration="SMOKE-2x1M")
        smoke_status, smoke_dir = run_group("SMOKE-2x1M", "smoke", "SMOKE", original["lte1"], original["lte2"], 20, bitrate="1M")
        update_status(last_completed_configuration="SMOKE-2x1M", last_status=smoke_status)
        sanitize_and_push("SMOKE-2x1M", "test: overnight smoke 2x1M")
        if args.smoke_only:
            return 0

        rounds = [("R1", MATRIX), ("R2", list(reversed(MATRIX)))]
        local_hour = dt.datetime.now().astimezone().hour
        if local_hour < 7:
            seed = 20260807
            random.seed(seed)
            third = MATRIX[:]
            random.shuffle(third)
            save_json(PUBLIC_ROOT / "round3_seed.json", {"seed": seed})
            rounds.append(("R3", third))

        completed: list[str] = []
        skipped: list[dict[str, Any]] = []
        for round_name, items in rounds:
            for matrix_id, label, lte1_band, lte2_band in items:
                group_id = f"{round_name}-{matrix_id}-{label}"
                update_status(state="setting_bands", next_configuration=group_id)
                set_lte_band(router, "lte1", lte1_band)
                set_lte_band(router, "lte2", lte2_band)
                ok, snapshot = wait_registered(router, ["lte1", "lte2"])
                save_json(RAW_ROOT / f"{group_id}_registration_snapshot.json", snapshot)
                if not ok:
                    skipped.append({"group_id": group_id, "reason": "SKIP_BAND_UNAVAILABLE", "snapshot": snapshot})
                    update_status(last_completed_configuration=group_id, last_status="SKIP_BAND_UNAVAILABLE", skipped=skipped)
                    sanitize_and_push(group_id, f"test: {group_id} skipped band unavailable")
                    continue
                time.sleep(30)
                update_status(state="running", current_configuration=group_id)
                status, run_dir = run_group(group_id, round_name, matrix_id, lte1_band, lte2_band, args.duration)
                completed.append(group_id)
                update_status(last_completed_configuration=group_id, last_status=status, tests_completed=completed, tests_skipped=skipped, next_configuration=None)
                sanitize_and_push(group_id, f"test: {group_id} 2x6M {args.duration}s")
                time.sleep(45)
        update_status(state="completed", tests_completed=completed, tests_skipped=skipped)
        return 0
    finally:
        if original:
            try:
                set_lte_band(router, "lte1", original["lte1"])
                set_lte_band(router, "lte2", original["lte2"])
                ok, snapshot = wait_registered(router, ["lte1", "lte2"], timeout_s=120, stable_s=10)
                save_json(PUBLIC_ROOT / "final_lte_monitor.json", snapshot)
                verify_router_rules(router)
                update_status(bands_restored=True, final_registration_ok=ok)
            except Exception as exc:
                update_status(state="restoration_failed", bands_restored=False, restoration_error=repr(exc))
            try:
                sanitize_and_push("FINAL", "test: overnight final status")
            except Exception as exc:
                print(f"Final push failed: {exc}", file=sys.stderr)
        router.close()


if __name__ == "__main__":
    raise SystemExit(main())
