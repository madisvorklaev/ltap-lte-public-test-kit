#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not a git repository: $repo_root" >&2
  exit 2
fi

python3 scripts/update_results_index.py

git add \
  results \
  campaign.json \
  campaign-dual-lte1.json \
  campaign-dual-lte2.json \
  RESULTS_INDEX.md 2>/dev/null || true

if git diff --cached --quiet; then
  echo "No result changes to commit."
  exit 0
fi

stamp="$(date +%Y%m%d-%H%M%S)"
git commit -m "data: sync ltap results ${stamp}"
git push
