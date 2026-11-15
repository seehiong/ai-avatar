#!/usr/bin/env bash
# One-time setup for tools/build_mpfb_avatar.py:
#   - installs the MPFB extension into Blender
#   - installs CC0 asset packs (system assets, ARKit faceunits, Meta visemes, hair01)
#   - downloads TalkingHead's CC0 Mixamo-style rig + weights to tools/.cache/talkinghead
set -euo pipefail
cd "$(dirname "$0")"
CACHE=.cache
mkdir -p "$CACHE/packs" "$CACHE/talkinghead"

blender --command extension sync
blender --command extension install mpfb --enable || true   # no-op if already installed

FILES=https://files.makehumancommunity.org
for u in asset_packs/makehuman_system_assets/makehuman_system_assets_cc0.zip \
         functional/faceunits01.zip functional/visemes02.zip \
         asset_packs/hair01/hair01_cc0.zip; do
  f="$CACHE/packs/$(basename "$u")"
  [ -f "$f" ] || curl -fL -o "$f" "$FILES/$u"
done

cat > "$CACHE/install_packs.py" <<'EOF'
import sys, glob, zipfile
from bl_ext.blender_org.mpfb.services import LocationService, AssetService
d = LocationService.get_user_data()
for z in sorted(glob.glob(sys.argv[-1] + "/*.zip")):
    with zipfile.ZipFile(z) as f:
        f.extractall(d)
    print("installed", z)
AssetService.update_all_asset_lists()
EOF
blender -b --python "$CACHE/install_packs.py" -- "$CACHE/packs"

TH=https://raw.githubusercontent.com/met4citizen/TalkingHead/main/blender/MPFB
for f in talkinghead.mpfbskel talkinghead.mhw; do
  [ -f "$CACHE/talkinghead/$f" ] || curl -fL -o "$CACHE/talkinghead/$f" "$TH/$f"
done
echo "MPFB setup done. Now run: tools/build_mpfb_avatar.sh"
