#!/usr/bin/env bash
# Baut "Buchhaltung.app" und legt sie in /Applications ab.
#
#   ./packaging/make_app.sh              -> /Applications
#   ./packaging/make_app.sh ~/Applications
#
# Die App ist eine schlanke Hülle: sie startet desktop.py aus DIESEM Ordner.
# Code-Änderungen sind also sofort wirksam, ohne neu zu bauen. Wird der Ordner
# verschoben, muss das Skript einmal neu laufen.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIEL="${1:-/Applications}"
APP="$ZIEL/Buchhaltung.app"

[ -x "$REPO/.venv/bin/python" ] || { echo "Fehlt: $REPO/.venv/bin/python"; exit 1; }
"$REPO/.venv/bin/python" -c "import webview" 2>/dev/null || {
  echo "pywebview fehlt. Installieren mit:"
  echo "  $REPO/.venv/bin/python -m pip install pywebview"
  exit 1
}

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>Buchhaltung</string>
  <key>CFBundleDisplayName</key>       <string>Buchhaltung</string>
  <key>CFBundleIdentifier</key>        <string>de.lennard.buchhaltung</string>
  <key>CFBundleVersion</key>           <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleExecutable</key>        <string>Buchhaltung</string>
  <key>CFBundleIconFile</key>          <string>Buchhaltung</string>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>LSMinimumSystemVersion</key>    <string>11.0</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/Buchhaltung" <<LAUNCHER
#!/usr/bin/env bash
# Erzeugt von packaging/make_app.sh — nicht von Hand ändern.
REPO="$REPO"
cd "\$REPO"
exec "\$REPO/.venv/bin/python" "\$REPO/desktop.py" \
  >> "\$HOME/Library/Logs/Buchhaltung.log" 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/Buchhaltung"

cp "$REPO/packaging/Buchhaltung.icns" "$APP/Contents/Resources/Buchhaltung.icns"

# Ad-hoc signieren, damit macOS die App ohne Warnung startet
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
touch "$APP"

echo "Fertig: $APP"
echo "Log:    ~/Library/Logs/Buchhaltung.log"
