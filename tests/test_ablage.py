"""Wo die Buchhaltung liegt – im Quellbaum vs. als installierte App.

Als gebündelte App liegt der Code schreibgeschützt in /Applications bzw.
Program Files, und ein Update würde danebenliegende Daten mitreißen. Aus dem
Quellbaum heraus muss dagegen alles bleiben, wo es ist.
"""
from pathlib import Path

import pytest

import core


@pytest.fixture
def sauber(monkeypatch):
    monkeypatch.delenv("BB_HOME", raising=False)
    return monkeypatch


def test_aus_dem_quellbaum_bleibt_alles_im_repo(sauber):
    sauber.delattr(__import__("sys"), "frozen", raising=False)
    assert core._benutzer_ablage() == core.ROOT


def test_bb_home_schlaegt_alles(sauber, tmp_path):
    sauber.setenv("BB_HOME", str(tmp_path / "woanders"))
    assert core._benutzer_ablage() == tmp_path / "woanders"


def test_bb_home_versteht_tilde(sauber):
    sauber.setenv("BB_HOME", "~/meine-buchhaltung")
    assert core._benutzer_ablage() == Path.home() / "meine-buchhaltung"


def test_bb_home_leer_wird_ignoriert(sauber):
    sauber.setenv("BB_HOME", "   ")
    sauber.delattr(__import__("sys"), "frozen", raising=False)
    assert core._benutzer_ablage() == core.ROOT


@pytest.mark.parametrize("plattform,erwartet", [
    ("darwin", Path.home() / "Library" / "Application Support" / "Buchhaltung"),
    ("linux", Path.home() / ".local" / "share" / "buchhaltung"),
])
def test_installiert_landet_im_benutzerverzeichnis(sauber, plattform, erwartet):
    import sys as _sys
    sauber.setattr(_sys, "frozen", True, raising=False)
    sauber.setattr(_sys, "platform", plattform)
    sauber.delenv("XDG_DATA_HOME", raising=False)
    assert core._benutzer_ablage() == erwartet


def test_windows_nimmt_appdata(sauber, tmp_path):
    import sys as _sys
    sauber.setattr(_sys, "frozen", True, raising=False)
    sauber.setattr(_sys, "platform", "win32")
    sauber.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert core._benutzer_ablage() == tmp_path / "Roaming" / "Buchhaltung"


def test_linux_beachtet_xdg(sauber, tmp_path):
    import sys as _sys
    sauber.setattr(_sys, "frozen", True, raising=False)
    sauber.setattr(_sys, "platform", "linux")
    sauber.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert core._benutzer_ablage() == tmp_path / "xdg" / "buchhaltung"


def test_mitgelieferte_dateien_kommen_aus_dem_buendel():
    """Schriften liegen im Bündel, nicht in der beschreibbaren Ablage."""
    assert core.FONT_DIR == core.BUNDLE / "fonts"
    assert core.FONT_DIR.is_dir(), "Schriften müssen mitgeliefert sein"


def test_profile_haengen_an_der_ablage_nicht_am_quellordner():
    assert core.PROFILES_DIR.parent == core.HOME


def test_frische_installation_legt_die_ablage_an(monkeypatch, tmp_path):
    """Bei einer frischen Installation existiert der Elternordner noch nicht.

    Ohne parents=True scheiterte db() genau daran – die gebündelte App zeigte
    dann nur ein leeres Fenster.
    """
    neu = tmp_path / "gibt" / "es" / "noch" / "nicht"
    monkeypatch.setattr(core, "HOME", neu)
    monkeypatch.setattr(core, "DATA_DIR", neu / "data")
    monkeypatch.setattr(core, "DB_PATH", neu / "data" / "bookkeeping.db")
    monkeypatch.setattr(core, "STATEMENT_DIR", neu / "data" / "statements")
    monkeypatch.setattr(core, "DOC_DIR", neu / "data" / "documents")
    monkeypatch.setattr(core, "SETTINGS_PATH", neu / "data" / "settings.json")

    conn = core.db()
    assert (neu / "data" / "bookkeeping.db").exists()
    assert (neu / "data" / "documents").is_dir()
    conn.close()
