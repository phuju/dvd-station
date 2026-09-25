#!/usr/bin/env bash
# Build the release firmware image the host serves to remotes for over-the-air
# updates (arduino/firmware/discstation.bin + firmware.json). Runs on `npm pack`
# / `npm publish` (prepack) so a published package always carries an image that
# matches its version. Needs arduino-cli with the esp32 core.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v arduino-cli >/dev/null || { echo "arduino-cli is required to build the firmware image" >&2; exit 1; }

VERSION=$(node -p "require('./package.json').version")
OUT=arduino/firmware
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Build from a copy without secrets.h so Wi-Fi credentials can never end up in a published image.
cp -R arduino/DiscStation "$TMP/DiscStation"
rm -f "$TMP/DiscStation/secrets.h"

rm -rf "$OUT" && mkdir -p "$OUT"
arduino-cli compile -b esp32:esp32:esp32 \
  --build-property "compiler.cpp.extra_flags=-DFW_VERSION=$VERSION" \
  --output-dir "$TMP/out" "$TMP/DiscStation" >/dev/null
cp "$TMP/out/DiscStation.ino.bin" "$OUT/discstation.bin"

SIZE=$(wc -c <"$OUT/discstation.bin" | tr -d ' ')
MD5=$( (md5sum "$OUT/discstation.bin" 2>/dev/null || md5 -r "$OUT/discstation.bin") | cut -d' ' -f1)
printf '{"version":"%s","size":%s,"md5":"%s"}\n' "$VERSION" "$SIZE" "$MD5" >"$OUT/firmware.json"
echo "firmware $VERSION: $SIZE bytes, md5 $MD5"
