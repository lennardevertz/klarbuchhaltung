"""Tool-Schicht (assistant.dispatch): dieselbe Schnittstelle wie Chat, CLI-call und MCP."""
import io
import json

import pytest

import assistant
import core


def call(_tool, **args):
    return json.loads(assistant.dispatch(_tool, args))


CSV_HEADER = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
              "Currency,State,Balance\n")


@pytest.fixture
def sandbox(sandbox, monkeypatch):
    """Datei-Tools (import/add_document) sind per _safe_path auf core.ROOT beschränkt –
    für die Tests ROOT auf das Sandbox-Verzeichnis legen, damit Testdateien erlaubt sind."""
    monkeypatch.setattr(core, "ROOT", sandbox)
    return sandbox


def write_file(sandbox, name, content):
    p = sandbox / name
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)
    return str(p)


# ── Basis-Listen ─────────────────────────────────────────────────────────
def test_list_leer(sandbox):
    assert call("list_invoices") == []
    assert call("list_customers") == []
    assert call("list_transactions") == []


def test_unbekanntes_tool(sandbox):
    assert "error" in call("gibtsnicht")


# ── Rechnungen ───────────────────────────────────────────────────────────
def test_create_invoice(sandbox):
    r = call("create_invoice", customer_name="ACME GmbH", kind="domestic",
             issue_date="2026-03-10",
             items=[{"description": "Beratung", "quantity": 10, "unit": "h",
                     "unit_price": 100, "vat_rate": 19}])
    assert r["created"].startswith("2026-03")
    assert float(r["net"]) == 1000.0 and float(r["gross"]) == 1190.0


def test_create_invoice_ohne_registereintrag(sandbox):
    r = call("create_invoice", customer_name="Einmal AG", kind="domestic",
             issue_date="2026-03-10", save_customer=False,
             items=[{"description": "Beratung", "unit_price": 100, "quantity": 1, "vat_rate": 19}])
    assert r["created"].startswith("2026-03")
    assert call("list_customers") == []


def test_mark_invoice_paid(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 100, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    assert call("mark_invoice_paid", number=num, paid_date="2026-04-01")["ok"] is True
    assert core.get_invoice_by_number(core.db(), num)["paid_date"] == "2026-04-01"


def test_create_invoice_ohne_betrag_legt_nichts_an(sandbox):
    """Default-/Platzhalterwerte dürfen NIE zu einer echten Rechnung führen."""
    r = call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
             items=[{"description": "Leistung", "unit_price": 0, "quantity": 1, "vat_rate": 19}])
    assert "error" in r
    assert core.list_invoices(core.db()) == []


def test_create_invoice_ohne_leistungstext_legt_nichts_an(sandbox):
    r = call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
             items=[{"description": "  ", "unit_price": 100, "quantity": 1, "vat_rate": 19}])
    assert "error" in r
    assert core.list_invoices(core.db()) == []


# ── Chat-Sperre: create_invoice zeigt IMMER die Maske ────────────────────
def _fake_llm(monkeypatch, *responses):
    """assistant._post nacheinander mit vorgegebenen Assistant-Nachrichten antworten lassen."""
    calls = list(responses)

    def fake_post(_base, _key, _body):
        msg = calls.pop(0) if calls else {"role": "assistant", "content": "fertig"}
        return {"choices": [{"message": msg}]}

    monkeypatch.setattr(assistant, "_post", fake_post)
    monkeypatch.setattr(assistant, "is_configured", lambda: True)
    monkeypatch.setattr(assistant, "settings",
                        lambda: {"api_key": "k", "base_url": "http://x", "model": "m"})


def _tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_chat_create_invoice_legt_nichts_an_sondern_zeigt_maske(sandbox, monkeypatch):
    _fake_llm(monkeypatch,
              _tool_call("create_invoice", {
                  "customer_name": "ACME GmbH", "kind": "domestic", "issue_date": "2026-03-10",
                  "items": [{"description": "Beratung", "unit_price": 1000, "quantity": 1,
                             "vat_rate": 19}]}),
              {"role": "assistant", "content": "Bitte ausfüllen."})
    r = assistant.run([{"role": "user", "content": "Ich möchte eine Rechnung erstellen."}])
    assert core.list_invoices(core.db()) == []          # nichts angelegt
    blocks = r["messages"][-1]["_blocks"]
    assert [b["type"] for b in blocks] == ["invoice_form"]
    assert blocks[0]["invoice"]["customer_name"] == "ACME GmbH"
    assert blocks[0]["invoice"]["amount"] == 1000.0     # vorbelegt aus dem Versuch


def test_chat_create_invoice_zeigt_maske_auch_ohne_daten(sandbox, monkeypatch):
    _fake_llm(monkeypatch,
              _tool_call("create_invoice", {"customer_name": "", "kind": "domestic",
                                            "issue_date": "2026-03-10", "items": []}),
              {"role": "assistant", "content": "Bitte ausfüllen."})
    r = assistant.run([{"role": "user", "content": "Rechnung"}])
    assert core.list_invoices(core.db()) == []
    blocks = r["messages"][-1]["_blocks"]
    assert blocks[0]["type"] == "invoice_form"
    assert "amount" not in blocks[0]["invoice"]


def test_chat_rechnungsformular_nur_einmal(sandbox, monkeypatch):
    _fake_llm(monkeypatch,
              _tool_call("present", {"type": "invoice_form", "invoice": {"customer_name": "ACME"}}),
              _tool_call("create_invoice", {"customer_name": "ACME", "kind": "domestic",
                                            "issue_date": "2026-03-10",
                                            "items": [{"description": "X", "unit_price": 10}]}),
              {"role": "assistant", "content": "Bitte ausfüllen."})
    r = assistant.run([{"role": "user", "content": "Rechnung"}])
    blocks = r["messages"][-1]["_blocks"]
    assert [b["type"] for b in blocks] == ["invoice_form"]


def test_mark_invoice_paid_unbekannt(sandbox):
    assert "error" in call("mark_invoice_paid", number="gibtsnicht", paid_date="2026-04-01")


def test_record_payment(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    r = call("record_payment", number=num, paid_date="2026-04-01", method="Bar")
    assert r["tx"] is True and r["number"] == num
    assert core.list_transactions(core.db())[0]["category"] == "einnahme_rechnung"


def test_delete_invoice(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    inv = core.list_invoices(core.db())[0]
    pdf = core.abs_pfad(inv["pdf_path"])
    assert pdf.exists()
    assert call("delete_invoice", number=inv["number"])["deleted"] == inv["number"]
    assert core.list_invoices(core.db()) == []
    assert not pdf.exists()


def test_delete_invoice_loest_zahlung_wieder(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    call("record_payment", number=num, paid_date="2026-04-01", method="Bar")
    assert call("delete_invoice", number=num)["buchungen_geloest"] == 1
    tx = core.list_transactions(core.db())[0]
    assert tx["invoice_id"] is None and tx["category"] == "offen"


def test_delete_invoice_unbekannt(sandbox):
    assert "error" in call("delete_invoice", number="gibtsnicht")


def test_delete_invoice_festgeschrieben_gesperrt(sandbox, monkeypatch):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    monkeypatch.setattr(core, "closed_periods", lambda: ["2026-Q1"])
    assert "error" in call("delete_invoice", number=num)
    assert len(core.list_invoices(core.db())) == 1


def test_regenerate_invoice_pdf(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 100, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    r = call("regenerate_invoice_pdf", number=num)
    assert core.abs_pfad(r["pdf_path"]).exists()


# ── Kunden ───────────────────────────────────────────────────────────────
def test_add_customer_und_email(sandbox):
    c = call("add_customer", name="ACME GmbH", address="Str. 1;10115 Berlin",
             vat_id="DE1", email="a@acme.de")
    assert c["name"] == "ACME GmbH" and c["email"] == "a@acme.de"
    assert call("set_customer_email", customer_id=c["id"], email="neu@acme.de")["ok"] is True
    assert core.get_customer(core.db(), c["id"])["email"] == "neu@acme.de"


# ── Ausgaben ─────────────────────────────────────────────────────────────
def test_create_expense_und_mark_paid(sandbox):
    r = call("create_expense", date="2026-03-01", vendor="Telekom", gross=119, vat_rate=19,
             category="4920 - Telefon")
    assert r["net"] == "100.00"
    eid = core.list_expenses(core.db())[0]["id"]
    assert call("mark_expense_paid", expense_id=eid, paid_date="2026-03-05")["ok"] is True


def test_create_expense_reverse_charge(sandbox):
    call("create_expense", date="2026-03-01", vendor="AWS", gross=1000, vat_rate=0,
         reverse_charge=True)
    assert core.db().execute("SELECT reverse_charge FROM expenses").fetchone()[0] == 1


# ── Bank / Zahlungen ─────────────────────────────────────────────────────
def test_import_bank_csv(sandbox):
    path = write_file(sandbox, "bank.csv", CSV_HEADER +
                      "CARD_PAYMENT,Current,2026-03-15 10:00:00,2026-03-15 10:00:00,"
                      "Vodafone,-59.99,0.00,EUR,COMPLETED,1000\n")
    r = call("import_bank_csv", path=path)
    assert r["imported"] == 1


def test_import_invoices_csv(sandbox):
    path = write_file(sandbox, "inv.csv",
                      "number,issue_date,kind,customer_name,description,quantity,unit_price,vat_rate\n"
                      "2026-05-0001,2026-05-01,domestic,ACME,Beratung,10,100,19\n")
    r = call("import_invoices_csv", path=path, render=False)
    assert r["created"] == ["2026-05-0001"]


def test_create_transaction_und_kategorie(sandbox):
    t = call("create_transaction", date="2026-03-01", description="Bar", amount=-25.5)
    assert t["kategorie"] == "offen"
    r = call("set_transaction_category", tx_id=t["id"], category="gebuehr")
    assert r["kategorie"] == "gebuehr"


def test_book_transaction_as_expense(sandbox):
    t = call("create_transaction", date="2026-03-01", description="Vodafone", amount=-59.99)
    r = call("book_transaction_as_expense", tx_id=t["id"], category="4920 - Telefon", vat_rate=19)
    assert r["kategorie"] == "ausgabe"
    assert core.list_expenses(core.db())[0]["category"] == "4920 - Telefon"


def test_book_transaction_eingang_fehler(sandbox):
    t = call("create_transaction", date="2026-03-01", description="Eingang", amount=100)
    assert "error" in call("book_transaction_as_expense", tx_id=t["id"], category="X")


def test_split_transaction(sandbox):
    t = call("create_transaction", date="2026-03-01", description="Amazon", amount=-150)
    call("split_transaction", tx_id=t["id"], lines=[
        {"category": "4930 - Bürobedarf", "amount": 100, "vat_rate": 19},
        {"category": "4980 - Sonstiges", "amount": 50, "vat_rate": 7}])
    assert len(core.list_expenses(core.db())) == 2


def test_link_reset_delete_transaction(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    t = call("create_transaction", date="2026-04-01", description="Überweisung", amount=1190)
    assert call("link_transaction_to_invoice", tx_id=t["id"], number=num)["ok"] is True
    assert core.get_invoice_by_number(core.db(), num)["paid_date"] == "2026-04-01"
    call("reset_transaction", tx_id=t["id"])
    assert core.get_invoice_by_number(core.db(), num)["paid_date"] is None
    assert call("delete_transaction", tx_id=t["id"])["deleted"] == t["id"]


def test_transaction_unbekannt_fehler(sandbox):
    assert "error" in call("book_transaction_as_expense", tx_id=999, category="X")
    assert "error" in call("reset_transaction", tx_id=999)


def test_list_rules(sandbox):
    t = call("create_transaction", date="2026-03-01", description="Vodafone", amount=-10)
    call("set_transaction_category", tx_id=t["id"], category="gebuehr")
    assert "vodafone" in call("list_rules")


# ── Belege ───────────────────────────────────────────────────────────────
def test_add_document_und_suggest(sandbox):
    beleg = sandbox / "beleg.pdf"
    beleg.write_bytes(b"%PDF x")
    d = call("add_document", path=str(beleg), vendor="Vodafone", amount=59.99, doc_date="2026-03-14")
    assert d["status"] == "to_review"
    # passende Zahlung anlegen und Vorschlag ziehen
    call("create_transaction", date="2026-03-15", description="Vodafone GmbH", amount=-59.99)
    r = call("suggest_document_match", document_id=d["id"])
    assert "vorschlag_tx_id" in r


def test_book_document_eigenstaendig(sandbox):
    beleg = sandbox / "b.pdf"
    beleg.write_bytes(b"%PDF x")
    d = call("add_document", path=str(beleg), vendor="Telekom", amount=119, doc_date="2026-03-01")
    r = call("book_document", document_id=d["id"], category="4920 - Telefon", vat_rate=19)
    assert r["gebucht"] == "b.pdf"
    assert core.get_document(core.db(), d["id"])["status"] == "matched"


def test_split_und_delete_document(sandbox):
    beleg = sandbox / "b.pdf"
    beleg.write_bytes(b"%PDF x")
    d = call("add_document", path=str(beleg), vendor="Amazon", amount=150, doc_date="2026-03-01")
    call("split_document", document_id=d["id"], lines=[
        {"category": "4930 - Bürobedarf", "amount": 100, "vat_rate": 19},
        {"category": "4980 - Sonstiges", "amount": 50, "vat_rate": 7}])
    assert len(core.list_expenses(core.db())) == 2
    d2 = call("add_document", path=str(beleg), vendor="X", amount=10)
    assert call("delete_document", document_id=d2["id"])["deleted"] == d2["id"]


# ── Auswertungen ─────────────────────────────────────────────────────────
def test_report_figures_vat_zm_vat_annual(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-02-01",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    call("mark_invoice_paid", number=num, paid_date="2026-02-15")
    assert call("report", year=2026)["betriebseinnahmen"] == 1190.0
    assert call("figures", period="2026-02")["einnahmen"] == 1190.0
    assert str(call("vat", year=2026, quarter=1)["ust19"]) == "190.00"
    assert call("vat_annual", year=2026)["year"] == 2026
    assert call("zm", year=2026, quarter=1)["rows"] == []


def test_figures_yearly(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-02-01",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    call("mark_invoice_paid", number=num, paid_date="2026-02-15")
    ov = call("figures_yearly")
    assert [r["jahr"] for r in ov] == [2026]
    assert ov[0]["einnahmen"] == 1190.0


def test_figures_yearly_leer(sandbox):
    assert call("figures_yearly") == []


# ── Stammdaten ───────────────────────────────────────────────────────────
def test_accounts_tools(sandbox):
    assert "4920 - Telefon" in call("list_accounts")
    assert "4600 - Werbekosten" in call("add_account", account="4600 - Werbekosten")["custom_accounts"]
    assert call("remove_account", account="4600 - Werbekosten")["custom_accounts"] == []


def test_recurring_tools(sandbox):
    it = call("add_recurring", client="ACME", service="Wartung", interval="monatlich",
              next_run="2026-08-01")
    assert call("list_recurring")[0]["id"] == it["id"]
    assert call("toggle_recurring", id=it["id"])["active"] is False
    assert call("advance_recurring", id=it["id"])["next_run"] == "2026-09-01"
    assert call("delete_recurring", id=it["id"])["deleted"] is True


def test_dunning_tools(sandbox):
    call("create_invoice", customer_name="ACME", kind="domestic", issue_date="2026-03-10",
         items=[{"description": "X", "unit_price": 1000, "quantity": 1, "vat_rate": 19}])
    num = core.list_invoices(core.db())[0]["number"]
    assert call("list_dunning")[0]["number"] == num
    assert call("advance_dunning", number=num)["stage"] == 1
    assert call("reset_dunning", number=num)["stage"] == 0


def test_period_tools(sandbox):
    assert call("close_period", token="2026-Q1")["closed_periods"] == ["2026-Q1"]
    assert call("list_closed_periods") == ["2026-Q1"]
    assert call("reopen_period", token="2026-Q1")["closed_periods"] == []


def test_settings_tools_maskieren(sandbox):
    core.save_settings({"api_key": "geheim", "model": "x"})
    s = call("get_settings")
    assert s["api_key"] == "········" and s["model"] == "x"
    assert call("set_settings", values={"home_currency": "USD"})["ok"] is True
    assert core.load_settings()["home_currency"] == "USD"


def test_fx_rate_tool(sandbox):
    assert call("fx_rate", base="EUR", quote="EUR")["rate"] == 1.0


# ── Konsistenz der Tool-Registrierung ────────────────────────────────────
def test_jedes_tool_ist_dispatchbar(sandbox):
    """Kein registriertes Tool darf mit 'Unbekanntes Tool' antworten – jedes muss
    in dispatch behandelt sein (Argumentfehler sind ok, fehlende Zweige nicht)."""
    for name in assistant.tool_names():
        if name == "present":            # reine UI-Karte, kein Datenpfad
            continue
        out = assistant.dispatch(name, {})
        assert "Unbekanntes Tool" not in out, f"{name} hat keinen dispatch-Zweig"


# ── Fehler dürfen nicht stumm verschwinden ───────────────────────────────
def test_api_fehler_landet_im_verlauf(sandbox, monkeypatch):
    """Die Oberfläche zeigt nur den Verlauf – ohne das bliebe der Chat stumm."""
    import urllib.error
    import assistant

    core.save_settings({"base_url": "https://x/v1", "api_key": "k", "model": "m"})

    def kaputt(*a, **kw):
        raise urllib.error.HTTPError("https://x/v1", 402, "Payment Required", {},
                                     io.BytesIO(b'{"error":"kein Guthaben"}'))

    monkeypatch.setattr(assistant, "_post", kaputt)
    r = assistant.run([{"role": "user", "content": "hallo"}])
    assert "402" in r["reply"]
    assert r["messages"][-1]["role"] == "assistant"
    assert "402" in r["messages"][-1]["content"], "Fehler steht im Verlauf"


def test_max_tokens_wird_gesetzt(sandbox, monkeypatch):
    """Ohne Angabe reservieren Anbieter das Modellmaximum und lehnen bei
    knappem Guthaben ab – genau das ist in der Praxis passiert."""
    import assistant

    core.save_settings({"base_url": "https://x/v1", "api_key": "k", "model": "m"})
    gesehen = {}

    def merke(base, key, payload):
        gesehen.update(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(assistant, "_post", merke)
    assistant.run([{"role": "user", "content": "hallo"}])
    assert gesehen.get("max_tokens") == 4096


def test_max_tokens_ist_einstellbar(sandbox, monkeypatch):
    import assistant

    core.save_settings({"base_url": "https://x/v1", "api_key": "k", "model": "m",
                        "max_tokens": 512})
    gesehen = {}
    monkeypatch.setattr(assistant, "_post",
                        lambda b, k, p: (gesehen.update(p),
                                         {"choices": [{"message": {"role": "assistant",
                                                                   "content": "ok"}}]})[1])
    assistant.run([{"role": "user", "content": "hallo"}])
    assert gesehen.get("max_tokens") == 512
