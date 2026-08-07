"""CLI-Ende-zu-Ende: jedes Kommando einmal über bookkeeping.main() fahren.
Prüft, dass die Argparse-Verdrahtung stimmt und die richtige core-Funktion trifft."""
import json

import pytest

import bookkeeping
import core


def run(capsys, *argv):
    bookkeeping.main(list(argv))
    return capsys.readouterr().out


def run_json(capsys, *argv):
    out = run(capsys, "--json", *argv)
    # letzte JSON-Struktur aus der Ausgabe holen (Texte davor ignorieren)
    return json.loads(out[out.index("{") if "{" in out.split("\n")[0] else 0:]) \
        if out.strip().startswith(("{", "[")) else json.loads(out)


# ── Rechnungen / Kunden / Ausgaben ───────────────────────────────────────
def test_cli_invoice_nicht_interaktiv(capsys, sandbox):
    out = run(capsys, "invoice", "--customer", "ACME GmbH", "--kind", "domestic",
              "--date", "2026-03-10", "--item", "Beratung|10|h|100|19", "--no-pdf")
    assert "2026-03-10"[:4] in out or "angelegt" in out
    conn = core.db()
    inv = core.list_invoices(conn)[0]
    assert inv["net"] == 1000.0 and inv["vat"] == 190.0


def test_cli_invoice_ohne_customer_bricht_ab(capsys, sandbox):
    with pytest.raises(SystemExit):
        run(capsys, "invoice", "--item", "X|1||100|19", "--no-pdf")


def test_cli_expense_und_list(capsys, sandbox):
    run(capsys, "expense", "--vendor", "Telekom", "--gross", "119", "--vat-rate", "19",
        "--category", "4920 - Telefon", "--date", "2026-03-01")
    out = run(capsys, "list", "expenses")
    assert "Telekom" in out
    conn = core.db()
    assert core.list_expenses(conn)[0]["net"] == 100.0


def test_cli_expense_reverse_charge(capsys, sandbox):
    run(capsys, "expense", "--vendor", "AWS", "--gross", "1000", "--vat-rate", "0",
        "--reverse-charge", "--date", "2026-03-01")
    assert core.db().execute("SELECT reverse_charge FROM expenses").fetchone()[0] == 1


def test_cli_customer_add_show_email(capsys, sandbox):
    run(capsys, "customer", "add", "--name", "ACME GmbH", "--address", "Str. 1;10115 Berlin",
        "--vat-id", "DE1")
    conn = core.db()
    cid = core.list_customers(conn)[0]["id"]
    run(capsys, "customer", "email", str(cid), "kontakt@acme.de")
    assert core.get_customer(conn, cid)["email"] == "kontakt@acme.de"
    out = run(capsys, "customer", "show", str(cid))
    assert "ACME GmbH" in out and "kontakt@acme.de" in out


def test_cli_pay_invoice(capsys, sandbox):
    run(capsys, "invoice", "--customer", "ACME", "--kind", "domestic", "--date", "2026-03-10",
        "--item", "X|1||100|19", "--no-pdf")
    conn = core.db()
    num = core.list_invoices(conn)[0]["number"]
    run(capsys, "pay", "invoice", num, "--date", "2026-04-01")
    assert core.get_invoice_by_number(conn, num)["paid_date"] == "2026-04-01"


def test_cli_pay_unbekannte_rechnung(capsys, sandbox):
    with pytest.raises(SystemExit):
        run(capsys, "pay", "invoice", "gibtsnicht")


def test_cli_payment_legt_buchung_an(capsys, sandbox):
    run(capsys, "invoice", "--customer", "ACME", "--kind", "domestic", "--date", "2026-03-10",
        "--item", "X|1||100|19", "--no-pdf")
    conn = core.db()
    num = core.list_invoices(conn)[0]["number"]
    run(capsys, "payment", num, "--date", "2026-04-01", "--method", "Bar")
    assert core.list_transactions(conn)[0]["category"] == "einnahme_rechnung"


# ── Auswertungen ─────────────────────────────────────────────────────────
@pytest.fixture
def seeded(sandbox, capsys):
    run(capsys, "customer", "add", "--name", "ACME", "--vat-id", "DE1")
    run(capsys, "invoice", "--customer", "ACME", "--kind", "domestic", "--date", "2026-02-01",
        "--item", "Beratung|1|h|1000|19", "--paid", "2026-02-15", "--no-pdf")
    run(capsys, "expense", "--vendor", "Telekom", "--gross", "119", "--vat-rate", "19",
        "--category", "4920 - Telefon", "--date", "2026-02-05")
    return sandbox


def test_cli_report(capsys, seeded):
    out = run(capsys, "report", "2026")
    assert "EÜR 2026" in out and "Betriebseinnahmen" in out


def test_cli_report_json(capsys, seeded):
    data = run_json(capsys, "report", "2026")
    assert data["betriebseinnahmen"] == 1190.0 and data["ergebnis"] == 1071.0


def test_cli_figures(capsys, seeded):
    data = run_json(capsys, "figures", "2026-02")
    assert data["einnahmen"] == 1190.0 and data["ust_zahllast"] == 171.0


def test_cli_figures_yearly(capsys, seeded):
    data = run_json(capsys, "figures-yearly")
    assert [r["jahr"] for r in data] == [2026]
    assert data[0]["einnahmen"] == 1190.0 and data[0]["ausgaben"] == 119.0


def test_cli_figures_yearly_text(capsys, seeded):
    out = run(capsys, "figures-yearly")
    assert "Jahr" in out and "2026" in out


def test_cli_figures_yearly_leer(capsys, sandbox):
    out = run(capsys, "figures-yearly")
    assert "keine jahresübersicht" in out.lower()


def test_cli_vat(capsys, seeded):
    data = run_json(capsys, "vat", "2026", "1")
    assert str(data["ust19"]) == "190.00" and str(data["zahllast"]) == "171.00"


def test_cli_vat_annual(capsys, seeded):
    out = run(capsys, "vat-annual", "2026")
    assert "USt-Jahreserklärung 2026" in out


def test_cli_zm_leer(capsys, seeded):
    out = run(capsys, "zm", "2026", "1")
    assert "Keine EU-Umsätze" in out


# ── Bank / tx ────────────────────────────────────────────────────────────
CSV_HEADER = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
              "Currency,State,Balance\n")


def write_bank_csv(sandbox, *rows):
    p = sandbox / "bank.csv"
    p.write_text(CSV_HEADER + "".join(rows), encoding="utf-8")
    return str(p)


def bank_row(desc, amt, date_="2026-03-15"):
    return (f"CARD_PAYMENT,Current,{date_} 10:00:00,{date_} 10:00:00,{desc},{amt},"
            f"0.00,EUR,COMPLETED,1000.00\n")


def test_cli_bank_import_und_tx_list(capsys, sandbox):
    path = write_bank_csv(sandbox, bank_row("Vodafone", "-59.99"))
    run(capsys, "bank-import", path)
    out = run(capsys, "tx", "list")
    assert "Vodafone" in out


def test_cli_tx_category_und_expense_und_reset(capsys, sandbox):
    path = write_bank_csv(sandbox, bank_row("Vodafone", "-59.99"))
    run(capsys, "bank-import", path)
    conn = core.db()
    tid = core.list_transactions(conn)[0]["id"]
    run(capsys, "tx", "expense", str(tid), "--category", "4920 - Telefon", "--vat-rate", "19")
    assert core.get_transaction(conn, tid)["category"] == "ausgabe"
    run(capsys, "tx", "reset", str(tid))
    assert core.get_transaction(conn, tid)["category"] == "offen"
    assert core.list_expenses(conn) == []


def test_cli_tx_manual_und_category(capsys, sandbox):
    run(capsys, "tx", "manual", "--description", "Bar", "--amount", "-25.50", "--date", "2026-03-01")
    conn = core.db()
    tid = core.list_transactions(conn)[0]["id"]
    run(capsys, "tx", "category", str(tid), "gebuehr")
    assert core.get_transaction(conn, tid)["category"] == "gebuehr"


def test_cli_tx_split(capsys, sandbox):
    run(capsys, "tx", "manual", "--description", "Amazon", "--amount", "-150", "--date", "2026-03-01")
    conn = core.db()
    tid = core.list_transactions(conn)[0]["id"]
    run(capsys, "tx", "split", str(tid), "--line", "4930 - Bürobedarf|100|19",
        "--line", "4980 - Sonstiges|50|7")
    assert len(core.list_expenses(conn)) == 2


def test_cli_tx_link(capsys, sandbox):
    run(capsys, "invoice", "--customer", "ACME", "--kind", "domestic", "--date", "2026-03-10",
        "--item", "X|1||1000|19", "--no-pdf")
    run(capsys, "tx", "manual", "--description", "Überweisung", "--amount", "1190", "--date", "2026-04-01")
    conn = core.db()
    num = core.list_invoices(conn)[0]["number"]
    tid = core.list_transactions(conn)[0]["id"]
    run(capsys, "tx", "link", str(tid), num)
    assert core.get_invoice_by_number(conn, num)["paid_date"] == "2026-04-01"


def test_cli_tx_bulk_category(capsys, sandbox):
    run(capsys, "tx", "manual", "--description", "A", "--amount", "-1", "--date", "2026-03-01")
    run(capsys, "tx", "manual", "--description", "B", "--amount", "-2", "--date", "2026-03-01")
    conn = core.db()
    ids = [str(t["id"]) for t in core.list_transactions(conn)]
    run(capsys, "tx", "bulk-category", *ids, "--category", "gebuehr")
    assert all(t["category"] == "gebuehr" for t in core.list_transactions(conn))


# ── Belege / doc ─────────────────────────────────────────────────────────
def test_cli_doc_add_list_confirm(capsys, sandbox):
    beleg = sandbox / "beleg.pdf"
    beleg.write_bytes(b"%PDF-1.4 x")
    run(capsys, "doc", "add", str(beleg), "--vendor", "Telekom", "--amount", "119",
        "--date", "2026-03-01")
    out = run(capsys, "doc", "list")
    assert "Telekom" in out
    conn = core.db()
    did = core.list_documents(conn)[0]["id"]
    run(capsys, "doc", "confirm", str(did), "--category", "4920 - Telefon", "--vat-rate", "19")
    assert core.get_document(conn, did)["status"] == "matched"
    assert core.list_expenses(conn)[0]["gross"] == 119.0


# ── Stammdaten ───────────────────────────────────────────────────────────
def test_cli_accounts(capsys, sandbox):
    run(capsys, "accounts", "add", "4600 - Werbekosten")
    out = run(capsys, "accounts", "list")
    assert "4600 - Werbekosten" in out
    run(capsys, "accounts", "remove", "4600 - Werbekosten")
    assert "4600 - Werbekosten" not in core.load_settings().get("custom_accounts", [])


def test_cli_recurring(capsys, sandbox):
    run(capsys, "recurring", "add", "--client", "ACME", "--service", "Wartung",
        "--interval", "monatlich", "--next-run", "2026-08-01")
    out = run(capsys, "recurring", "list")
    assert "ACME" in out
    rid = core.list_recurring()[0]["id"]
    run(capsys, "recurring", "advance", rid)
    assert core.list_recurring()[0]["next_run"] == "2026-09-01"


def test_cli_dunning(capsys, sandbox):
    run(capsys, "invoice", "--customer", "ACME", "--kind", "domestic", "--date", "2026-03-10",
        "--item", "X|1||1000|19", "--no-pdf")
    conn = core.db()
    num = core.list_invoices(conn)[0]["number"]
    out = run(capsys, "dunning", "list")
    assert num in out
    run(capsys, "dunning", "advance", num)
    assert core.dunning_cases(conn)[0]["stage"] == 1


def test_cli_period(capsys, sandbox):
    run(capsys, "period", "close", "2026-Q1")
    out = run(capsys, "period", "list")
    assert "2026-Q1" in out
    run(capsys, "period", "reopen", "2026-Q1")
    assert core.closed_periods() == []


def test_cli_settings_set_get_unset(capsys, sandbox):
    run(capsys, "settings", "set", "home_currency=USD", "euer_method=netto")
    data = run_json(capsys, "settings", "get")
    assert data["home_currency"] == "USD" and data["euer_method"] == "netto"
    run(capsys, "settings", "unset", "home_currency")
    assert "home_currency" not in core.load_settings()


def test_cli_settings_maskiert_geheimnisse(capsys, sandbox):
    core.save_settings({"api_key": "geheim123", "model": "gpt"})
    out = run(capsys, "settings", "get")
    assert "geheim123" not in out and "········" in out


def test_cli_fx_gleiche_waehrung(capsys, sandbox):
    data = run_json(capsys, "fx", "EUR", "EUR")
    assert data["rate"] == 1.0


# ── Tool-Schnittstelle ───────────────────────────────────────────────────
def test_cli_tools_listet_auf(capsys, sandbox):
    out = run(capsys, "tools")
    assert "list_invoices" in out and "create_invoice" in out


def test_cli_call_dispatch(capsys, sandbox):
    out = run(capsys, "call", "list_customers")
    assert json.loads(out) == []


def test_cli_call_unbekannt(capsys, sandbox):
    with pytest.raises(SystemExit):
        run(capsys, "call", "gibtsnicht")
