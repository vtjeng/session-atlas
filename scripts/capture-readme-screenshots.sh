#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

# Keep generated timestamps independent of the host and CI runner timezone.
export TZ=America/Los_Angeles

fixture_dir="$(mktemp -d)"
cleanup() {
  if [[ -n "${fixture_dir:-}" && -d "$fixture_dir" ]]; then
    rm -r -- "$fixture_dir"
  fi
}
trap cleanup EXIT

python3 scripts/build_screenshot_site.py --out "$fixture_dir/site"
SCREENSHOT_SITE_DIR="$fixture_dir/site" node scripts/capture-readme-screenshots.js
