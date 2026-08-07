#!/usr/bin/env python3
"""
MCP-Client für den Nuri-Wirex-Server (Karte, IBAN, Kontoumsätze, SEPA).

Der Server ist ein *stateless* Streamable-HTTP-MCP: ein JSON-RPC-POST pro
Aufruf, kein initialize, kein SDK. Deshalb reicht urllib.

Die Tools werden per tools/list geholt und ins OpenAI-Function-Format
übersetzt — der Chat benutzt sie dann wie eigene Tools. Kein Schema wird hier
von Hand gepflegt: kommt auf dem Server ein Tool dazu, taucht es automatisch
auf.

Auth ist der Passkey des Nutzers. Lese-Tools brauchen ein Session-Token aus
wirex_session_start -> (Nutzer tippt Passkey) -> wirex_session_result. Das
Token gilt ~1h und wird hier nur im Prozessspeicher gehalten.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import core

PREFIX = "wirex_"
TOOLS_TTL = 600  # Sekunden, dann tools/list neu holen

# Tools, bei denen die gespeicherte credential_id (der vorhandene Passkey)
# automatisch mitgeschickt wird, damit der Browser NICHT den falschen Passkey
# anbietet (RP-ID nuri.com ist über Sandbox/Prod geteilt). Onboarding gehört
# bewusst NICHT dazu – dort wird ein NEUER Passkey angelegt.
_CRED_TOOLS = {"wirex_session_start", "wirex_card_details_start",
               "wirex_card_action_start"}
# Karten-Tools brauchen die SMART-ACCOUNT-wallet. session_start NICHT (dort = EOA).
_WALLET_TOOLS = {"wirex_card_details_start", "wirex_card_action_start"}

# Session-Handling wie in nuri-expo (services/wirex/WirexService.ts): das Token
# ist "<exp>.<sig>", exp = Unix-Sekunden. Bis kurz vor Ablauf wiederverwenden,
# statt bei jedem Read neu per Passkey zu minten.
_SESSION_SKEW_SEC = 60
_SESSION_ERROR_CODES = ("session_missing_or_expired", "invalid_session_signature", "session_expired")

# Zwei Umgebungen mit IDENTISCHEM Code, nur andere Kette + Wirex-Credentials.
# Sandbox (Base Sepolia) hat Testgeld via wirex_mint_sandbox – zum Ausprobieren
# ohne echtes Geld und ohne echte KYC.
ENVIRONMENTS = {
    "prod": "https://banking.nuri.com/mcp",      # Base Mainnet, echtes Konto
    "sandbox": "https://wirex.nuri.com/mcp",     # Base Sepolia, Testgeld
}
DEFAULT_ENV = "prod"

# Tool-Katalog im Speicher; das Session-Token liegt persistiert im Konto-Block
# (pro Umgebung) – so übersteht die Session einen Neustart und muss nicht bei
# jedem Read neu per Passkey gemintet werden.
_tools_cache: tuple[float, list] | None = None


def env() -> str:
    e = str(core.load_settings().get("wirex_env") or DEFAULT_ENV).lower()
    return e if e in ENVIRONMENTS else DEFAULT_ENV


def is_sandbox() -> bool:
    return env() == "sandbox"


def set_env(new_env: str) -> str:
    """Umgebung wechseln. Session + Tool-Cache leeren – sie gehören zur alten Umgebung."""
    global _tools_cache
    new_env = str(new_env).lower()
    if new_env not in ENVIRONMENTS:
        raise ValueError(f"unbekannte Umgebung: {new_env}")
    s = core.load_settings()
    s["wirex_env"] = new_env
    core.save_settings(s)
    _tools_cache = None   # Tool-Katalog gehörte zur alten Umgebung
    return new_env


def base_url() -> str:
    # Explizite Override-URL gewinnt (für lokale Tests), sonst die Umgebung.
    override = core.load_settings().get("wirex_base_url")
    return str(override or ENVIRONMENTS[env()]).rstrip("/")


def _token_exp(token: str) -> int:
    """exp (Unix-Sekunden) aus dem '<exp>.<sig>'-Token. 0 = unbekannt."""
    try:
        return int(str(token).split(".", 1)[0])
    except (ValueError, AttributeError):
        return 0


def session() -> str | None:
    """Gültiges Session-Token der aktuellen Umgebung oder None. Läuft es (bald) ab,
       wird es verworfen, damit deterministisch neu gemintet wird – kein Raten."""
    tok = account().get("session_token")
    if not tok:
        return None
    exp = _token_exp(tok)
    if exp and exp - _SESSION_SKEW_SEC <= int(time.time()):
        _store_session(None)
        return None
    return tok


def _store_session(token: str | None) -> None:
    """Session-Token pro Umgebung persistieren (überlebt Neustart)."""
    s = core.load_settings()
    accs = dict(s.get("wirex_accounts") or {})
    acc = dict(account())
    if token:
        acc["session_token"] = token
    else:
        acc.pop("session_token", None)
    accs[env()] = acc
    s["wirex_accounts"] = accs
    if env() == "prod":
        s.pop("wirex", None)
    core.save_settings(s)


# ── Konto: lokal gespeichert, PRO UMGEBUNG getrennt ──────────────────────
def _accounts() -> dict:
    a = core.load_settings().get("wirex_accounts")
    return dict(a) if isinstance(a, dict) else {}


def account() -> dict:
    """Das lokal hinterlegte Konto der AKTUELLEN Umgebung. Leer = nicht registriert.
       Fällt für prod auf den alten Einzel-Key `wirex` zurück (Altbestand)."""
    acc = _accounts().get(env())
    if acc is None and env() == "prod":
        acc = core.load_settings().get("wirex")   # Migration: früher lag prod hier
    return dict(acc) if isinstance(acc, dict) else {}


def is_onboarded() -> bool:
    """Konto existiert. Sagt NICHTS darüber, ob es nutzbar ist – dafür kyc_done()."""
    return bool(account().get("wallet"))


def smart_account() -> str | None:
    """Smart-Account-Adresse (Deposit) – das gespeicherte `wallet` ist die EOA.
       Die Karten-Tools referenzieren den Smart-Account. Bei Bedarf frisch holen."""
    sa = account().get("smart_account")
    if sa:
        return sa
    if not session():
        return None
    try:
        w = json.loads(call("wirex_wallet", {}))
    except Exception:  # noqa
        return None
    sa = w.get("wallet_address")
    if isinstance(sa, str) and sa:
        save_account(smart_account=sa)
        return sa
    return None


# Wirex meldet den KYC-Stand als Text; nur diese Werte heißen "durch".
_KYC_OK = {"approved", "verified", "completed", "active", "success"}


def kyc_done() -> bool:
    acc = account()
    if str(acc.get("verification_status") or "").strip().lower() in _KYC_OK:
        return True
    # verification_status ist beim kalten Read (ohne email) null. Ein AKTIVES
    # Bankkonto/IBAN oder eine Karte existiert aber nur nach bestandener KYC –
    # das ist das verlässlichere Signal als der (oft leere) Status-Text.
    return bool(acc.get("iban") or acc.get("last4") or acc.get("card_id"))


def kyc_label() -> str:
    """Was dem Nutzer angezeigt wird, wenn die Verifizierung noch offen ist."""
    raw = str(account().get("verification_status") or "").strip()
    return {"": "noch nicht begonnen", "applied": "eingereicht, Wirex prüft",
            "pending": "in Prüfung", "rejected": "abgelehnt",
            "resubmission_requested": "Nachweise erneut nötig"}.get(raw.lower(), raw)


def save_account(**fields) -> dict:
    """Konto-Felder der aktuellen Umgebung ergänzen (wallet, credential_id, iban …)."""
    s = core.load_settings()
    accs = dict(s.get("wirex_accounts") or {})
    acc = dict(account())   # ggf. inkl. migriertem prod-Altbestand
    acc.update({k: v for k, v in fields.items() if v not in (None, "")})
    accs[env()] = acc
    s["wirex_accounts"] = accs
    # Alt-Key nur entfernen, wenn wir gerade prod schreiben – DER Altbestand war
    # prod. Beim Sandbox-Speichern stehenlassen, sonst geht prod verloren.
    if env() == "prod":
        s.pop("wirex", None)
    core.save_settings(s)
    return acc


def clear_account() -> None:
    """Nur das Konto der AKTUELLEN Umgebung löschen."""
    s = core.load_settings()
    accs = dict(s.get("wirex_accounts") or {})
    accs.pop(env(), None)
    s["wirex_accounts"] = accs
    if env() == "prod":
        s.pop("wirex", None)
    core.save_settings(s)


# ── JSON-RPC über einen einzigen POST ────────────────────────────────────
def _rpc(method: str, params: dict | None = None, timeout: int = 60) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        base_url(),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    data = _parse(raw)
    if "error" in data:
        raise RuntimeError(data["error"].get("message") or str(data["error"]))
    return data.get("result") or {}


def _parse(raw: str) -> dict:
    """Antwort ist entweder plain JSON oder ein SSE-Stream mit data:-Zeilen."""
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in reversed(text.splitlines()):
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise RuntimeError(f"Unlesbare MCP-Antwort: {text[:200]}")


# ── Tool-Katalog -> OpenAI-Function-Format ───────────────────────────────
def list_tools(force: bool = False) -> list:
    global _tools_cache
    if not force and _tools_cache and time.time() - _tools_cache[0] < TOOLS_TTL:
        return _tools_cache[1]
    tools = _rpc("tools/list").get("tools") or []
    _tools_cache = (time.time(), tools)
    return tools


def openai_tools() -> list:
    """Die MCP-Tools als Function-Definitionen. Fehler nie nach oben geben:
       ein nicht erreichbarer Kartenserver darf den Chat nicht lahmlegen."""
    try:
        tools = list_tools()
    except Exception:
        return []
    out = []
    for t in tools:
        # Physische Karte + PIN werden bewusst nicht angeboten – das Feature
        # ist nicht Teil des Produkts.
        if "physical" in t["name"] or "_pin_" in t["name"] or t["name"].endswith("_pin"):
            continue
        schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        out.append({"type": "function", "function": {
            "name": t["name"],
            "description": (t.get("description") or "")[:1024],
            "parameters": schema}})
    return out


def handles(name: str) -> bool:
    return name.startswith(PREFIX)


# ── Aufruf ───────────────────────────────────────────────────────────────
def call(name: str, args: dict) -> str:
    """Ein MCP-Tool ausführen. Gibt den Text-Content als String zurück."""
    a = dict(args or {})
    tok = session()   # validiert inkl. Ablauf
    # Läuft schon eine gültige Session, ist ein erneutes session_start überflüssig –
    # sonst entsteht eine Endlosschleife aus Passkey-Karten. Kurzschließen.
    if name == "wirex_session_start" and tok:
        return json.dumps({"ok": True, "already_active": True,
                           "hinweis": "Session ist bereits aktiv – kein Passkey nötig. "
                                      "Lies direkt mit wirex_status/wirex_wallet."},
                          ensure_ascii=False)
    # Lese-Tools brauchen die Session; das Modell soll sie nicht erfinden müssen.
    if tok and not a.get("session") and _wants_session(name):
        a["session"] = tok
    # Solange die KYC offen ist, gibt wirex_status die kyc_url NUR mit email zurück
    # (erzwingt den Re-Sync). Ohne das endet der Nutzer bei "kein Link verfügbar".
    if name == "wirex_status" and not a.get("email") and not kyc_done():
        mail = account().get("email")
        if mail:
            a["email"] = mail
    # credential_id NUR bei bekannten Tools automatisch mitgeben – der Browser
    # soll genau den vorhandenen Passkey nehmen, nicht den falschen. Beim
    # Onboarding NIEMALS (dort wird ein NEUER Passkey angelegt).
    if (name in _CRED_TOOLS and not a.get("credential_id")
            and _wants_field(name, "credential_id")):
        cred = account().get("credential_id")
        if cred:
            a["credential_id"] = cred
    # Die Karten-Tools brauchen die EOA (Signer-Adresse) – nuri-expo übergibt
    # dort getEthereumAddress(). Das ist die von session_result zurückgegebene
    # "recovered wallet", NICHT der Smart-Account/Deposit (wirex_wallet.wallet_address).
    # session_start bekommt gar keine wallet (Konto wird aus der Signatur abgeleitet).
    if (name in _WALLET_TOOLS and not a.get("wallet") and _wants_field(name, "wallet")):
        eoa = account().get("eoa")   # NUR die aus session_result recovered EOA
        if eoa:
            a["wallet"] = eoa
    try:
        res = _rpc("tools/call", {"name": name, "arguments": a}, timeout=180)
    except Exception as e:  # noqa
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    text = _content_text(res)
    if res.get("isError"):
        return json.dumps({"error": text or "MCP-Fehler"}, ensure_ascii=False)
    # Abgelaufene/ungültige Session verwerfen, damit session_start wieder greift.
    if any(code in text for code in _SESSION_ERROR_CODES):
        _store_session(None)
    _remember_session(text)
    return text


def _wants_field(name: str, field: str) -> bool:
    """Kennt das Tool laut seinem Schema dieses Feld?"""
    try:
        for t in list_tools():
            if t["name"] == name:
                return field in ((t.get("inputSchema") or {}).get("properties") or {})
    except Exception:
        pass
    return False


def _wants_session(name: str) -> bool:
    return _wants_field(name, "session")


def _content_text(res: dict) -> str:
    parts = [c.get("text") or "" for c in (res.get("content") or []) if c.get("type") == "text"]
    joined = "\n".join(p for p in parts if p)
    if joined:
        return joined
    if res.get("structuredContent") is not None:
        return json.dumps(res["structuredContent"], ensure_ascii=False, default=str)
    return json.dumps(res, ensure_ascii=False, default=str)


def _remember_session(text: str) -> None:
    """Aus jeder Tool-Antwort mitnehmen, was den Zustand ausmacht: das Session-Token
       (nur Speicher, ~1h) und die Konto-Identität (dauerhaft in den Einstellungen).
       Erst dadurch weiß die App nach einem Neustart, dass registriert wurde."""
    try:
        data = json.loads(text)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    d = data.get("data") if isinstance(data.get("data"), dict) else data

    tok = d.get("session") or data.get("session")
    if isinstance(tok, str) and tok:
        _store_session(tok)
        # session_result liefert {session, wallet} – dieses wallet ist die aus der
        # Signatur "recovered" EOA (der Signer). Genau die brauchen die Karten-Tools.
        eoa = d.get("wallet") or data.get("wallet")
        if isinstance(eoa, str) and eoa:
            save_account(eoa=eoa)

    # Wirex mischt snake_case und camelCase – beide Schreibweisen annehmen.
    # WICHTIG: wallet_address ist der SMART-ACCOUNT, NICHT die EOA `wallet` – nie
    # als wallet speichern (sonst überschreibt ein wirex_wallet-Read die EOA).
    aliases = {"wallet": ("wallet",),
               "smart_account": ("wallet_address",),
               "credential_id": ("credential_id", "credentialId"),
               "username": ("username",), "email": ("email",),
               "verification_status": ("verification_status", "verificationStatus", "kyc_status"),
               "kyc_url": ("kyc_url", "kycUrl", "kyc_link")}
    keep = {}
    for field, names in aliases.items():
        v = _pick(d, *names) or _pick(data, *names)
        if isinstance(v, str) and v:
            keep[field] = v
    banks = d.get("bank_accounts") or data.get("bank_accounts")
    if isinstance(banks, list) and banks and isinstance(banks[0], dict):
        iban = banks[0].get("iban") or banks[0].get("account_number")
        if isinstance(iban, str) and iban:
            keep["iban"] = iban
    cards = d.get("cards") or data.get("cards")
    if isinstance(cards, list) and cards and isinstance(cards[0], dict):
        cid = cards[0].get("id") or cards[0].get("card_id")
        if cid:
            keep["card_id"] = str(cid)
        last4 = cards[0].get("last4") or cards[0].get("last_four")
        if last4:
            keep["last4"] = str(last4)
    if keep:
        save_account(**keep)
        # Ist die Verifizierung durch, ist der alte KYC-Link wertlos – weg damit,
        # damit er dem Nutzer nicht weiter angeboten wird.
        if kyc_done() and account().get("kyc_url"):
            s = core.load_settings()
            accs = s.get("wirex_accounts") or {}
            if env() in accs:
                accs[env()].pop("kyc_url", None)
                core.save_settings(s)


def forget_session() -> None:
    _store_session(None)


def cache_transactions(rows: list) -> None:
    """Zuletzt geholte Umsätze lokal ablegen, damit sie ohne Passkey/Session
       sofort angezeigt werden. Auf 100 begrenzt."""
    if isinstance(rows, list):
        save_account(tx_cache=rows[:100])


def cached_transactions() -> list:
    c = account().get("tx_cache")
    return c if isinstance(c, list) else []


# ── Umsätze -> Zahlungen ────────────────────────────────────────────────
def _pick(d: dict, *names):
    """Erstes vorhandenes Feld. Wirex' Feldnamen sind nicht dokumentiert, deshalb
       mehrere Kandidaten je Wert; unbekannte Shapes fallen sichtbar durch."""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _rows_from(payload) -> list:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("transactions", "items", "results", "activity", "data"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                inner = _rows_from(v)
                if inner:
                    return inner
    return []


def _amount_of(v):
    """Betrag aus {amount, ...}-Objekt ODER flachem Wert ziehen."""
    if isinstance(v, dict):
        v = v.get("amount")
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _counterparty(raw: dict, rail: str) -> str:
    """Gegenpartei-Name aus der echten Wirex-Aktivitätsstruktur."""
    direction = str(raw.get("direction") or "").lower()
    # Bei Ausgang zählt destination, bei Eingang source.
    primary, other = ("destination", "source") if "out" in direction else ("source", "destination")
    for side in (primary, other):
        node = raw.get(side)
        if not isinstance(node, dict):
            continue
        for sub in ("merchant", "bank_account", "card", "wallet", "user"):
            obj = node.get(sub)
            if isinstance(obj, dict):
                nm = obj.get("name") or obj.get("owner_name") or obj.get("iban") or obj.get("address")
                if nm:
                    return str(nm)
    # Flache Fallbacks (Sandbox / andere Shapes)
    cp = _pick(raw, "merchant_name", "merchant", "counterparty", "recipient_name",
               "beneficiary", "description", "narrative", "reference")
    if isinstance(cp, dict):
        cp = _pick(cp, "name", "title")
    return str(cp) if cp else "—"


def normalize_tx(raw: dict, rail: str) -> dict | None:
    """Ein Wirex-Umsatz -> Zeile für core.import_bank_rows. None, wenn unbrauchbar.
       Echte Shape: source_amount = {amount (vorzeichenbehaftet, WEUR≈EUR), ...}."""
    tx_id = _pick(raw, "id", "transfer_id", "transaction_id", "reference", "uuid")
    date_ = _pick(raw, "created_at", "createdAt", "date", "completed_at", "timestamp")
    # Betrag: source_amount (in WEUR/EUR, schon signiert) bevorzugen, sonst flach.
    amount = _amount_of(raw.get("source_amount"))
    if amount is None:
        amount = _amount_of(_pick(raw, "amount", "value", "gross_amount"))
    if tx_id is None or date_ is None or amount is None:
        return None
    # Falls unsigniert: Richtung anwenden.
    direction = str(_pick(raw, "direction", "type", "side") or "").lower()
    if amount > 0 and any(w in direction for w in ("out", "debit", "withdraw", "payment", "send")):
        amount = -amount
    fee = _amount_of(raw.get("fee_amount")) or 0.0
    return {
        "ref": f"wirex:{rail}:{tx_id}",
        "date": str(date_)[:10],
        "amount": amount,
        "currency": "EUR",   # source_amount ist WEUR/EURC = 1:1 EUR
        "fee": abs(fee),
        "description": _counterparty(raw, rail),
        "tx_type": f"Wirex {rail}",
    }


# ── Onboarding als deterministische Zustandsmaschine ────────────────────
# Nachbau von nuri-expo runOnboard: onboard_start -> (Passkey erstellen) ->
# onboard_result. Ist das Konto noch nicht on-chain, folgt eine ZWEITE Signatur
# (register) -> register_result. Erst danach gibt es kyc_url. Diese Kette gehört
# in Code, nicht ins LLM – das LLM konnte den Register-Schritt nie abwickeln.

def _first(d: dict, *names):
    for n in names:
        v = _pick(d, n) if isinstance(d, dict) else None
        if v not in (None, ""):
            return v
    return None


def _payload(text: str) -> dict:
    try:
        data = json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("data") if isinstance(data.get("data"), dict) else data


def onboard_start(username: str, email: str, country: str | None = None) -> dict:
    """Onboarding beginnen. Gibt approval_url (Passkey ANLEGEN) + session_id zurück.
       KEINE credential_id/use_existing_passkey -> immer frischer Passkey.
       country ist optional: nur durchreichen, wenn gesetzt – sonst nutzt Wirex
       seinen eigenen Default. Nichts hartkodieren."""
    # Alte Identitätsreste (wallet/credential_id/session) VERWERFEN, sonst mischt
    # save_account später ein Wallet aus diesem Lauf mit einer credential_id aus
    # einem früheren Abbruch – das Paar passt dann nicht zusammen und der Bridge
    # lehnt mit signature_address_does_not_match_expected_address ab.
    clear_account()
    args = {"username": username, "email": email}
    if country:
        args["country"] = country.upper()
    d = _payload(call("wirex_onboard_start", args))
    url = _first(d, "approval_url")
    sid = _first(d, "session_id")
    if not url or not sid:
        return {"ok": False, "error": d.get("error") or "onboard_start ohne approval_url/session_id"}
    # E-Mail sofort binden – ab jetzt die feste Kontaktadresse dieses Kontos.
    save_account(username=username, email=email)
    return {"ok": True, "approval_url": url, "session_id": sid}


_ONBOARD_DONE = {"approved", "registered", "active", "completed", "verified"}


def _classify_onboard(d: dict) -> dict:
    """onboard_result einordnen: pending | register (2. Signatur) | done."""
    kyc = _first(d, "kyc_url", "kycUrl", "kyc_link")
    if kyc:
        _remember_session(json.dumps(d))   # wallet/kyc_url/status übernehmen
        return {"state": "done", "kyc_url": kyc}
    reg_url = _first(d, "register_approval_url", "registerApprovalUrl")
    reg_sid = _first(d, "register_session_id", "registerSessionId")
    if reg_url and reg_sid:
        return {"state": "register", "approval_url": reg_url, "register_session_id": reg_sid}
    # BESTANDSNUTZER: Konto existiert & ist verifiziert -> fertig, auch ohne
    # kyc_url (die gibt es nur bei OFFENER KYC). Sonst hängt der Poller ewig.
    status = str(_first(d, "status", "verification_status") or "").lower()
    if status in _ONBOARD_DONE:
        _remember_session(json.dumps(d))
        return {"state": "done", "status": status}
    return {"state": "pending", "status": status or "pending"}


def onboard_poll(session_id: str) -> dict:
    """Nach dem 1. Passkey-Tap: onboard_result abfragen und einordnen."""
    return _classify_onboard(_payload(call("wirex_onboard_result", {"session_id": session_id})))


def register_poll(register_session_id: str) -> dict:
    """Nach dem 2. (Register-)Tap: register_result abfragen, bis kyc_url da ist."""
    d = _payload(call("wirex_register_result", {"session_id": register_session_id}))
    kyc = _first(d, "kyc_url", "kycUrl", "kyc_link")
    if kyc:
        _remember_session(json.dumps(d))
        return {"state": "done", "kyc_url": kyc}
    status = str(_first(d, "status", "verification_status") or "").lower()
    if status in _ONBOARD_DONE:
        _remember_session(json.dumps(d))
        return {"state": "done", "status": status}
    return {"state": "pending", "status": status or "pending"}


# Wirex' Activity-Endpoint antwortet ab page_size 100 mit activity_read_failed:400;
# bis 50 ist ok. Hart deckeln, sonst schlägt jeder Abruf fehl.
_ACTIVITY_MAX_PAGE = 50


def _read_activity(tool: str, page_size: int, tries: int = 3) -> dict:
    """Eine Aktivitäts-Rail lesen, page_size gedeckelt. Bei transientem
       activity_read_failed ein paar kurze Retries."""
    page_size = min(int(page_size or 50), _ACTIVITY_MAX_PAGE)
    last = {"error": "unbekannt"}
    for i in range(tries):
        try:
            payload = json.loads(call(tool, {"page_size": page_size}))
        except Exception as e:  # noqa
            last = {"error": str(e)}
        else:
            if not (isinstance(payload, dict) and payload.get("error")):
                return payload
            last = payload
            if "activity_read_failed" not in json.dumps(payload):
                return payload   # anderer Fehler -> nicht wiederholen
        if i < tries - 1:
            time.sleep(0.8 * (i + 1))
    return last


def fetch_transactions(page_size: int = 50) -> dict:
    """Karten- und Bankumsätze holen. Braucht eine Passkey-Session."""
    if not session():
        return {"error": "no_session", "hinweis": "Erst wirex_session_start aufrufen und "
                                                  "den Link mit dem Passkey bestätigen."}
    # Pro Rail EINZELN: ein (oft transienter) Fehler auf einer Rail darf die
    # andere nicht mitreißen. Fehler sammeln, nicht abbrechen.
    rows, skipped, seen_keys, rail_errors = [], 0, set(), []
    for tool, rail in (("wirex_card_transactions", "card"), ("wirex_bank_transactions", "bank")):
        payload = _read_activity(tool, page_size)
        if isinstance(payload, dict) and payload.get("error"):
            rail_errors.append(f"{rail}: {payload['error']}")
            continue
        for raw in _rows_from(payload):
            seen_keys.update(raw.keys())
            row = normalize_tx(raw, rail)
            if row:
                rows.append(row)
            else:
                skipped += 1
    # Nur wenn BEIDE Rails scheitern, ist es ein echter Fehler.
    if rail_errors and len(rail_errors) == 2:
        return {"error": "; ".join(rail_errors)}
    return {"rows": rows, "skipped": skipped, "felder": sorted(seen_keys),
            "rail_errors": rail_errors}


def status_debug() -> dict:
    """Roher Status + capabilities (aus diagnose, sandbox) für Konsole/Polling.
       Zeigt, ob/wann die SEPA-Capability aktiv wird und die IBAN kommt."""
    if not session():
        return {"error": "no_session"}
    out = {}
    s = _payload(call("wirex_status", {}))   # country optional, Wirex hat Default
    banks = [b for b in (s.get("bank_accounts") or []) if isinstance(b, dict)]
    out["bank_accounts"] = banks
    out["cards"] = s.get("cards")
    out["verification_status"] = s.get("verification_status")
    if banks:
        out["iban"] = banks[0].get("iban") or ""
        out["iban_status"] = banks[0].get("status")
        out["bic"] = banks[0].get("bic") or ""
    # capabilities/stage nur via diagnose (sandbox); prod liefert das nicht.
    if is_sandbox():
        try:
            d = _payload(call("wirex_diagnose", {}))
            out["capabilities"] = d.get("capabilities")
            out["stage"] = d.get("stage")
            out["real_failures"] = d.get("real_failures")
            out["user_status"] = d.get("user_status")
        except Exception:  # noqa
            pass
    # gefundene IBAN gleich übernehmen
    if out.get("iban"):
        save_account(iban=out["iban"], iban_status=out.get("iban_status") or "Active")
    return out


def live_summary() -> dict:
    """Guthaben, IBAN, Karte live holen (Session nötig). Diese Werte sind bewusst
       NICHT lokal – sie driften. Aktualisiert nebenbei last4/IBAN im Konto."""
    if not session():
        return {"error": "no_session"}
    out = {}
    try:
        w = json.loads(call("wirex_wallet", {}))
    except Exception as e:  # noqa
        return {"error": f"wallet: {e}"}
    if w.get("error"):
        return {"error": f"wallet: {w['error']}"}
    balances = [b for b in (w.get("balances") or []) if isinstance(b, dict)]
    # EUR-Produkt: Guthaben in EUR ausweisen. WEUR/EURC sind 1:1 EUR – deren
    # Token-Balance summieren. Wirex' reference_balance ist in USD und wird hier
    # bewusst NICHT verwendet (sonst stünde USD in einem EUR-Konto).
    _EUR_TOKENS = {"WEUR", "EURC", "EUR"}
    total = sum(float(b.get("balance") or 0) for b in balances
                if str(b.get("token_symbol") or "").upper() in _EUR_TOKENS)
    out["balance"] = total
    out["balance_currency"] = "EUR"
    out["deposit_address"] = w.get("wallet_address") or ""
    out["tokens"] = [{"symbol": b.get("token_symbol"), "balance": b.get("balance"),
                      "fiat": b.get("reference_balance")} for b in balances]
    # Status: IBAN (async) + Karte. Solange keine echte IBAN da ist, mit email
    # abfragen – das stößt bei Wirex das Provisioning (create_user->bank->card) an.
    have_iban = bool(account().get("iban"))
    args = {} if have_iban else {"email": account().get("email") or ""}
    try:
        s = _payload(call("wirex_status", {k: v for k, v in args.items() if v}))
    except Exception:  # noqa
        s = {}
    banks = [b for b in (s.get("bank_accounts") or []) if isinstance(b, dict)]
    if banks:
        iban = banks[0].get("iban") or banks[0].get("account_number") or ""
        out["iban"] = iban
        out["iban_status"] = banks[0].get("status") or ("Active" if iban else "Pending")
        out["bic"] = banks[0].get("bic") or ""
    else:
        out["iban_status"] = "none"
    cards = [c for c in (s.get("cards") or []) if isinstance(c, dict)]
    card_id = cards[0].get("id") if cards else account().get("card_id")
    if cards:
        out["last4"] = str(cards[0].get("last4") or "")
        out["card_status"] = cards[0].get("status") or ""
    # Karten-Limits (jetzt verfügbar; -1 = kein Limit gesetzt / unbegrenzt).
    if card_id:
        try:
            cst = _payload(call("wirex_card_status", {"card_id": card_id})).get("card") or {}
            lim = cst.get("limit") or {}
            if lim:
                out["limit"] = {"daily": lim.get("daily_limit"),
                                "daily_usage": lim.get("daily_usage"),
                                "currency": lim.get("currency") or "EUR"}
        except Exception:  # noqa
            pass
    # Letzten bekannten Stand dauerhaft merken – die UI fällt NIE auf "—" zurück,
    # sondern immer auf diese Werte.
    keep = {"balance": out["balance"], "balance_currency": out["balance_currency"]}
    if out.get("deposit_address"):
        keep["smart_account"] = out["deposit_address"]
    if out.get("iban"):
        keep["iban"] = out["iban"]
    if out.get("iban_status"):
        keep["iban_status"] = out["iban_status"]
    if out.get("last4"):
        keep["last4"] = out["last4"]
    if card_id:
        keep["card_id"] = str(card_id)
    if out.get("limit"):
        keep["limit_daily"] = out["limit"].get("daily")
        keep["limit_usage"] = out["limit"].get("daily_usage")
        keep["limit_currency"] = out["limit"].get("currency") or "EUR"
    save_account(**{k: v for k, v in keep.items() if v is not None})
    return out


def import_transactions(page_size: int = 50) -> dict:
    """Umsätze abrufen und als OFFENE Zahlungen einpflegen (kein Buchungssatz,
       keine Festschreibung – das macht der Nutzer in den Zahlungen)."""
    res = fetch_transactions(page_size)
    if res.get("error"):
        return res
    out = core.import_bank_rows(core.db(), res["rows"])
    out["abgerufen"] = len(res["rows"])
    if res["skipped"]:
        out["unlesbar"] = res["skipped"]
        out["gesehene_felder"] = res["felder"]
    return out
