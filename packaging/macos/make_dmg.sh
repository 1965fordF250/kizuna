#!/bin/bash
# Package dist/Kizuna.app into a drag-to-Applications disk image.
#   packaging/macos/make_dmg.sh <version> <arch>      e.g. 1.0.0 arm64
set -euo pipefail
VERSION="$1"
ARCH="$2"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/dist/Kizuna.app"
OUT="$ROOT/dist/Kizuna-$VERSION-macOS-$ARCH.dmg"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

[ -d "$APP" ] || { echo "missing $APP" >&2; exit 1; }
ditto "$APP" "$STAGE/Kizuna.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
Kizuna

1. Drag "Kizuna" onto the Applications folder.
2. Open it from Applications.

The first time, macOS may say it can't verify the app (this build is not signed
with a paid Apple Developer certificate). To allow it:
  - Open System Settings > Privacy & Security, scroll down, and click
    "Open Anyway" next to the Kizuna message, then confirm.
You only need to do this once.

Kizuna opens in your web browser. It has no Dock icon; use the Quit
button on the page to close it (it also closes itself after 15 idle minutes).
TXT
rm -f "$OUT"
hdiutil create -volname "Kizuna $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
hdiutil verify "$OUT" >/dev/null
echo "$OUT"
