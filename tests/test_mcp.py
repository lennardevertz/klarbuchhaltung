"""MCP-Server: dieselben Tools wie die Assistant-Schicht, korrekt an dispatch verdrahtet."""
import asyncio
import json

import pytest

import assistant
import core
import mcp_server


def list_tools():
    return asyncio.run(mcp_server.mcp.list_tools())


# ── Registrierung / Parität ──────────────────────────────────────────────
def test_mcp_registriert_alle_daten_tools():
    """Jedes Assistant-Tool außer den UI-/Dev-Tools muss als MCP-Tool existieren."""
    mcp_names = {t.name for t in list_tools()}
    expected = {n for n in assistant.tool_names() if n not in mcp_server.EXCLUDED}
    assert mcp_names == expected


def test_mcp_schliesst_ui_und_dev_tools_aus():
    names = {t.name for t in list_tools()}
    assert "present" not in names
    assert "run_shell" not in names


def test_mcp_deckt_kernfunktionen_ab():
    names = {t.name for t in list_tools()}
    for must in ("list_invoices", "create_invoice", "create_expense", "report", "vat",
                 "zm", "vat_annual", "figures", "figures_yearly", "import_bank_csv",
                 "split_transaction", "record_payment", "send_invoice", "close_period",
                 "fx_rate"):
        assert must in names, f"{must} fehlt im MCP-Server"


def test_mcp_pflichtfelder_stimmen():
    by_name = {t.name: t for t in list_tools()}
    assert set(by_name["vat"].inputSchema["required"]) == {"year", "quarter"}
    assert by_name["report"].inputSchema["required"] == ["year"]
    assert set(by_name["create_expense"].inputSchema["required"]) >= {"date", "vendor", "gross"}


def test_mcp_jedes_tool_hat_beschreibung():
    for t in list_tools():
        assert t.description and len(t.description) > 5


# ── Ausführung über den MCP-Funktionskörper ──────────────────────────────
def _fn(name):
    """Die tatsächlich registrierte MCP-Funktion holen (ruft dispatch auf)."""
    return mcp_server.mcp._tool_manager._tools[name].fn


def test_mcp_call_list_invoices(sandbox):
    assert json.loads(_fn("list_invoices")()) == []


def test_mcp_call_create_und_report(sandbox):
    r = json.loads(_fn("create_invoice")(
        customer_name="ACME", kind="domestic", issue_date="2026-02-01",
        items=[{"description": "X", "quantity": 1, "unit_price": 1000, "vat_rate": 19}]))
    assert r["created"].startswith("2026-02")
    num = core.list_invoices(core.db())[0]["number"]
    json.loads(_fn("mark_invoice_paid")(number=num, paid_date="2026-02-15"))
    rep = json.loads(_fn("report")(year=2026))
    assert rep["betriebseinnahmen"] == 1190.0


def test_mcp_leere_optionale_felder_werden_verworfen(sandbox):
    # only_open/query leer -> dispatch bekommt sie gar nicht erst
    out = json.loads(_fn("list_transactions")(query="", only_open=False))
    assert out == []


def test_mcp_build_typkonvertierung(sandbox):
    # der dynamische Wrapper reicht year als int durch
    core.create_customer(core.db(), "ACME", "Str", None)
    out = json.loads(_fn("vat")(year=2026, quarter=1))
    assert "zahllast" in out
