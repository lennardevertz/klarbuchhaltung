"""Profile: jedes ist eine eigenständige Buchhaltung mit eigener DB und Ablage."""
import pytest

import core


GLOBALS = ("HOME", "CONFIG_PATH", "DATA_DIR", "DB_PATH", "STATEMENT_DIR", "DOC_DIR",
           "SETTINGS_PATH", "AKTIVES_PROFIL")


@pytest.fixture
def profilraum(tmp_path, monkeypatch):
    """Profilverzeichnis in ein Wegwerf-Verzeichnis umlenken.

    use_profile() schreibt Modulglobals direkt; monkeypatch weiß davon nichts.
    Deshalb hier selbst sichern und zurückgeben – sonst tragen spätere Tests
    die Pfade dieses Wegwerf-Verzeichnisses weiter.
    """
    vorher = {n: getattr(core, n) for n in GLOBALS}
    pdir = tmp_path / "profiles"
    monkeypatch.setattr(core, "PROFILES_DIR", pdir)
    monkeypatch.setattr(core, "PROFILES_INDEX", pdir / "profiles.json")
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "HOME", tmp_path)
    core.AKTIVES_PROFIL = None
    try:
        yield tmp_path
    finally:
        for n, v in vorher.items():
            setattr(core, n, v)


# ── slugify ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,erwartet", [
    ("Beispiel Technik", "beispiel-technik"),
    ("Müller & Söhne GmbH", "mueller-soehne-gmbh"),
    ("  viele   Leerzeichen  ", "viele-leerzeichen"),
    ("Straße 1", "strasse-1"),
    ("???", "profil"),
    ("", "profil"),
])
def test_slugify(name, erwartet):
    assert core.slugify(name) == erwartet


# ── anlegen ──────────────────────────────────────────────────────────────
def test_create_profile_legt_ordner_und_eintrag_an(profilraum):
    p = core.create_profile("Beispiel Technik")
    assert p["slug"] == "beispiel-technik" and p["name"] == "Beispiel Technik"
    assert core.profil_pfade(p["slug"])["data"].is_dir()
    assert [x["slug"] for x in core.list_profiles()] == ["beispiel-technik"]


def test_gleicher_name_bekommt_eigenen_slug(profilraum):
    a = core.create_profile("Acme")
    b = core.create_profile("Acme")
    assert a["slug"] == "acme" and b["slug"] == "acme-2"
    assert core.profil_pfade(b["slug"])["data"].is_dir()


def test_leerer_name_wirft(profilraum):
    with pytest.raises(ValueError):
        core.create_profile("   ")


def test_neues_profil_gilt_als_nicht_eingerichtet(profilraum):
    core.create_profile("Acme")
    assert core.list_profiles()[0]["eingerichtet"] is False


# ── umschalten ───────────────────────────────────────────────────────────
def test_use_profile_biegt_alle_pfade_um(profilraum):
    p = core.create_profile("Acme")
    core.use_profile(p["slug"])
    basis = core.PROFILES_DIR / "acme"
    assert core.CONFIG_PATH == basis / "config.toml"
    assert core.DATA_DIR == basis / "data"
    assert core.DB_PATH == basis / "data" / "bookkeeping.db"
    assert core.DOC_DIR == basis / "data" / "documents"
    assert core.STATEMENT_DIR == basis / "data" / "statements"
    assert core.SETTINGS_PATH == basis / "data" / "settings.json"
    assert core.AKTIVES_PROFIL == "acme"


def test_use_profile_none_geht_zurueck_auf_die_wurzel(profilraum):
    core.create_profile("Acme")
    core.use_profile("acme")
    core.use_profile(None)
    assert core.CONFIG_PATH == core.HOME / "config.toml"
    assert core.DATA_DIR == core.HOME / "data"
    assert core.active_profile() is None


def test_unbekanntes_profil_wirft(profilraum):
    with pytest.raises(KeyError):
        core.use_profile("gibt-es-nicht")


def test_active_profile_liefert_den_eintrag(profilraum):
    core.create_profile("Acme")
    core.use_profile("acme")
    assert core.active_profile()["name"] == "Acme"
    assert core.active_profile()["aktiv"] is True


# ── Trennung der Daten ───────────────────────────────────────────────────
def test_profile_teilen_sich_nichts(profilraum):
    core.create_profile("Eins")
    core.create_profile("Zwei")

    core.use_profile("eins")
    conn = core.db()
    core.create_customer(conn, "Kunde A", "Berlin", None)
    conn.commit()

    core.use_profile("zwei")
    conn2 = core.db()
    assert core.list_customers(conn2) == [], "Profil 2 startet leer"
    core.create_customer(conn2, "Kunde B", "Hamburg", None)
    conn2.commit()

    core.use_profile("eins")
    namen = [k["name"] for k in core.list_customers(core.db())]
    assert namen == ["Kunde A"], "Profil 1 sieht nur die eigenen Kunden"


def test_belege_landen_im_profil(profilraum, tmp_path):
    core.create_profile("Eins")
    core.use_profile("eins")
    conn = core.db()
    quelle = tmp_path / "b.pdf"
    quelle.write_bytes(b"%PDF-1.4")
    core.create_expense(conn, date_="2026-03-05", vendor="V", description=None,
                        category="4980 - Sonstiges", gross="10", vat_rate=19,
                        paid_date=None, receipt_src=str(quelle))
    gespeichert = core.list_expenses(conn)[0]["receipt_path"]
    assert not gespeichert.startswith("/")
    assert core.PROFILES_DIR / "eins" / "data" in core.abs_pfad(gespeichert).parents


# ── umbenennen und entfernen ─────────────────────────────────────────────
def test_rename_profile(profilraum):
    core.create_profile("Alt")
    core.rename_profile("alt", "Neu")
    assert core.get_profile("alt")["name"] == "Neu"


def test_rename_unbekannt_wirft(profilraum):
    with pytest.raises(KeyError):
        core.rename_profile("weg", "X")


def test_delete_profile_nimmt_aus_der_liste_laesst_daten_liegen(profilraum):
    core.create_profile("Weg")
    daten = core.profil_pfade("weg")["data"]
    core.delete_profile("weg")
    assert core.list_profiles() == []
    assert daten.is_dir(), "Daten bleiben zur Sicherheit auf der Platte"


def test_delete_unbekannt_wirft(profilraum):
    with pytest.raises(KeyError):
        core.delete_profile("weg")


# ── Registry robust ──────────────────────────────────────────────────────
def test_kaputte_registry_wirft_nicht(profilraum):
    core.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    core.PROFILES_INDEX.write_text("{kein json", encoding="utf-8")
    assert core.list_profiles() == []


def test_ohne_registry_leere_liste(profilraum):
    assert core.list_profiles() == []
