"""Willkommens-Screen und Profilwechsel in der Web-App."""
import pytest

import app as webapp
import core


@pytest.fixture
def client(sandbox):
    core.CONFIG_PATH.unlink()          # keine Wurzel-config: reiner Profilbetrieb
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as c:
        yield c


def _einrichten(slug, name="Firma"):
    """Profil so weit bringen, dass es eine config.toml hat."""
    core.use_profile(slug)
    core.write_config(business={"name": name, "owner": name, "address_lines": ["A", "B"],
                                "vat_id": "", "tax_number": "", "email": "", "phone": ""},
                      bank={"holder": name, "iban": "", "bic": ""},
                      invoice={"currency": "EUR", "default_vat_rate": 19,
                               "payment_terms_days": 14, "language": "de"},
                      tax={"taxation": "ist", "va_period": "quarter", "small_business": False})


# ── ohne Profil ──────────────────────────────────────────────────────────
def test_ohne_profil_fuehrt_alles_zur_auswahl(client):
    r = client.get("/invoices")
    assert r.status_code == 302 and r.headers["Location"].endswith("/profile")


def test_auswahl_ist_erreichbar_und_zeigt_neu(client):
    html = client.get("/profile").get_data(as_text=True)
    assert "Wer bucht?" in html and "Neues Profil" in html


# ── anlegen ──────────────────────────────────────────────────────────────
def test_neues_profil_fuehrt_in_die_einrichtung(client):
    r = client.post("/profile/neu", data={"name": "Zweite Firma"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup")
    assert [p["name"] for p in core.list_profiles()] == ["Zweite Firma"]


def test_neues_profil_ohne_namen_wird_abgelehnt(client):
    r = client.post("/profile/neu", data={"name": "  "})
    assert r.headers["Location"].endswith("/profile")
    assert core.list_profiles() == []


def test_angelegtes_profil_ist_direkt_aktiv(client):
    client.post("/profile/neu", data={"name": "Meins"})
    with client.session_transaction() as s:
        assert s["profil"] == "meins"


# ── auswählen ────────────────────────────────────────────────────────────
def test_einziges_profil_wird_ohne_klick_geoeffnet(client):
    p = core.create_profile("Einzig")
    _einrichten(p["slug"])
    assert client.get("/invoices").status_code == 200


def test_bei_mehreren_erscheint_die_auswahl(client):
    for n in ("Eins", "Zwei"):
        p = core.create_profile(n)
        _einrichten(p["slug"], n)
    r = client.get("/invoices")
    assert r.status_code == 302 and r.headers["Location"].endswith("/profile")

    html = client.get("/profile").get_data(as_text=True)
    assert "Eins" in html and "Zwei" in html


def test_oeffnen_setzt_das_profil(client):
    for n in ("Eins", "Zwei"):
        _einrichten(core.create_profile(n)["slug"], n)
    r = client.post("/profile/zwei/oeffnen")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    with client.session_transaction() as s:
        assert s["profil"] == "zwei"


def test_unbekanntes_profil_oeffnen_gibt_404(client):
    core.create_profile("Eins")
    assert client.post("/profile/gibtsnicht/oeffnen").status_code == 404


def test_wechseln_leert_die_auswahl(client):
    for n in ("Eins", "Zwei"):
        _einrichten(core.create_profile(n)["slug"], n)
    client.post("/profile/eins/oeffnen")
    r = client.post("/profile/wechseln")
    assert r.headers["Location"].endswith("/profile")
    with client.session_transaction() as s:
        assert "profil" not in s


def test_entferntes_profil_faellt_aus_der_sitzung(client):
    """Bleiben mehrere übrig, landet man wieder bei der Auswahl."""
    for n in ("Eins", "Zwei", "Drei"):
        _einrichten(core.create_profile(n)["slug"], n)
    client.post("/profile/eins/oeffnen")
    core.delete_profile("eins")
    r = client.get("/invoices")
    assert r.status_code == 302 and r.headers["Location"].endswith("/profile")
    with client.session_transaction() as s:
        assert "profil" not in s


def test_letztes_verbliebenes_profil_wird_direkt_geoeffnet(client):
    for n in ("Eins", "Zwei"):
        _einrichten(core.create_profile(n)["slug"], n)
    client.post("/profile/eins/oeffnen")
    core.delete_profile("eins")
    assert client.get("/invoices").status_code == 200
    with client.session_transaction() as s:
        assert s["profil"] == "zwei"


# ── Trennung über die Web-Oberfläche ─────────────────────────────────────
def test_daten_bleiben_je_profil_getrennt(client):
    for n in ("Eins", "Zwei"):
        _einrichten(core.create_profile(n)["slug"], n)

    client.post("/profile/eins/oeffnen")
    client.post("/customers", data={"name": "Nur bei Eins", "address": "Berlin"},
                follow_redirects=True)

    client.post("/profile/zwei/oeffnen")
    html = client.get("/invoices?tab=customers").get_data(as_text=True)
    assert "Nur bei Eins" not in html, "Profil 2 sieht die Kunden von Profil 1 nicht"

    client.post("/profile/eins/oeffnen")
    html = client.get("/invoices?tab=customers").get_data(as_text=True)
    assert "Nur bei Eins" in html


# ── verwalten ────────────────────────────────────────────────────────────
def test_umbenennen(client):
    _einrichten(core.create_profile("Alt")["slug"])
    client.post("/profile/alt/umbenennen", data={"name": "Neu"})
    assert core.get_profile("alt")["name"] == "Neu"


def test_entfernen_laesst_die_daten_liegen(client):
    p = core.create_profile("Weg")
    _einrichten(p["slug"])
    daten = core.profil_pfade(p["slug"])["data"]
    client.post("/profile/weg/entfernen")
    assert core.list_profiles() == []
    assert daten.is_dir()


def test_seitenleiste_zeigt_das_aktive_profil(client):
    _einrichten(core.create_profile("Meine Firma")["slug"], "Meine Firma")
    html = client.get("/invoices").get_data(as_text=True)
    assert "Meine Firma" in html


def test_konto_menue_buendelt_die_aktionen(client):
    """Unten nur EINE Kachel; Einstellungen, Profilwahl und Verlassen im Menü."""
    _einrichten(core.create_profile("Meine Firma")["slug"], "Meine Firma")
    html = client.get("/invoices").get_data(as_text=True)
    assert html.count('class="kl-account"') == 1
    assert html.count('class="kl-user"') == 1, "keine gestapelten Kacheln mehr"
    assert "Einstellungen" in html
    assert "Profile verwalten" in html
    assert "Profil verlassen" in html


def test_setup_zeigt_keine_konto_kachel(client):
    """Während der Einrichtung führen die Menüpunkte ins Leere."""
    client.post("/profile/neu", data={"name": "Frisch"})
    html = client.get("/setup").get_data(as_text=True)
    assert 'class="kl-account"' not in html
