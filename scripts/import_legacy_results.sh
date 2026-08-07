#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
legacy="${1:-/home/madis/projects/ltap-lte-testbench/references/public-iperf-kit}"

if [[ ! -d "$legacy/results" ]]; then
  echo "Legacy result directory not found: $legacy/results" >&2
  exit 2
fi

mkdir -p "$repo_root/results"
cp -a "$legacy/results/." "$repo_root/results/"

for f in campaign.json campaign-dual-lte1.json campaign-dual-lte2.json; do
  if [[ -f "$legacy/$f" ]]; then
    cp -a "$legacy/$f" "$repo_root/$f"
  fi
done

echo "Imported results from $legacy"
echo "Review, regenerate RESULTS_INDEX.md if needed, then run scripts/push_results.sh"

