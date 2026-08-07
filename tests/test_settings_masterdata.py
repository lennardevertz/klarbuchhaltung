"""Einstellungen, Konten, wiederkehrende Rechnungen, Mahnwesen, Perioden."""
import pytest

import core


# ── Settings ─────────────────────────────────────────────────────────────
def test_load_settings_ohne_datei(sandbox):
    assert core.load_settings() == {}


def test_save_und_load_settings(sandbox):
    core.save_settings({"home_currency": "USD", "x": 1})
    assert core.load_settings() == {"home_currency": "USD", "x": 1}


def test_load_settings_bei_kaputter_datei(sandbox):
    core.SETTINGS_PATH.write_text("{kein json", encoding="utf-8")
    assert core.load_settings() == {}


def test_update_settings_bewahrt_uebrige(sandbox):
    core.save_settings({"a": 1, "b": 2})
    core.update_settings(b=3, c=4)
    assert core.load_settings() == {"a": 1, "b": 3, "c": 4}


def test_home_currency_default_und_gross(sandbox):
    assert core.home_currency() == "EUR"
    core.save_settings({"home_currency": "usd"})
    assert core.home_currency() == "USD"


# ── Konten ───────────────────────────────────────────────────────────────
def test_add_und_remove_custom_account(sandbox):
    assert core.add_custom_account("4600 - Werbekosten") == ["4600 - Werbekosten"]
    core.add_custom_account("4600 - Werbekosten")            # doppelt = no-op
    assert core.load_settings()["custom_accounts"] == ["4600 - Werbekosten"]
    assert core.remove_custom_account("4600 - Werbekosten") == []


def test_add_custom_account_ignoriert_leer(sandbox):
    assert core.add_custom_account("   ") == []


# ── Wiederkehrende Rechnungen ────────────────────────────────────────────
def test_add_recurring(sandbox):
    it = core.add_recurring("ACME", "Wartung", "monatlich", "500", "2026-08-01")
    assert it["client"] == "ACME" and it["active"] is True
    assert len(it["id"]) == 8
    assert core.list_recurring() == [it]


def test_add_recurring_ungueltiges_intervall(sandbox):
    with pytest.raises(ValueError, match="Intervall"):
        core.add_recurring("ACME", "Wartung", "täglich")


def test_toggle_recurring(sandbox):
    it = core.add_recurring("ACME", "Wartung")
    assert core.toggle_recurring(it["id"]) is False
    assert core.toggle_recurring(it["id"]) is True
    assert core.toggle_recurring("unbekannt") is None


def test_delete_recurring(sandbox):
    it = core.add_recurring("ACME", "Wartung")
    assert core.delete_recurring(it["id"]) is True
    assert core.delete_recurring(it["id"]) is False
    assert core.list_recurring() == []


@pytest.mark.parametrize("interval,start,expected", [
    ("monatlich", "2026-01-15", "2026-02-15"),
    ("quartalsweise", "2026-01-15", "2026-04-15"),
    ("jährlich", "2026-01-15", "2027-01-15"),
    ("monatlich", "2026-12-20", "2027-01-20"),      # Jahreswechsel
    ("monatlich", "2026-01-31", "2026-02-28"),      # Monatsende wird gekappt
])
def test_advance_recurring(sandbox, interval, start, expected):
    it = core.add_recurring("ACME", "Wartung", interval, next_run=start)
    assert core.advance_recurring(it["id"]) == expected
    assert core.list_recurring()[0]["next_run"] == expected


def test_advance_recurring_unbekannt(sandbox):
    assert core.advance_recurring("gibtsnicht") is None


# ── Mahnwesen ────────────────────────────────────────────────────────────
def test_dunning_cases_nur_unbezahlte(conn, cfg, customer, make_invoice):
    offen = make_invoice(customer=customer, issue_date="2026-03-01")
    bezahlt = make_invoice(customer=customer, issue_date="2026-03-02")
    core.mark_invoice_paid(conn, bezahlt["id"], "2026-03-10")
    cases = core.dunning_cases(conn)
    assert [c["number"] for c in cases] == [offen["number"]]
    assert cases[0]["stage"] == 0 and cases[0]["stage_name"] == "Zahlungserinnerung"


def test_advance_und_reset_dunning(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer)
    assert core.advance_dunning(inv["number"]) == 1
    assert core.advance_dunning(inv["number"]) == 2
    assert core.dunning_cases(conn)[0]["stage_name"] == "2. Mahnung"
    core.reset_dunning(inv["number"])
    assert core.dunning_cases(conn)[0]["stage"] == 0


def test_advance_dunning_deckelt_bei_inkasso(conn, cfg, customer, make_invoice):
    inv = make_invoice(customer=customer)
    for _ in range(10):
        core.advance_dunning(inv["number"])
    assert core.advance_dunning(inv["number"]) == len(core.DUNNING_STAGES) - 1


# ── Perioden ─────────────────────────────────────────────────────────────
def test_close_und_reopen_period(sandbox):
    assert core.closed_periods() == []
    core.close_period("2026-Q1")
    core.close_period("2026-Q1")                    # idempotent
    assert core.closed_periods() == ["2026-Q1"]
    assert core.is_period_closed("2026-Q1") is True
    assert core.is_period_closed("2026-Q2") is False
    core.reopen_period("2026-Q1")
    assert core.closed_periods() == []


def test_close_period_leer_wirft(sandbox):
    with pytest.raises(ValueError):
        core.close_period("  ")
