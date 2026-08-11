# PyInstaller-Spec für alle drei Plattformen.
#   pyinstaller packaging/buchhaltung.spec --noconfirm
#
# Einstiegspunkt ist desktop.py: es startet den Flask-Server im Prozess und
# zeigt ihn in einem nativen Fenster.
import sys
from pathlib import Path

WURZEL = Path(SPECPATH).parent          # SPECPATH setzt PyInstaller
NAME = "Buchhaltung"

# Mitgelieferte Dateien. core.BUNDLE zeigt zur Laufzeit auf sys._MEIPASS,
# app.py holt Vorlagen und statische Dateien von dort.
datas = [
    (str(WURZEL / "templates"), "templates"),
    (str(WURZEL / "static"), "static"),
    (str(WURZEL / "fonts"), "fonts"),
    (str(WURZEL / "config.example.toml"), "."),
    (str(WURZEL / "import_template.csv"), "."),
]

# Was PyInstaller nicht von allein findet: Flask-Erweiterungen werden über
# Strings geladen, die pywebview-Backends je nach Plattform dynamisch.
hiddenimports = [
    "flask_sock", "simple_websocket", "wsproto",
    "webview.platforms.cocoa" if sys.platform == "darwin" else
    "webview.platforms.winforms" if sys.platform.startswith("win") else
    "webview.platforms.gtk",
]

a = Analysis(
    [str(WURZEL / "desktop.py")],
    pathex=[str(WURZEL)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=NAME,
    console=False,                       # kein Terminalfenster im Hintergrund
    icon=str(WURZEL / "packaging" / ("Buchhaltung.icns" if sys.platform == "darwin"
                                     else "Buchhaltung.ico")),
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{NAME}.app",
        icon=str(WURZEL / "packaging" / "Buchhaltung.icns"),
        bundle_identifier="de.buchhaltung.app",
        info_plist={
            "CFBundleName": NAME,
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": "1.0.1",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # Der Server läuft auf 127.0.0.1 – ohne das blockiert ATS ihn.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
