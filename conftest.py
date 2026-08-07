"""
Gemeinsame Test-Fixtures.

Grundregel: kein Test fasst echte Projektdaten an. Alle Pfade von core.py zeigen
während der Tests in ein tmp-Verzeichnis, und es geht kein Netzverkehr raus
(Wechselkurse werden im Cache vorbelegt oder bleiben bei EUR).
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402

CONFIG_TOML = """
[business]
name = "Testfirma"
owner = "Test Owner"
address_lines = ["Teststr. 1", "12345 Teststadt", "Germany"]
vat_id = "DE123456789"
tax_number = ""
email = "test@example.com"
phone = ""

[bank]
holder = "Test Owner"
iban = "DE00 0000 0000 0000 0000 00"
bic = "XXXXDEXXXXX"

[invoice]
currency = "EUR"
default_vat_rate = 19
payment_terms_days = 14
language = "en"
signature_path = ""
intro = "Intro."
closing = "Closing."
signoff = "Best regards,"
reverse_charge_note = "Reverse charge."
third_country_note = "Not subject to German VAT."
footer_note = ""

[tax]
taxation = "ist"
va_period = "quarter"
small_business = false

[bookkeeping]
expense_accounts = [
  "4920 - Telefon",
  "4930 - Bürobedarf",
  "4980 - Sonstiges",
]
"""


# Pfad-Globals von core. use_profile() schreibt sie direkt, deshalb reicht
# monkeypatch nicht – die Fixture sichert und stellt sie selbst wieder her.
CORE_PFADE = ("HOME", "CONFIG_PATH", "DATA_DIR", "DB_PATH", "STATEMENT_DIR", "DOC_DIR",
              "SETTINGS_PATH", "PROFILES_DIR", "PROFILES_INDEX", "AKTIVES_PROFIL")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """core.py komplett in ein Wegwerf-Verzeichnis umlenken, Netz zunageln."""
    vorher = {n: getattr(core, n) for n in ("ROOT",) + CORE_PFADE}
    data = tmp_path / "data"
    data.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TOML, encoding="utf-8")

    # ROOT und das Profilverzeichnis MÜSSEN mitwandern: sonst finden
    # migrate_wurzel_zu_profil() und list_profiles() die echte Installation.
    core.ROOT = tmp_path
    core.HOME = tmp_path
    core.PROFILES_DIR = tmp_path / "profiles"
    core.PROFILES_INDEX = core.PROFILES_DIR / "profiles.json"
    core.AKTIVES_PROFIL = None
    core.CONFIG_PATH = cfg_path
    core.DATA_DIR = data
    core.DB_PATH = data / "bookkeeping.db"
    core.STATEMENT_DIR = data / "statements"
    core.DOC_DIR = data / "documents"
    core.SETTINGS_PATH = data / "settings.json"

    def _no_net(*a, **kw):
        raise AssertionError("Test wollte ins Netz (Wechselkurs-API).")

    monkeypatch.setattr(urllib.request, "urlopen", _no_net)
    try:
        yield tmp_path
    finally:
        for n, v in vorher.items():
            setattr(core, n, v)


@pytest.fixture(autouse=True)
def _projektdaten_schuetzen(tmp_path_factory):
    """Reißleine: kein Test kommt an die echte Installation.

    Grundsatz „verboten, außer erlaubt": vor JEDEM Test zeigen Daten- und
    Profilpfade in ein Wegwerf-Verzeichnis. sandbox verfeinert das nur noch.
    Ohne diese Sperre schreibt ein Test, der sandbox vergisst, in die echte
    Buchhaltung – genau das ist einmal passiert.

    core.ROOT bleibt echt: assistant beschränkt Dateizugriffe darauf, und
    FONT_DIR wurde beim Import daraus abgeleitet.
    """
    leer = tmp_path_factory.mktemp("kein-zugriff")
    vorher = {n: getattr(core, n) for n in CORE_PFADE}
    core.HOME = leer
    core.PROFILES_DIR = leer / "profiles"
    core.PROFILES_INDEX = core.PROFILES_DIR / "profiles.json"
    core.AKTIVES_PROFIL = None
    core.CONFIG_PATH = leer / "config.toml"
    core.DATA_DIR = leer / "data"
    core.DB_PATH = core.DATA_DIR / "bookkeeping.db"
    core.STATEMENT_DIR = core.DATA_DIR / "statements"
    core.DOC_DIR = core.DATA_DIR / "documents"
    core.SETTINGS_PATH = core.DATA_DIR / "settings.json"
    try:
        yield
    finally:
        for n, v in vorher.items():
            setattr(core, n, v)


@pytest.fixture
def cfg(sandbox):
    return core.load_config()


@pytest.fixture
def conn(sandbox):
    c = core.db()
    yield c
    c.close()


@pytest.fixture
def customer(conn):
    return core.create_customer(conn, "ACME GmbH", "Hauptstr. 1\n10115 Berlin", None)


@pytest.fixture
def eu_customer(conn):
    return core.create_customer(conn, "Dutch BV", "Damrak 1\n1012 Amsterdam", "NL123456789B01")


@pytest.fixture
def make_invoice(conn, cfg):
    """Rechnung anlegen – standardmäßig ohne PDF, damit die Tests schnell bleiben."""
    def _make(*, customer, kind="domestic", issue_date="2026-03-10", items=None,
              paid_date=None, number=None, render=False, service_from=None,
              service_to=None, notes=None):
        return core.create_invoice(
            conn, cfg, customer=customer, kind=kind, issue_date=issue_date,
            service_from=service_from or issue_date, service_to=service_to,
            items=items or [{"description": "Beratung", "quantity": "1", "unit": "h",
                             "unit_price": "1000.00", "vat_rate": 19}],
            notes=notes, number=number, paid_date=paid_date, render=render)
    return _make


def _echte_datenbanken() -> dict[Path, int]:
    """Alle echten DBs: Altlayout data/ UND jedes Profil unter profiles/."""
    kandidaten = [ROOT / "data" / "bookkeeping.db"]
    kandidaten += sorted((ROOT / "profiles").glob("*/data/bookkeeping.db"))
    return {p: p.stat().st_mtime_ns for p in kandidaten if p.exists()}


@pytest.fixture(autouse=True)
def _protect_real_database():
    """Sicherung: Tests dürfen keine echte Ablage verändern.

    Die Profile müssen mitgeprüft werden – nach der Migration liegt die echte
    Datenbank nicht mehr unter data/, und genau dadurch lief diese Sicherung
    einmal ins Leere, während Tests in die echte Buchhaltung schrieben.
    """
    vorher = _echte_datenbanken()
    yield
    nachher = _echte_datenbanken()
    assert vorher == nachher, (
        "Ein Test hat eine echte Datenbank angefasst: "
        f"{sorted(str(p) for p in set(vorher) ^ set(nachher)) or 'Inhalt geändert'}")


def pytest_configure(config):
    config.addinivalue_line("markers", "pdf: erzeugt echte PDF-Dateien (langsamer)")
