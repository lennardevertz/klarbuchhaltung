"""Umzug einer bestehenden Installation ins erste Profil.

Das ist der einzige Vorgang, der echte Nutzerdaten bewegt – entsprechend
kleinteilig geprüft: nichts darf verloren gehen, und Belege müssen danach
weiter auffindbar sein.
"""
import sqlite3

import pytest

import core
from conftest import CONFIG_TOML

GLOBALS = ("HOME", "CONFIG_PATH", "DATA_DIR", "DB_PATH", "STATEMENT_DIR", "DOC_DIR",
           "SETTINGS_PATH", "AKTIVES_PROFIL")


@pytest.fixture
def altinstallation(tmp_path, monkeypatch):
    """Wurzel mit config.toml + data/ wie vor der Profil-Umstellung."""
    vorher = {n: getattr(core, n) for n in GLOBALS}
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "HOME", tmp_path)
    monkeypatch.setattr(core, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(core, "PROFILES_INDEX", tmp_path / "profiles" / "profiles.json")
    core.AKTIVES_PROFIL = None
    core.use_profile(None)          # bindet CONFIG_PATH/DATA_DIR auf tmp_path

    (tmp_path / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    try:
        yield tmp_path
    finally:
        for n, v in vorher.items():
            setattr(core, n, v)


def _fuelle(tmp_path, offen_lassen=False):
    """Etwas Bestand anlegen: Kunde, Ausgabe mit Beleg.

    Schliesst die Verbindung wieder – Windows verschiebt keinen Ordner, in dem
    noch eine Datei geöffnet ist, und danach wird migriert.
    """
    conn = core.db()
    core.create_customer(conn, "Kunde A", "Berlin", None)
    quelle = tmp_path / "beleg.pdf"
    quelle.write_bytes(b"%PDF-1.4 inhalt")
    core.create_expense(conn, date_="2026-03-05", vendor="Telekom", description=None,
                        category="4920 - Telefon", gross="119", vat_rate=19,
                        paid_date=None, receipt_src=str(quelle))
    conn.commit()
    if offen_lassen:
        return conn
    conn.close()
    return None


# ── Erkennung ────────────────────────────────────────────────────────────
def test_braucht_migration_bei_altlayout(altinstallation):
    assert core.braucht_migration() is True


def test_braucht_keine_migration_ohne_config(altinstallation):
    (altinstallation / "config.toml").unlink()
    assert core.braucht_migration() is False


def test_braucht_keine_migration_wenn_profile_existieren(altinstallation):
    core.create_profile("Schon da")
    assert core.braucht_migration() is False


def test_migration_ist_idempotent(altinstallation):
    _fuelle(altinstallation)
    erst = core.migrate_wurzel_zu_profil()
    assert erst is not None
    assert core.migrate_wurzel_zu_profil() is None, "zweiter Lauf tut nichts"
    assert len(core.list_profiles()) == 1


# ── Umzug ────────────────────────────────────────────────────────────────
def test_profilname_kommt_aus_der_config(altinstallation):
    p = core.migrate_wurzel_zu_profil()
    assert p["name"] == "Testfirma" and p["slug"] == "testfirma"


def test_config_und_daten_landen_im_profil(altinstallation):
    _fuelle(altinstallation)
    p = core.migrate_wurzel_zu_profil()
    pfade = core.profil_pfade(p["slug"])
    assert pfade["config"].exists() and pfade["db"].exists()
    assert not (altinstallation / "config.toml").exists(), "Wurzel ist geräumt"
    assert not (altinstallation / "data").exists()


def test_bestand_ueberlebt_den_umzug(altinstallation):
    _fuelle(altinstallation)
    p = core.migrate_wurzel_zu_profil()
    core.use_profile(p["slug"])
    conn = core.db()
    assert [k["name"] for k in core.list_customers(conn)] == ["Kunde A"]
    assert len(core.list_expenses(conn)) == 1


def test_beleg_bleibt_nach_dem_umzug_lesbar(altinstallation):
    """Der Kern: absolute Pfade würden hier ins Leere zeigen."""
    _fuelle(altinstallation)
    p = core.migrate_wurzel_zu_profil()
    core.use_profile(p["slug"])
    gespeichert = core.list_expenses(core.db())[0]["receipt_path"]
    assert not gespeichert.startswith("/"), "relativ gespeichert"
    ziel = core.abs_pfad(gespeichert)
    assert ziel.exists() and ziel.read_bytes() == b"%PDF-1.4 inhalt"
    assert core.PROFILES_DIR in ziel.parents


def test_absolute_altpfade_werden_vor_dem_umzug_gezogen(altinstallation):
    """DB mit absoluten Pfaden von vor der Umstellung."""
    conn = _fuelle(altinstallation, offen_lassen=True)
    beleg = core.abs_pfad(core.list_expenses(conn)[0]["receipt_path"])
    conn.execute("UPDATE expenses SET receipt_path = ?", (str(beleg),))   # zurück auf absolut
    conn.commit()
    conn.close()

    p = core.migrate_wurzel_zu_profil()
    core.use_profile(p["slug"])
    gespeichert = core.list_expenses(core.db())[0]["receipt_path"]
    assert not gespeichert.startswith("/")
    assert core.abs_pfad(gespeichert).read_bytes() == b"%PDF-1.4 inhalt"


def test_config_inhalt_bleibt_unveraendert(altinstallation):
    original = (altinstallation / "config.toml").read_text(encoding="utf-8")
    p = core.migrate_wurzel_zu_profil()
    assert core.profil_pfade(p["slug"])["config"].read_text(encoding="utf-8") == original


def test_umzug_ohne_datenordner(altinstallation):
    """Frisch eingerichtet, aber noch nie benutzt – data/ fehlt."""
    assert not (altinstallation / "data").exists()
    p = core.migrate_wurzel_zu_profil()
    pfade = core.profil_pfade(p["slug"])
    assert pfade["config"].exists() and pfade["data"].is_dir()


def test_nach_migration_ist_profil_eingerichtet(altinstallation):
    core.migrate_wurzel_zu_profil()
    assert core.list_profiles()[0]["eingerichtet"] is True


def test_zaehlwerte_bleiben_gleich(altinstallation):
    """Stichprobe über alle Tabellen: kein Datensatz geht verloren."""
    conn = _fuelle(altinstallation, offen_lassen=True)
    vorher = {t: conn.execute(f'select count(*) from "{t}"').fetchone()[0]
              for (t,) in conn.execute("select name from sqlite_master where type='table'")}
    conn.close()

    p = core.migrate_wurzel_zu_profil()
    core.use_profile(p["slug"])
    conn2 = core.db()
    nachher = {t: conn2.execute(f'select count(*) from "{t}"').fetchone()[0] for t in vorher}
    assert nachher == vorher


def test_migration_laeuft_nicht_waehrend_einer_anfrage(altinstallation):
    """Sie verschiebt Ordner – das gehört an den Start, nicht in before_request.

    Unter Windows scheitert ein Verschieben, sobald irgendwo eine Datei offen
    ist; genau das ist im plattformübergreifenden Lauf aufgeschlagen.
    """
    import app as webapp
    quelle = (webapp.__file__ and open(webapp.__file__, encoding="utf-8").read())
    vor_request = quelle.split("def _profil_binden")[1].split("def ")[0]
    assert "migrate_wurzel_zu_profil" not in vor_request


def test_altinstallation_bleibt_ohne_migration_bedienbar(altinstallation):
    """Bis zum nächsten Start arbeitet die App einfach mit der Wurzel-config."""
    import app as webapp
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as c:
        assert c.get("/invoices").status_code == 200
    assert core.braucht_migration() is True, "Umzug steht weiterhin aus"
