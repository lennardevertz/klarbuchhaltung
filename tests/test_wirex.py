"""Wirex-Umsätze: Normalisierung und Einpflegen in die Zahlungen."""
import json
import time
from types import SimpleNamespace

import pytest

import core
import wirex


CARD = {"id": "tx-1", "created_at": "2026-07-20T10:00:00Z", "amount": 12.5,
        "currency": "EUR", "direction": "Out", "merchant_name": "Spotify"}
BANK = {"transfer_id": "tr-9", "created_at": "2026-07-21T08:00:00Z", "amount": 500,
        "currency": "EUR", "type": "Sepa", "counterparty": {"name": "Kunde GmbH"}}


def rows():
    return [wirex.normalize_tx(CARD, "card"), wirex.normalize_tx(BANK, "bank")]


# ── Normalisierung ──────────────────────────────────────────────────────
def test_ausgang_wird_negativ():
    r = wirex.normalize_tx(CARD, "card")
    assert r["amount"] == -12.5
    assert r["ref"] == "wirex:card:tx-1"
    assert r["description"] == "Spotify"


def test_eingang_bleibt_positiv_und_liest_verschachtelten_namen():
    r = wirex.normalize_tx(BANK, "bank")
    assert r["amount"] == 500.0
    assert r["description"] == "Kunde GmbH"
    assert r["date"] == "2026-07-21"


def test_unbrauchbarer_umsatz_wird_verworfen():
    assert wirex.normalize_tx({"foo": "bar"}, "card") is None


def test_alternative_feldnamen_werden_erkannt():
    r = wirex.normalize_tx({"transaction_id": "x", "date": "2026-01-02",
                            "value": "9,90", "asset": "EUR"}, "card")
    assert r["amount"] == 9.9 and r["currency"] == "EUR"


# ── Einpflegen ──────────────────────────────────────────────────────────
def test_import_legt_offene_zahlungen_an(conn):
    res = core.import_bank_rows(conn, rows())
    assert res["imported"] == 2
    txs = core.list_transactions(conn)
    assert len(txs) == 2
    # Kein Buchungssatz, keine Festschreibung – der Nutzer ordnet selbst zu.
    assert all(t["category"] == "offen" for t in txs)


def test_erneuter_abruf_legt_nichts_doppelt_an(conn):
    core.import_bank_rows(conn, rows())
    res = core.import_bank_rows(conn, rows())
    assert res["imported"] == 0 and res["duplicates"] == 2
    assert len(core.list_transactions(conn)) == 2


def test_zeilen_ohne_ref_oder_datum_werden_uebersprungen(conn):
    res = core.import_bank_rows(conn, [{"ref": "", "date": "2026-01-01", "amount": 5},
                                       {"ref": "a", "date": "", "amount": 5}])
    assert res["imported"] == 0
    assert core.list_transactions(conn) == []


# ── Konto-Zustand ───────────────────────────────────────────────────────
def test_import_ohne_session_meldet_fehler():
    wirex.forget_session()
    assert wirex.import_transactions().get("error") == "no_session"


# ── Umgebungen ──────────────────────────────────────────────────────────
def _reset_env(monkeypatch, tmp_path):
    """settings.json isolieren, damit Tests den echten Zustand nicht anfassen."""
    import core
    monkeypatch.setattr(core, "SETTINGS_PATH", tmp_path / "settings.json")
    wirex.set_env("prod")


def test_umgebung_waehlt_richtige_url(monkeypatch, tmp_path):
    _reset_env(monkeypatch, tmp_path)
    assert "banking.nuri.com" in wirex.base_url()
    wirex.set_env("sandbox")
    assert "wirex.nuri.com" in wirex.base_url() and wirex.is_sandbox()
    wirex.set_env("prod")


def test_konten_bleiben_pro_umgebung_getrennt(monkeypatch, tmp_path):
    _reset_env(monkeypatch, tmp_path)
    wirex.save_account(wallet="0xPROD", username="live")
    wirex.set_env("sandbox")
    assert wirex.account() == {}                      # Sandbox startet leer
    wirex.save_account(wallet="0xSAND", username="test")
    wirex.set_env("prod")
    assert wirex.account()["wallet"] == "0xPROD"      # Live unberührt
    wirex.set_env("sandbox")
    assert wirex.account()["wallet"] == "0xSAND"
    wirex.set_env("prod")


def test_unbekannte_umgebung_wirft(monkeypatch, tmp_path):
    _reset_env(monkeypatch, tmp_path)
    import pytest
    with pytest.raises(ValueError):
        wirex.set_env("mainnet-lol")


# ── Isolierte Wirex-Fixture (Settings in tmp, MCP gemockt, kein Netz) ─────
@pytest.fixture
def wx(sandbox, monkeypatch):
    """wirex vollständig isoliert. Gibt SimpleNamespace(calls, responses):
       calls = Liste der an tools/call übergebenen {name, arguments};
       responses[tool] = dict überschreibt die Antwort eines Tools."""
    schema = {
        "wirex_session_start":       {"wallet": {}, "credential_id": {}, "timeout_ms": {}},
        "wirex_session_result":      {"session_id": {}},
        "wirex_status":              {"session": {}, "email": {}, "country": {}},
        "wirex_wallet":              {"session": {}},
        "wirex_card_details_start":  {"wallet": {}, "card_id": {}, "credential_id": {}},
        "wirex_card_action_start":   {"wallet": {}, "card_id": {}, "credential_id": {}, "action": {}},
        "wirex_onboard_start":       {"username": {}, "email": {}, "credential_id": {},
                                      "use_existing_passkey": {}},
        "wirex_card_transactions":   {"session": {}, "page_size": {}},
        "wirex_bank_transactions":   {"session": {}, "page_size": {}},
        "wirex_physical_card_order": {"wallet": {}},
        "wirex_card_pin_start":      {"wallet": {}, "card_id": {}},
    }
    tools = [{"name": n, "inputSchema": {"type": "object", "properties": p}}
             for n, p in schema.items()]
    monkeypatch.setattr(wirex, "_tools_cache", (time.time(), tools))

    default = {
        "wirex_session_start":  {"ok": True, "session_id": "sid", "approval_url": "https://b?x"},
        "wirex_session_result": {"ok": True, "session": f"{int(time.time())+3600}.sig",
                                 "wallet": "0xEOA"},
        "wirex_wallet":         {"ok": True, "wallet_address": "0xSMART", "balances": []},
        "wirex_status":         {"ok": True, "status": "approved"},
        "wirex_onboard_start":  {"ok": True, "session_id": "os", "approval_url": "ou"},
        "wirex_card_details_start": {"ok": True, "session_id": "s", "approval_url": "u"},
        "wirex_card_action_start":  {"ok": True, "session_id": "s", "approval_url": "u"},
        "wirex_card_transactions":  {"ok": True, "count": 0, "transactions": []},
        "wirex_bank_transactions":  {"ok": True, "count": 0, "transactions": []},
    }
    responses, calls = {}, []

    def fake_rpc(method, params=None, timeout=60):
        if method == "tools/list":
            return {"tools": tools}
        name = params["name"]
        calls.append({"name": name, "arguments": params.get("arguments", {})})
        payload = responses.get(name, default.get(name, {"ok": True}))
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    monkeypatch.setattr(wirex, "_rpc", fake_rpc)
    wirex.set_env("sandbox")
    return SimpleNamespace(calls=calls, responses=responses)


def _valid_token():
    return f"{int(time.time()) + 3600}.sig"


def _args_of(wx, tool):
    hits = [c["arguments"] for c in wx.calls if c["name"] == tool]
    assert hits, f"{tool} wurde nicht aufgerufen"
    return hits[-1]


# ── Normalizer: echte Wirex-Aktivitätsstruktur (der „0 erkannt"-Bug) ─────
REAL_CARD = {
    "id": "c1", "created_at": "2026-07-26T22:58:00.000Z", "direction": "Outbound",
    "source_amount": {"amount": -5.30, "token_symbol": "WEUR"},
    "destination": {"type": "Merchant", "merchant": {"name": "Fresco Max"}},
    "fee_amount": {"amount": 0, "currency": "EUR"},
}
REAL_BANK = {
    "id": "b1", "created_at": "2026-07-15T08:00:00.000Z", "direction": "Outbound",
    "source_amount": {"amount": -10, "token_symbol": "WEUR"},
    "destination": {"type": "SepaBankAccount",
                    "bank_account": {"owner_name": "Max Mustermann", "iban": "EE00..."}},
}


def test_normalize_echte_card_shape():
    r = wirex.normalize_tx(REAL_CARD, "card")
    assert r["amount"] == -5.30
    assert r["description"] == "Fresco Max"
    assert r["currency"] == "EUR"
    assert r["ref"] == "wirex:card:c1"
    assert r["date"] == "2026-07-26"


def test_normalize_echte_bank_shape_liest_owner_name():
    r = wirex.normalize_tx(REAL_BANK, "bank")
    assert r["amount"] == -10
    assert r["description"] == "Max Mustermann"


# ── Onboarding-Zustandsmaschine ─────────────────────────────────────────
def test_classify_onboard_pending(wx):
    assert wirex._classify_onboard({"status": "pending"})["state"] == "pending"


def test_classify_onboard_kyc_offen(wx):
    assert wirex._classify_onboard({"kyc_url": "https://k"})["state"] == "done"


def test_classify_onboard_register(wx):
    d = {"register_approval_url": "u", "register_session_id": "s"}
    assert wirex._classify_onboard(d)["state"] == "register"


def test_classify_onboard_bestandsnutzer_approved_ist_done(wx):
    # approved OHNE kyc_url (Bestandskonto) darf NICHT ewig pending sein
    assert wirex._classify_onboard({"status": "approved"})["state"] == "done"


# ── Session: Ablauf + Kurzschluss ───────────────────────────────────────
def test_session_wird_bei_ablauf_verworfen(wx):
    wirex.save_account(wallet="0xA")
    wirex._store_session(_valid_token())
    assert wirex.session() is not None
    wirex._store_session(f"{int(time.time()) - 10}.sig")   # abgelaufen
    assert wirex.session() is None


def test_session_start_kurzschluss_bei_aktiver_session(wx):
    wirex.save_account(wallet="0xA")
    wirex._store_session(_valid_token())
    out = json.loads(wirex.call("wirex_session_start", {}))
    assert out.get("already_active") is True
    assert "approval_url" not in out
    assert not any(c["name"] == "wirex_session_start" for c in wx.calls)


# ── Passkey-Bindung: EOA vs Smart-Account (die teuerste Verwechslung) ────
def test_session_start_bekommt_credential_aber_nie_wallet(wx):
    wirex.save_account(wallet="0xEOA", credential_id="CRED")
    wirex.call("wirex_session_start", {})
    args = _args_of(wx, "wirex_session_start")
    assert args.get("credential_id") == "CRED"
    assert "wallet" not in args          # wallet=EOA würde signer!=expected brechen


def test_card_tool_bekommt_eoa_nicht_smart_account(wx):
    # nuri-expo revealCardDetails übergibt getEthereumAddress() = die EOA (Signer),
    # NICHT den Smart-Account. Die EOA kommt aus session_result und liegt als `eoa`.
    wirex.save_account(eoa="0xEOA", smart_account="0xSMART", credential_id="CRED")
    wirex._store_session(_valid_token())
    wirex.call("wirex_card_details_start", {"card_id": "c"})
    args = _args_of(wx, "wirex_card_details_start")
    assert args.get("wallet") == "0xEOA"       # EOA (Signer), NICHT Smart-Account
    assert args.get("credential_id") == "CRED"


def test_onboard_start_niemals_mit_credential_oder_wallet(wx):
    # frischer Passkey: Onboarding darf keine alte credential/wallet mitschicken
    wirex.save_account(credential_id="CRED", wallet="0xE", smart_account="0xS")
    wirex.call("wirex_onboard_start", {"username": "u", "email": "e"})
    args = _args_of(wx, "wirex_onboard_start")
    assert "credential_id" not in args
    assert "wallet" not in args


# ── KYC-Status: aktive IBAN/Karte = verifiziert ─────────────────────────
def test_kyc_done_bei_aktiver_iban_trotz_leerem_status(wx):
    wirex.save_account(wallet="0xA", iban="MT98CFTE28004000000000006502436")
    assert wirex.kyc_done() is True


def test_kyc_done_false_ohne_iban_karte_status(wx):
    wirex.save_account(wallet="0xA")
    assert wirex.kyc_done() is False


# ── Umsatz-Cache ────────────────────────────────────────────────────────
def test_tx_cache_roundtrip(wx):
    rows = [{"ref": "wirex:card:1", "date": "2026-07-01", "amount": -1.0,
             "description": "X", "currency": "EUR"}]
    wirex.cache_transactions(rows)
    assert wirex.cached_transactions()[0]["ref"] == "wirex:card:1"


# ── Activity: page_size-Deckel (der 400-Bug) ────────────────────────────
def test_activity_page_size_gedeckelt_auf_50(wx):
    wirex.save_account(wallet="0xA")
    wirex._store_session(_valid_token())
    wirex.fetch_transactions(200)
    sizes = [c["arguments"].get("page_size") for c in wx.calls
             if "transactions" in c["name"]]
    assert sizes and all(s <= 50 for s in sizes)


# ── LLM-Tools: physische Karte + PIN sind ausgefiltert ──────────────────
def test_openai_tools_ohne_physical_und_pin(wx):
    names = [t["function"]["name"] for t in wirex.openai_tools()]
    assert "wirex_physical_card_order" not in names
    assert "wirex_card_pin_start" not in names
    assert "wirex_session_start" in names
