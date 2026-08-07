"""Beleg-Posteingang: Aufnehmen, Zuordnen, Verbuchen, Aufteilen."""
import pytest

import core

CSV_HEADER = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,"
              "Currency,State,Balance\n")


def bank_row(desc, amount, date_="2026-03-15"):
    return (CSV_HEADER + f"CARD_PAYMENT,Current,{date_} 10:00:00,{date_} 10:00:00,"
            f"{desc},{amount},0.00,EUR,COMPLETED,1000.00\n")


@pytest.fixture
def doc(conn):
    did = core.add_document(conn, b"%PDF-1.4 beleg", "vodafone_03_2026.pdf",
                            vendor="Vodafone GmbH", amount=59.99, doc_date="2026-03-14")
    return core.get_document(conn, did)


# ── Aufnehmen ────────────────────────────────────────────────────────────
def test_add_document(conn, doc):
    assert doc["status"] == "to_review"
    assert doc["filetype"] == "pdf"
    assert doc["amount"] == 59.99 and doc["vendor"] == "Vodafone GmbH"
    p = core.abs_pfad(doc["file_path"])
    assert p.exists() and core.DOC_DIR / "eingang" in p.parents   # Eingangsbeleg, Jahr/Quartal
    assert p.read_bytes() == b"%PDF-1.4 beleg"


@pytest.mark.parametrize("filename,ftype", [
    ("a.pdf", "pdf"), ("a.PDF", "pdf"), ("b.jpg", "jpg"), ("b.jpeg", "jpg"),
    ("c.png", "png"), ("d.txt", "txt"), ("ohne_endung", "file"),
])
def test_dateityp_erkennung(conn, filename, ftype):
    did = core.add_document(conn, b"x", filename)
    assert core.get_document(conn, did)["filetype"] == ftype


@pytest.mark.parametrize("date_str,quarter", [
    ("2025-01-01", "Q1"), ("2025-03-31", "Q1"), ("2025-04-01", "Q2"),
    ("2025-06-30", "Q2"), ("2025-07-01", "Q3"), ("2025-09-30", "Q3"),
    ("2025-10-01", "Q4"), ("2025-12-31", "Q4"),
])
def test_beleg_dir_quartalsgrenzen(sandbox, date_str, quarter):
    assert core.beleg_dir("eingang", date_str) == core.DOC_DIR / "eingang" / "2025" / quarter


def test_beleg_dir_richtung_getrennt(sandbox):
    assert core.beleg_dir("eingang", "2026-07-01") == core.DOC_DIR / "eingang" / "2026" / "Q3"
    assert core.beleg_dir("ausgang", "2026-07-01") == core.DOC_DIR / "ausgang" / "2026" / "Q3"


def test_beleg_dir_legt_ordner_an(sandbox):
    p = core.beleg_dir("eingang", "2027-05-05")
    assert p.exists() and p.is_dir()


@pytest.mark.parametrize("bad", [None, "", "kein-datum", "2026-13-99"])
def test_beleg_dir_faellt_auf_heute_zurueck(sandbox, bad):
    p = core.beleg_dir("eingang", bad)
    heute = core.date.today()
    assert p == core.DOC_DIR / "eingang" / str(heute.year) / f"Q{(heute.month - 1)//3 + 1}"


def test_add_document_saeubert_dateinamen(conn):
    did = core.add_document(conn, b"x", "../../böse/pfad.pdf")
    p = core.abs_pfad(core.get_document(conn, did)["file_path"])
    assert core.DOC_DIR in p.parents, "kein Ausbruch aus dem Dokumentenordner"
    assert "/" not in p.name and "\\" not in p.name


def test_list_und_get_und_delete(conn, doc):
    assert [d["id"] for d in core.list_documents(conn)] == [doc["id"]]
    core.delete_document(conn, doc["id"])
    assert core.get_document(conn, doc["id"]) is None
    assert core.list_documents(conn) == []


# ── Vorschlag ────────────────────────────────────────────────────────────
def test_suggest_findet_passende_zahlung(conn, doc):
    core.import_bank_csv(conn, bank_row("Vodafone GmbH Mobilfunk", "-59.99"))
    tid = core.list_transactions(conn)[0]["id"]
    assert core.suggest_tx_for_document(conn, doc["amount"], doc["doc_date"], doc["vendor"]) == tid


def test_suggest_verlangt_centgenauen_betrag(conn, doc):
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.98"))
    assert core.suggest_tx_for_document(conn, 59.99, "2026-03-14", "Vodafone GmbH") is None


def test_suggest_ohne_betrag_kein_vorschlag(conn):
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.99"))
    assert core.suggest_tx_for_document(conn, None, "2026-03-14", "Vodafone GmbH") is None
    assert core.suggest_tx_for_document(conn, "keine Zahl", "2026-03-14", "Vodafone") is None


def test_suggest_betrag_allein_genuegt_nicht(conn):
    """Ohne passenden Anbieter UND ohne nahes Datum darf nichts vorgeschlagen werden."""
    core.import_bank_csv(conn, bank_row("Irgendein Laden", "-59.99", date_="2026-03-15"))
    assert core.suggest_tx_for_document(conn, 59.99, "2025-01-01", "Vodafone GmbH") is None


def test_suggest_akzeptiert_nahes_datum_ohne_anbieter(conn):
    core.import_bank_csv(conn, bank_row("Unbekannt", "-59.99", date_="2026-03-15"))
    assert core.suggest_tx_for_document(conn, 59.99, "2026-03-14", "Vodafone") is not None


def test_suggest_ignoriert_eingaenge_und_gebuchte(conn):
    core.import_bank_csv(conn, bank_row("Vodafone Gutschrift", "59.99"))
    assert core.suggest_tx_for_document(conn, 59.99, "2026-03-14", "Vodafone") is None
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.99", date_="2026-03-16"))
    tid = core.list_transactions(conn, only_open=True)[0]["id"]
    core.set_tx_category(conn, tid, "gebuehr")
    assert core.suggest_tx_for_document(conn, 59.99, "2026-03-14", "Vodafone") is None


def test_suggest_bevorzugt_anbieter_treffer(conn):
    core.import_bank_csv(conn, bank_row("Unbekannter Laden", "-59.99", date_="2026-03-14"))
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.99", date_="2026-03-15"))
    txs = {t["description"]: t["id"] for t in core.list_transactions(conn)}
    best = core.suggest_tx_for_document(conn, 59.99, "2026-03-14", "Vodafone GmbH")
    assert best == txs["Vodafone GmbH"]


# ── Verbuchen ────────────────────────────────────────────────────────────
def test_match_document_to_tx(conn, doc):
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.99"))
    tid = core.list_transactions(conn)[0]["id"]
    core.match_document_to_tx(conn, doc["id"], tid, category="4920 - Telefon",
                              vat_rate=19, private_share=80)
    d = core.get_document(conn, doc["id"])
    t = core.get_transaction(conn, tid)
    e = core.list_expenses(conn)[0]
    assert d["status"] == "matched" and d["expense_id"] == e["id"]
    assert t["category"] == "ausgabe" and t["expense_id"] == e["id"]
    assert t["receipt_path"] is not None, "der Beleg muss an der Zahlung hängen"
    assert e["private_share"] == 80.0
    assert len(core.list_expenses(conn)) == 1, "kein zweiter Datensatz"


def test_match_unbekanntes_dokument_ist_harmlos(conn):
    core.match_document_to_tx(conn, 999, 1, category="X", vat_rate=19)


def test_confirm_document_bucht_eigenstaendig(conn, doc):
    core.confirm_document(conn, doc["id"], category="4920 - Telefon", vat_rate=19)
    e = core.list_expenses(conn)[0]
    d = core.get_document(conn, doc["id"])
    assert d["status"] == "matched" and d["expense_id"] == e["id"]
    assert e["vendor"] == "Vodafone GmbH" and e["gross"] == 59.99
    assert e["date"] == "2026-03-14" and e["paid_date"] == "2026-03-14"
    assert e["receipt_path"] == doc["file_path"]


def test_confirm_document_ohne_datum_nutzt_heute(conn):
    did = core.add_document(conn, b"x", "beleg.pdf", vendor="Laden", amount=10)
    core.confirm_document(conn, did, category="4980 - Sonstiges", vat_rate=19)
    e = core.list_expenses(conn)[0]
    assert e["date"] == core.date.today().isoformat()


def test_confirm_unbekanntes_dokument_ist_harmlos(conn):
    core.confirm_document(conn, 999, category="X", vat_rate=19)
    assert core.list_expenses(conn) == []


# ── Aufteilen ────────────────────────────────────────────────────────────
def test_split_document_eigenstaendig(conn, doc):
    core.split_document(conn, doc["id"], [
        {"category": "4920 - Telefon", "amount": "40.00", "vat_rate": 19},
        {"category": "4980 - Sonstiges", "amount": "19,99", "vat_rate": 19}])
    exps = core.list_expenses(conn)
    assert len(exps) == 2
    assert all(e["receipt_path"] == doc["file_path"] for e in exps)
    assert core.get_document(conn, doc["id"])["status"] == "matched"
    assert all(e["date"] == "2026-03-14" for e in exps)


def test_split_document_an_zahlung(conn, doc):
    core.import_bank_csv(conn, bank_row("Vodafone GmbH", "-59.99", date_="2026-03-20"))
    tid = core.list_transactions(conn)[0]["id"]
    core.split_document(conn, doc["id"], [
        {"category": "4920 - Telefon", "amount": "40.00", "vat_rate": 19},
        {"category": "4980 - Sonstiges", "amount": "19.99", "vat_rate": 19}], tx_id=tid)
    t = core.get_transaction(conn, tid)
    exps = core.list_expenses(conn)
    assert t["category"] == "ausgabe" and t["receipt_path"] is not None
    assert all(e["source_tx"] == tid for e in exps)
    assert all(e["date"] == "2026-03-20" for e in exps), "Buchungsdatum kommt von der Zahlung"


def test_split_document_ohne_gueltige_zeile_aendert_nichts(conn, doc):
    core.split_document(conn, doc["id"], [{"category": "A", "amount": "0"}])
    assert core.list_expenses(conn) == []
    assert core.get_document(conn, doc["id"])["status"] == "to_review"


def test_split_unbekanntes_dokument_ist_harmlos(conn):
    core.split_document(conn, 999, [{"category": "A", "amount": "10"}])
    assert core.list_expenses(conn) == []
