#!/usr/bin/env bash
set -euo pipefail

# Linux Mint / Ubuntu-family dependencies for the public iPerf3 collector.
# Safe to run repeatedly.

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv \
  iperf3 \
  openssh-client sshpass \
  iproute2 iputils-ping iputils-arping \
  jq curl git tcpdump

echo
echo "Versions:"
python3 --version
iperf3 --version | head -n 2
ssh -V 2>&1 | head -n 1
ip -Version
nmcli --version

echo
echo "Dependencies installed."
