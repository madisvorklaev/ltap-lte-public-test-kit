#!/usr/bin/env bash
set -euo pipefail

# Temporary Linux source IP setup for one Ethernet connection to the LtAP.
# Usage:
#   sudo ./setup_test_ips.sh enp3s0 192.168.88.201/24 192.168.88.202/24
#
# These addresses are intentionally temporary: reboot / link reconfiguration removes them.
# OpenClaw may make them persistent with NetworkManager ONLY after it verifies the
# correct wired connection profile.

if [[ $# -ne 3 ]]; then
  echo "Usage: sudo $0 <interface> <lte1-ip/cidr> <lte2-ip/cidr>" >&2
  exit 2
fi

IFACE="$1"
IP1="$2"
IP2="$3"

ip link show "$IFACE" >/dev/null
ip addr add "$IP1" dev "$IFACE" 2>/dev/null || true
ip addr add "$IP2" dev "$IFACE" 2>/dev/null || true

echo "IPv4 addresses now present on $IFACE:"
ip -4 addr show dev "$IFACE"
