#!/usr/bin/env python3
"""Create a sanitized public copy of one raw LtAP result group."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

SENSITIVE_KEYS = {
    "imsi", "imei", "iccid", "uicc", "sim", "pin", "password", "secret",
    "software-id", "serial-number", "mac-address", "current-cellid",
}
SENSITIVE_PATTERNS = [
    re.compile(r"private key", re.I),
    re.compile(r"\bimsi\b|\bimei\b|\biccid\b|\buicc\b", re.I),
    re.compile(r"\bserial-number\b|\bsoftware-id\b", re.I),
]


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            lk = key.lower()
            if key in {"lte_raw", "stats_raw", "raw", "stdout", "stderr"} or any(s in lk for s in SENSITIVE_KEYS):
                continue
            out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        text = obj
        for pattern in SENSITIVE_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text
    return obj


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(redact(obj), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sanitize_telemetry(src: Path, dst: Path) -> None:
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            fout.write(json.dumps(redact(row), ensure_ascii=False) + "\n")


def verify_public_tree(path: Path) -> list[str]:
    problems: list[str] = []
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(text):
                problems.append(f"{file_path}: {pattern.pattern}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("raw_run_dir", type=Path)
    ap.add_argument("--public-root", type=Path, default=Path("results-public/overnight-2026-08-07-7.24rc3"))
    args = ap.parse_args()

    raw = args.raw_run_dir
    if not raw.exists():
        raise SystemExit(f"Run directory not found: {raw}")
    dst = args.public_root / raw.name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    json_files = ["test_group.json", "group_summary.json", "radio_state_summary.json"]
    for name in json_files:
        if (raw / name).exists():
            write_json(dst / name, load_json(raw / name))
    for name in ("events_v2.jsonl", "radio_state_summary.csv", "lte1_ping.txt", "lte2_ping.txt", "lte1_iperf.json", "lte2_iperf.json", "lte1_iperf_stderr.txt", "lte2_iperf_stderr.txt"):
        if (raw / name).exists():
            shutil.copy2(raw / name, dst / name)
    if (raw / "telemetry.jsonl").exists():
        sanitize_telemetry(raw / "telemetry.jsonl", dst / "telemetry_sanitized.jsonl")

    problems = verify_public_tree(dst)
    if problems:
        print("Sanitizer verification failed:", file=sys.stderr)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 3
    print(dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
