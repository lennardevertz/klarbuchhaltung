"""Kundenregister."""
import pytest

import core


def test_create_und_get_customer(conn):
    c = core.create_customer(conn, "ACME GmbH", "Hauptstr. 1\n10115 Berlin", "DE111111111")
    assert c["id"] == 1
    assert c["name"] == "ACME GmbH"
    assert c["vat_id"] == "DE111111111"
    assert core.get_customer(conn, c["id"])["name"] == "ACME GmbH"


def test_leere_vat_id_wird_null(conn):
    c = core.create_customer(conn, "Ohne USt-IdNr.", "Str. 1", "")
    assert c["vat_id"] is None


def test_get_customer_unbekannt(conn):
    assert core.get_customer(conn, 999) is None


def test_list_customers_sortiert_nach_id(conn):
    for name in ("B", "A", "C"):
        core.create_customer(conn, name, "Str. 1", None)
    assert [c["name"] for c in core.list_customers(conn)] == ["B", "A", "C"]


def test_resolve_or_create_customer_wiederverwendet(conn):
    a = core.resolve_or_create_customer(conn, "ACME GmbH", "Hauptstr. 1", None)
    b = core.resolve_or_create_customer(conn, "  acme gmbh  ", "andere Adresse", "DE1")
    assert a["id"] == b["id"], "gleicher Name (case-insensitiv) darf keinen Doppelkunden anlegen"
    assert len(core.list_customers(conn)) == 1


def test_resolve_or_create_customer_trennt_adresszeilen(conn):
    c = core.resolve_or_create_customer(conn, "Neu AG", "Str. 1; 12345 Stadt; Germany", None)
    assert c["address"] == "Str. 1\n12345 Stadt\nGermany"


def test_resolve_or_create_customer_ohne_name_wirft(conn):
    with pytest.raises(ValueError):
        core.resolve_or_create_customer(conn, "   ", "Str. 1", None)


def test_set_customer_email(conn, customer):
    core.set_customer_email(conn, customer["id"], " kontakt@acme.de ")
    assert core.get_customer(conn, customer["id"])["email"] == "kontakt@acme.de"


def test_set_customer_email_ignoriert_leere_werte(conn, customer):
    core.set_customer_email(conn, customer["id"], "a@b.de")
    core.set_customer_email(conn, customer["id"], "")
    assert core.get_customer(conn, customer["id"])["email"] == "a@b.de"


# ── Löschen ──────────────────────────────────────────────────────────────
def test_delete_customer_ohne_rechnungen(conn):
    c = core.create_customer(conn, "Weg AG", "Str. 1", None)
    assert core.delete_customer(conn, c["id"])["deleted"] == "Weg AG"
    assert core.list_customers(conn) == []


def test_delete_customer_mit_rechnung_erlaubt(conn, cfg, customer):
    """Kunden muss man unabhängig von Rechnungen löschen können – die Rechnung
    behält ihre eingefrorenen Kundendaten und bleibt bestehen."""
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2026-03-10", service_from="2026-03-01", service_to=None,
                        items=[{"description": "X", "quantity": 1, "unit": "h",
                                "unit_price": 100, "vat_rate": 19}])
    r = core.delete_customer(conn, customer["id"])
    assert r["rechnungen_entkoppelt"] == 1
    assert core.list_customers(conn) == []
    inv = core.list_invoices(conn)[0]
    assert inv["customer_id"] is None
    assert inv["customer_name"] == "ACME GmbH"
    assert inv["customer_address"] == "Hauptstr. 1\n10115 Berlin"


def test_delete_customer_unbekannt(conn):
    with pytest.raises(ValueError):
        core.delete_customer(conn, 999)


# ── Doppelte zusammenführen ──────────────────────────────────────────────
def test_duplicate_customers_erkennt_namensgleiche(conn):
    core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    core.create_customer(conn, "  example  software  oü ", "", None)
    core.create_customer(conn, "Andere GmbH", "", None)
    d = core.duplicate_customers(conn)
    assert len(d) == 1 and d[0]["keep"] == 1 and d[0]["drop"] == [2]


def test_merge_duplicate_customers_haengt_rechnungen_um(conn, cfg):
    voll = core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    leer = core.create_customer(conn, "Example Software OÜ", "", None)
    core.create_invoice(conn, cfg, customer=leer, kind="domestic", issue_date="2026-03-10",
                        service_from="2026-03-01", service_to=None,
                        items=[{"description": "X", "quantity": 1, "unit": "h",
                                "unit_price": 100, "vat_rate": 19}])
    r = core.merge_duplicate_customers(conn)
    assert r["zusammengefuehrt"] == 1
    assert [c["id"] for c in core.list_customers(conn)] == [voll["id"]]
    inv = core.list_invoices(conn)[0]
    assert inv["customer_id"] == voll["id"]
    # Der vollständigere Datensatz bleibt, seine Stammdaten sind unberührt
    assert core.get_customer(conn, voll["id"])["vat_id"] == "EE1"


def test_merge_uebernimmt_fehlende_stammdaten(conn):
    leer = core.create_customer(conn, "Example Software OÜ", "", None)
    voll = core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    core.merge_customers(conn, leer["id"], voll["id"])
    keep = core.get_customer(conn, leer["id"])
    assert keep["address"] == "Tallinn" and keep["vat_id"] == "EE1"


def test_merge_duplicate_customers_ohne_doppel(conn):
    core.create_customer(conn, "Einzig AG", "Str. 1", None)
    assert core.merge_duplicate_customers(conn)["zusammengefuehrt"] == 0


def test_count_customer_invoices(conn, cfg, customer):
    assert core.count_customer_invoices(conn, customer["id"]) == 0
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2026-03-10", service_from="2026-03-01", service_to=None,
                        items=[{"description": "X", "quantity": 1, "unit": "h",
                                "unit_price": 100, "vat_rate": 19}])
    assert core.count_customer_invoices(conn, customer["id"]) == 1


# ── Kundennummern ────────────────────────────────────────────────────────
def test_create_customer_mit_nummer(conn):
    c = core.create_customer(conn, "ACME GmbH", "Str. 1", None, "10001")
    assert c["number"] == "10001"
    assert core.get_customer_by_number(conn, "10001")["id"] == c["id"]


def test_kundennummer_ist_optional(conn):
    assert core.create_customer(conn, "Ohne Nr.", "Str. 1", None)["number"] is None


def test_kundennummer_muss_eindeutig_sein(conn):
    core.create_customer(conn, "A", "Str. 1", None, "10001")
    with pytest.raises(ValueError, match="schon an 'A' vergeben"):
        core.create_customer(conn, "B", "Str. 2", None, "10001")
    with pytest.raises(ValueError):
        b = core.create_customer(conn, "B", "Str. 2", None)
        core.set_customer_number(conn, b["id"], "10001")


def test_set_customer_number_setzt_und_entfernt(conn):
    c = core.create_customer(conn, "ACME", "Str. 1", None)
    assert core.set_customer_number(conn, c["id"], "K-42")["number"] == "K-42"
    assert core.get_customer(conn, c["id"])["number"] == "K-42"
    assert core.set_customer_number(conn, c["id"], "  ")["number"] is None
    assert core.get_customer(conn, c["id"])["number"] is None


def test_set_customer_number_eigene_nummer_bleibt_erlaubt(conn):
    c = core.create_customer(conn, "ACME", "Str. 1", None, "10001")
    assert core.set_customer_number(conn, c["id"], "10001")["number"] == "10001"


def test_set_customer_number_unbekannt(conn):
    with pytest.raises(ValueError):
        core.set_customer_number(conn, 999, "10001")


def test_next_customer_number(conn):
    assert core.next_customer_number(conn) == "10001"
    core.create_customer(conn, "A", "Str. 1", None, "10001")
    core.create_customer(conn, "B", "Str. 2", None, "K-7")   # nicht-numerisch zählt nicht mit
    assert core.next_customer_number(conn) == "10002"


def test_merge_uebernimmt_kundennummer(conn):
    leer = core.create_customer(conn, "Example Software OÜ", "Tallinn", "EE1")
    mit_nr = core.create_customer(conn, "Example Software OÜ", "", None, "10001")
    core.merge_customers(conn, leer["id"], mit_nr["id"])
    assert core.get_customer(conn, leer["id"])["number"] == "10001"


def test_list_customer_invoices(conn, cfg, customer):
    assert core.list_customer_invoices(conn, customer["id"]) == []
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2026-03-10", service_from="2026-03-01", service_to=None,
                        items=[{"description": "X", "quantity": 1, "unit": "h",
                                "unit_price": 100, "vat_rate": 19}])
    rows = core.list_customer_invoices(conn, customer["id"])
    assert [r["number"] for r in rows] == ["2026-03-0001"]
    assert rows[0]["gross"] == 119.0


# ── Empfänger ohne Registereintrag ───────────────────────────────────────
def test_adhoc_customer_legt_keinen_kunden_an(conn, cfg):
    cust = core.adhoc_customer("Einmal AG", "Str. 1; 12345 Stadt", "DE9")
    inv = core.create_invoice(conn, cfg, customer=cust, kind="domestic",
                              issue_date="2026-03-10", service_from="2026-03-01",
                              service_to=None,
                              items=[{"description": "X", "quantity": 1, "unit": "h",
                                      "unit_price": 100, "vat_rate": 19}])
    assert core.list_customers(conn) == [], "Ad-hoc-Empfänger darf nicht ins Register"
    assert inv["customer_id"] is None
    assert inv["customer_name"] == "Einmal AG"
    assert inv["customer_address"] == "Str. 1\n12345 Stadt"
    assert inv["customer_vat_id"] == "DE9"


def test_adhoc_customer_ohne_namen_wird_abgelehnt(conn, cfg):
    with pytest.raises(ValueError, match="Empfänger"):
        core.create_invoice(conn, cfg, customer=core.adhoc_customer("  "), kind="domestic",
                            issue_date="2026-03-10", service_from="2026-03-01", service_to=None,
                            items=[{"description": "X", "quantity": 1, "unit": "h",
                                    "unit_price": 100, "vat_rate": 19}])
