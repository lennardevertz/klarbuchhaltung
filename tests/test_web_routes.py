"""Flask-Routen, die eigene Datei-Ablage-Logik enthalten (nicht nur core durchreichen).

Hintergrund: Der Beleg-Upload in /expenses schrieb früher direkt nach RECEIPT_DIR.
Nach der Umstellung auf documents/{eingang|ausgang}/JJJJ/Qn wurde dieser Ordner nicht
mehr angelegt -> FileNotFoundError. Reine core-Tests haben das nicht gefangen,
weil die Route ihren eigenen Pfad baut. Daher hier auf Routen-Ebene testen.
"""
import io

import pytest

import app as webapp
import core


@pytest.fixture
def client(sandbox):
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as c:
        yield c


def post_expense(client, *, receipt=None, date_="2026-03-05", vendor="Telekom"):
    data = {"date": date_, "vendor": vendor, "description": "Mobilfunk",
            "category": "4920 - Telefon", "gross": "119", "vat_rate": "19"}
    if receipt is not None:
        data["receipt"] = (io.BytesIO(receipt), "rechnung März.pdf")
    return client.post("/expenses", data=data,
                       content_type="multipart/form-data" if receipt is not None else None,
                       follow_redirects=False)


# ── /expenses: Ausgabe + Beleg-Upload ────────────────────────────────────
def test_expense_ohne_beleg(client, conn):
    r = post_expense(client)
    assert r.status_code == 302
    e = core.list_expenses(conn)[0]
    assert e["vendor"] == "Telekom" and e["net"] == 100.0
    assert e["receipt_path"] is None


def test_expense_mit_beleg_landet_im_eingang(client, conn):
    """Regression: der Upload muss in documents/eingang/JJJJ/Qn schreiben."""
    r = post_expense(client, receipt=b"%PDF-1.4 beleg")
    assert r.status_code == 302, "Upload darf nicht mit 500 abbrechen"
    e = core.list_expenses(conn)[0]
    assert e["receipt_path"], "Beleg muss an der Ausgabe hängen"
    p = core.abs_pfad(e["receipt_path"])
    assert p.exists() and p.read_bytes() == b"%PDF-1.4 beleg"
    assert p.parent == core.DOC_DIR / "eingang" / "2026" / "Q1"
    assert p.name == f"exp{e['id']}_rechnung März.pdf"


def test_expense_beleg_quartal_folgt_belegdatum(client, conn):
    post_expense(client, receipt=b"%PDF", date_="2026-08-14")
    p = core.abs_pfad(core.list_expenses(conn)[0]["receipt_path"])
    assert p.parent == core.DOC_DIR / "eingang" / "2026" / "Q3"


def test_expense_ohne_vendor_legt_nichts_an(client, conn):
    r = client.post("/expenses", data={"date": "2026-03-05", "vendor": "  ", "gross": "10"})
    assert r.status_code == 302
    assert core.list_expenses(conn) == []


def test_expense_leerer_dateiname_wird_ignoriert(client, conn):
    """Browser schicken bei leerem Feld eine Datei mit filename='' – die darf nichts schreiben."""
    r = client.post("/expenses", data={
        "date": "2026-03-05", "vendor": "Telekom", "category": "4920 - Telefon",
        "gross": "119", "vat_rate": "19", "receipt": (io.BytesIO(b""), "")},
        content_type="multipart/form-data")
    assert r.status_code == 302
    assert core.list_expenses(conn)[0]["receipt_path"] is None


# ── Smoke: Kernseiten rendern ────────────────────────────────────────────
def test_healthz(client):
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("url", ["/", "/invoices", "/reports", "/zahlungen", "/files",
                                 "/categories", "/recurring", "/dunning", "/settings"])
def test_seiten_rendern_ohne_daten(client, conn, url):
    r = client.get(url)
    assert r.status_code == 200, f"{url} rendert nicht"


# ── /invoices/<id>/delete: Löschen aus der Liste ─────────────────────────
def _make_invoice(conn):
    cfg = core.load_config()
    cust = core.resolve_or_create_customer(conn, "ACME GmbH", "Str. 1;10115 Berlin", None)
    return core.create_invoice(
        conn, cfg, customer=cust, kind="domestic", issue_date="2026-03-10",
        service_from="2026-03-01", service_to=None,
        items=[{"description": "Beratung", "quantity": 1, "unit": "Pauschal",
                "unit_price": 1000, "vat_rate": 19}])


def test_invoice_delete_route(client, conn):
    inv = _make_invoice(conn)
    r = client.post(f"/invoices/{inv['id']}/delete")
    assert r.status_code == 302
    assert "/invoices?dir=out&year=2026" in r.headers["Location"]
    assert core.list_invoices(conn) == []


def test_invoice_delete_zeigt_x_in_der_zeile(client, conn):
    inv = _make_invoice(conn)
    html = client.get("/invoices?dir=out&year=2026").get_data(as_text=True)
    assert f"/invoices/{inv['id']}/delete" in html
    assert "inv-del" in html
    # Bestätigung als Klar-Modal, nicht als Browser-confirm()
    assert 'id="del-modal"' in html and "confirm(" not in html


def test_invoice_delete_unbekannt_ohne_crash(client, conn):
    r = client.post("/invoices/999/delete")
    assert r.status_code == 302


# ── Kunden: löschen & Doppel zusammenführen ──────────────────────────────
def test_customer_delete_route(client, conn):
    c = core.create_customer(conn, "Weg AG", "Str. 1", None)
    r = client.post(f"/customers/{c['id']}/delete")
    assert r.status_code == 302
    assert core.list_customers(conn) == []


def test_customer_delete_auch_mit_rechnung(client, conn):
    """Löschen ist unabhängig von Rechnungen – die Rechnung bleibt bestehen."""
    inv = _make_invoice(conn)
    r = client.post(f"/customers/{inv['customer_id']}/delete", follow_redirects=True)
    assert "1 Rechnung(en) bleiben bestehen" in r.get_data(as_text=True)
    assert core.list_customers(conn) == []
    assert core.get_invoice(conn, inv["id"])["customer_name"] == "ACME GmbH"


def test_kundenliste_zeigt_x_und_duplikat_banner(client, conn):
    core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    core.create_customer(conn, "example software oü", "", None)
    html = client.get("/invoices?tab=customers").get_data(as_text=True)
    assert "cust-del" in html and "/customers/1/delete" in html
    assert "doppelt angelegt" in html and "/customers/merge-duplicates" in html


def test_merge_duplicates_route(client, conn):
    core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    core.create_customer(conn, "example software oü", "", None)
    r = client.post("/customers/merge-duplicates", follow_redirects=True)
    assert r.status_code == 200
    assert len(core.list_customers(conn)) == 1
    assert "doppelt angelegt" not in r.get_data(as_text=True)


# ── Kundennummern ────────────────────────────────────────────────────────
def test_kunde_anlegen_mit_nummer(client, conn):
    r = client.post("/invoices/customer", data={"name": "ACME GmbH", "number": "10001"})
    assert r.status_code == 302
    assert core.list_customers(conn)[0]["number"] == "10001"


def test_kunde_anlegen_doppelte_nummer_wird_abgelehnt(client, conn):
    core.create_customer(conn, "A", "Str. 1", None, "10001")
    r = client.post("/invoices/customer", data={"name": "B", "number": "10001"},
                    follow_redirects=True)
    assert "schon an" in r.get_data(as_text=True)
    assert len(core.list_customers(conn)) == 1


def test_customer_number_route_setzt_und_entfernt(client, conn):
    c = core.create_customer(conn, "ACME", "Str. 1", None)
    client.post(f"/customers/{c['id']}/number", data={"number": "K-42"})
    assert core.get_customer(conn, c["id"])["number"] == "K-42"
    client.post(f"/customers/{c['id']}/number", data={"number": ""})
    assert core.get_customer(conn, c["id"])["number"] is None


def test_kundenliste_zeigt_nummer_und_vorschlag(client, conn):
    core.create_customer(conn, "ACME", "Str. 1", None, "10001")
    html = client.get("/invoices?tab=customers").get_data(as_text=True)
    assert "10001" in html and f"/customers/1/number" in html
    assert "10002" in html, "nächste freie Nummer wird vorgeschlagen"


def test_delete_modal_nennt_die_rechnungen(client, conn):
    inv = _make_invoice(conn)
    html = client.get("/invoices?tab=customers").get_data(as_text=True)
    assert f'data-invoices="{inv["number"]} (2026)"' in html


def test_rechnung_an_empfaenger_ohne_registereintrag(client, conn):
    r = client.post("/invoices/new", data={
        "new_name": "Einmal AG", "new_address": "Str. 1; 12345 Stadt", "no_save": "1",
        "kind": "domestic", "issue_date": "2026-03-10", "service_from": "2026-03-01",
        "item_desc": "Beratung", "item_qty": "1", "item_unit": "h",
        "item_price": "1000", "item_rate": "19"})
    assert r.status_code == 302
    assert core.list_customers(conn) == [], "Empfänger darf nicht im Register landen"
    inv = core.list_invoices(conn)[0]
    assert inv["customer_name"] == "Einmal AG" and inv["customer_id"] is None


def test_rechnung_legt_kunden_an_wenn_no_save_fehlt(client, conn):
    client.post("/invoices/new", data={
        "new_name": "Neu AG", "kind": "domestic", "issue_date": "2026-03-10",
        "service_from": "2026-03-01", "item_desc": "Beratung", "item_qty": "1",
        "item_unit": "h", "item_price": "1000", "item_rate": "19"})
    assert [c["name"] for c in core.list_customers(conn)] == ["Neu AG"]


def test_chat_rechnung_ohne_registereintrag(client, conn):
    r = client.post("/chat/api/invoice", json={
        "customer_name": "Einmal AG", "amount": 1000, "term_days": 14,
        "service": "Beratung", "kind": "domestic", "vat_rate": 19, "no_save": True})
    assert r.status_code == 200 and "error" not in r.get_json()
    assert core.list_customers(conn) == []
    assert core.list_invoices(conn)[0]["customer_name"] == "Einmal AG"


# ── USt-Sonderfälle in der Oberfläche ────────────────────────────────────
def test_ustva_zeigt_offene_13b_belege_mit_zuordnungs_buttons(client, conn):
    """Ohne EU/Drittland-Zuordnung fehlt die Steuer in der Anmeldung – der Report muss das
    zeigen und direkt auflösbar machen, statt still weiterzurechnen."""
    core.create_expense(conn, date_="2026-03-05", vendor="OpenAI, LLC", description=None,
                        category="4980 - Sonstiges", gross="10.00", vat_rate=0,
                        paid_date="2026-03-05", reverse_charge=1)
    html = client.get("/reports?p=2026-Q1&gen=ustva").get_data(as_text=True)
    assert "OpenAI, LLC" in html and "ohne Zuordnung" in html
    assert 'value="rc_other"' in html and 'value="rc_eu"' in html

    eid = conn.execute("SELECT id FROM expenses").fetchone()["id"]
    r = client.post(f"/expenses/{eid}/vat-kind", data={"vat_kind": "rc_other", "back": "2026-Q1"})
    assert r.status_code == 302
    assert core.vat_open_rc(conn, "2026-01-01", "2026-03-31") == []
    html = client.get("/reports?p=2026-Q1&gen=ustva").get_data(as_text=True)
    assert "ohne Zuordnung" not in html
    assert ">84<" in html                       # Kz 84 ist jetzt belegt


def test_buchungssatz_modal_bietet_die_ust_arten_an(client):
    """Die §13b-Pille war früher nur Kosmetik (setzte bloß 0 % Vorsteuer). Jetzt muss jede
    USt-Art wählbar sein und als eigenes Feld mitgeschickt werden."""
    html = client.get("/zahlungen").get_data(as_text=True)
    assert 'name="vat_kind"' in html
    for kind in ("rc_eu", "rc_other", "ig_erwerb", "import"):
        assert f'data-k="{kind}"' in html
    assert 'data-k="rc_unknown"' not in html     # „offen" ist kein wählbarer Zustand


def test_ausgabe_aus_bankbuchung_uebernimmt_die_ust_art(client, conn, cfg):
    """Der Weg, den Nutzer real gehen: Zahlung -> Buchungssatz -> §13b."""
    tid = core.create_manual_transaction(conn, "2026-03-05", "ANTHROPIC PBC", -100)
    r = client.post(f"/zahlungen/tx/{tid}/set",
                    data={"action": "expense", "category": "4980 - Sonstiges",
                          "vat_rate": "19", "vat_kind": "rc_other"})
    assert r.status_code == 302
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert (fig["rc_other_base"], fig["rc_other_tax"]) == (100, 19)


def test_ausgabe_per_formular_mit_13b_landet_in_kz_84(client, conn, cfg):
    client.post("/expenses", data={"date": "2026-03-05", "vendor": "Anthropic, PBC",
                                   "category": "4980 - Sonstiges", "gross": "100",
                                   "vat_rate": "19", "vat_kind": "rc_other"})
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert (fig["rc_other_base"], fig["rc_other_tax"]) == (100, 19)


def test_einstellungen_speichern_dauerfristverlaengerung(client):
    r = client.post("/settings", data={"section": "steuer", "ust_dauerfrist": "on",
                                       "ust_sondervorauszahlung": "1100"})
    assert r.status_code == 302
    assert core.load_config()["tax"]["dauerfrist"] is True
    assert core.load_config()["tax"]["sondervorauszahlung"] == 1100


def test_rechnungsformular_bietet_leistungsart(client):
    html = client.get("/invoices/new").get_data(as_text=True)
    assert 'name="supply_type"' in html
    for st in ("service", "goods", "exempt"):
        assert f'value="{st}"' in html


def test_einstellungen_zeigen_den_beta_hinweis(client):
    """Nutzer sollen wissen, dass die App weder zertifiziert noch geprüft ist."""
    html = client.get("/settings").get_data(as_text=True)
    assert "Beta" in html and "eigene Gefahr" in html
    assert "ersetzt keine" in html
