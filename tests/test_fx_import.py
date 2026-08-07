"""Wechselkurse (aus dem Cache, ohne Netz) und Rechnungs-CSV-Import."""
import pytest

import core


# ── Wechselkurse ─────────────────────────────────────────────────────────
def seed_rate(conn, base, quote, rate, on_date):
    core._fx_store(conn, on_date, base, quote, rate)


def test_fx_gleiche_waehrung(conn):
    assert core.fx_rate(conn, "EUR", "EUR") == 1.0


def test_fx_usdc_wird_wie_usd_behandelt(conn):
    assert core.fx_rate(conn, "USDC", "USD") == 1.0


def test_fx_aus_cache(conn):
    seed_rate(conn, "USD", "EUR", 0.92, "2026-03-01")
    assert core.fx_rate(conn, "USD", "EUR", "2026-03-01") == 0.92


def test_fx_ohne_cache_wuerde_ins_netz(conn):
    # conftest hat urlopen zugenagelt -> Beweis, dass ohne Cache Netz nötig wäre
    with pytest.raises(AssertionError, match="Netz"):
        core.fx_rate(conn, "USD", "EUR", "2020-01-01")


def test_to_home_gleiche_waehrung(conn):
    core.save_settings({"home_currency": "EUR"})
    assert core.to_home(conn, 100, "EUR") == (100.0, 1.0)


def test_to_home_mit_kurs(conn):
    core.save_settings({"home_currency": "EUR"})
    seed_rate(conn, "USD", "EUR", 0.90, "2026-03-01")
    betrag, kurs = core.to_home(conn, 100, "USD", "2026-03-01")
    assert betrag == 90.0 and kurs == 0.90


def test_to_home_bei_netzfehler_none(conn):
    core.save_settings({"home_currency": "EUR"})
    # kein Cache-Eintrag -> to_home fängt den Fehler ab und liefert (None, None)
    assert core.to_home(conn, 100, "USD", "2020-01-01") == (None, None)


# ── Rechnungs-CSV-Import ─────────────────────────────────────────────────
CSV = """number,issue_date,kind,customer_name,customer_address,customer_vat_id,description,quantity,unit,unit_price,vat_rate
2026-05-0001,2026-05-01,domestic,ACME GmbH,Hauptstr. 1; 10115 Berlin,,Beratung,10,h,100,19
"""


def test_import_erstellt_rechnung(conn, cfg):
    res = core.import_invoices_csv(conn, cfg, CSV, render=False)
    assert res["created"] == ["2026-05-0001"]
    assert res["skipped"] == [] and res["errors"] == []
    inv = core.get_invoice_by_number(conn, "2026-05-0001")
    assert inv["net"] == 1000.0 and inv["vat"] == 190.0
    assert core.list_customers(conn)[0]["address"] == "Hauptstr. 1\n10115 Berlin"


def test_import_gruppiert_positionen_je_nummer(conn, cfg):
    csv = ("number,issue_date,kind,customer_name,description,quantity,unit_price,vat_rate\n"
           "2026-05-0009,2026-05-01,domestic,ACME,Pos A,1,100,19\n"
           "2026-05-0009,2026-05-01,domestic,ACME,Pos B,2,50,19\n")
    core.import_invoices_csv(conn, cfg, csv, render=False)
    inv = core.get_invoice_by_number(conn, "2026-05-0009")
    assert len(core.get_invoice_items(conn, inv["id"])) == 2
    assert inv["net"] == 200.0                          # 100 + 2*50


def test_import_zeilen_ohne_nummer_je_eigene_rechnung(conn, cfg):
    csv = ("issue_date,kind,customer_name,description,quantity,unit_price,vat_rate\n"
           "2026-05-01,domestic,ACME,Pos A,1,100,19\n"
           "2026-05-02,domestic,ACME,Pos B,1,50,19\n")
    res = core.import_invoices_csv(conn, cfg, csv, render=False)
    assert len(res["created"]) == 2


def test_import_akzeptiert_deutsches_datum(conn, cfg):
    csv = ("issue_date,kind,customer_name,description,quantity,unit_price,vat_rate\n"
           "01.05.2026,domestic,ACME,Pos,1,100,19\n")
    core.import_invoices_csv(conn, cfg, csv, render=False)
    assert core.list_invoices(conn)[0]["issue_date"] == "2026-05-01"


def test_import_doppelte_nummer_wird_uebersprungen(conn, cfg):
    core.import_invoices_csv(conn, cfg, CSV, render=False)
    res = core.import_invoices_csv(conn, cfg, CSV, render=False)
    assert res["created"] == [] and len(res["skipped"]) == 1


def test_import_unbekannter_typ_meldet_fehler(conn, cfg):
    csv = ("issue_date,kind,customer_name,description,quantity,unit_price,vat_rate\n"
           "2026-05-01,mars,ACME,Pos,1,100,19\n")
    res = core.import_invoices_csv(conn, cfg, csv, render=False)
    assert res["created"] == [] and len(res["errors"]) == 1


def test_import_leere_csv(conn, cfg):
    res = core.import_invoices_csv(conn, cfg, "", render=False)
    assert res["errors"]


@pytest.mark.parametrize("raw,kind", [
    ("inland", "domestic"), ("EU", "eu"), ("reverse-charge", "eu"),
    ("drittland", "third"), ("uk", "third"), ("", "domestic"),
])
def test_norm_kind_aliase(raw, kind):
    assert core._norm_kind(raw) == kind


def test_norm_kind_unbekannt():
    assert core._norm_kind("mars") is None
