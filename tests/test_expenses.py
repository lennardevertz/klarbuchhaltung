"""Betriebsausgaben inkl. Privatanteil und Belegablage."""
from decimal import Decimal

import pytest

import core


def mk(conn, **kw):
    base = dict(date_="2026-03-10", vendor="Telekom", description="Mobilfunk",
                category="4920 - Telefon", gross="119.00", vat_rate=19,
                paid_date="2026-03-10")
    base.update(kw)
    return core.create_expense(conn, **base)


def test_netto_und_vorsteuer_aus_brutto(conn):
    r = mk(conn)
    assert r["net"] == Decimal("100.00")
    assert r["vat"] == Decimal("19.00")
    assert r["gross"] == Decimal("119.00")
    assert r["private_share"] == Decimal("0")


@pytest.mark.parametrize("rate,net,vat", [
    (19, "100.00", "19.00"), (7, "111.21", "7.79"), (0, "119.00", "0.00"),
])
def test_verschiedene_ust_saetze(conn, rate, net, vat):
    r = mk(conn, vat_rate=rate)
    assert (str(r["net"]), str(r["vat"])) == (net, vat)


def test_privatanteil_kuerzt_betriebsausgabe(conn):
    r = mk(conn, private_share=80)
    assert r["net"] == Decimal("20.00"), "nur 20 % sind betrieblich"
    assert r["vat"] == Decimal("3.80")
    assert r["gross"] == Decimal("23.80")
    assert r["gross_full"] == Decimal("119.00"), "Originalbetrag bleibt erhalten"


@pytest.mark.parametrize("share,expected_net", [
    (-50, "100.00"),    # negativ wird auf 0 geklemmt
    (0, "100.00"),
    (100, "0.00"),
    (150, "0.00"),      # über 100 wird auf 100 geklemmt
])
def test_privatanteil_wird_begrenzt(conn, share, expected_net):
    assert str(mk(conn, private_share=share)["net"]) == expected_net


def test_komma_betrag(conn):
    assert mk(conn, gross="1.190,00".replace(".", ""))["net"] == Decimal("1000.00")


def test_unbezahlte_ausgabe(conn):
    r = mk(conn, paid_date=None)
    row = core.list_expenses(conn)[0]
    assert row["id"] == r["id"] and row["paid_date"] is None


def test_mark_expense_paid(conn):
    r = mk(conn, paid_date=None)
    core.mark_expense_paid(conn, r["id"], "2026-04-05")
    assert core.list_expenses(conn)[0]["paid_date"] == "2026-04-05"


def test_list_expenses_sortiert_nach_datum(conn):
    mk(conn, date_="2026-05-01", vendor="B")
    mk(conn, date_="2026-01-01", vendor="A")
    assert [e["vendor"] for e in core.list_expenses(conn)] == ["A", "B"]


def test_beleg_wird_in_die_ablage_kopiert(conn, tmp_path):
    src = tmp_path / "rechnung.pdf"
    src.write_bytes(b"%PDF-1.4 beleg")
    r = mk(conn, receipt_src=str(src))
    stored = core.list_expenses(conn)[0]["receipt_path"]
    assert stored and core.abs_pfad(stored).exists()
    assert core.abs_pfad(stored).read_bytes() == b"%PDF-1.4 beleg"
    assert core.DOC_DIR / "eingang" in core.abs_pfad(stored).parents   # Ausgaben-Beleg -> Eingang
    assert r["id"] == 1


def test_fehlender_belegpfad_ist_kein_fehler(conn, tmp_path):
    mk(conn, receipt_src=str(tmp_path / "gibtsnicht.pdf"))
    assert core.list_expenses(conn)[0]["receipt_path"] is None


# ── Bearbeiten / Löschen ─────────────────────────────────────────────────
def test_update_expense_rechnet_neu(conn):
    r = mk(conn, gross="119.00", vat_rate=19, private_share=0)
    core.update_expense(conn, r["id"], category="4930 - Bürobedarf", vat_rate=7, private_share=50)
    e = core.list_expenses(conn)[0]
    assert e["category"] == "4930 - Bürobedarf" and e["vat_rate"] == 7
    assert e["private_share"] == 50.0
    assert float(e["net"]) == 55.61                # 119/1,07=111,21; davon 50 %
    assert float(e["gross"]) == 59.50


def test_update_expense_unbekannt_wirft(conn):
    import pytest
    with pytest.raises(ValueError):
        core.update_expense(conn, 999, category="X")


def test_update_expense_gesperrt_bei_festschreibung(conn):
    import pytest
    r = mk(conn, date_="2026-03-10")
    core.close_period("2026-Q1")
    with pytest.raises(ValueError):
        core.update_expense(conn, r["id"], vat_rate=7)


def test_delete_expense_entfernt(conn):
    r = mk(conn)
    core.delete_expense(conn, r["id"])
    assert core.list_expenses(conn) == []


def test_delete_expense_gesperrt_bei_festschreibung(conn):
    import pytest
    r = mk(conn, date_="2026-03-10")
    core.close_period("2026-Q1")
    with pytest.raises(ValueError):
        core.delete_expense(conn, r["id"])
    assert len(core.list_expenses(conn)) == 1


# ── Umbuchen (Ausgabe -> Privatentnahme / Bank-Kategorie) ────────────────
def test_book_expense_as_category_privatentnahme(conn):
    r = mk(conn, gross="4000", vat_rate=0)
    core.book_expense_as_category(conn, r["id"], "privatentnahme")
    assert core.list_expenses(conn) == []
    txs = core.list_transactions(conn)
    assert len(txs) == 1 and txs[0]["category"] == "privatentnahme"
    assert float(txs[0]["amount"]) == -4000.0
    assert core.get_rules_map(conn) == {}, "keine Auto-Regel beim Umbuchen"


def test_book_expense_as_category_nutzt_quellzahlung(conn):
    core.import_bank_csv(conn, ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
                                "Currency,State,Balance\n"
                                "CARD_PAYMENT,Current,2026-02-10 10:00:00,2026-02-10 10:00:00,"
                                "Rewe,-50.00,0.00,EUR,COMPLETED,100\n"))
    tid = core.list_transactions(conn)[0]["id"]
    core.create_expense_from_tx(conn, tid, "4930 - Bürobedarf", 19)
    eid = core.list_expenses(conn)[0]["id"]
    core.book_expense_as_category(conn, eid, "privatentnahme")
    assert core.list_expenses(conn) == []
    t = core.get_transaction(conn, tid)
    assert t["category"] == "privatentnahme" and t["expense_id"] is None


def test_book_expense_as_category_ungueltig_wirft(conn):
    import pytest
    r = mk(conn)
    with pytest.raises(ValueError):
        core.book_expense_as_category(conn, r["id"], "ausgabe")


# ── 0 % USt (kein Phantom-Vorsteuer) + Ausgaben-Erstattung ───────────────
def test_null_prozent_ausgabe_ohne_phantom_vorsteuer(conn):
    mk(conn, gross="17.10", vat_rate=0)
    row = next(x for x in core.list_ledger(conn, "2026-01-01", "2026-12-31")
               if x["kind"] == "expense")
    assert row["_euer"]["vorsteuer"] == 0.0          # 0 % bleibt 0 %, keine Phantom-VSt
    assert row["_euer"]["aufwand_net"] == 17.10


def test_erstattung_eingang_wird_negative_ausgabe(conn):
    """Bank-Eingang als Betriebsausgabe kategorisiert = Erstattung/Minderung (negativ)."""
    tid = core.create_manual_transaction(conn, "2026-07-23", "USt-Erstattung", 17.10, "EUR")
    core.create_expense_from_tx(conn, tid, "4964 - Softwarekosten (SaaS)", 0)
    e = core.list_expenses(conn)[0]
    assert float(e["gross"]) == -17.10 and float(e["vat"]) == 0.0
    t = core.get_transaction(conn, tid)
    assert t["category"] == "ausgabe" and t["expense_id"] == e["id"]
    # EÜR: Betriebsausgaben sinken um 17,10 (Minderung), keine USt
    eu = core.euer(conn, None, "2026-07-01", "2026-09-30")
    assert eu["betriebsausgaben"] == -17.10
