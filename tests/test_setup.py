"""Ersteinrichtung: ohne config.toml führt alles zum Setup, danach nie wieder."""
import pytest

import app as webapp
import core

GUELTIG = {
    "name": "Beispiel Design", "owner": "Erika Mustermann",
    "street": "Beispielweg 7", "city": "20095 Hamburg", "country": "Germany",
    "vat_id": "DE999999999", "email": "erika@example.com",
    "iban": "DE00 0000 0000 0000 0000 00", "bic": "TESTDEFF",
    "currency": "EUR", "language": "de", "default_vat_rate": "19",
    "payment_terms_days": "30", "taxation": "soll", "va_period": "month",
}


@pytest.fixture
def frisch(sandbox):
    """Frisch angelegtes, noch nicht eingerichtetes Profil – wie nach „Neues Profil"."""
    core.CONFIG_PATH.unlink()          # Wurzel-config aus der Sandbox weg
    eintrag = core.create_profile("Neue Firma")
    core.use_profile(eintrag["slug"])
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as c:
        with c.session_transaction() as s:
            s["profil"] = eintrag["slug"]
        yield c


# ── Weiterleitung ────────────────────────────────────────────────────────
@pytest.mark.parametrize("pfad", ["/", "/invoices", "/reports", "/settings", "/files"])
def test_ohne_config_immer_zum_setup(frisch, pfad):
    r = frisch.get(pfad)
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup")


def test_setup_seite_ist_erreichbar(frisch):
    r = frisch.get("/setup")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Neue Firma einrichten" in html, "Überschrift nennt das Profil"
    assert 'name="vat_id"' in html and 'name="iban"' in html


def test_setup_laeuft_in_der_app_huelle_ohne_navigation(frisch):
    """setup_modus blendet die Navigation aus – die Ziele kämen ohnehin zurück."""
    html = frisch.get("/setup").get_data(as_text=True)
    assert 'class="kl-side"' in html, "App-Hülle ist da"
    assert 'class="kl-nav"' not in html, "aber ohne Navigation"


def test_static_bleibt_ohne_config_erreichbar(frisch):
    """Sonst lädt die Setup-Seite ihr eigenes CSS nicht."""
    assert frisch.get("/static/tokens.css").status_code == 200


# ── Formular ─────────────────────────────────────────────────────────────
def test_pflichtfelder_fehlen_keine_config(frisch):
    r = frisch.post("/setup", data={"name": "", "owner": ""})
    assert r.status_code == 400
    assert not core.config_exists()


def test_setup_schreibt_config_und_leitet_weiter(frisch):
    r = frisch.post("/setup", data=GUELTIG)
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    cfg = core.load_config()
    assert cfg["business"]["name"] == "Beispiel Design"
    assert cfg["business"]["address_lines"] == ["Beispielweg 7", "20095 Hamburg", "Germany"]
    assert cfg["invoice"]["payment_terms_days"] == 30
    assert cfg["tax"]["taxation"] == "soll" and cfg["tax"]["va_period"] == "month"
    assert cfg["bookkeeping"]["expense_accounts"], "Kontenrahmen wurde mitgeschrieben"


def test_kontoinhaber_faellt_auf_inhaber_zurueck(frisch):
    daten = {**GUELTIG}
    daten.pop("iban")
    frisch.post("/setup", data=daten)
    assert core.load_config()["bank"]["holder"] == "Erika Mustermann"


def test_kleinunternehmer_wird_uebernommen(frisch):
    frisch.post("/setup", data={**GUELTIG, "small_business": "1"})
    assert core.load_config()["tax"]["small_business"] is True


def test_kaputte_zahlen_fallen_auf_vorgabe(frisch):
    frisch.post("/setup", data={**GUELTIG, "payment_terms_days": "bald", "default_vat_rate": ""})
    cfg = core.load_config()
    assert cfg["invoice"]["payment_terms_days"] == 14
    assert cfg["invoice"]["default_vat_rate"] == 19


def test_anfuehrungszeichen_im_namen_zerlegen_die_toml_nicht(frisch):
    frisch.post("/setup", data={**GUELTIG, "name": 'Ana "Ana" Ruiz \\ Co'})
    assert core.load_config()["business"]["name"] == 'Ana "Ana" Ruiz \\ Co'


# ── Danach ───────────────────────────────────────────────────────────────
def test_setup_nach_einrichtung_leitet_weg(frisch):
    frisch.post("/setup", data=GUELTIG)
    r = frisch.get("/setup")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")


def test_seiten_tragen_nach_einrichtung(frisch):
    frisch.post("/setup", data=GUELTIG)
    assert frisch.get("/").status_code == 200
    assert frisch.get("/invoices").status_code == 200


def test_write_config_ueberschreibt_bestehende_nicht(sandbox):
    with pytest.raises(FileExistsError):
        core.write_config(business={"name": "X"}, bank={}, invoice={}, tax={})
