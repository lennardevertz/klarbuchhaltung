"""Abgleich gegen die AMTLICHEN Zahlen (Anlage EÜR & USt-Voranmeldung).

Der stärkste Korrektheits-Schutz: unsere Berechnung muss cent-genau den beim Finanzamt
eingereichten Abschlüssen entsprechen. Läuft die Engine je davon weg, wird der Test rot.

Die amtlichen Werte liegen in data/reconcile_official.json (LOKAL, gitignored – enthält
Steuerdaten und darf nie ins Repo). Fehlt die Datei oder die echte DB, wird übersprungen.
Gelesen wird die echte Datenbank read-only – die abgegebenen Jahre sind stabil.
"""
import json
import sqlite3
from pathlib import Path

import pytest

import core

ROOT = Path(__file__).resolve().parent.parent
REAL_DB = ROOT / "data" / "bookkeeping.db"
OFFICIAL_FILE = ROOT / "data" / "reconcile_official.json"


def _official():
    if not OFFICIAL_FILE.exists():
        return {}
    try:
        return json.loads(OFFICIAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ro_conn():
    c = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


_OFF = _official()
_EUER = _OFF.get("euer", {}) if REAL_DB.exists() else {}
_USTVA = _OFF.get("ustva", {}) if REAL_DB.exists() else {}


@pytest.mark.skipif(not _EUER, reason="amtliche EÜR-Zahlen (data/reconcile_official.json) fehlen")
@pytest.mark.parametrize("year", sorted(_EUER))
def test_euer_stimmt_mit_amtlicher_eur(year):
    exp = _EUER[year]
    conn = _ro_conn()
    e = core.euer(conn, core.load_config(), f"{year}-01-01", f"{year}-12-31")
    assert round(e["betriebseinnahmen"], 2) == exp["einnahmen"], f"Betriebseinnahmen {year}"
    assert round(e["betriebsausgaben"], 2) == exp["ausgaben"], f"Betriebsausgaben {year}"
    assert round(e["ergebnis"], 2) == exp["gewinn"], f"Ergebnis {year}"


@pytest.mark.skipif(not _USTVA, reason="amtliche USt-VA-Zahlen (data/reconcile_official.json) fehlen")
@pytest.mark.parametrize("key", sorted(_USTVA))
def test_ustva_stimmt_mit_amtlicher_voranmeldung(key):
    year, q = (int(x) for x in key.split("-Q"))
    lo, hi, _ = core.period_bounds(year, "quarter", q)
    conn = _ro_conn()
    vf = core._vat_figures(conn, core.load_config(), lo, hi)
    assert round(float(vf["zahllast"]), 2) == _USTVA[key], f"USt-Zahllast {key}"
