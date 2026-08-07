"""Umsatzsteuerliche Sondertatbestände: §13b als Leistungsempfänger, innergemeinschaftliche
Erwerbe, Einfuhrumsatzsteuer, steuerfreie/nicht steuerbare Ausgangsumsätze und die
Sondervorauszahlung.

Der Kern: jeder dieser Fälle hat ein eigenes Feld in der Voranmeldung. Fehlt er dort, ist die
Anmeldung unvollständig – auch dann, wenn die Zahllast am Ende gleich bleibt (§13b und i.g.
Erwerb sind bei vollem Vorsteuerabzug ein Nullsummenspiel, aber trotzdem erklärungspflichtig).
"""
import pytest

import core


def _kz(fig):
    """Kennzahl -> (Bemessung, Steuer) für bequeme Zusicherungen."""
    return {row[0]: (row[2], row[4]) for row in core.vat_kz_rows(fig)}


# ── § 13b als Leistungsempfänger ─────────────────────────────────────────
def test_13b_eu_landet_in_kz_46_47_und_67(conn, cfg):
    """Sonstige Leistung eines EU-Unternehmers: Steuer in 46/47, zugleich Vorsteuer in 67."""
    core.create_expense(conn, date_="2026-02-10", vendor="AWS EMEA (IE)", description=None,
                        category="4980 - Sonstiges", gross="1000.00", vat_rate=19,
                        paid_date="2026-02-10", vat_kind="rc_eu")
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    kz = _kz(fig)
    assert kz["46"] == (1000, 190)
    assert kz["67"][1] == 190
    assert "84" not in kz                      # Drittland-Feld bleibt leer
    assert fig["zahllast"] == 0                # geschuldet = abziehbar


def test_13b_drittland_landet_in_kz_84_85(conn, cfg):
    """US-Anbieter (Katalogleistung) ist § 13b Abs. 2 – anderes Feld als der EU-Fall."""
    core.create_expense(conn, date_="2026-02-10", vendor="Anthropic, PBC", description=None,
                        category="4980 - Sonstiges", gross="100.00", vat_rate=19,
                        paid_date="2026-02-10", vat_kind="rc_other")
    kz = _kz(core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31"))
    assert kz["84"] == (100, 19)
    assert "46" not in kz
    assert kz["67"][1] == 19


def test_13b_ohne_zuordnung_wird_ausgewiesen_statt_geraten(conn, cfg):
    """Ist unklar, ob EU oder Drittland, darf das Tool nicht raten – es muss nachfragen."""
    core.create_expense(conn, date_="2026-02-10", vendor="Irgendwer", description=None,
                        category="4980 - Sonstiges", gross="100.00", vat_rate=0,
                        paid_date="2026-02-10", reverse_charge=1)
    offen = core.vat_open_rc(conn, "2026-01-01", "2026-03-31")
    assert len(offen) == 1 and offen[0]["vendor"] == "Irgendwer"
    kz = _kz(core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31"))
    assert "46" not in kz and "84" not in kz    # kein geratenes Feld
    core.update_expense(conn, offen[0]["id"], vat_kind="rc_other")
    assert core.vat_open_rc(conn, "2026-01-01", "2026-03-31") == []
    assert _kz(core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31"))["84"] == (100, 19)


def test_13b_ohne_satz_faellt_auf_regelsatz_zurueck(conn):
    """vat_rate=0 darf die selbst geschuldete Steuer nicht still auf null setzen."""
    r = core.create_expense(conn, date_="2026-02-10", vendor="X", description=None,
                            category="4980 - Sonstiges", gross="100.00", vat_rate=0,
                            paid_date="2026-02-10", vat_kind="rc_eu")
    e = conn.execute("SELECT * FROM expenses WHERE id=?", (r["id"],)).fetchone()
    assert core.expense_tax_rate(e) == 19
    assert e["net"] == 100 and e["vat"] == 0      # Lieferant weist keine Steuer aus


def test_13b_privatanteil_kuerzt_nur_den_vorsteuerabzug(conn, cfg):
    """Die Steuer entsteht auf das volle Entgelt, abziehbar ist nur der betriebliche Anteil."""
    core.create_expense(conn, date_="2026-02-10", vendor="EU-Dienst", description=None,
                        category="4980 - Sonstiges", gross="1000.00", vat_rate=19,
                        paid_date="2026-02-10", vat_kind="rc_eu", private_share=40)
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert fig["rc_eu_base"] == 1000 and fig["rc_eu_tax"] == 190
    assert fig["rc_vst"] == 114                    # 190 * 60 %
    assert fig["zahllast"] == 76


def test_13b_bleibt_in_der_euer_neutral(conn, cfg):
    """Geschuldete Steuer und Vorsteuer heben sich auf – der Gewinn ändert sich nicht."""
    core.create_expense(conn, date_="2026-02-10", vendor="EU-Dienst", description=None,
                        category="4980 - Sonstiges", gross="1000.00", vat_rate=19,
                        paid_date="2026-02-10", vat_kind="rc_eu")
    e = core.euer(conn, cfg, "2026-01-01", "2026-12-31")
    assert e["ust_13b"] == pytest.approx(190)
    assert e["ergebnis"] == pytest.approx(-1000)   # nur der Aufwand wirkt


def test_altdaten_ohne_vat_kind_werden_weiter_als_13b_gerechnet(conn, cfg):
    """Eine noch nicht migrierte Zeile (nur reverse_charge=1) darf nicht stillschweigend
    zur normalen Inlandsausgabe werden – sonst verschwindet die Steuer aus der EÜR."""
    conn.execute(
        "INSERT INTO expenses (date, paid_date, vendor, description, category, net, vat_rate, "
        "vat, gross, created_at, private_share, gross_full, reverse_charge, vat_kind) "
        "VALUES ('2026-02-10','2026-02-10','Alt',NULL,'4980 - Sonstiges',100,0,0,100,'x',0,100,1,'')")
    conn.commit()
    row = conn.execute("SELECT * FROM expenses").fetchone()
    assert core.expense_vat_kind(row) == "rc_unknown"
    assert core.euer(conn, cfg, "2026-01-01", "2026-12-31")["ust_13b"] == pytest.approx(19)


# ── Innergemeinschaftlicher Erwerb & Einfuhr ─────────────────────────────
def test_ig_erwerb_in_kz_89_und_61(conn, cfg):
    core.create_expense(conn, date_="2026-05-02", vendor="Shop NL", description=None,
                        category="4980 - Sonstiges", gross="500.00", vat_rate=19,
                        paid_date="2026-05-02", vat_kind="ig_erwerb")
    kz = _kz(core._vat_figures(conn, cfg, "2026-04-01", "2026-06-30"))
    assert kz["89"] == (500, 95)
    assert kz["61"][1] == 95


def test_ig_erwerb_mit_7_prozent_in_kz_93(conn, cfg):
    core.create_expense(conn, date_="2026-05-02", vendor="Buchhandel FR", description=None,
                        category="4980 - Sonstiges", gross="200.00", vat_rate=7,
                        paid_date="2026-05-02", vat_kind="ig_erwerb")
    kz = _kz(core._vat_figures(conn, cfg, "2026-04-01", "2026-06-30"))
    assert kz["93"] == (200, 14)
    assert "89" not in kz


def test_einfuhrumsatzsteuer_in_kz_62_statt_66(conn, cfg):
    """Am Zoll gezahlte EUSt ist echte Vorsteuer – aber in einem eigenen Feld."""
    core.create_expense(conn, date_="2026-05-02", vendor="Zoll", description=None,
                        category="4980 - Sonstiges", gross="119.00", vat_rate=19,
                        paid_date="2026-05-02", vat_kind="import")
    fig = core._vat_figures(conn, cfg, "2026-04-01", "2026-06-30")
    assert fig["import_vst"] == 19
    assert fig["vorsteuer"] == 0                   # nicht in Kz 66
    assert fig["zahllast"] == -19


# ── Ausgangsseite: Leistungsart entscheidet über die Kennzahl ────────────
def test_eu_warenlieferung_ist_kz_41_nicht_21(conn, cfg, eu_customer):
    core.create_invoice(conn, cfg, customer=eu_customer, kind="eu", issue_date="2026-03-05",
                        service_from="2026-03-05", service_to=None, paid_date="2026-03-05",
                        render=False, supply_type="goods",
                        items=[{"description": "Hardware", "quantity": "1", "unit": "",
                                "unit_price": "2000", "vat_rate": 19}])
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert fig["ig_lieferung_net"] == 2000
    assert fig["eu_net"] == 0


def test_drittland_warenlieferung_ist_ausfuhr_kz_43(conn, cfg, customer):
    core.create_invoice(conn, cfg, customer=customer, kind="third", issue_date="2026-03-05",
                        service_from="2026-03-05", service_to=None, paid_date="2026-03-05",
                        render=False, supply_type="goods",
                        items=[{"description": "Export", "quantity": "1", "unit": "",
                                "unit_price": "800", "vat_rate": 19}])
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert fig["ausfuhr_net"] == 800 and fig["third_net"] == 0


def test_steuerfreier_inlandsumsatz_ist_kz_48_und_ohne_ust(conn, cfg, customer):
    """§ 4 Nr. 8–29: steuerfrei ohne Vorsteuerabzug – nicht in 81, keine USt auf der Rechnung."""
    inv = core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                              issue_date="2026-03-05", service_from="2026-03-05",
                              service_to=None, paid_date="2026-03-05", render=False,
                              supply_type="exempt",
                              items=[{"description": "Unterricht", "quantity": "1", "unit": "",
                                      "unit_price": "600", "vat_rate": 19}])
    assert inv["vat"] == 0
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    assert fig["steuerfrei_net"] == 600 and fig["net19"] == 0


def test_zm_trennt_warenlieferung_und_sonstige_leistung(conn, cfg, eu_customer):
    """In der ZM sind das zwei Meldearten ('L' und 'S') – sie dürfen nicht verschmelzen."""
    for st, price in (("goods", "1000"), ("service", "500")):
        core.create_invoice(conn, cfg, customer=eu_customer, kind="eu", issue_date="2026-03-05",
                            service_from="2026-03-05", service_to=None, render=False,
                            supply_type=st,
                            items=[{"description": st, "quantity": "1", "unit": "",
                                    "unit_price": price, "vat_rate": 19}])
    z = core.zm_data(conn, 2026, 1)
    arten = {r["art"]: r["net"] for r in z["rows"]}
    assert arten == {"L": 1000, "S": 500}
    assert z["total"] == 1500


# ── Kz 39: Sondervorauszahlung ───────────────────────────────────────────
def test_sondervorauszahlung_nur_monatlich_und_nur_im_dezember(cfg):
    """Quartalszahler bekommen die Dauerfristverlängerung ohne Sondervorauszahlung."""
    monatlich = {"tax": {**cfg["tax"], "va_period": "month", "dauerfrist": True,
                         "sondervorauszahlung": 1100}}
    assert core._sondervorauszahlung(monatlich, "2026-12-01", "2026-12-31") == 1100
    assert core._sondervorauszahlung(monatlich, "2026-11-01", "2026-11-30") == 0
    quartal = {"tax": {**monatlich["tax"], "va_period": "quarter"}}
    assert core._sondervorauszahlung(quartal, "2026-12-01", "2026-12-31") == 0
    ohne = {"tax": {**monatlich["tax"], "dauerfrist": False}}
    assert core._sondervorauszahlung(ohne, "2026-12-01", "2026-12-31") == 0


def test_sondervorauszahlung_mindert_die_zahllast(conn, cfg, customer):
    core.create_invoice(conn, cfg, customer=customer, kind="domestic", issue_date="2026-12-05",
                        service_from="2026-12-05", service_to=None, paid_date="2026-12-05",
                        render=False,
                        items=[{"description": "Beratung", "quantity": "1", "unit": "",
                                "unit_price": "10000", "vat_rate": 19}])
    dez = {**cfg, "tax": {**cfg["tax"], "va_period": "month", "dauerfrist": True,
                          "sondervorauszahlung": 1100}}
    fig = core._vat_figures(conn, dez, "2026-12-01", "2026-12-31")
    assert fig["ust19"] == 1900 and fig["sondervorauszahlung"] == 1100
    assert fig["zahllast"] == 800


# ── Darstellung ──────────────────────────────────────────────────────────
def test_kz_zeilen_zeigen_nur_belegte_felder(conn, cfg):
    """Das Grundgerüst (81/86/66) steht immer, Sonderfälle nur wenn sie Werte tragen."""
    zeilen = {r[0] for r in core.vat_kz_rows(core._vat_figures(conn, cfg,
                                                               "2026-01-01", "2026-03-31"))}
    assert zeilen == {"81", "86", "66"}
    core.create_expense(conn, date_="2026-02-10", vendor="EU", description=None,
                        category="4980 - Sonstiges", gross="100.00", vat_rate=19,
                        paid_date="2026-02-10", vat_kind="rc_eu")
    zeilen = {r[0] for r in core.vat_kz_rows(core._vat_figures(conn, cfg,
                                                               "2026-01-01", "2026-03-31"))}
    assert {"46", "67"} <= zeilen


def test_wiederkehrender_lieferant_behaelt_seine_ust_art(conn):
    """Die Buchungsregel merkt sich die USt-Art – sonst müsste jeder OpenAI-Beleg neu
    eingestuft werden."""
    core.save_rule(conn, "ANTHROPIC PBC", "expense", "4980 - Sonstiges", 19, 0,
                   vat_kind="rc_other")
    s = core.suggestion_for(core.get_rules_map(conn), "Anthropic PBC")
    assert core.expense_vat_kind(s) == "rc_other"


def test_kz_zeilen_summieren_sich_zur_zahllast(conn, cfg, customer):
    """Wichtigste Zusicherung der Darstellung: was angezeigt wird, muss die Zahllast ergeben –
    auch solange ein §13b-Beleg noch nicht zugeordnet ist (der Betrag steht ja fest)."""
    core.create_invoice(conn, cfg, customer=customer, kind="domestic", issue_date="2026-02-01",
                        service_from="2026-02-01", service_to=None, paid_date="2026-02-01",
                        render=False,
                        items=[{"description": "Beratung", "quantity": "1", "unit": "",
                                "unit_price": "5000", "vat_rate": 19}])
    core.create_expense(conn, date_="2026-02-05", vendor="Anthropic, PBC", description=None,
                        category="4980 - Sonstiges", gross="107.10", vat_rate=0,
                        paid_date="2026-02-05", reverse_charge=1)          # Zuordnung offen
    core.create_expense(conn, date_="2026-02-06", vendor="AWS (IE)", description=None,
                        category="4980 - Sonstiges", gross="200.00", vat_rate=19,
                        paid_date="2026-02-06", vat_kind="rc_eu")
    core.create_expense(conn, date_="2026-02-07", vendor="Zoll", description=None,
                        category="4930 - Bürobedarf", gross="119.00", vat_rate=19,
                        paid_date="2026-02-07", vat_kind="import")
    fig = core._vat_figures(conn, cfg, "2026-01-01", "2026-03-31")
    geschuldet = abziehbar = 0
    for kz_, _lbl, base, _kt, tax in core.vat_kz_rows(fig):
        if kz_ in ("66", "61", "62", "67", "39"):
            abziehbar += tax
        elif tax is not None:
            geschuldet += tax
    assert geschuldet - abziehbar == fig["zahllast"]
    # Der offene Beleg ist als eigene Zeile sichtbar, nicht stumm eingerechnet.
    assert "46/84" in {r[0] for r in core.vat_kz_rows(fig)}
