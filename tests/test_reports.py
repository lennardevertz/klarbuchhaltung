"""Steuerlich kritische Auswertungen: EÜR (Brutto/Netto), USt-VA, ZM, Kennzahlen.
Fehler hier kosten beim Finanzamt – daher besonders dicht getestet.

Die EÜR kennt zwei Sichten (Schalter settings['euer_method']):
  brutto  – amtlich: vereinnahmte USt & Vorsteuer sind Betriebseinnahmen/-ausgaben
  netto   – wirtschaftlich: USt neutral
Default ist brutto."""
import pytest

import core

CSV_HEADER = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
              "Currency,State,Balance\n")


@pytest.fixture
def data(conn, cfg, customer, eu_customer):
    """Ein realistisches Jahr 2026: Inland bezahlt/offen, EU, Drittland, Ausgaben."""
    def inv(**kw):
        kw.setdefault("service_from", kw["issue_date"])
        kw.setdefault("service_to", None)
        return core.create_invoice(conn, cfg, render=False, **kw)

    inv(customer=customer, kind="domestic", issue_date="2026-02-01",
        paid_date="2026-02-15", items=[{"description": "Beratung", "quantity": "1",
        "unit": "h", "unit_price": "1000", "vat_rate": 19}])          # 1000 net / 190 USt
    inv(customer=customer, kind="domestic", issue_date="2026-04-01",
        paid_date="2026-04-10", items=[{"description": "Buch", "quantity": "1",
        "unit": "", "unit_price": "100", "vat_rate": 7}])             # 100 net / 7 USt
    inv(customer=customer, kind="domestic", issue_date="2026-03-01",  # unbezahlt -> Ist zählt nicht
        items=[{"description": "Offen", "quantity": "1", "unit": "", "unit_price": "500",
                "vat_rate": 19}])
    inv(customer=eu_customer, kind="eu", issue_date="2026-03-05", paid_date="2026-03-20",
        items=[{"description": "Dev", "quantity": "1", "unit": "", "unit_price": "2000",
                "vat_rate": 19}])                                     # EU Reverse Charge
    inv(customer=customer, kind="third", issue_date="2026-03-10", paid_date="2026-03-25",
        items=[{"description": "Consulting", "quantity": "1", "unit": "", "unit_price": "800",
                "vat_rate": 19}])                                     # Drittland
    core.create_expense(conn, date_="2026-02-05", vendor="Telekom", description=None,
                        category="4920 - Telefon", gross="119.00", vat_rate=19,
                        paid_date="2026-02-05")                       # 100 net / 19 VSt
    core.create_expense(conn, date_="2026-03-01", vendor="Vermieter", description=None,
                        category="1525 - Kautionen (geleistet)", gross="1000.00", vat_rate=0,
                        paid_date="2026-03-01")                       # neutral (Bestandskonto)
    return conn


# ── euer(): Brutto- und Netto-Sicht ──────────────────────────────────────
def test_euer_umsatz_nur_bezahlt(data, cfg):
    e = core.euer(data, cfg, "2026-01-01", "2026-12-31")
    assert e["umsatz_net"] == 3900          # 1000 + 100 + 2000 + 800 ; offene 500 zählt nicht
    assert e["domestic_net"] == 1100
    assert e["eu_net"] == 2000 and e["third_net"] == 800


def test_euer_brutto_einnahmen_enthalten_ust(data, cfg):
    e = core.euer(data, cfg, "2026-01-01", "2026-12-31")
    assert e["method"] == "brutto"
    assert e["ust_vereinnahmt"] == 197      # 190 + 7 ; EU/Drittland ohne USt
    assert e["betriebseinnahmen"] == 4097   # 3900 umsatz + 197 USt
    assert e["einnahmen"] == 4097           # aktive Sicht = brutto


def test_euer_brutto_ausgaben_enthalten_vorsteuer(data, cfg):
    e = core.euer(data, cfg, "2026-01-01", "2026-12-31")
    assert e["aufwand_net"] == 100          # Kaution (neutral) ausgeklammert
    assert e["vorsteuer_ges"] == 19
    assert e["betriebsausgaben"] == 119
    assert e["ergebnis"] == 3978            # 4097 - 119


def test_euer_netto_sicht(data):
    core.update_settings(euer_method="netto")
    e = core.euer(data, None, "2026-01-01", "2026-12-31")
    assert e["method"] == "netto"
    assert e["einnahmen"] == 3900 and e["ausgaben"] == 100
    assert e["gewinn"] == 3800              # USt neutral
    # Brutto-Felder bleiben trotzdem berechnet
    assert e["betriebseinnahmen"] == 4097


def test_euer_by_cat_ohne_bestandskonto(data, cfg):
    e = core.euer(data, cfg, "2026-01-01", "2026-12-31")
    assert e["by_cat"] == [("4920 - Telefon", pytest.approx(100))]


def test_euer_eigenverbrauch_aus_privatanteil(conn, cfg):
    # 119 brutto @19, 50 % privat -> Eigenverbrauch: halber Netto/USt-Anteil
    core.create_expense(conn, date_="2026-05-01", vendor="Telekom", description=None,
                        category="4920 - Telefon", gross="119.00", vat_rate=19,
                        paid_date="2026-05-01", private_share=50)
    e = core.euer(conn, cfg, "2026-01-01", "2026-12-31")
    assert e["eigenverbrauch_net"] == pytest.approx(50)
    assert e["eigenverbrauch_ust"] == pytest.approx(9.5)
    assert e["aufwand_net"] == pytest.approx(100)   # voller Netto-Aufwand
    assert e["expense_net"] == pytest.approx(50)     # Netto-Sicht: nur betrieblich


def test_euer_reverse_charge_ausgabe_13b(conn, cfg):
    """§13b: ausländische Eingangsrechnung, netto=brutto, Steuerschuld = Vorsteuer (neutral)."""
    core.create_expense(conn, date_="2026-06-01", vendor="AWS EU", description=None,
                        category="4980 - Sonstiges", gross="1000.00", vat_rate=0,
                        paid_date="2026-06-01", reverse_charge=1)
    e = core.euer(conn, cfg, "2026-01-01", "2026-12-31")
    assert e["ust_13b"] == pytest.approx(190)
    assert e["aufwand_net"] == pytest.approx(1000)
    assert e["ergebnis"] == pytest.approx(e["betriebseinnahmen"] - e["betriebsausgaben"])


def test_euer_leerer_zeitraum(conn):
    e = core.euer(conn, None, "2030-01-01", "2030-12-31")
    assert e["einnahmen"] == 0 and e["ausgaben"] == 0 and e["by_cat"] == []


def test_euer_beruecksichtigt_sonstige_einnahmen(conn):
    tid = core.create_manual_transaction(conn, "2026-05-01", "Bar-Workshop", "500.00")
    core.set_tx_category(conn, tid, "einnahme_sonstige")
    e = core.euer(conn, None, "2026-01-01", "2026-12-31")
    assert e["sonstige"] == 500
    assert e["income_net"] == 500


# ── report_data (EÜR Jahr) ───────────────────────────────────────────────
def test_report_data_spiegelt_euer(data):
    r = core.report_data(data, 2026)
    assert r["year"] == 2026 and r["method"] == "brutto"
    assert r["betriebseinnahmen"] == 4097 and r["betriebsausgaben"] == 119
    assert r["ergebnis"] == 3978
    assert r["income_net"] == 3900 and r["expense_net"] == 100 and r["profit_net"] == 3800
    assert r["profit"] == 3978              # Kompat-Feld folgt aktiver Methode


def test_report_data_netto_profit_kompat(data):
    core.update_settings(euer_method="netto")
    r = core.report_data(data, 2026)
    assert r["profit"] == 3800


# ── vat_data (USt-VA, Ist) ───────────────────────────────────────────────
def test_vat_q1_nur_bezahlte_inlandsumsaetze(data, cfg):
    v = core.vat_data(data, cfg, 2026, 1)
    assert v["label"] == "2026 Q1"
    assert v["net19"] == 1000 and v["ust19"] == 190
    assert v["net7"] == 0 and v["ust7"] == 0
    assert v["eu_net"] == 2000 and v["third_net"] == 800


def test_vat_q1_vorsteuer_und_zahllast(data, cfg):
    v = core.vat_data(data, cfg, 2026, 1)
    assert v["vorsteuer"] == 19
    assert v["zahllast"] == 171             # 190 - 19


def test_vat_q2_hat_den_7prozent_umsatz(data, cfg):
    v = core.vat_data(data, cfg, 2026, 2)
    assert v["net7"] == 100 and v["ust7"] == 7
    assert v["net19"] == 0 and v["zahllast"] == 7


def test_vat_soll_besteuerung_zaehlt_nach_rechnungsdatum(data):
    cfg = core.load_config()
    cfg["tax"]["taxation"] = "soll"
    v = core.vat_data(data, cfg, 2026, 1)
    assert v["net19"] == 1500 and v["ust19"] == 285   # 1000 bezahlt + 500 offen
    assert v["taxation"] == "soll"


def test_vat_erstattung_bei_hoher_vorsteuer(conn, cfg):
    core.create_expense(conn, date_="2026-01-10", vendor="Laptop", description=None,
                        category="0480 - GWG", gross="1190.00", vat_rate=19,
                        paid_date="2026-01-10")
    v = core.vat_data(conn, cfg, 2026, 1)
    assert v["vorsteuer"] == 190 and v["zahllast"] == -190


# ── vat_annual ───────────────────────────────────────────────────────────
def test_vat_annual_summiert_quartale(data, cfg):
    a = core.vat_annual(data, cfg, 2026)
    assert a["year"] == 2026 and len(a["quarters"]) == 4
    assert a["prepaid"] == sum(q["zahllast"] for q in a["quarters"])
    assert a["zahllast"] == 178             # 171 (Q1) + 7 (Q2)
    assert a["final"] == 0                  # Jahr == Summe Quartale


# ── zm_data ──────────────────────────────────────────────────────────────
def test_zm_listet_eu_umsatz(data):
    z = core.zm_data(data, 2026, 1)
    assert z["total"] == 2000 and len(z["rows"]) == 1
    assert z["rows"][0]["customer_vat_id"] == "NL123456789B01"
    assert z["missing"] is False and z["deadline"] == "25.04."


def test_zm_nach_rechnungsdatum_nicht_zahlung(conn, cfg, eu_customer):
    core.create_invoice(conn, cfg, customer=eu_customer, kind="eu", issue_date="2026-02-01",
                        service_from="2026-02-01", service_to=None, render=False,
                        items=[{"description": "x", "quantity": "1", "unit": "",
                                "unit_price": "1000", "vat_rate": 19}])
    assert core.zm_data(conn, 2026, 1)["total"] == 1000


def test_zm_meldet_fehlende_vat_id(conn, cfg):
    kunde = core.create_customer(conn, "EU ohne ID", "Str. 1", None)
    core.create_invoice(conn, cfg, customer=kunde, kind="eu", issue_date="2026-02-01",
                        service_from="2026-02-01", service_to=None, render=False,
                        items=[{"description": "x", "quantity": "1", "unit": "",
                                "unit_price": "500", "vat_rate": 19}])
    assert core.zm_data(conn, 2026, 1)["missing"] is True


def test_zm_leer(conn):
    z = core.zm_data(conn, 2026, 1)
    assert z["rows"] == [] and z["total"] == 0 and z["missing"] is False


@pytest.mark.parametrize("q,deadline", [
    (1, "25.04."), (2, "25.07."), (3, "25.10."), (4, "25.01. (Folgejahr)")])
def test_zm_deadlines(conn, q, deadline):
    assert core.zm_data(conn, 2026, q)["deadline"] == deadline


# ── figures_for_period ───────────────────────────────────────────────────
def test_figures_monat_brutto(data, cfg):
    f = core.figures_for_period(data, cfg, "2026-02")
    assert f["token"] == "2026-02" and f["methode"] == "brutto"
    assert f["einnahmen"] == 1190           # 1000 + 190 USt
    assert f["ausgaben"] == 119             # 100 + 19 VSt
    assert f["gewinn"] == 1071
    assert f["ust_zahllast"] == 171


def test_figures_quartal(data, cfg):
    f = core.figures_for_period(data, cfg, "2026-Q1")
    # Q1 umsatz bezahlt: 1000 + 2000 + 800 = 3800 ; USt vereinnahmt 190 -> brutto 3990
    assert f["einnahmen"] == 3990
    assert f["ust_zahllast"] == 171


def test_figures_jahr(data, cfg):
    f = core.figures_for_period(data, cfg, "2026")
    assert f["einnahmen"] == 4097 and f["gewinn"] == 3978


def test_figures_netto_methode(data, cfg):
    core.update_settings(euer_method="netto")
    f = core.figures_for_period(data, cfg, "2026")
    assert f["methode"] == "netto"
    assert f["einnahmen"] == 3900 and f["gewinn"] == 3800


# ── figures_for_period: Brutto/Netto-Felder explizit ─────────────────────
def test_figures_liefert_beide_sichten(data, cfg):
    """Der Chat/Agent soll NICHTS selbst rechnen – daher beide Sichten explizit im Rückgabewert."""
    f = core.figures_for_period(data, cfg, "2026")
    assert f["einnahmen_brutto"] == 4097 and f["einnahmen_netto"] == 3900
    assert f["ausgaben_brutto"] == 119 and f["ausgaben_netto"] == 100
    assert f["gewinn_brutto"] == 3978 and f["gewinn_netto"] == 3800
    assert f["umsatzerloese_netto"] == 3900
    assert f["vereinnahmte_ust"] == 197
    # aktive Sicht (brutto) spiegelt die brutto-Felder
    assert (f["einnahmen"], f["ausgaben"], f["gewinn"]) == (4097, 119, 3978)


def test_figures_aktive_sicht_folgt_methode(data, cfg):
    core.update_settings(euer_method="netto")
    f = core.figures_for_period(data, cfg, "2026")
    assert (f["einnahmen"], f["ausgaben"], f["gewinn"]) == (3900, 100, 3800)
    # die expliziten Brutto/Netto-Felder bleiben unabhängig von der Methode
    assert f["einnahmen_brutto"] == 4097 and f["einnahmen_netto"] == 3900


# ── activity_years / yearly_overview ─────────────────────────────────────
def test_activity_years_nur_jahre_mit_daten(data):
    # data: alle bezahlten Belege liegen 2026; unbezahlte zählen bei paid_date nicht
    assert core.activity_years(data) == [2026]


def test_activity_years_mehrere_jahre(conn, cfg, customer):
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2024-05-01", service_from="2024-05-01", service_to=None,
                        paid_date="2024-05-10", render=False,
                        items=[{"description": "X", "quantity": "1", "unit": "",
                                "unit_price": "100", "vat_rate": 19}])
    core.create_manual_transaction(conn, "2026-01-05", "Bar", "50.00")
    assert core.activity_years(conn) == [2024, 2026]


def test_activity_years_leer(conn):
    assert core.activity_years(conn) == []


def test_yearly_overview(conn, cfg, customer):
    def inv(issue, paid, price):
        core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                            issue_date=issue, service_from=issue, service_to=None,
                            paid_date=paid, render=False,
                            items=[{"description": "X", "quantity": "1", "unit": "",
                                    "unit_price": price, "vat_rate": 19}])
    inv("2025-06-01", "2025-06-15", "1000")
    inv("2026-02-01", "2026-02-15", "2000")
    core.create_expense(conn, date_="2026-02-05", vendor="T", description=None,
                        category="4920 - Telefon", gross="119", vat_rate=19,
                        paid_date="2026-02-05")
    ov = core.yearly_overview(conn, cfg)
    assert [r["jahr"] for r in ov] == [2025, 2026]
    assert ov[0]["einnahmen"] == 1190 and ov[0]["ausgaben"] == 0 and ov[0]["gewinn"] == 1190
    assert ov[1]["einnahmen"] == 2380 and ov[1]["ausgaben"] == 119 and ov[1]["gewinn"] == 2261
    # beide Sichten je Zeile
    assert ov[1]["einnahmen_netto"] == 2000 and ov[1]["einnahmen_brutto"] == 2380


def test_yearly_overview_folgt_methode(conn, cfg, customer):
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2026-02-01", service_from="2026-02-01", service_to=None,
                        paid_date="2026-02-15", render=False,
                        items=[{"description": "X", "quantity": "1", "unit": "",
                                "unit_price": "1000", "vat_rate": 19}])
    core.update_settings(euer_method="netto")
    ov = core.yearly_overview(conn, cfg)
    assert ov[0]["einnahmen"] == 1000          # netto-Sicht aktiv


def test_yearly_overview_leer(conn, cfg):
    assert core.yearly_overview(conn, cfg) == []


# ── sonstige_einnahmen ───────────────────────────────────────────────────
def test_sonstige_einnahmen(conn):
    tid = core.create_manual_transaction(conn, "2026-06-15", "Bar", "300.00")
    core.set_tx_category(conn, tid, "einnahme_sonstige")
    assert core.sonstige_einnahmen(conn, "2026-01-01", "2026-12-31") == 300.0
    assert core.sonstige_einnahmen(conn, "2026-07-01", "2026-12-31") == 0.0
