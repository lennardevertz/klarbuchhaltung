#!/usr/bin/env python3
"""Desktop-Hülle: zeigt die Buchhaltung in einem nativen Fenster statt im Browser.

Zwei Betriebsarten:

* Aus dem Quellbaum: app.py läuft als KINDPROZESS unter einem Supervisor.
  Der "Server neu laden"-Knopf beendet ihn mit Code 42, hier wird neu
  gestartet – so greifen Code-Änderungen ohne Neustart der Hülle.
* Als gebündelte App: der Server läuft IM PROZESS in einem Thread. Es gibt
  kein zweites Python daneben, das man starten könnte, und Neuladen von Code
  ergibt ohne Quellbaum ohnehin keinen Sinn.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import webview

GEFROREN = getattr(sys, "frozen", False)
DIR = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(DIR, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"

_state = {"proc": None, "stop": False}


def _port_frei():
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", PORT)) != 0


def _server_env():
    env = dict(os.environ)
    env["BB_SUPERVISED"] = "1"
    env["BB_NO_BROWSER"] = "1"  # nie den Browser aufmachen, wir haben ein Fenster
    return env


def _supervisor():
    """Startet app.py und startet ihn bei Exit-Code 42 neu."""
    while not _state["stop"]:
        proc = subprocess.Popen([PY, os.path.join(DIR, "app.py")],
                                cwd=DIR, env=_server_env())
        _state["proc"] = proc
        code = proc.wait()
        if _state["stop"] or code != 42:
            break
        time.sleep(0.2)
        _warte_auf_server(timeout=20)
        try:
            webview.windows[0].load_url(URL)
        except Exception:
            pass


def _fehler_melden(e):
    """Absturz in eine Datei neben der Buchhaltung schreiben.

    Ein Fenster-Programm hat keine Konsole – ohne das stirbt der Server
    lautlos und man sieht nur ein leeres Fenster.
    """
    import traceback
    try:
        import core
        ziel = core.HOME / "fehler.log"
        ziel.parent.mkdir(parents=True, exist_ok=True)
        with open(ziel, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            traceback.print_exception(type(e), e, e.__traceback__, file=f)
    except Exception:
        pass
    traceback.print_exc()


def _server_im_thread():
    """Flask im eigenen Prozess starten (gebündelte App)."""
    try:
        import core
        import app as webapp

        core.migrate_wurzel_zu_profil()   # vor der ersten DB-Verbindung
        core.db()                   # Ablage/DB sicherstellen
        webapp.app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True,
                       use_reloader=False)
    except Exception as e:
        _fehler_melden(e)


def _warte_auf_server(timeout=30):
    ende = time.time() + timeout
    while time.time() < ende:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _beenden():
    _state["stop"] = True
    proc = _state.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _speicherort():
    """Ablage für den Webview-Zustand – im Bündel darf das nicht ins Programm."""
    import core
    ziel = core.HOME / "data" / ".webview"
    ziel.mkdir(parents=True, exist_ok=True)
    return str(ziel)


def main():
    if _port_frei():
        ziel = _server_im_thread if GEFROREN else _supervisor
        threading.Thread(target=ziel, daemon=True).start()
        if not _warte_auf_server():
            print("Server startet nicht.", file=sys.stderr)
            sys.exit(1)
    # sonst: läuft schon (z. B. via ./bb-web) -> einfach anhängen

    fenster = webview.create_window(
        "Buchhaltung",
        URL,
        width=1440, height=920,
        min_size=(1000, 640),
        background_color="#FBF9F3",
        text_select=True,
    )
    fenster.events.closed += lambda: _beenden()
    try:
        webview.start(private_mode=False, storage_path=_speicherort())
    finally:
        _beenden()


if __name__ == "__main__":
    main()
