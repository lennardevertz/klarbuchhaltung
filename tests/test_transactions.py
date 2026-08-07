"""Bank-Import, Kategorisierung, Splits, Regeln und Verknüpfungen."""
import pytest

import core

CSV_HEADER = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
              "Currency,State,Balance\n")


def bank_csv(*rows):
    return CSV_HEADER + "".join(rows)


def row(desc, amount, date_="2026-03-15", state="COMPLETED", currency="EUR",
        fee="0.00", typ="CARD_PAYMENT", balance="1000.00"):
    return (f"{typ},Current,{date_} 10:00:00,{date_} 10:00:00,{desc},{amount},"
            f"{fee},{currency},{state},{balance}\n")


@pytest.fixture
def tx(conn):
    """Eine offene Ausgangs-Buchung."""
    core.import_bank_csv(conn, bank_csv(row("Vodafone GmbH", "-59.99")))
    return core.list_transactions(conn)[0]


# ── Import ───────────────────────────────────────────────────────────────
def test_import_legt_buchungen_an(conn):
    res = core.import_bank_csv(conn, bank_csv(
        row("Vodafone GmbH", "-59.99"), row("Kunde Zahlung", "1190.00")))
    assert res == {"imported": 2, "duplicates": 0, "matched": 0}
    t = core.list_transactions(conn)[0]
    assert t["category"] == "offen"
    assert t["amount_home"] == t["amount"] and t["fx_rate"] == 1.0


def test_import_dedupliziert_bei_erneutem_lauf(conn):
    csv = bank_csv(row("Vodafone GmbH", "-59.99"))
    core.import_bank_csv(conn, csv)
    res = core.import_bank_csv(conn, csv)
    assert res["imported"] == 0 and res["duplicates"] == 1
    assert len(core.list_transactions(conn)) == 1


def test_import_ueberspringt_nicht_abgeschlossene(conn):
    res = core.import_bank_csv(conn, bank_csv(
        row("Pending", "-10.00", state="PENDING"),
        row("Reverted", "-10.00", state="REVERTED"),
        row("Fertig", "-10.00")))
    assert res["imported"] == 1
    assert core.list_transactions(conn)[0]["description"] == "Fertig"


def test_import_ueberspringt_zeilen_ohne_datum(conn):
    csv = CSV_HEADER + "CARD_PAYMENT,Current,,,Ohne Datum,-10.00,0,EUR,COMPLETED,0\n"
    assert core.import_bank_csv(conn, csv)["imported"] == 0


def test_import_kommt_mit_semikolon_csv_klar(conn):
    csv = CSV_HEADER.replace(",", ";") + row("Test", "-10.00").replace(",", ";")
    assert core.import_bank_csv(conn, csv)["imported"] == 1


def test_import_uebernimmt_gebuehr(conn):
    core.import_bank_csv(conn, bank_csv(row("Mit Gebühr", "-100.00", fee="1.50")))
    assert core.list_transactions(conn)[0]["fee"] == 1.5


def test_import_ordnet_eingang_offener_rechnung_zu(conn, customer, make_invoice):
    inv = make_invoice(customer=customer, issue_date="2026-03-10")   # brutto 1190
    res = core.import_bank_csv(conn, bank_csv(row("Zahlung ACME", "1190.00")))
    assert res["matched"] == 1
    t = core.list_transactions(conn)[0]
    assert t["invoice_id"] == inv["id"] and t["category"] == "einnahme_rechnung"
    assert core.get_invoice(conn, inv["id"])["paid_date"] == "2026-03-15"


def test_auto_match_waehlt_die_zeitlich_naechste_rechnung(conn, customer, make_invoice):
    fern = make_invoice(customer=customer, issue_date="2026-01-05")
    nah = make_invoice(customer=customer, issue_date="2026-03-14")
    core.import_bank_csv(conn, bank_csv(row("Zahlung", "1190.00", date_="2026-03-15")))
    t = core.list_transactions(conn)[0]
    assert t["invoice_id"] == nah["id"] and t["invoice_id"] != fern["id"]


def test_auto_match_ignoriert_abweichende_betraege(conn, customer, make_invoice):
    make_invoice(customer=customer)
    core.import_bank_csv(conn, bank_csv(row("Zahlung", "1189.00")))
    assert core.list_transactions(conn)[0]["invoice_id"] is None


# ── Manuelle Buchung & Abfragen ──────────────────────────────────────────
def test_create_manual_transaction(conn):
    tid = core.create_manual_transaction(conn, "2026-03-01", "Barzahlung Büro", "-25,50",
                                         method="Kasse")
    t = core.get_transaction(conn, tid)
    assert t["amount"] == -25.5 and t["amount_home"] == -25.5
    assert t["category"] == "offen" and t["tx_type"] == "Kasse"
    assert t["ref"].startswith("manual-")


def test_get_transaction_unbekannt(conn):
    assert core.get_transaction(conn, 999) is None


def test_list_transactions_filter(conn):
    core.import_bank_csv(conn, bank_csv(
        row("Vodafone GmbH", "-59.99"), row("Miete Büro", "-800.00")))
    alle = core.list_transactions(conn)
    assert len(alle) == 2
    assert len(core.list_transactions(conn, query="vodafone")) == 1
    assert len(core.list_transactions(conn, query="  MIETE ")) == 1
    assert core.list_transactions(conn, query="gibtsnicht") == []
    core.set_tx_category(conn, alle[0]["id"], "gebuehr")
    assert len(core.list_transactions(conn, only_open=True)) == 1


def test_transactions_open_count(conn, tx):
    assert core.transactions_open_count(conn) == 1
    core.set_tx_category(conn, tx["id"], "ignorieren")
    assert core.transactions_open_count(conn) == 0


# ── Kategorisieren ───────────────────────────────────────────────────────
def test_set_tx_category(conn, tx):
    core.set_tx_category(conn, tx["id"], "gebuehr", note="Kontoführung")
    t = core.get_transaction(conn, tx["id"])
    assert t["category"] == "gebuehr" and t["note"] == "Kontoführung"


def test_set_tx_category_unbekannt_wirft(conn, tx):
    with pytest.raises(ValueError, match="Unbekannte Kategorie"):
        core.set_tx_category(conn, tx["id"], "quatsch")


def test_kategorisieren_lernt_eine_regel(conn, tx):
    core.set_tx_category(conn, tx["id"], "privatentnahme")
    rules = core.get_rules_map(conn)
    assert core.suggestion_for(rules, "Vodafone GmbH")["action"] == "privatentnahme"
    assert core.suggestion_for(rules, "  vodafone gmbh ")["action"] == "privatentnahme"
    assert core.suggestion_for(rules, "Etwas anderes") is None


def test_offen_wird_nicht_als_regel_gelernt(conn, tx):
    core.set_tx_category(conn, tx["id"], "offen")
    assert core.get_rules_map(conn) == {}


def test_save_rule_merkt_konto_und_satz(conn):
    core.save_rule(conn, "Vodafone GmbH", "expense", "4920 - Telefon", 19, 80)
    r = core.get_rules_map(conn)["vodafone gmbh"]
    assert (r["category"], r["vat_rate"], r["private_share"]) == ("4920 - Telefon", 19, 80.0)
    core.save_rule(conn, "Vodafone GmbH", "expense", "4930 - Bürobedarf", 7, 0)
    assert core.get_rules_map(conn)["vodafone gmbh"]["category"] == "4930 - Bürobedarf"


def test_save_rule_ignoriert_leere_beschreibung(conn):
    core.save_rule(conn, "", "expense", "4920 - Telefon", 19)
    core.save_rule(conn, "Egal", "invoice")
    assert core.get_rules_map(conn) == {}


# ── Buchung -> Ausgabe ───────────────────────────────────────────────────
def test_create_expense_from_tx(conn, tx):
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19)
    t = core.get_transaction(conn, tx["id"])
    e = core.list_expenses(conn)[0]
    assert t["category"] == "ausgabe" and t["expense_id"] == e["id"]
    assert e["gross"] == 59.99 and e["category"] == "4920 - Telefon"
    assert e["source_tx"] == tx["id"]
    assert core.get_rules_map(conn)["vodafone gmbh"]["action"] == "expense"


def test_create_expense_from_tx_mit_privatanteil(conn, tx):
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19, private_share=80)
    e = core.list_expenses(conn)[0]
    assert e["gross"] == pytest.approx(12.0)          # 20 % von 59,99
    assert e["gross_full"] == pytest.approx(59.99)


def test_bulk_create_expense_ueberspringt_eingaenge(conn):
    core.import_bank_csv(conn, bank_csv(
        row("Ausgang A", "-10.00"), row("Ausgang B", "-20.00"), row("Eingang", "30.00")))
    ids = [t["id"] for t in core.list_transactions(conn)]
    assert core.bulk_create_expense(conn, ids, "4980 - Sonstiges", 19) == 2
    assert len(core.list_expenses(conn)) == 2


def test_bulk_set_category(conn):
    core.import_bank_csv(conn, bank_csv(row("A", "-1.00"), row("B", "-2.00")))
    ids = [t["id"] for t in core.list_transactions(conn)]
    assert core.bulk_set_category(conn, ids, "gebuehr") == 2
    assert all(t["category"] == "gebuehr" for t in core.list_transactions(conn))


def test_bulk_set_category_unbekannt_wirft(conn, tx):
    with pytest.raises(ValueError):
        core.bulk_set_category(conn, [tx["id"]], "quatsch")


# ── Split ────────────────────────────────────────────────────────────────
def test_create_split_from_tx(conn):
    core.import_bank_csv(conn, bank_csv(row("Amazon Sammelbestellung", "-150.00")))
    t = core.list_transactions(conn)[0]
    core.create_split_from_tx(conn, t["id"], [
        {"category": "4930 - Bürobedarf", "amount": "100.00", "vat_rate": 19},
        {"category": "4980 - Sonstiges", "amount": "50,00", "vat_rate": 7}])
    exps = core.list_expenses(conn)
    assert len(exps) == 2
    assert {e["category"] for e in exps} == {"4930 - Bürobedarf", "4980 - Sonstiges"}
    assert all(e["source_tx"] == t["id"] for e in exps)
    assert core.get_transaction(conn, t["id"])["category"] == "ausgabe"


def test_split_ueberspringt_ungueltige_zeilen(conn, tx):
    core.create_split_from_tx(conn, tx["id"], [
        {"category": "A", "amount": "0"}, {"category": "B", "amount": "keine Zahl"},
        {"category": "C", "amount": "59.99", "vat_rate": 19}])
    assert [e["category"] for e in core.list_expenses(conn)] == ["C"]


def test_split_ohne_gueltige_zeile_laesst_buchung_offen(conn, tx):
    core.create_split_from_tx(conn, tx["id"], [{"category": "A", "amount": "0"}])
    assert core.get_transaction(conn, tx["id"])["category"] == "offen"
    assert core.list_expenses(conn) == []


# ── Verknüpfen / Zurücksetzen / Löschen ──────────────────────────────────
def test_link_tx_invoice(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    tid = core.create_manual_transaction(conn, "2026-04-01", "Überweisung", "1190.00")
    core.link_tx_invoice(conn, tid, inv["id"])
    assert core.get_transaction(conn, tid)["category"] == "einnahme_rechnung"
    assert core.get_invoice(conn, inv["id"])["paid_date"] == "2026-04-01"


def test_reset_tx_loest_ausgabe(conn, tx):
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19)
    core.reset_tx(conn, tx["id"])
    t = core.get_transaction(conn, tx["id"])
    assert t["category"] == "offen" and t["expense_id"] is None
    assert core.list_expenses(conn) == [], "die erzeugte Ausgabe muss mitgelöscht werden"


def test_reset_tx_loest_split_positionen(conn, tx):
    core.create_split_from_tx(conn, tx["id"], [
        {"category": "A", "amount": "30"}, {"category": "B", "amount": "29.99"}])
    core.reset_tx(conn, tx["id"])
    assert core.list_expenses(conn) == []


def test_reset_tx_setzt_rechnung_wieder_auf_offen(conn, customer, make_invoice):
    inv = make_invoice(customer=customer)
    tid = core.create_manual_transaction(conn, "2026-04-01", "Überweisung", "1190.00")
    core.link_tx_invoice(conn, tid, inv["id"])
    core.reset_tx(conn, tid)
    assert core.get_invoice(conn, inv["id"])["paid_date"] is None
    assert core.get_transaction(conn, tid)["invoice_id"] is None


def test_reset_tx_unbekannt_ist_harmlos(conn):
    core.reset_tx(conn, 999)


def test_delete_transaction(conn, tx):
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19)
    core.delete_transaction(conn, tx["id"])
    assert core.get_transaction(conn, tx["id"]) is None
    assert core.list_expenses(conn) == []


def test_delete_transaction_unbekannt_ist_harmlos(conn):
    core.delete_transaction(conn, 999)


# ── Belege an Buchungen ──────────────────────────────────────────────────
def test_attach_receipt_to_tx(conn, tx):
    dest = core.attach_receipt_to_tx(conn, tx["id"], b"%PDF beleg", "rechnung 03/2026.pdf")
    assert dest.exists() and core.DOC_DIR / "eingang" in dest.parents
    assert core.abs_pfad(core.get_transaction(conn, tx["id"])["receipt_path"]) == dest


def test_attach_receipt_reicht_an_die_ausgabe_durch(conn, tx):
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19)
    dest = core.attach_receipt_to_tx(conn, tx["id"], b"%PDF", "b.pdf")
    assert core.abs_pfad(core.list_expenses(conn)[0]["receipt_path"]) == dest


def test_beleg_der_buchung_landet_auf_neuer_ausgabe(conn, tx):
    core.attach_receipt_to_tx(conn, tx["id"], b"%PDF", "b.pdf")
    core.create_expense_from_tx(conn, tx["id"], "4920 - Telefon", 19)
    assert core.list_expenses(conn)[0]["receipt_path"] is not None


# ── Buchungsjournal (vollständige Zahlungen-Ansicht) ─────────────────────
def test_list_ledger_vereint_ausgaben_rechnungen_bank(conn, cfg, customer, make_invoice):
    make_invoice(customer=customer, issue_date="2026-02-01", paid_date="2026-02-05")
    core.create_expense(conn, date_="2026-02-10", vendor="Telekom", description="Mobilfunk",
                        category="4920 - Telefon", gross="119", vat_rate=19, paid_date="2026-02-10")
    core.create_manual_transaction(conn, "2026-02-15", "Privatentnahme", -500, "EUR",
                                   method="Überweisung")
    led = core.list_ledger(conn, "2026-01-01", "2026-12-31")
    kinds = sorted(r["kind"] for r in led)
    assert kinds == ["bank", "expense", "invoice"]
    exp = next(r for r in led if r["kind"] == "expense")
    inv = next(r for r in led if r["kind"] == "invoice")
    assert exp["amount"] < 0 and inv["amount"] > 0            # Ausgang −, Eingang +
    assert exp["skr_no"] == "4920" and exp["tax"] == "19% VSt."


def test_list_ledger_zaehlt_nicht_doppelt(conn, cfg):
    """Eine Bank-Buchung, aus der eine Ausgabe gebucht wurde, erscheint nur als Ausgabe."""
    core.import_bank_csv(conn, ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
                                "Currency,State,Balance\n"
                                "CARD_PAYMENT,Current,2026-02-10 10:00:00,2026-02-10 10:00:00,"
                                "Amazon,-40.00,0.00,EUR,COMPLETED,100\n"))
    tid = core.list_transactions(conn)[0]["id"]
    core.create_expense_from_tx(conn, tid, "4930 - Bürobedarf", 19)
    led = core.list_ledger(conn, "2026-01-01", "2026-12-31")
    assert [r["kind"] for r in led] == ["expense"], "Bank-Zeile darf nicht zusätzlich auftauchen"


# ── Eine Datenlage: EÜR == Summe des Journals (list_ledger) ───────────────
def test_euer_ist_summe_des_journals(conn, cfg, customer, make_invoice):
    make_invoice(customer=customer, issue_date="2026-02-01", paid_date="2026-02-05")
    core.create_expense(conn, date_="2026-02-10", vendor="Telekom", description="Handy",
                        category="4920 - Telefon", gross="119", vat_rate=19,
                        paid_date="2026-02-10", private_share=80)
    lo, hi = "2026-01-01", "2026-12-31"
    led = core.list_ledger(conn, lo, hi)
    e = core.euer(conn, cfg, lo, hi)

    def s(key):
        return round(sum((r.get("_euer") or {}).get(key, 0.0) for r in led), 2)

    # Jede EÜR-Kennzahl ist exakt die Summe der sichtbaren Journal-Zeilen:
    assert round(e["umsatz_net"], 2) == s("umsatz_net")
    assert round(e["aufwand_net"], 2) == s("aufwand_net")
    assert round(e["vorsteuer"], 2) == s("vorsteuer")
    assert round(e["eigenverbrauch_net"], 2) == s("eig_net")
    # Der Privatanteil ist eine SICHTBARE Zeile, keine versteckte Rechnung:
    nutz = [r for r in led if r["kind"] == "nutzungsentnahme"]
    assert len(nutz) == 1
    assert round(nutz[0]["amount"], 2) == round(nutz[0]["_euer"]["eig_net"]
                                                + nutz[0]["_euer"]["eig_ust"], 2)
    assert nutz[0]["skr_no"] == "8921"
