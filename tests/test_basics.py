"""Reine Rechen-/Formatfunktionen und Zeitraum-Logik – ohne Datenbank."""
from datetime import date
from decimal import Decimal

import pytest

import core


# ── Geld & Formatierung ──────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("1000", "1000.00"), (1234.5, "1234.50"), ("0.005", "0.01"),
    ("0.004", "0.00"), ("-19.995", "-20.00"), (Decimal("2.345"), "2.35"),
])
def test_money_rundet_kaufmaennisch(value, expected):
    assert str(core.money(value)) == expected


@pytest.mark.parametrize("value,expected", [
    (1234.5, "1.234,50"), (0, "0,00"), (1000000, "1.000.000,00"), (-5.5, "-5,50"),
])
def test_fmt_deutsche_schreibweise(value, expected):
    assert core.fmt(value) == expected


def test_eur_und_cur():
    assert core.eur(1234.5) == "1.234,50 €"
    assert core.cur(1234.5) == "€1,234.50 (EUR)"


def test_de_date():
    assert core.de_date("2026-03-09") == "09.03.2026"


# ── Bestandskonten (EÜR-neutral) ─────────────────────────────────────────
@pytest.mark.parametrize("account,neutral", [
    ("1525 - Kautionen (geleistet)", True),
    ("1000 - Kasse", True),
    ("2999 - Grenzfall oben", True),
    ("0999 - unter dem Bereich", False),
    ("3000 - direkt darüber", False),
    ("4920 - Telefon", False),
    ("0480 - Geringwertige Wirtschaftsgüter (GWG)", False),
    ("8400 - Erlöse", False),
    ("Sonstiges", False),
    ("", False),
    (None, False),
])
def test_is_neutral_account(account, neutral):
    assert core.is_neutral_account(account) is neutral


def test_expense_accounts_ergaenzt_eigene_konten(sandbox, cfg):
    base = core.expense_accounts(cfg)
    assert "4920 - Telefon" in base
    core.add_custom_account("4600 - Werbekosten")
    assert core.expense_accounts(cfg)[-1] == "4600 - Werbekosten"
    # doppeltes Hinzufügen ändert nichts
    core.add_custom_account("4600 - Werbekosten")
    assert core.expense_accounts(cfg).count("4600 - Werbekosten") == 1


def test_expense_accounts_faellt_auf_default_zurueck(sandbox):
    assert core.expense_accounts({}) == core.DEFAULT_EXPENSE_ACCOUNTS


# ── Rechnungsnummern ─────────────────────────────────────────────────────
@pytest.mark.parametrize("number,parsed", [
    ("2026-06-0001", (2026, 6, 1)),
    ("RE-2026-6-42", (2026, 6, 42)),
    ("2026-12-9999", (2026, 12, 9999)),
])
def test_parse_invoice_number(number, parsed):
    assert core.parse_invoice_number(number) == parsed


@pytest.mark.parametrize("bad", ["abc", "2026/06/0001", "26-6-1", ""])
def test_parse_invoice_number_wirft(bad):
    with pytest.raises(ValueError):
        core.parse_invoice_number(bad)


# ── Zeiträume ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("year,period,n,expected", [
    (2026, "month", 1, ("2026-01-01", "2026-01-31", "2026-01")),
    (2026, "month", 2, ("2026-02-01", "2026-02-28", "2026-02")),
    (2028, "month", 2, ("2028-02-01", "2028-02-29", "2028-02")),   # Schaltjahr
    (2026, "month", 12, ("2026-12-01", "2026-12-31", "2026-12")),
    (2026, "quarter", 1, ("2026-01-01", "2026-03-31", "2026 Q1")),
    (2026, "quarter", 4, ("2026-10-01", "2026-12-31", "2026 Q4")),
])
def test_period_bounds(year, period, n, expected):
    assert core.period_bounds(year, period, n) == expected


@pytest.mark.parametrize("token,expected", [
    ("2026", ("2026-01-01", "2026-12-31", "Jahr 2026", "2026")),
    ("2026-Q2", ("2026-04-01", "2026-06-30", "2. Quartal 2026", "2026-Q2")),
    ("2026-07", ("2026-07-01", "2026-07-31", "07/2026", "2026-07")),
    ("2026-7", ("2026-07-01", "2026-07-31", "07/2026", "2026-07")),
])
def test_period_bounds_token(token, expected):
    assert core.period_bounds_token(token) == expected


@pytest.mark.parametrize("token", ["", None, "quatsch", "2026-Q5"])
def test_period_bounds_token_faellt_auf_aktuellen_monat(token):
    lo, hi, label, canon = core.period_bounds_token(token)
    t = date.today()
    assert canon == f"{t.year}-{t.month:02d}"
    assert lo.startswith(f"{t.year}-{t.month:02d}")
    assert label == f"{t.month:02d}/{t.year}"
