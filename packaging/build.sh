#!/usr/bin/env bash
# Baut das auslieferbare Paket für die aktuelle Plattform.
#
#   ./packaging/build.sh            -> dist/ und ein Archiv in dist/release/
#
# macOS   -> Buchhaltung.app + Buchhaltung-macOS.dmg
# Windows -> Buchhaltung\ + Buchhaltung-Windows.zip   (in Git Bash / MSYS)
# Linux   -> Buchhaltung/  + Buchhaltung-Linux.tar.gz
#
# Unsigniert. Signierung kommt später über den Release-Workflow dazu.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

VERSION="$("$PY" -c "import sys;print(sys.argv[1])" "${VERSION:-1.1.0}")"
RELEASE="$REPO/dist/release"
mkdir -p "$RELEASE"

echo "==> PyInstaller"
"$PY" -m PyInstaller packaging/buchhaltung.spec --noconfirm --clean

case "$(uname -s)" in
  Darwin)
    ZIEL="$RELEASE/Buchhaltung-${VERSION}-macOS.dmg"
    rm -f "$ZIEL"
    STAGE="$(mktemp -d)"
    cp -R "dist/Buchhaltung.app" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"      # Ziehen-und-Ablegen im Fenster
    echo "==> DMG"
    hdiutil create -volname "Buchhaltung" -srcfolder "$STAGE" \
                   -ov -format UDZO "$ZIEL" >/dev/null
    rm -rf "$STAGE"
    ;;
  Linux)
    ZIEL="$RELEASE/Buchhaltung-${VERSION}-Linux.tar.gz"
    echo "==> Tarball"
    tar -C dist -czf "$ZIEL" Buchhaltung
    ;;
  MINGW*|MSYS*|CYGWIN*)
    # Archiv über Python, damit keine Pfadübersetzung zwischen MSYS und
    # PowerShell nötig ist.
    echo "==> ZIP"
    ZIEL="$("$PY" - "$RELEASE" "$VERSION" <<'PYEOF'
import shutil, sys
from pathlib import Path
release, version = Path(sys.argv[1]), sys.argv[2]
basis = release / f"Buchhaltung-{version}-Windows"
print(shutil.make_archive(str(basis), "zip", root_dir="dist", base_dir="Buchhaltung"))
PYEOF
)"
    ;;
  *)
    echo "Unbekannte Plattform: $(uname -s)" >&2; exit 1;;
esac

echo "Fertig: $ZIEL"
ls -lh "$ZIEL"
