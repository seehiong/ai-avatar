#!/usr/bin/env bash
# Build assets/models/mpfb.glb (run tools/setup_mpfb.sh once first).
# Extra args go to build_mpfb_avatar.py, e.g. --gender 0 --skin young_asian_female --hair bob02
set -euo pipefail
cd "$(dirname "$0")/.."
blender -b --python tools/build_mpfb_avatar.py -- \
  --out assets/models/mpfb.glb --rig-dir tools/.cache/talkinghead "$@" 2>&1 \
  | grep -E '^(added|WROTE)|Error|Traceback' || true
