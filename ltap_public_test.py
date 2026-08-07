#!/usr/bin/env python3
"""
LtAP public-server LTE test collector.

Purpose:
- Use one pinned public iperf3 server for a test campaign.
- Bind generated traffic to a Linux source IP that MikroTik policy-routes to LTE1 or LTE2.
- Collect RouterOS LTE monitor + interface counters continuously during the test.
- Collect a simultaneous path-bound ping.
- Save raw data and a compact summary.

Python dependencies: standard library only.
External commands: ssh, iperf3, ping, ip, getent.

Designed for Linux Mint / Ubuntu-family systems and RouterOS v7.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

VERSION = "0.4.1"

RADIO_KEYS = {
    "status", "model", "revision", "current-operator", "lac", "cell-id",
    "enb-id", "sector-id", "phy-cellid", "access-technology",
    "session-uptime", "primary-band", "ca-band", "rssi", "rsrp", "rsrq",
    "sinr", "cqi", "ri", "earfcn", "uicc", "imsi", "imei",
}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def now_local() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip())
    return s.strip("_") or "test"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(cmd: list[str], timeout: float | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)


def require_commands(names: list[str]) -> None:
    missing = [x for x in names if shutil.which(x) is None]
    if missing:
        raise SystemExit("Missing required commands: " + ", ".join(missing))


def parse_rate(v: str) -> float:
    """Return bits/s from 8M, 8Mbps, 8000K, 1000000."""
    s = v.strip().lower().replace("bps", "")
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmg]?)", s)
    if not m:
        raise ValueError(f"Invalid rate {v!r}")
    n = float(m.group(1))
    mult = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2)]
    return n * mult


def numeric_radio(v: Any) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", str(v))
    return float(m.group(0)) if m else None


class RouterSSH:
    def __init__(self, cfg: dict[str, Any]):
        self.host = cfg["host"]
        self.user = cfg.get("user", "admin")
        self.port = int(cfg.get("port", 22))
        key = cfg.get("ssh_key")
        self.key = os.path.expanduser(key) if key else None
        self.control_path = f"/tmp/ltap-public-test-{os.getpid()}-%C"

    def base(self) -> list[str]:
        cmd = [
            "ssh", "-p", str(self.port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=30",
            "-o", f"ControlPath={self.control_path}",
        ]
        if self.key:
            cmd += ["-i", self.key]
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def call(self, command: str, timeout: float = 7) -> subprocess.CompletedProcess[str]:
        return run(self.base() + [command], timeout=timeout)

    def close(self) -> None:
        try:
            run([
                "ssh", "-O", "exit",
                "-o", f"ControlPath={self.control_path}",
                f"{self.user}@{self.host}",
            ], timeout=2)
        except Exception:
            pass


def parse_lte_monitor(raw: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in raw.splitlines():
        # RouterOS monitor output normally uses "key: value".
        m = re.match(r"\s*([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", line)
        if m:
            data[m.group(1)] = m.group(2)
    return data


def parse_stats(raw: str) -> dict[str, int]:
    data: dict[str, int] = {}
    # stats-detail can wrap. Match a few counters conservatively.
    compact = " ".join(raw.splitlines())
    for key in (
        "rx-byte", "tx-byte", "rx-packet", "tx-packet",
        "fp-rx-byte", "fp-tx-byte", "fp-rx-packet", "fp-tx-packet",
        "tx-queue-drop",
    ):
        m = re.search(rf"(?:^|\s){re.escape(key)}=([0-9 ]+)", compact)
        if m:
            try:
                data[key] = int(m.group(1).replace(" ", ""))
            except ValueError:
                pass
    return data


def parse_counter_lines(raw: str) -> dict[str, int]:
    vals: list[int] = []
    for line in raw.splitlines():
        s = line.strip().replace(" ", "")
        if re.fullmatch(r"[0-9]+", s):
            vals.append(int(s))
    return {"bytes": vals[0], "packets": vals[1]} if len(vals) >= 2 else {}


def source_rule_comment(path_name: str, path_cfg: dict[str, Any]) -> str:
    return str(path_cfg.get("source_rule_comment") or f"ELMO TEST: source via {path_name}")


def mangle_rule_counters(router: RouterSSH, comment: str) -> dict[str, Any]:
    escaped = comment.replace("\\", "\\\\").replace('"', '\\"')
    command = (
        f':put ([/ip/firewall/mangle/get [find comment="{escaped}"] bytes]); '
        f':put ([/ip/firewall/mangle/get [find comment="{escaped}"] packets])'
    )
    try:
        cp = router.call(command, timeout=6)
        out: dict[str, Any] = {
            "comment": comment,
            "rc": cp.returncode,
            "raw": cp.stdout,
        }
        out.update(parse_counter_lines(cp.stdout))
        if cp.stderr.strip():
            out["stderr"] = cp.stderr.strip()
        return out
    except Exception as exc:
        return {"comment": comment, "error": repr(exc)}


def counter_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int | None:
    if key not in before or key not in after:
        return None
    return max(0, int(after[key]) - int(before[key]))


def poll_router(router: RouterSSH, iface: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        lte = router.call(f"/interface/lte/monitor {iface} once", timeout=6)
        out["lte_rc"] = lte.returncode
        out["lte"] = parse_lte_monitor(lte.stdout)
        out["lte_raw"] = lte.stdout
        if lte.stderr.strip():
            out["lte_stderr"] = lte.stderr.strip()
    except Exception as exc:
        out["lte_error"] = repr(exc)

    try:
        st = router.call(f'/interface/print stats-detail where name="{iface}"', timeout=6)
        out["stats_rc"] = st.returncode
        out["stats"] = parse_stats(st.stdout)
        out["stats_raw"] = st.stdout
        if st.stderr.strip():
            out["stats_stderr"] = st.stderr.strip()
    except Exception as exc:
        out["stats_error"] = repr(exc)
    return out


def telemetry_loop(
    router: RouterSSH,
    interfaces: list[str],
    interval: float,
    path: Path,
    stop: threading.Event,
) -> None:
    next_t = time.monotonic()
    with path.open("a", encoding="utf-8") as f:
        while not stop.is_set():
            row: dict[str, Any] = {"timestamp_utc": now_utc(), "interfaces": {}}
            t0 = time.monotonic()
            for iface in interfaces:
                row["interfaces"][iface] = poll_router(router, iface)
            row["collector_ms"] = round((time.monotonic() - t0) * 1000, 1)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            else:
                next_t = time.monotonic()


def ip_present(interface: str, ip: str) -> bool:
    cp = run(["ip", "-4", "addr", "show", "dev", interface])
    return cp.returncode == 0 and re.search(rf"\binet\s+{re.escape(ip)}/", cp.stdout) is not None


def resolve_ipv4(host: str) -> list[str]:
    out: list[str] = []
    try:
        for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = item[4][0]
            if ip not in out:
                out.append(ip)
    except socket.gaierror:
        pass
    return out


def choose_port(server: str, source_ip: str, ports: list[int], timeout_ms: int = 3500) -> tuple[int, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for p in ports:
        cmd = [
            "iperf3", "-c", server, "-p", str(p), "-4",
            "-B", source_ip, "-t", "1", "-J",
            "--connect-timeout", str(timeout_ms),
        ]
        try:
            cp = run(cmd, timeout=8)
            attempt = {"port": p, "rc": cp.returncode, "stderr": cp.stderr.strip()}
            if cp.stdout.strip():
                try:
                    j = json.loads(cp.stdout)
                    attempt["iperf_error"] = j.get("error")
                    bps = (((j.get("end") or {}).get("sum_received") or {}).get("bits_per_second"))
                    if bps is not None:
                        attempt["bps"] = bps
                except json.JSONDecodeError:
                    attempt["json_error"] = True
            attempts.append(attempt)
            if cp.returncode == 0 and not attempt.get("iperf_error"):
                return p, {"attempts": attempts}
        except Exception as exc:
            attempts.append({"port": p, "exception": repr(exc)})
    raise RuntimeError("No usable iperf3 port on pinned server: " + json.dumps(attempts))


def build_iperf(
    server: str,
    port: int,
    source_ip: str,
    protocol: str,
    duration: int,
    bitrate: str,
    packet_length: int,
    reverse: bool,
) -> list[str]:
    cmd = [
        "iperf3", "-c", server, "-p", str(port), "-4",
        "-B", source_ip, "-t", str(duration),
        "-i", "1", "-J",
    ]
    if protocol == "udp":
        cmd += ["-u", "-b", bitrate, "-l", str(packet_length)]
    if reverse:
        cmd.append("-R")
    return cmd


def summarize_iperf(j: dict[str, Any], protocol: str, reverse: bool) -> dict[str, Any]:
    end = j.get("end") or {}
    result: dict[str, Any] = {}
    if "error" in j:
        result["error"] = j["error"]
        return result

    if protocol == "udp":
        # Receiver-side UDP loss/jitter/throughput is the useful result for video tests.
        candidates = [end.get("sum_received"), end.get("sum"), end.get("sum_sent")]
        x = next((v for v in candidates if isinstance(v, dict) and "bits_per_second" in v), {})
        result.update({
            "bits_per_second": x.get("bits_per_second"),
            "mbps": (x.get("bits_per_second") / 1e6) if x.get("bits_per_second") is not None else None,
            "jitter_ms": x.get("jitter_ms"),
            "lost_packets": x.get("lost_packets"),
            "packets": x.get("packets"),
            "lost_percent": x.get("lost_percent"),
        })
    else:
        sent = end.get("sum_sent") or {}
        recv = end.get("sum_received") or {}
        # For normal upload, receiver is what reached the server. Reverse means receiver is local.
        preferred = recv if not reverse else recv
        result.update({
            "bits_per_second": preferred.get("bits_per_second"),
            "mbps": (preferred.get("bits_per_second") / 1e6) if preferred.get("bits_per_second") is not None else None,
            "sender_mbps": (sent.get("bits_per_second") / 1e6) if sent.get("bits_per_second") is not None else None,
            "receiver_mbps": (recv.get("bits_per_second") / 1e6) if recv.get("bits_per_second") is not None else None,
            "retransmits": sent.get("retransmits"),
        })
    return result


def read_telemetry(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def summarize_radio(rows: list[dict[str, Any]], iface: str) -> dict[str, Any]:
    vals: dict[str, list[float]] = {k: [] for k in ("rssi", "rsrp", "rsrq", "sinr", "cqi", "ri")}
    primary: list[str] = []
    ca: list[str] = []
    statuses: list[str] = []
    cells: list[str] = []

    first_stats = None
    last_stats = None

    for row in rows:
        d = (((row.get("interfaces") or {}).get(iface) or {}))
        lte = d.get("lte") or {}
        st = d.get("stats") or {}
        if st:
            if first_stats is None:
                first_stats = st
            last_stats = st

        for k in vals:
            n = numeric_radio(lte.get(k))
            if n is not None:
                vals[k].append(n)
        if lte.get("primary-band"):
            primary.append(lte["primary-band"])
        # No CA is also meaningful. Record a marker.
        ca.append(lte.get("ca-band") or "")
        if lte.get("status"):
            statuses.append(lte["status"])
        cell = lte.get("cell-id") or lte.get("phy-cellid")
        if cell:
            cells.append(cell)

    def median(a: list[float]) -> float | None:
        if not a:
            return None
        b = sorted(a)
        n = len(b)
        return b[n//2] if n % 2 else (b[n//2 - 1] + b[n//2]) / 2

    out: dict[str, Any] = {}
    for k, a in vals.items():
        out[f"{k}_median"] = median(a)
        out[f"{k}_min"] = min(a) if a else None
        out[f"{k}_max"] = max(a) if a else None

    out["primary_bands_seen"] = sorted(set(primary))
    out["ca_bands_seen"] = sorted(set(x for x in ca if x))
    out["ca_missing_samples"] = sum(1 for x in ca if not x)
    out["samples"] = len(rows)
    out["statuses_seen"] = sorted(set(statuses))
    out["cell_changes"] = sum(1 for a, b in zip(cells, cells[1:]) if a != b)

    if first_stats and last_stats:
        for k in ("tx-byte", "rx-byte", "tx-packet", "rx-packet", "tx-queue-drop"):
            if k in first_stats and k in last_stats:
                out[f"{k}_delta"] = max(0, last_stats[k] - first_stats[k])
    return out


def parse_ping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    s = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, Any] = {}
    m = re.search(r"(\d+) packets transmitted,\s*(\d+) received.*?([0-9.]+)% packet loss", s)
    if m:
        out["sent"] = int(m.group(1))
        out["received"] = int(m.group(2))
        out["loss_percent"] = float(m.group(3))
    m = re.search(r"rtt min/avg/max/mdev = ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", s)
    if m:
        out["min_ms"], out["avg_ms"], out["max_ms"], out["mdev_ms"] = map(float, m.groups())
    # Capture individual RTTs for p95.
    rtts = [float(x) for x in re.findall(r"time=([0-9.]+)\s*ms", s)]
    if rtts:
        b = sorted(rtts)
        idx = min(len(b)-1, max(0, int(round(0.95 * (len(b)-1)))))
        out["p95_ms"] = b[idx]
        out["sample_count"] = len(rtts)
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def summarize_events(rows: list[dict[str, Any]], iface: str, sinr_low_threshold: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous: dict[str, Any] = {}
    sinr_was_low: bool | None = None

    def add(ts: str, event_type: str, **fields: Any) -> None:
        events.append({"timestamp_utc": ts, "type": event_type, "interface": iface, **fields})

    for row in rows:
        ts = row.get("timestamp_utc")
        if not ts:
            continue
        d = (((row.get("interfaces") or {}).get(iface) or {}))
        lte = d.get("lte") or {}
        status = lte.get("status") or lte.get("registration-status")
        primary = lte.get("primary-band") or ""
        ca = lte.get("ca-band") or ""
        cell = lte.get("cell-id") or lte.get("current-cellid") or lte.get("phy-cellid") or ""

        current = {"status": status, "primary": primary, "ca": ca, "cell": cell}
        if previous:
            if current["primary"] != previous.get("primary"):
                add(ts, "primary_band_changed", old=previous.get("primary"), new=current["primary"])
            if bool(current["ca"]) and not previous.get("ca"):
                add(ts, "ca_appeared", ca_band=current["ca"])
            if previous.get("ca") and not current["ca"]:
                add(ts, "ca_disappeared", old=previous.get("ca"))
            if current["ca"] and previous.get("ca") and current["ca"] != previous.get("ca"):
                add(ts, "ca_band_changed", old=previous.get("ca"), new=current["ca"])
            if current["cell"] and previous.get("cell") and current["cell"] != previous.get("cell"):
                add(ts, "cell_changed", old=previous.get("cell"), new=current["cell"])
            if current["status"] != previous.get("status"):
                if current["status"] == "registered":
                    add(ts, "lte_recovered", old=previous.get("status"), new=current["status"])
                else:
                    add(ts, "lte_status_changed", old=previous.get("status"), new=current["status"])
        previous = current

        sinr = numeric_radio(lte.get("sinr"))
        if sinr is None:
            continue
        sinr_is_low = sinr < sinr_low_threshold
        if sinr_was_low is None:
            sinr_was_low = sinr_is_low
        elif sinr_is_low and not sinr_was_low:
            add(ts, "sinr_low_started", sinr=sinr, threshold=sinr_low_threshold)
            sinr_was_low = True
        elif not sinr_is_low and sinr_was_low:
            add(ts, "sinr_low_ended", sinr=sinr, threshold=sinr_low_threshold)
            sinr_was_low = False

    return events


def append_summary_csv(path: Path, row: dict[str, Any]) -> None:
    # Keep this stable and compact. Full data remains in per-test JSON/JSONL files.
    fields = [
        "timestamp_local", "tag", "path", "lte_interface", "source_ip",
        "server", "server_port", "protocol", "direction", "target_bitrate",
        "actual_mbps", "udp_loss_percent", "udp_jitter_ms", "tcp_retransmits",
        "ping_avg_ms", "ping_p95_ms", "ping_loss_percent",
        "rsrp_median", "rsrq_median", "sinr_median", "cqi_median", "ri_median",
        "primary_bands_seen", "ca_bands_seen", "ca_missing_samples",
        "cell_changes", "source_rule_bytes_delta", "source_rule_packets_delta",
        "lte_tx_bytes_delta", "lte_rx_bytes_delta",
        "other_lte_tx_bytes_delta", "other_lte_rx_bytes_delta",
        "path_verification", "test_dir",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k) for k in fields})


def collect_router_metadata(router: RouterSSH, interfaces: list[str]) -> dict[str, Any]:
    cmds = {
        "resource": "/system/resource/print",
        "routerboard": "/system/routerboard/print",
        "identity": "/system/identity/print",
        "lte_config": "/interface/lte/print detail without-paging",
    }
    out: dict[str, Any] = {"timestamp_utc": now_utc(), "interfaces": interfaces}
    for name, cmd in cmds.items():
        try:
            cp = router.call(cmd, timeout=8)
            out[name] = {"rc": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
        except Exception as exc:
            out[name] = {"error": repr(exc)}
    return out


def campaign_init(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    server = args.server
    ips = resolve_ipv4(server)
    if not ips:
        print(f"Could not resolve {server}", file=sys.stderr)
        return 2
    campaign = {
        "created_local": now_local(),
        "server_hostname": server,
        "server_ipv4": ips[0],
        "all_resolved_ipv4": ips,
        "ports": args.ports,
        "note": (
            "Server IP is pinned for comparability. Do not silently change it mid-campaign. "
            "If the server becomes unavailable, start a new campaign."
        ),
    }
    save_json(Path(args.campaign), campaign)
    print(json.dumps(campaign, indent=2))
    return 0


def run_test(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    require_commands(["ssh", "iperf3", "ping", "ip"])
    router = RouterSSH(cfg["router"])

    paths = cfg["paths"]
    if args.path not in paths:
        raise SystemExit(f"Unknown path {args.path!r}; config has {list(paths)}")
    path_cfg = paths[args.path]
    iface = path_cfg["lte_interface"]
    source_ip = path_cfg["source_ip"]
    source_comment = source_rule_comment(args.path, path_cfg)
    linux_if = cfg["linux_interface"]

    if not ip_present(linux_if, source_ip):
        raise SystemExit(
            f"Source IP {source_ip} is not assigned to {linux_if}. "
            f"Run setup_test_ips.sh or have OpenClaw configure the test IPs first."
        )

    campaign = load_json(Path(args.campaign))
    server = campaign["server_ipv4"]
    ports = [int(x) for x in campaign["ports"]]

    # Verify SSH before creating result dir.
    cp = router.call("/system/identity/print", timeout=7)
    if cp.returncode != 0:
        raise SystemExit(f"Router SSH failed: {cp.stderr.strip()}")

    all_lte = [p["lte_interface"] for p in paths.values()]
    all_lte = list(dict.fromkeys(all_lte))

    try:
        port, port_probe = choose_port(server, source_ip, ports)
    except Exception as exc:
        raise SystemExit(str(exc))

    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    direction = "download" if args.reverse else "upload"
    name = f"{stamp}_{slug(args.tag)}_{slug(args.path)}_{args.protocol}_{direction}"
    out_dir = Path(args.output) / name
    out_dir.mkdir(parents=True, exist_ok=False)

    meta = {
        "collector_version": VERSION,
        "created_local": now_local(),
        "created_utc": now_utc(),
        "tag": args.tag,
        "path": args.path,
        "lte_interface": iface,
        "source_ip": source_ip,
        "linux_interface": linux_if,
        "server_hostname": campaign.get("server_hostname"),
        "server_ipv4": server,
        "server_port": port,
        "port_probe": port_probe,
        "protocol": args.protocol,
        "direction": direction,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "cooldown_s": args.cooldown,
        "target_bitrate": args.bitrate if args.protocol == "udp" else None,
        "packet_length": args.packet_length if args.protocol == "udp" else None,
        "routing_mode": cfg.get("routing_mode"),
        "routing_table": path_cfg.get("routing_table"),
        "source_rule_comment": source_comment,
    }
    save_json(out_dir / "test.json", meta)
    save_json(out_dir / "router_metadata.json", collect_router_metadata(router, all_lte))
    source_rule_before = mangle_rule_counters(router, source_comment)
    source_rule_after: dict[str, Any] = {}

    stop = threading.Event()
    tele_path = out_dir / "telemetry.jsonl"
    tele = threading.Thread(
        target=telemetry_loop,
        args=(router, all_lte, args.telemetry_interval, tele_path, stop),
        daemon=True,
    )
    tele.start()

    ping_target = cfg.get("ping_target", "1.1.1.1")
    ping_cmd = ["ping", "-n", "-D", "-O", "-I", source_ip, "-i", str(args.ping_interval), ping_target]
    ping_out = (out_dir / "ping.txt").open("w", encoding="utf-8")
    ping_err = (out_dir / "ping_stderr.txt").open("w", encoding="utf-8")
    ping_proc = subprocess.Popen(ping_cmd, stdout=ping_out, stderr=ping_err, text=True)

    iperf_cmd = build_iperf(
        server, port, source_ip, args.protocol, args.duration,
        args.bitrate, args.packet_length, args.reverse
    )
    (out_dir / "iperf_command.txt").write_text(shlex.join(iperf_cmd) + "\n", encoding="utf-8")

    rc = 99
    try:
        print(f"Warm-up {args.warmup}s...")
        time.sleep(args.warmup)
        print("Running:", shlex.join(iperf_cmd))
        with (out_dir / "iperf.json").open("w", encoding="utf-8") as fo, \
             (out_dir / "iperf_stderr.txt").open("w", encoding="utf-8") as fe:
            cp = subprocess.run(iperf_cmd, text=True, stdout=fo, stderr=fe)
            rc = cp.returncode
        print(f"Cool-down {args.cooldown}s...")
        time.sleep(args.cooldown)
    finally:
        if ping_proc.poll() is None:
            ping_proc.send_signal(getattr(__import__("signal"), "SIGINT"))
            try:
                ping_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ping_proc.terminate()
        ping_out.close()
        ping_err.close()
        stop.set()
        tele.join(timeout=10)
        source_rule_after = mangle_rule_counters(router, source_comment)
        router.close()

    (out_dir / "iperf_exit_code.txt").write_text(str(rc) + "\n", encoding="utf-8")

    iperf_j: dict[str, Any] = {}
    try:
        iperf_j = json.loads((out_dir / "iperf.json").read_text(encoding="utf-8"))
    except Exception as exc:
        iperf_j = {"error": f"could not parse iperf JSON: {exc}"}

    iperf_sum = summarize_iperf(iperf_j, args.protocol, args.reverse)
    ping_sum = parse_ping(out_dir / "ping.txt")
    rows = read_telemetry(tele_path)
    radio_target = summarize_radio(rows, iface)
    events = summarize_events(rows, iface, args.sinr_low_threshold)
    write_jsonl(out_dir / "events.jsonl", events)

    other_ifaces = [x for x in all_lte if x != iface]
    other_summaries = {x: summarize_radio(rows, x) for x in other_ifaces}

    target_tx = radio_target.get("tx-byte_delta", 0) or 0
    target_rx = radio_target.get("rx-byte_delta", 0) or 0
    other_tx = sum((d.get("tx-byte_delta", 0) or 0) for d in other_summaries.values())
    other_rx = sum((d.get("rx-byte_delta", 0) or 0) for d in other_summaries.values())
    source_rule_bytes_delta = counter_delta(source_rule_before, source_rule_after, "bytes")
    source_rule_packets_delta = counter_delta(source_rule_before, source_rule_after, "packets")

    # Direction-aware path verification. This is deliberately conservative.
    if args.reverse:
        selected = target_rx
        other = other_rx
    else:
        selected = target_tx
        other = other_tx
    if not source_rule_packets_delta or not source_rule_bytes_delta:
        path_verification = "FAIL_SOURCE_RULE_NOT_MATCHED"
    elif selected <= 1_000_000:
        path_verification = "FAIL_WRONG_LTE"
    elif selected >= 4 * max(1, other):
        path_verification = "PASS"
    else:
        path_verification = "WARN_BACKGROUND_OTHER_LTE"

    summary = {
        "test": meta,
        "iperf": iperf_sum,
        "ping": ping_sum,
        "radio_target": radio_target,
        "radio_other": other_summaries,
        "source_rule": {
            "comment": source_comment,
            "before": source_rule_before,
            "after": source_rule_after,
            "bytes_delta": source_rule_bytes_delta,
            "packets_delta": source_rule_packets_delta,
        },
        "path_verification": path_verification,
        "events_count": len(events),
    }
    save_json(out_dir / "summary.json", summary)

    row = {
        "timestamp_local": meta["created_local"],
        "tag": args.tag,
        "path": args.path,
        "lte_interface": iface,
        "source_ip": source_ip,
        "server": server,
        "server_port": port,
        "protocol": args.protocol,
        "direction": direction,
        "target_bitrate": args.bitrate if args.protocol == "udp" else "",
        "actual_mbps": iperf_sum.get("mbps"),
        "udp_loss_percent": iperf_sum.get("lost_percent"),
        "udp_jitter_ms": iperf_sum.get("jitter_ms"),
        "tcp_retransmits": iperf_sum.get("retransmits"),
        "ping_avg_ms": ping_sum.get("avg_ms"),
        "ping_p95_ms": ping_sum.get("p95_ms"),
        "ping_loss_percent": ping_sum.get("loss_percent"),
        "rsrp_median": radio_target.get("rsrp_median"),
        "rsrq_median": radio_target.get("rsrq_median"),
        "sinr_median": radio_target.get("sinr_median"),
        "cqi_median": radio_target.get("cqi_median"),
        "ri_median": radio_target.get("ri_median"),
        "primary_bands_seen": "|".join(radio_target.get("primary_bands_seen", [])),
        "ca_bands_seen": "|".join(radio_target.get("ca_bands_seen", [])),
        "ca_missing_samples": radio_target.get("ca_missing_samples"),
        "cell_changes": radio_target.get("cell_changes"),
        "source_rule_bytes_delta": source_rule_bytes_delta,
        "source_rule_packets_delta": source_rule_packets_delta,
        "lte_tx_bytes_delta": target_tx,
        "lte_rx_bytes_delta": target_rx,
        "other_lte_tx_bytes_delta": other_tx,
        "other_lte_rx_bytes_delta": other_rx,
        "path_verification": path_verification,
        "test_dir": str(out_dir),
    }
    valid_for_summary = rc == 0 and not iperf_sum.get("error") and path_verification == "PASS"
    if valid_for_summary:
        append_summary_csv(Path(args.output) / "summary.csv", row)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nSaved: {out_dir}")
    if not valid_for_summary:
        print("Run kept as raw artifacts only; not appended to summary.csv.", file=sys.stderr)
    if rc != 0:
        print("iperf3 exited non-zero; inspect iperf_stderr.txt and iperf.json", file=sys.stderr)
        return rc or 1
    if path_verification.startswith("FAIL"):
        print("WARNING: selected LTE path was not verified from counters.", file=sys.stderr)
        return 3
    return 0


def preflight(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    require_commands(["ssh", "iperf3", "ping", "ip"])
    problems: list[str] = []
    print("Collector version:", VERSION)
    print("Linux interface:", cfg["linux_interface"])
    for name, p in cfg["paths"].items():
        ok = ip_present(cfg["linux_interface"], p["source_ip"])
        print(f"{name}: source={p['source_ip']} lte={p['lte_interface']} assigned={ok}")
        if not ok:
            problems.append(f"{p['source_ip']} not assigned to {cfg['linux_interface']}")

    router = RouterSSH(cfg["router"])
    try:
        cp = router.call("/system/resource/print", timeout=7)
        print("Router SSH:", "OK" if cp.returncode == 0 else "FAIL")
        if cp.returncode != 0:
            problems.append("Router SSH failed")
        for p in cfg["paths"].values():
            x = poll_router(router, p["lte_interface"])
            print(p["lte_interface"], "LTE keys:", ", ".join(sorted((x.get("lte") or {}).keys())))
            if not (x.get("lte") or {}):
                problems.append(f"Could not parse LTE monitor for {p['lte_interface']}")
    finally:
        router.close()

    if Path(args.campaign).exists():
        campaign = load_json(Path(args.campaign))
        print("Pinned public server:", campaign.get("server_hostname"), campaign.get("server_ipv4"))
    else:
        problems.append(f"Campaign file missing: {args.campaign}")

    if problems:
        print("\nPRE-FLIGHT FAILED/WARNINGS:")
        for p in problems:
            print(" -", p)
        return 2
    print("\nPRE-FLIGHT OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="command", required=True)

    ci = sub.add_parser("campaign-init", help="Pin one public iperf3 server/IP for the campaign")
    ci.add_argument("--server", required=True, help="Public iperf3 hostname")
    ci.add_argument("--ports", type=int, nargs="+", default=list(range(5201, 5211)))
    ci.add_argument("--campaign", default="campaign.json")

    pf = sub.add_parser("preflight", help="Validate Linux test IPs, RouterOS SSH/telemetry and campaign")
    pf.add_argument("--campaign", default="campaign.json")

    rt = sub.add_parser("run", help="Run one test")
    rt.add_argument("--path", required=True, help="Path name from config, e.g. lte1 or lte2")
    rt.add_argument("--campaign", default="campaign.json")
    rt.add_argument("--protocol", choices=["udp", "tcp"], default="udp")
    rt.add_argument("--bitrate", default="8M")
    rt.add_argument("--packet-length", type=int, default=1200)
    rt.add_argument("--duration", type=int, default=60)
    rt.add_argument("--warmup", type=int, default=5)
    rt.add_argument("--cooldown", type=int, default=5)
    rt.add_argument("--telemetry-interval", type=float, default=1.0)
    rt.add_argument("--ping-interval", type=float, default=0.2)
    rt.add_argument("--sinr-low-threshold", type=float, default=3.0)
    rt.add_argument("--reverse", action="store_true", help="Server -> client download")
    rt.add_argument("--tag", default="manual")
    rt.add_argument("--output", default="results")

    args = ap.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}")
    cfg = load_json(cfg_path)

    if args.command == "campaign-init":
        return campaign_init(args, cfg)
    if args.command == "preflight":
        return preflight(args, cfg)
    if args.command == "run":
        return run_test(args, cfg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
