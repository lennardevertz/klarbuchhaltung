"""Ausgangsrechnungen: Berechnung, Nummernkreis, Zahlung, PDF-Verwaltung."""
import pytest

import core


# ── Berechnung ───────────────────────────────────────────────────────────
def test_inland_rechnung_rechnet_ust_auf(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer)
    assert (inv["net"], inv["vat"], inv["gross"]) == (1000.0, 190.0, 1190.0)
    assert inv["kind"] == "domestic"


def test_mehrere_positionen_und_mengen(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, items=[
        {"description": "Beratung", "quantity": "7.5", "unit": "h", "unit_price": "120", "vat_rate": 19},
        {"description": "Buch", "quantity": "2", "unit": "Stk", "unit_price": "25", "vat_rate": 7},
    ])
    assert inv["net"] == pytest.approx(950.0)          # 900 + 50
    assert inv["vat"] == pytest.approx(174.5)          # 171 + 3.50
    assert inv["gross"] == pytest.approx(1124.5)


def test_komma_als_dezimaltrenner(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, items=[
        {"description": "Pos", "quantity": "1,5", "unit": "h", "unit_price": "99,99", "vat_rate": 19}])
    assert inv["net"] == pytest.approx(149.99)         # 1,5 * 99,99 = 149,985 -> 149,99


@pytest.mark.parametrize("kind", ["eu", "third"])
def test_reverse_charge_hat_keine_ust(conn, eu_customer, make_invoice, kind):
    inv = make_invoice(customer=eu_customer, kind=kind, items=[
        {"description": "Service", "quantity": "1", "unit": "", "unit_price": "500", "vat_rate": 19}])
    assert inv["vat"] == 0.0, "bei Reverse Charge darf nie USt entstehen"
    assert inv["gross"] == inv["net"] == 500.0


def test_unbekannter_typ_wirft(conn, cfg, customer):
    with pytest.raises(ValueError, match="Ungültiger Rechnungstyp"):
        core.create_invoice(conn, cfg, customer=customer, kind="mars",
                            issue_date="2026-03-10", service_from="2026-03-10",
                            service_to=None, items=[{"description": "x", "quantity": 1,
                                                     "unit_price": 1, "vat_rate": 19}])


def test_rechnung_ohne_position_wirft(conn, cfg, customer):
    with pytest.raises(ValueError, match="mindestens eine Position"):
        core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                            issue_date="2026-03-10", service_from="2026-03-10",
                            service_to=None, items=[])


def test_faelligkeit_folgt_zahlungsziel(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10")
    assert inv["due_date"] == "2026-03-24"             # 14 Tage laut Test-Config


def test_kundendaten_werden_eingefroren(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    core.create_customer(conn, "ACME GmbH", "neue Adresse", None)
    assert inv["customer_address"] == "Hauptstr. 1\n10115 Berlin"


# ── Nummernkreis ─────────────────────────────────────────────────────────
def test_nummernkreis_zaehlt_je_monat_hoch(conn, customer, make_invoice):
    a = make_invoice(customer=customer, issue_date="2026-03-01")
    b = make_invoice(customer=customer, issue_date="2026-03-20")
    c = make_invoice(customer=customer, issue_date="2026-04-01")
    assert a["number"] == "2026-03-0001"
    assert b["number"] == "2026-03-0002"
    assert c["number"] == "2026-04-0001", "neuer Monat startet wieder bei 1"


def test_next_invoice_number_leerer_monat(conn):
    assert core.next_invoice_number(conn, 2026, 5) == ("2026-05-0001", 1)


def test_vorgegebene_nummer_wird_uebernommen(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, number="2025-11-0007", issue_date="2026-03-10")
    assert (inv["number"], inv["year"], inv["month"], inv["seq"]) == ("2025-11-0007", 2025, 11, 7)


def test_doppelte_nummer_wirft(conn, customer, make_invoice):
    make_invoice(customer=customer, number="2026-03-0001")
    with pytest.raises(ValueError, match="existiert bereits"):
        make_invoice(customer=customer, number="2026-03-0001")


# ── Abruf ────────────────────────────────────────────────────────────────
def test_get_invoice_varianten(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    assert core.get_invoice(conn, inv["id"])["number"] == inv["number"]
    assert core.get_invoice_by_number(conn, inv["number"])["id"] == inv["id"]
    assert core.get_invoice_by_number(conn, "gibt-es-nicht") is None


def test_get_invoice_items(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, items=[
        {"description": "A", "quantity": "2", "unit": "h", "unit_price": "10", "vat_rate": 19},
        {"description": "B", "quantity": "1", "unit": "", "unit_price": "5", "vat_rate": 7}])
    items = core.get_invoice_items(conn, inv["id"])
    assert [i["description"] for i in items] == ["A", "B"]
    assert items[0]["line_net"] == 20.0
    assert items[1]["unit"] == "-", "leere Einheit wird als '-' gespeichert"


def test_list_invoices_sortiert_nach_nummernkreis(conn, customer, make_invoice):
    make_invoice(customer=customer, issue_date="2026-04-01")
    make_invoice(customer=customer, issue_date="2026-03-01")
    assert [i["number"] for i in core.list_invoices(conn)] == ["2026-03-0001", "2026-04-0001"]


# ── Zahlung ──────────────────────────────────────────────────────────────
def test_mark_invoice_paid(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    assert inv["paid_date"] is None
    core.mark_invoice_paid(conn, inv["id"], "2026-04-02")
    assert core.get_invoice(conn, inv["id"])["paid_date"] == "2026-04-02"


def test_record_invoice_payment_legt_buchung_an(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    r = core.record_invoice_payment(conn, inv["id"], "2026-04-02", method="Bar")
    assert r["tx"] is True
    assert r["amount"] == 1190.0 and r["amount_home"] == 1190.0
    assert core.get_invoice(conn, inv["id"])["paid_date"] == "2026-04-02"
    tx = core.list_transactions(conn)[0]
    assert tx["category"] == "einnahme_rechnung"
    assert tx["invoice_id"] == inv["id"]
    assert "Bar" in tx["description"]


def test_record_invoice_payment_teilbetrag(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    r = core.record_invoice_payment(conn, inv["id"], "2026-04-02", amount="500,50")
    assert r["amount"] == 500.5
    assert core.list_transactions(conn)[0]["amount"] == 500.5


def test_record_invoice_payment_ohne_buchung(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    r = core.record_invoice_payment(conn, inv["id"], "2026-04-02", create_tx=False)
    assert r["tx"] is False
    assert core.list_transactions(conn) == []
    assert core.get_invoice(conn, inv["id"])["paid_date"] == "2026-04-02"


def test_record_invoice_payment_unlesbarer_betrag_faellt_auf_brutto_zurueck(conn, customer,
                                                                           make_invoice):
    inv = make_invoice(customer=customer)
    r = core.record_invoice_payment(conn, inv["id"], "2026-04-02", amount="keine Zahl")
    assert r["amount"] == 1190.0


# ── PDF-Verwaltung (eigene Rechnungen -> documents/ausgang/JJJJ/Qn) ───────
def test_set_invoice_pdf_landet_im_ausgang(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10")   # Q1
    dest = core.set_invoice_pdf(conn, inv["id"], b"%PDF-1.4 fake", "extern.pdf")
    assert dest.exists() and dest.read_bytes().startswith(b"%PDF")
    assert dest.parent == core.DOC_DIR / "ausgang" / "2026" / "Q1"
    assert dest.name == f"Invoice-{inv['number']}.pdf"
    assert core.abs_pfad(core.get_invoice(conn, inv["id"])["pdf_path"]) == dest


def test_set_invoice_pdf_nutzt_zahldatum_fuers_quartal(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10", paid_date="2026-07-05")
    dest = core.set_invoice_pdf(conn, inv["id"], b"%PDF fake", "x.pdf")
    assert dest.parent == core.DOC_DIR / "ausgang" / "2026" / "Q3", "Zahldatum (Q3) schlägt Rechnungsdatum"


def test_regenerate_invoice_pdf_ohne_rechnung_wirft(conn, cfg):
    with pytest.raises(ValueError, match="nicht gefunden"):
        core.regenerate_invoice_pdf(conn, cfg, 999)


@pytest.mark.pdf
def test_create_invoice_erzeugt_pdf_im_ausgang(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10", render=True)
    path = core.abs_pfad(inv["pdf_path"])
    assert path.exists() and path.read_bytes().startswith(b"%PDF")
    assert path.name == f"Invoice-{inv['number']}.pdf"
    assert path.parent == core.DOC_DIR / "ausgang" / "2026" / "Q1"


@pytest.mark.pdf
@pytest.mark.parametrize("kind", ["domestic", "eu", "third"])
def test_regenerate_invoice_pdf(conn, cfg, eu_customer, make_invoice, kind):
    inv = make_invoice(customer=eu_customer, kind=kind, issue_date="2026-11-02")   # Q4
    assert inv["pdf_path"] is None
    dest = core.regenerate_invoice_pdf(conn, cfg, inv["id"])
    assert dest.exists() and dest.read_bytes().startswith(b"%PDF")
    assert dest.parent == core.DOC_DIR / "ausgang" / "2026" / "Q4"
    assert core.abs_pfad(core.get_invoice(conn, inv["id"])["pdf_path"]) == dest


@pytest.mark.pdf
def test_eingang_und_ausgang_liegen_getrennt(conn, cfg, customer, make_invoice, tmp_path):
    """Kern der Umstellung: eigene Rechnung -> ausgang/, Ausgaben-Beleg -> eingang/."""
    inv = make_invoice(customer=customer, issue_date="2026-03-10", render=True)
    src = tmp_path / "beleg.pdf"
    src.write_bytes(b"%PDF-1.4 beleg")
    core.create_expense(conn, date_="2026-03-05", vendor="Telekom", description=None,
                        category="4920 - Telefon", gross="119", vat_rate=19,
                        paid_date="2026-03-05", receipt_src=str(src))
    inv_pdf = core.abs_pfad(core.get_invoice(conn, inv["id"])["pdf_path"])
    exp_receipt = core.abs_pfad(core.list_expenses(conn)[0]["receipt_path"])
    assert core.DOC_DIR / "ausgang" in inv_pdf.parents
    assert core.DOC_DIR / "eingang" in exp_receipt.parents


# ── Löschen ──────────────────────────────────────────────────────────────
def test_delete_invoice_entfernt_alles_und_loest_zahlung(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10")
    # Zahlung künstlich zuordnen
    conn.execute("INSERT INTO transactions (date, description, amount, currency, category, invoice_id, created_at) "
                 "VALUES ('2026-03-12','Zahlung',1190,'EUR','einnahme',?,?)", (inv["id"], core._now()))
    conn.commit()
    res = core.delete_invoice(conn, inv["id"])
    assert res["deleted"] == inv["number"] and res["buchungen_geloest"] == 1
    assert core.get_invoice(conn, inv["id"]) is None
    assert core.get_invoice_items(conn, inv["id"]) == []
    freed = conn.execute("SELECT category, invoice_id FROM transactions WHERE description='Zahlung'").fetchone()
    assert freed["invoice_id"] is None and freed["category"] == "offen"


def test_delete_invoice_unbekannt_wirft(conn):
    with pytest.raises(ValueError):
        core.delete_invoice(conn, 9999)


def test_delete_invoice_gesperrt_bei_festschreibung(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10")
    core.close_period("2026-Q1")
    with pytest.raises(ValueError):
        core.delete_invoice(conn, inv["id"])
    assert core.get_invoice(conn, inv["id"]) is not None, "nichts gelöscht"


# ── Rechnungstyp streng aus dem Kunden (kein LLM-Raten) ──────────────────
def test_customer_kind_streng_abgeleitet():
    assert core.customer_kind({"vat_id": "EE100000000", "address": "Tallinn\nEstonia"}) == "eu"
    assert core.customer_kind({"vat_id": "DE123456789", "address": "Berlin"}) == "domestic"
    assert core.customer_kind({"vat_id": "GB123456789", "address": "London"}) == "third"
    assert core.customer_kind({"vat_id": None, "address": "Str 1\n10115 Berlin\nGermany"}) == "domestic"
    assert core.customer_kind({"vat_id": None, "address": "unklar"}) is None


def test_create_invoice_kind_wird_aus_kunde_erzwungen(conn, cfg, eu_customer):
    """EU-Kunde (NL-USt-ID): selbst wenn 'domestic' übergeben wird, wird Reverse Charge gebucht."""
    inv = core.create_invoice(
        conn, cfg, customer=eu_customer, kind="domestic", issue_date="2026-03-10",
        service_from="2026-03-10", service_to=None,
        items=[{"description": "Beratung", "quantity": "1", "unit": "h",
                "unit_price": "1000", "vat_rate": 19}], render=False)
    assert inv["kind"] == "eu"
    assert float(inv["vat"]) == 0.0 and float(inv["gross"]) == 1000.0
