#!/usr/bin/env python3
"""
Kernlogik der Buchhaltung – ohne Ein-/Ausgabe.
Wird von der CLI (bookkeeping.py) und der Web-App (app.py) gemeinsam genutzt.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tomllib
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# ── Pfade ────────────────────────────────────────────────────────────────
# Jedes Profil ist eine eigenständige Buchhaltung unter profiles/<slug>/ mit
# eigener config.toml und eigenem data/. Die Pfade unten zeigen immer auf das
# gerade aktive Profil; use_profile() bindet sie um.
ROOT = Path(__file__).resolve().parent


def _benutzer_ablage() -> Path:
    """Wo die Buchhaltung liegt, wenn die App installiert ist.

    Als gebündelte App liegt der Code schreibgeschützt in /Applications bzw.
    Program Files, und ein Update würde danebenliegende Daten mitnehmen. Aus
    dem Quellbaum heraus bleibt alles im Repo – so ändert sich für Entwicklung
    und bestehende Installationen nichts. BB_HOME schlägt beides.
    """
    gesetzt = (os.environ.get("BB_HOME") or "").strip()
    if gesetzt:
        return Path(gesetzt).expanduser()
    if not getattr(sys, "frozen", False):
        return ROOT
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Buchhaltung"
    if sys.platform.startswith("win"):
        basis = os.environ.get("APPDATA")
        return (Path(basis) if basis else Path.home() / "AppData" / "Roaming") / "Buchhaltung"
    basis = os.environ.get("XDG_DATA_HOME")
    return (Path(basis) if basis else Path.home() / ".local" / "share") / "buchhaltung"


HOME = _benutzer_ablage()
PROFILES_DIR = HOME / "profiles"
PROFILES_INDEX = PROFILES_DIR / "profiles.json"

# Mitgelieferte Dateien liegen im Bündel, nicht in der Ablage.
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
FONT_DIR = BUNDLE / "fonts"

CONFIG_PATH = HOME / "config.toml"
DATA_DIR = HOME / "data"
DB_PATH = DATA_DIR / "bookkeeping.db"
STATEMENT_DIR = DATA_DIR / "statements"   # gespeicherte Zahlungs-/Bank-CSVs
DOC_DIR = DATA_DIR / "documents"          # Belege: eingang/ + ausgang/ je JJJJ/Qn (siehe beleg_dir)

AKTIVES_PROFIL: str | None = None         # None = Altlayout in der Projektwurzel


# ── Belegpfade ───────────────────────────────────────────────────────────
# In der DB stehen Pfade RELATIV zum Datenordner. Sonst zeigen sie nach
# jedem Umzug (anderer Ordner, anderes Profil) ins Leere. Ältere Bestände
# enthalten absolute Pfade – die bleiben gültig.
def rel_pfad(p) -> str:
    """Pfad so ablegen, wie er in die DB gehört.

    Immer mit Schrägstrichen: unter Windows lieferte str() sonst Backslashes,
    und ein Profilordner liesse sich nicht mehr zwischen Rechnern austauschen.
    """
    try:
        return Path(p).resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return str(p)


def abs_pfad(gespeichert):
    """Gespeicherten Pfad zu einem echten Pfad auflösen. None, wenn leer."""
    if not gespeichert:
        return None
    p = Path(gespeichert)
    if p.is_absolute():
        return p                      # Altbestand, unangetastet lassen
    # Relative Pfade erzeugen wir selbst – Backslashes stammen dann aus einem
    # Windows-Bestand von vor dieser Umstellung.
    return DATA_DIR / Path(str(gespeichert).replace("\\", "/"))


# ── Profile (Mandanten) ──────────────────────────────────────────────────
def slugify(name: str) -> str:
    """Ordnername aus einem Anzeigenamen. Immer nicht-leer und eindeutig machbar."""
    umlaute = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
               "á": "a", "à": "a", "é": "e", "è": "e", "í": "i", "ó": "o", "ú": "u", "ñ": "n"}
    s = "".join(umlaute.get(z, z) for z in (name or "").lower())
    s = "".join(z if z.isalnum() else "-" for z in s)
    s = "-".join(t for t in s.split("-") if t)
    return s[:48] or "profil"


def _index_lesen() -> list[dict]:
    if not PROFILES_INDEX.exists():
        return []
    try:
        daten = json.loads(PROFILES_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return daten.get("profile", []) if isinstance(daten, dict) else []


def _index_schreiben(profile: list[dict]) -> None:
    PROFILES_DIR.mkdir(exist_ok=True)
    PROFILES_INDEX.write_text(json.dumps({"profile": profile}, indent=2, ensure_ascii=False),
                              encoding="utf-8")


def profil_pfade(slug: str) -> dict:
    """Die sechs Pfade eines Profils, ohne etwas anzulegen."""
    basis = PROFILES_DIR / slug
    daten = basis / "data"
    return {"basis": basis, "config": basis / "config.toml", "data": daten,
            "db": daten / "bookkeeping.db", "statements": daten / "statements",
            "documents": daten / "documents", "settings": daten / "settings.json"}


def list_profiles() -> list[dict]:
    """Alle Profile mit Anzeigename, slug und ob sie schon eingerichtet sind."""
    raus = []
    for p in _index_lesen():
        slug = p.get("slug")
        if not slug:
            continue
        pfade = profil_pfade(slug)
        raus.append({**p, "eingerichtet": pfade["config"].exists(),
                     "aktiv": slug == AKTIVES_PROFIL})
    return raus


def get_profile(slug: str) -> dict | None:
    return next((p for p in list_profiles() if p["slug"] == slug), None)


def create_profile(name: str) -> dict:
    """Profil anlegen (Ordner + Registry). Die config.toml folgt über write_config."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Profilname fehlt")
    profile = _index_lesen()
    belegt = {p.get("slug") for p in profile}
    basis = slugify(name)
    slug, n = basis, 2
    while slug in belegt or (PROFILES_DIR / slug).exists():
        slug, n = f"{basis}-{n}", n + 1
    pfade = profil_pfade(slug)
    pfade["data"].mkdir(parents=True, exist_ok=True)
    eintrag = {"slug": slug, "name": name, "angelegt": date.today().isoformat()}
    _index_schreiben(profile + [eintrag])
    return eintrag


def rename_profile(slug: str, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("Profilname fehlt")
    profile = _index_lesen()
    if not any(p.get("slug") == slug for p in profile):
        raise KeyError(slug)
    for p in profile:
        if p.get("slug") == slug:
            p["name"] = name
    _index_schreiben(profile)


def delete_profile(slug: str) -> None:
    """Aus der Registry nehmen. Die Daten bleiben auf der Platte liegen."""
    profile = _index_lesen()
    if not any(p.get("slug") == slug for p in profile):
        raise KeyError(slug)
    _index_schreiben([p for p in profile if p.get("slug") != slug])


def use_profile(slug: str | None) -> None:
    """Alle Pfade auf ein Profil umbiegen. None = Altlayout in der Projektwurzel."""
    global AKTIVES_PROFIL, CONFIG_PATH, DATA_DIR, DB_PATH, STATEMENT_DIR, DOC_DIR, SETTINGS_PATH
    if slug is None:
        AKTIVES_PROFIL = None
        CONFIG_PATH, DATA_DIR = HOME / "config.toml", HOME / "data"
    else:
        if not any(p.get("slug") == slug for p in _index_lesen()):
            raise KeyError(f"Unbekanntes Profil: {slug}")
        pfade = profil_pfade(slug)
        AKTIVES_PROFIL = slug
        CONFIG_PATH, DATA_DIR = pfade["config"], pfade["data"]
    DB_PATH = DATA_DIR / "bookkeeping.db"
    STATEMENT_DIR = DATA_DIR / "statements"
    DOC_DIR = DATA_DIR / "documents"
    SETTINGS_PATH = DATA_DIR / "settings.json"


def active_profile() -> dict | None:
    return get_profile(AKTIVES_PROFIL) if AKTIVES_PROFIL else None


def profil_aus_umgebung() -> str | None:
    """Profil für CLI und MCP bestimmen: BB_PROFILE, sonst das einzige.

    Wirft, wenn die Wahl nicht eindeutig ist – lieber abbrechen als in der
    falschen Buchhaltung buchen. Gibt den gewählten Slug zurück (oder None,
    wenn es noch gar keine Profile gibt und das Altlayout gilt).
    """
    migrate_wurzel_zu_profil()
    profile = list_profiles()
    gewuenscht = (os.environ.get("BB_PROFILE") or "").strip()
    if gewuenscht:
        if not any(p["slug"] == gewuenscht for p in profile):
            bekannt = ", ".join(p["slug"] for p in profile) or "keine"
            raise SystemExit(f"Unbekanntes Profil {gewuenscht!r}. Vorhanden: {bekannt}")
        use_profile(gewuenscht)
        return gewuenscht
    if not profile:
        return None                     # Altlayout in der Wurzel
    if len(profile) == 1:
        use_profile(profile[0]["slug"])
        return profile[0]["slug"]
    liste = ", ".join(p["slug"] for p in profile)
    raise SystemExit(f"Mehrere Profile vorhanden – bitte BB_PROFILE setzen. Wahl: {liste}")


def braucht_migration() -> bool:
    """Altlayout (config.toml + data/ in der Wurzel) und noch keine Profile."""
    return not PROFILES_INDEX.exists() and (HOME / "config.toml").exists()


def migrate_wurzel_zu_profil() -> dict | None:
    """Bestehende Installation als erstes Profil übernehmen.

    Verschiebt config.toml und data/ nach profiles/<slug>/. Vorher werden die
    Belegpfade in der DB relativ gemacht – sonst zeigen sie nach dem Umzug ins
    Leere. Gibt den Profileintrag zurück, oder None wenn nichts zu tun war.
    """
    if not braucht_migration():
        return None
    alt_config, alt_data = HOME / "config.toml", HOME / "data"

    name = "Meine Buchhaltung"
    try:
        with open(alt_config, "rb") as f:
            biz = tomllib.load(f).get("business", {})
        name = (biz.get("name") or biz.get("owner") or name).strip() or name
    except (OSError, tomllib.TOMLDecodeError):
        pass

    # Pfade relativ machen, SOLANGE die DB noch am alten Ort liegt.
    if (alt_data / "bookkeeping.db").exists():
        conn = sqlite3.connect(alt_data / "bookkeeping.db")
        try:
            global DATA_DIR
            merker = DATA_DIR
            DATA_DIR = alt_data
            try:
                _pfade_geprueft.discard(str(alt_data.resolve()) + os.sep)
                _pfade_relativieren(conn)
            finally:
                DATA_DIR = merker
        finally:
            conn.close()

    eintrag = create_profile(name)
    ziel = profil_pfade(eintrag["slug"])
    if ziel["data"].exists():
        ziel["data"].rmdir()                   # create_profile legt sie leer an
    if alt_data.exists():
        try:
            alt_data.rename(ziel["data"])
        except PermissionError as e:                  # praktisch nur Windows
            # Dort lässt sich ein Ordner nicht verschieben, solange eine Datei
            # darin geöffnet ist. Die Migration gehört deshalb an den Anfang,
            # bevor irgendwo eine Verbindung zur Datenbank aufgemacht wurde.
            raise PermissionError(
                f"Der Datenordner {alt_data} lässt sich nicht verschieben – "
                "vermutlich ist die Buchhaltung noch anderweitig geöffnet."
            ) from e
    else:
        ziel["data"].mkdir(parents=True, exist_ok=True)
    alt_config.rename(ziel["config"])
    return eintrag


CENT = Decimal("0.01")
KINDS = ("domestic", "eu", "third")

# ── Umsatzsteuerliche Einordnung ─────────────────────────────────────────
# Der Rechnungstyp (KINDS) kommt streng aus dem Kunden (Land/USt-ID). Ob eine Ware oder eine
# sonstige Leistung abgerechnet wird, steht dort NICHT drin – ist umsatzsteuerlich aber ein
# eigener Tatbestand mit eigener Kennzahl. Deshalb pro Rechnung zusätzlich die Leistungsart.
SUPPLY_TYPES = {
    "service": "Sonstige Leistung",
    "goods":   "Warenlieferung",
    "exempt":  "Steuerfrei ohne Vorsteuerabzug (§ 4 Nr. 8–29 UStG)",
}

# Ausgangsseite: (Rechnungstyp, Leistungsart) → Kennzahl der Bemessungsgrundlage.
# Steuerpflichtige Inlandsumsätze laufen nicht hierüber, die gehen nach Steuersatz in 81/86.
SUPPLY_KZ = {
    ("eu", "service"):     ("21", "Nicht steuerbare sonstige Leistungen (§ 3a Abs. 2), ZM-pflichtig"),
    ("eu", "goods"):       ("41", "Innergemeinschaftliche Lieferungen (§ 4 Nr. 1b), ZM-pflichtig"),
    ("third", "service"):  ("45", "Übrige nicht steuerbare Umsätze (Leistungsort im Drittland)"),
    ("third", "goods"):    ("43", "Steuerfreie Ausfuhrlieferungen (§ 4 Nr. 1a)"),
    ("domestic", "exempt"): ("48", "Steuerfreie Umsätze ohne Vorsteuerabzug (§ 4 Nr. 8–29)"),
    ("eu", "exempt"):      ("48", "Steuerfreie Umsätze ohne Vorsteuerabzug (§ 4 Nr. 8–29)"),
    ("third", "exempt"):   ("48", "Steuerfreie Umsätze ohne Vorsteuerabzug (§ 4 Nr. 8–29)"),
}

# Eingangsseite: USt-Art einer Eingangsrechnung. Bestimmt, in welchen UStVA-Feldern der Beleg
# landet. 'domestic' = normaler Vorsteuerabzug; die übrigen Arten sind Fälle, in denen der
# Empfänger die Steuer selbst schuldet (und i. d. R. zugleich als Vorsteuer abziehen darf).
EXPENSE_VAT_KINDS = {
    "domestic":   "Vorsteuer aus Rechnung (Kz 66)",
    "rc_eu":      "§ 13b Abs. 1 – sonstige Leistung aus der EU (Kz 46/47, Vorsteuer Kz 67)",
    "rc_other":   "§ 13b Abs. 2 – Drittland / Bauleistung (Kz 84/85, Vorsteuer Kz 67)",
    "rc_unknown": "§ 13b – EU oder Drittland noch offen (muss zugeordnet werden)",
    "ig_erwerb":  "Innergemeinschaftlicher Erwerb von Waren (Kz 89/93, Vorsteuer Kz 61)",
    "import":     "Einfuhr aus dem Drittland – entrichtete Einfuhrumsatzsteuer (Kz 62)",
}
RC_KINDS = ("rc_eu", "rc_other", "rc_unknown")     # § 13b: Lieferant stellt ohne USt., Netto = Brutto
SELF_TAXED = RC_KINDS + ("ig_erwerb",)             # Steuer selbst berechnen, zugleich Vorsteuer


def _col(row, key, default=None):
    """Spalte lesen, egal ob sqlite3.Row oder dict – und ohne zu knallen, wenn eine ältere
    Datenbank die Spalte noch nicht hat."""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _vat_kind(v) -> str:
    v = (v or "domestic").strip().lower()
    return v if v in EXPENSE_VAT_KINDS else "domestic"


def expense_vat_kind(row) -> str:
    """USt-Art eines Ausgabe-Datensatzes. Fällt auf das alte reverse_charge-Flag zurück, damit
    auch eine noch nicht migrierte Datenbank (z. B. read-only geöffnet) richtig gerechnet wird."""
    v = _col(row, "vat_kind")
    if v:
        return _vat_kind(v)
    return "rc_unknown" if _col(row, "reverse_charge") else "domestic"


def expense_tax_rate(row) -> int:
    """Steuersatz eines Belegs. 0 ist bei selbst zu versteuernden Arten kein gültiger Satz –
    dort galt und gilt der Regelsatz, sonst fiele die Steuerschuld still auf null."""
    rate = int(_col(row, "vat_rate") or 0)
    if rate == 0 and expense_vat_kind(row) in SELF_TAXED:
        return 19
    return rate


def _supply_type(v) -> str:
    v = (v or "service").strip().lower()
    return v if v in SUPPLY_TYPES else "service"


DEFAULT_EXPENSE_ACCOUNTS = [
    "4920 - Telefon", "4925 - Internet / Telekommunikation", "4930 - Bürobedarf",
    "4940 - Fachliteratur", "4945 - Fortbildungskosten / Weiterbildung",
    "4210 - Miete / Arbeitszimmer", "4250 - Büromiete",
    "4360 - Versicherungen (betrieblich)", "4380 - Beiträge & Gebühren",
    "4600 - Werbekosten", "4650 - Bewirtungskosten", "4670 - Reisekosten",
    "4950 - Rechts- & Beratungskosten", "4955 - Buchführungskosten",
    "4909 - Bankgebühren / Nebenkosten Geldverkehr",
    "4980 - Software / SaaS / sonstiger Betriebsbedarf",
    "0480 - Geringwertige Wirtschaftsgüter (GWG)",
    "1525 - Kautionen (geleistet)", "4980 - Sonstiges",
]


def is_neutral_account(category) -> bool:
    """True für SKR-Bestandskonten (Kontonummer 1000–2999), z. B. 1525 Kautionen.
    Solche Buchungen sind EÜR-neutral (kein Aufwand/Ertrag) – sie dürfen Gewinn & USt nicht berühren.
    4xxx (Aufwand), 8xxx (Erlös) und 0480 GWG bleiben dagegen EÜR-wirksam."""
    import re
    m = re.match(r"\s*(\d{3,4})", category or "")
    if not m:
        return False
    return 1000 <= int(m.group(1)) <= 2999


def expense_accounts(cfg) -> list:
    """Effektive Kontenliste für Dropdowns: Basis (config.toml oder Default)
    + die im Kategorien-Reiter selbst angelegten Konten (settings.custom_accounts)."""
    base = (cfg.get("bookkeeping", {}) or {}).get("expense_accounts") or DEFAULT_EXPENSE_ACCOUNTS
    out = list(base)
    for a in load_settings().get("custom_accounts", []):
        if a not in out:
            out.append(a)
    return out


# ── App-Einstellungen (lokal): LLM-Zugang, frei wählbarer Anbieter ────────
SETTINGS_PATH = DATA_DIR / "settings.json"


def load_settings() -> dict:
    import json
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    import json
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_settings(**changes) -> dict:
    """Einzelne Einstellungen setzen, ohne die übrigen zu verlieren."""
    s = load_settings()
    s.update(changes)
    save_settings(s)
    return s


# ── Eigene Konten (Kategorien-Reiter) ────────────────────────────────────
def add_custom_account(account: str) -> list:
    acc = (account or "").strip()
    s = load_settings()
    custom = list(s.get("custom_accounts", []))
    if acc and acc not in custom:
        custom.append(acc)
        s["custom_accounts"] = custom
        save_settings(s)
    return custom


def remove_custom_account(account: str) -> list:
    acc = (account or "").strip()
    s = load_settings()
    custom = [a for a in s.get("custom_accounts", []) if a != acc]
    s["custom_accounts"] = custom
    save_settings(s)
    return custom


# ── Wiederkehrende Rechnungen (Vorlagen) ─────────────────────────────────
RECURRING_INTERVALS = ("monatlich", "quartalsweise", "jährlich")


def list_recurring() -> list:
    return load_settings().get("recurring", [])


def add_recurring(client: str, service: str, interval: str = "monatlich",
                  amount="", next_run: str | None = None) -> dict:
    """Vorlage für eine wiederkehrende Rechnung anlegen. Gibt den neuen Eintrag zurück."""
    import uuid
    if interval not in RECURRING_INTERVALS:
        raise ValueError(f"Unbekanntes Intervall: {interval} "
                         f"(erlaubt: {', '.join(RECURRING_INTERVALS)})")
    item = {"id": uuid.uuid4().hex[:8], "client": (client or "").strip(),
            "service": (service or "").strip(), "interval": interval,
            "amount": str(amount or "").strip(),
            "next_run": next_run or date.today().isoformat(), "active": True}
    s = load_settings()
    items = list(s.get("recurring", []))
    items.append(item)
    s["recurring"] = items
    save_settings(s)
    return item


def toggle_recurring(rid: str) -> bool | None:
    """Vorlage aktivieren/pausieren. Gibt den neuen Zustand zurück (None = unbekannte id)."""
    s = load_settings()
    items = list(s.get("recurring", []))
    state = None
    for it in items:
        if it.get("id") == rid:
            state = not it.get("active", True)
            it["active"] = state
    s["recurring"] = items
    save_settings(s)
    return state


def delete_recurring(rid: str) -> bool:
    s = load_settings()
    items = list(s.get("recurring", []))
    rest = [it for it in items if it.get("id") != rid]
    s["recurring"] = rest
    save_settings(s)
    return len(rest) != len(items)


def advance_recurring(rid: str) -> str | None:
    """next_run einer Vorlage um ein Intervall weiterschreiben. Gibt das neue Datum zurück."""
    step = {"monatlich": 1, "quartalsweise": 3, "jährlich": 12}
    s = load_settings()
    items = list(s.get("recurring", []))
    new = None
    for it in items:
        if it.get("id") != rid:
            continue
        months = step.get(it.get("interval"), 1)
        d = date.fromisoformat(it.get("next_run") or date.today().isoformat())
        total = d.month - 1 + months
        y, m = d.year + total // 12, total % 12 + 1
        # Monatsende sauber kappen (31.01. + 1 Monat -> 28./29.02.)
        last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).day
        new = date(y, m, min(d.day, last)).isoformat()
        it["next_run"] = new
    s["recurring"] = items
    save_settings(s)
    return new


# ── Mahnwesen ────────────────────────────────────────────────────────────
DUNNING_STAGES = ["Zahlungserinnerung", "1. Mahnung", "2. Mahnung", "Inkasso"]


def dunning_cases(conn) -> list:
    """Alle unbezahlten Rechnungen mit ihrer aktuellen Mahnstufe."""
    stages = load_settings().get("dunning", {})
    return [{"number": i["number"], "client": i["customer_name"], "gross": i["gross"],
             "issue": i["issue_date"], "stage": stages.get(i["number"], 0),
             "stage_name": DUNNING_STAGES[stages.get(i["number"], 0)]}
            for i in list_invoices(conn) if not i["paid_date"]]


def advance_dunning(number: str) -> int:
    """Mahnstufe einer Rechnung erhöhen (gedeckelt bei 'Inkasso'). Gibt die neue Stufe zurück."""
    s = load_settings()
    stages = dict(s.get("dunning", {}))
    stages[number] = min(stages.get(number, 0) + 1, len(DUNNING_STAGES) - 1)
    s["dunning"] = stages
    save_settings(s)
    return stages[number]


def reset_dunning(number: str) -> None:
    s = load_settings()
    stages = dict(s.get("dunning", {}))
    stages.pop(number, None)
    s["dunning"] = stages
    save_settings(s)


# ── Festgeschriebene Perioden ────────────────────────────────────────────
def closed_periods() -> list:
    return load_settings().get("closed_periods", [])


def close_period(token: str) -> list:
    """Zeitraum festschreiben (z. B. '2026-Q1'). Idempotent."""
    tok = (token or "").strip()
    if not tok:
        raise ValueError("Kein Zeitraum angegeben.")
    s = load_settings()
    cp = list(s.get("closed_periods", []))
    if tok not in cp:
        cp.append(tok)
    s["closed_periods"] = cp
    save_settings(s)
    return cp


def reopen_period(token: str) -> list:
    s = load_settings()
    cp = [p for p in s.get("closed_periods", []) if p != (token or "").strip()]
    s["closed_periods"] = cp
    save_settings(s)
    return cp


def is_period_closed(token: str) -> bool:
    return (token or "").strip() in closed_periods()


# ── Währungen / Wechselkurse (Frankfurter, EZB-Referenzkurse, kein Key) ───
# Von der EZB veröffentlichte Kurse – genau die, die das Finanzamt für die
# Umrechnung von Fremdwährungsumsätzen akzeptiert. Historisch abrufbar,
# danach lokal in fx_rates gecacht → funktioniert offline weiter.
FX_CURRENCIES = ["EUR", "USD", "GBP", "CHF", "USDC",  # USDC wie USD behandelt
                 "AUD", "CAD", "JPY", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF"]
FX_API = "https://api.frankfurter.dev/v1"


def home_currency() -> str:
    return (load_settings().get("home_currency") or "EUR").upper()


def _fx_norm(cur: str) -> str:
    cur = (cur or "").upper()
    return "USD" if cur == "USDC" else cur   # Stablecoin 1:1 an USD gekoppelt


def _fx_cached(conn, on_date, base, quote):
    r = conn.execute("SELECT rate FROM fx_rates WHERE date=? AND base=? AND quote=?",
                     (on_date, base, quote)).fetchone()
    return r["rate"] if r else None


def _fx_store(conn, on_date, base, quote, rate):
    conn.execute("INSERT OR REPLACE INTO fx_rates(date, base, quote, rate) VALUES (?,?,?,?)",
                 (on_date, base, quote, rate))
    conn.commit()


def fx_rate(conn, base: str, quote: str, on_date: str | None = None) -> float:
    """1 base = ? quote am Stichtag (Cache → sonst Frankfurter/EZB). Wirft bei Netzfehler."""
    import json
    import urllib.request
    base, quote = _fx_norm(base), _fx_norm(quote)
    if base == quote:
        return 1.0
    on_date = (on_date or date.today().isoformat())[:10]
    hit = _fx_cached(conn, on_date, base, quote)
    if hit is not None:
        return hit
    url = f"{FX_API}/{on_date}?base={base}&symbols={quote}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bookkeeping)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    rate = float(data["rates"][quote])
    _fx_store(conn, on_date, base, quote, rate)          # unter angefragtem Datum
    if data.get("date") and data["date"] != on_date:     # EZB-Kurs des Vor-Handelstags
        _fx_store(conn, data["date"], base, quote, rate)
    return rate


def to_home(conn, amount, currency: str, on_date: str | None = None):
    """(Betrag_in_Heimwährung, Kurs) – nie werfend. Bei Netzfehler (None, None)."""
    hc = home_currency()
    cur = _fx_norm(currency or hc)
    if cur == _fx_norm(hc):
        return round(float(amount), 2), 1.0
    try:
        rate = fx_rate(conn, cur, hc, on_date)
        return round(float(amount) * rate, 2), rate
    except Exception:
        return None, None


def save_statement(data: bytes, filename: str):
    """Hochgeladene Zahlungs-/Bank-CSV im Projekt ablegen (für die Dokumenten-Übersicht)."""
    STATEMENT_DIR.mkdir(exist_ok=True)
    safe = "".join(c for c in (filename or "statement.csv")
                   if c.isalnum() or c in "._- ").strip() or "statement.csv"
    dest = STATEMENT_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{safe}"
    dest.write_bytes(data)
    return dest


# ── Dokumenten-Inbox ─────────────────────────────────────────────────────
def beleg_dir(direction: str, date_str=None) -> Path:
    """Zielordner für einen Beleg: documents/{eingang|ausgang}/JJJJ/Qn (nach Belegdatum, sonst heute).
    direction: 'eingang' (Ausgaben-Belege) oder 'ausgang' (eigene Rechnungen)."""
    ymd = (date_str or "")[:10]
    try:
        d = date.fromisoformat(ymd)
    except (ValueError, TypeError):
        d = date.today()
    p = DOC_DIR / direction / f"{d.year}" / f"Q{(d.month - 1)//3 + 1}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def add_document(conn, data: bytes, filename: str, *, vendor=None, amount=None,
                 doc_date=None, status="to_review") -> int:
    safe = "".join(c for c in (filename or "beleg") if c.isalnum() or c in "._- ").strip() or "beleg"
    ext = (safe.rsplit(".", 1)[-1].lower() if "." in safe else "")
    ftype = "pdf" if ext == "pdf" else ("jpg" if ext in ("jpg", "jpeg") else
                                        ("png" if ext == "png" else ext or "file"))
    dest = beleg_dir("eingang", doc_date) / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{safe}"
    dest.write_bytes(data)
    cur = conn.execute(
        """INSERT INTO documents (filename, filetype, file_path, status, vendor, amount, doc_date, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (filename, ftype, rel_pfad(dest), status, vendor,
         float(amount) if amount not in (None, "") else None, doc_date, _now()))
    conn.commit()
    return cur.lastrowid


def list_documents(conn) -> list:
    return conn.execute("SELECT * FROM documents ORDER BY created_at DESC, id DESC").fetchall()


def get_document(conn, doc_id: int):
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def delete_document(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()


def delete_transaction(conn, tx_id: int) -> None:
    """Bankzahlung entfernen. Löst vorher verknüpfte Buchungen (Ausgabe löschen,
    Rechnungszuordnung lösen), damit keine Waisen/FK-Verletzungen entstehen."""
    if not get_transaction(conn, tx_id):
        return
    reset_tx(conn, tx_id)
    conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()


def suggest_tx_for_document(conn, amount, doc_date, vendor):
    """Beste offene Ausgangs-Transaktion für ein Dokument vorschlagen.
    Regel gegen Fehlvorschläge: der Betrag MUSS centgenau passen; zusätzlich muss
    Anbieter ODER Datum (≤ 31 Tage) stützen. Ein bloßer Betrags-Zufall reicht nicht."""
    try:
        amt = abs(float(amount)) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amt = None
    if amt is None:
        return None  # ohne Betrag kein sicherer Vorschlag
    dd0 = None
    if doc_date:
        try:
            dd0 = date.fromisoformat(doc_date[:10])
        except ValueError:
            dd0 = None
    v = (vendor or "").strip().lower()
    cands = conn.execute(
        "SELECT * FROM transactions WHERE category = 'offen' AND amount < 0").fetchall()
    best, best_score = None, 0
    for t in cands:
        ah = abs(t["amount_home"] if t["amount_home"] is not None else t["amount"])
        if abs(ah - amt) >= 0.005:          # centgenau, sonst raus
            continue
        vendor_match = bool(v and t["description"] and v[:5] in t["description"].lower())
        date_close = False
        if dd0 and t["date"]:
            try:
                date_close = abs((dd0 - date.fromisoformat(t["date"][:10])).days) <= 31
            except ValueError:
                pass
        if not (vendor_match or date_close):
            continue  # Betrag allein genügt nicht – Anbieter oder Datum muss stützen
        score = 3 + (2 if vendor_match else 0) + (1 if date_close else 0)
        if score > best_score:
            best, best_score = t, score
    return best["id"] if best else None


def match_document_to_tx(conn, doc_id: int, tx_id: int, *, category: str, vat_rate,
                         private_share=0) -> None:
    """Dokument an eine bestehende Transaktion hängen: Beleg anhängen, Transaktion als
    Betriebsausgabe kategorisieren, Dokument auf 'matched'. Kein zweiter Datensatz."""
    doc = get_document(conn, doc_id)
    if not doc:
        return
    reset_tx(conn, tx_id)   # falls schon zugeordnet -> sauber neu
    if doc["file_path"] and abs_pfad(doc["file_path"]).exists():
        attach_receipt_to_tx(conn, tx_id, abs_pfad(doc["file_path"]).read_bytes(),
                             doc["filename"] or "beleg")
    create_expense_from_tx(conn, tx_id, category or "Sonstiges", vat_rate,
                           private_share=private_share)
    eid = get_transaction(conn, tx_id)["expense_id"]
    conn.execute("UPDATE documents SET status = 'matched', expense_id = ? WHERE id = ?",
                 (eid, doc_id))
    conn.commit()


def confirm_document(conn, doc_id: int, *, category: str, vat_rate, private_share=0) -> None:
    """Fallback (kein Transaktions-Treffer): Dokument als eigenständige Ausgabe verbuchen."""
    doc = get_document(conn, doc_id)
    if not doc:
        return
    r = create_expense(
        conn, date_=(doc["doc_date"] or date.today().isoformat()),
        vendor=(doc["vendor"] or doc["filename"] or "Beleg")[:120],
        description=doc["filename"], category=category or "Sonstiges",
        gross=abs(float(doc["amount"] or 0)), vat_rate=int(vat_rate or 0),
        paid_date=(doc["doc_date"] or date.today().isoformat()),
        private_share=int(private_share or 0))
    if doc["file_path"]:
        conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?", (doc["file_path"], r["id"]))
    conn.execute("UPDATE documents SET status = 'matched', expense_id = ? WHERE id = ?", (r["id"], doc_id))
    conn.commit()


def split_document(conn, doc_id: int, lines: list, tx_id: int | None = None) -> None:
    """Einen Beleg auf mehrere Buchungssätze (Unterbuchungen) aufteilen.
    lines: [{category, amount, vat_rate, private_share}] – je Zeile eine Ausgabe.
    Mit tx_id werden alle Positionen der Zahlung zugeordnet, sonst eigenständig gebucht.
    Der Beleg (PDF) wird an jede Position gehängt."""
    doc = get_document(conn, doc_id)
    if not doc:
        return

    def amt(x):
        try:
            return abs(float(str(x).replace(",", ".")))
        except (TypeError, ValueError):
            return 0.0

    valid = [ln for ln in lines if amt(ln.get("amount")) > 0]
    if not valid:
        return

    today = date.today().isoformat()
    vendor = (doc["vendor"] or doc["filename"] or "Beleg")[:120]
    if tx_id:
        reset_tx(conn, tx_id)
        if doc["file_path"] and abs_pfad(doc["file_path"]).exists():
            attach_receipt_to_tx(conn, tx_id, abs_pfad(doc["file_path"]).read_bytes(),
                                 doc["filename"] or "beleg")
        t = get_transaction(conn, tx_id)
        book_date = t["date"]
    else:
        book_date = doc["doc_date"] or today

    first_eid = None
    for ln in valid:
        r = create_expense(
            conn, date_=book_date, vendor=vendor, description=doc["filename"],
            category=ln.get("category") or "Sonstiges", gross=amt(ln.get("amount")),
            vat_rate=int(ln.get("vat_rate") or 0), paid_date=book_date,
            private_share=int(float(ln.get("private_share") or 0)))
        if tx_id:
            conn.execute("UPDATE expenses SET source_tx = ? WHERE id = ?", (tx_id, r["id"]))
        if doc["file_path"]:
            conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?", (doc["file_path"], r["id"]))
        if first_eid is None:
            first_eid = r["id"]
    if tx_id and first_eid is not None:
        conn.execute("UPDATE transactions SET category = 'ausgabe', expense_id = ? WHERE id = ?",
                     (first_eid, tx_id))
    conn.execute("UPDATE documents SET status = 'matched', expense_id = ? WHERE id = ?",
                 (first_eid, doc_id))
    conn.commit()


# ── Konfiguration ────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "Keine config.toml gefunden – die Einrichtung unter /setup öffnen "
            "(oder: cp config.example.toml config.toml)"
        )
    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    # Bank- & Steuer-Stammdaten aus den Einstellungen überschreiben config.toml (editierbar in der UI)
    s = load_settings()
    for key, sk in (("holder", "bank_holder"), ("iban", "bank_iban"), ("bic", "bank_bic")):
        val = (s.get(sk) or "").strip()
        if val:
            cfg.setdefault("bank", {})[key] = val
    for key, sk in (("vat_id", "business_vat_id"), ("tax_number", "business_tax_number")):
        val = (s.get(sk) or "").strip()
        if val:                                       # leer = config.toml bleibt maßgeblich
            cfg.setdefault("business", {})[key] = val
    # Dauerfristverlängerung + Sondervorauszahlung (Kz 39) – in der UI pflegbar.
    if "ust_dauerfrist" in s:
        cfg.setdefault("tax", {})["dauerfrist"] = bool(s.get("ust_dauerfrist"))
    if s.get("ust_sondervorauszahlung") not in (None, ""):
        cfg.setdefault("tax", {})["sondervorauszahlung"] = _num(s["ust_sondervorauszahlung"])
    return cfg


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def _toml_str(v) -> str:
    """Wert als TOML-Literal. Nur die Typen, die die config.toml braucht."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[\n" + "".join(f"  {_toml_str(x)},\n" for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{s}"'


# Vorgaben für die Ersteinrichtung; der Nutzer füllt nur die eigenen Stammdaten.
_SETUP_DEFAULTS = {
    "invoice": {
        "signature_path": "",
        "intro": ("I would like to thank you for the good cooperation and accordingly "
                  "invoice you for the following deliverables:"),
        "closing": "If you have any questions, please feel free to contact me at any time.",
        "signoff": "Best regards,",
        "reverse_charge_note": ("Reverse charge: VAT to be accounted for by the recipient "
                                "(Steuerschuldnerschaft des Leistungsempfängers according to "
                                "Art. 196 EU VAT Directive and § 13b UStG)."),
        "third_country_note": ("Not subject to German VAT – place of supply outside the EU "
                               "(§ 3a (2) UStG). Reverse charge: the customer is liable for "
                               "any VAT due under local law."),
        "footer_note": "",
    },
    "tax": {"dauerfrist": False, "sondervorauszahlung": 0.0},
}


def write_config(business: dict, bank: dict, invoice: dict, tax: dict) -> Path:
    """config.toml aus den Onboarding-Angaben schreiben. Überschreibt nichts."""
    if CONFIG_PATH.exists():
        raise FileExistsError("config.toml existiert bereits")
    inv = {**invoice, **_SETUP_DEFAULTS["invoice"]}
    tx = {**tax, **_SETUP_DEFAULTS["tax"]}
    teile = [
        "# Von der Ersteinrichtung erzeugt. Änderungen jederzeit von Hand möglich –\n"
        "# danach die App über 'Server neu laden' (Einstellungen) neu starten.\n",
        "[business]", *[f"{k} = {_toml_str(v)}" for k, v in business.items()], "",
        "[bank]", *[f"{k} = {_toml_str(v)}" for k, v in bank.items()], "",
        "[invoice]", *[f"{k} = {_toml_str(v)}" for k, v in inv.items()], "",
        "[tax]", *[f"{k} = {_toml_str(v)}" for k, v in tx.items()], "",
        "[bookkeeping]",
        f"expense_accounts = {_toml_str(list(DEFAULT_EXPENSE_ACCOUNTS))}", "",
    ]
    CONFIG_PATH.write_text("\n".join(teile), encoding="utf-8")
    return CONFIG_PATH


# ── Geld & Datum ─────────────────────────────────────────────────────────
def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value) -> str:
    """Deutsche Zahl ohne Währung: 1234.5 -> '1.234,50'."""
    s = f"{money(value):,.2f}"
    return s.replace(",", "#").replace(".", ",").replace("#", ".")


def eur(value) -> str:
    return f"{fmt(value)} €"


def cur(value) -> str:
    """Rechnungs-Ausgabe (englisch): '€1,234.50 (EUR)'."""
    return f"€{money(value):,.2f} (EUR)"


def de_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


# ── Unicode-Schrift ──────────────────────────────────────────────────────
_SANITIZE_MAP = {
    "€": "EUR", "–": "-", "—": "-", "„": '"', "“": '"', "”": '"',
    "‚": "'", "‘": "'", "’": "'", "…": "...", "→": "->", "•": "-", " ": " ",
}


def _sanitize(s: str) -> str:
    for k, v in _SANITIZE_MAP.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _register_font(pdf) -> tuple[str, bool]:
    reg = FONT_DIR / "Body-Regular.ttf"
    bold = FONT_DIR / "Body-Bold.ttf"
    try:
        if reg.exists():
            pdf.add_font("Body", "", str(reg))
            pdf.add_font("Body", "B", str(bold if bold.exists() else reg))
            return "Body", False
    except Exception:
        pass
    return "Helvetica", True


# ── Datenbank ────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    # parents=True: bei einer frischen Installation existiert der übergeordnete
    # Ordner noch nicht – etwa ~/Library/Application Support/Buchhaltung.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATEMENT_DIR.mkdir(parents=True, exist_ok=True)   # Bank-/Zahlungs-CSVs
    DOC_DIR.mkdir(parents=True, exist_ok=True)         # Belege: eingang/ + ausgang/ je JJJJ/Qn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    _pfade_relativieren(conn)
    return conn


_PFAD_SPALTEN = (("invoices", "pdf_path"), ("expenses", "receipt_path"),
                 ("transactions", "receipt_path"), ("documents", "file_path"))
_pfade_geprueft: set[str] = set()   # je Datenordner nur einmal pro Prozess


def _pfade_relativieren(conn: sqlite3.Connection) -> int:
    """Altbestand mit absoluten Pfaden im eigenen Datenordner auf relativ ziehen.

    Nur lesen, solange nichts zu tun ist – sonst nähme jedes Öffnen der DB eine
    Schreibsperre. Pfade außerhalb des Datenordners bleiben unangetastet.
    """
    praefix = str(DATA_DIR.resolve()) + os.sep
    if praefix in _pfade_geprueft:
        return 0
    geaendert = 0
    for tabelle, spalte in _PFAD_SPALTEN:
        try:
            treffer = conn.execute(
                f'SELECT 1 FROM "{tabelle}" WHERE {spalte} LIKE ? || \'%\' LIMIT 1',
                (praefix,)).fetchone()
            if not treffer:
                continue
            cur = conn.execute(
                f'UPDATE "{tabelle}" SET {spalte} = substr({spalte}, ?) '
                f"WHERE {spalte} LIKE ? || '%'", (len(praefix) + 1, praefix))
            geaendert += cur.rowcount
        except sqlite3.OperationalError:
            pass          # Tabelle/Spalte gibt es in diesem Schema (noch) nicht
    if geaendert:
        conn.commit()
    _pfade_geprueft.add(praefix)
    return geaendert


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            address    TEXT NOT NULL,
            vat_id     TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id                INTEGER PRIMARY KEY,
            number            TEXT UNIQUE NOT NULL,
            year              INTEGER NOT NULL,
            month             INTEGER NOT NULL,
            seq               INTEGER NOT NULL,
            customer_id       INTEGER REFERENCES customers(id),
            customer_name     TEXT NOT NULL,
            customer_address  TEXT NOT NULL,
            customer_vat_id   TEXT,
            issue_date        TEXT NOT NULL,
            service_from      TEXT NOT NULL,
            service_to        TEXT,
            due_date          TEXT NOT NULL,
            paid_date         TEXT,
            kind              TEXT NOT NULL DEFAULT 'domestic',
            net               REAL NOT NULL,
            vat               REAL NOT NULL,
            gross             REAL NOT NULL,
            notes             TEXT,
            pdf_path          TEXT,
            created_at        TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoice_items (
            id          INTEGER PRIMARY KEY,
            invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            quantity    REAL NOT NULL,
            unit        TEXT,
            unit_price  REAL NOT NULL,
            vat_rate    INTEGER NOT NULL,
            line_net    REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id           INTEGER PRIMARY KEY,
            date         TEXT NOT NULL,
            paid_date    TEXT,
            vendor       TEXT NOT NULL,
            description  TEXT,
            category     TEXT NOT NULL,
            net          REAL NOT NULL,
            vat_rate     INTEGER NOT NULL,
            vat          REAL NOT NULL,
            gross        REAL NOT NULL,
            receipt_path TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS booking_rules (
            id            INTEGER PRIMARY KEY,
            match_key     TEXT UNIQUE NOT NULL,   -- normalisierte Beschreibung
            action        TEXT NOT NULL,
            category      TEXT,
            vat_rate      INTEGER,
            private_share REAL,
            vat_kind      TEXT,
            updated_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY,
            ref          TEXT UNIQUE,          -- Dedupe bei Re-Import
            date         TEXT NOT NULL,
            amount       REAL NOT NULL,        -- signiert: + Eingang, - Ausgang
            fee          REAL NOT NULL DEFAULT 0,
            currency     TEXT NOT NULL,
            description  TEXT,
            tx_type      TEXT,
            category     TEXT NOT NULL DEFAULT 'offen',
            invoice_id   INTEGER REFERENCES invoices(id),
            expense_id   INTEGER REFERENCES expenses(id),
            receipt_path TEXT,
            note         TEXT,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fx_rates (
            date   TEXT NOT NULL,   -- Kursdatum (YYYY-MM-DD)
            base   TEXT NOT NULL,   -- Ausgangswährung
            quote  TEXT NOT NULL,   -- Zielwährung
            rate   REAL NOT NULL,   -- 1 base = rate quote
            PRIMARY KEY (date, base, quote)
        );
        CREATE TABLE IF NOT EXISTS documents (
            id         INTEGER PRIMARY KEY,
            filename   TEXT,
            filetype   TEXT,               -- pdf | jpg | png
            file_path  TEXT,
            status     TEXT NOT NULL DEFAULT 'to_review',  -- processing | to_review | matched
            vendor     TEXT,
            amount     REAL,
            doc_date   TEXT,
            expense_id INTEGER,
            suggest_tx INTEGER,
            created_at TEXT NOT NULL
        );
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN kind TEXT NOT NULL DEFAULT 'domestic'")
        if "reverse_charge" in cols:
            conn.execute("UPDATE invoices SET kind = 'eu' WHERE reverse_charge = 1")
    if "currency" not in cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'")
    if "sent_at" not in cols:
        conn.execute("ALTER TABLE invoices ADD COLUMN sent_at TEXT")
    ccols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}
    if "email" not in ccols:
        conn.execute("ALTER TABLE customers ADD COLUMN email TEXT")
    if "number" not in ccols:
        conn.execute("ALTER TABLE customers ADD COLUMN number TEXT")
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(expenses)")}
    if "private_share" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN private_share REAL NOT NULL DEFAULT 0")
    if "gross_full" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN gross_full REAL")
    if "source_tx" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN source_tx INTEGER")
    if "reverse_charge" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN reverse_charge INTEGER NOT NULL DEFAULT 0")
        ecols.add("reverse_charge")
    if "vat_kind" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN vat_kind TEXT NOT NULL DEFAULT 'domestic'")
        # Bestand: ob ein §13b-Beleg aus der EU (Kz 46/47) oder dem Drittland (Kz 84/85) kommt,
        # steht in den Altdaten nicht drin und wird hier NICHT geraten -> 'rc_unknown'.
        # Der USt-VA-Report weist die offenen Belege aus, bis sie zugeordnet sind.
        conn.execute("UPDATE expenses SET vat_kind = 'rc_unknown' WHERE reverse_charge = 1")
        # §13b wurde bisher fest mit 19 % gerechnet. Satz explizit hinterlegen, damit die
        # Steuer aus den Daten kommt und nicht aus einer Konstante im Code.
        conn.execute("UPDATE expenses SET vat_rate = 19 "
                     "WHERE reverse_charge = 1 AND (vat_rate IS NULL OR vat_rate = 0)")
    icols2 = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
    if "supply_type" not in icols2:
        # Bestand: bisher konnte das Tool nur Dienstleistungen abrechnen.
        conn.execute("ALTER TABLE invoices ADD COLUMN supply_type TEXT NOT NULL DEFAULT 'service'")
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(booking_rules)")}
    if rcols and "vat_kind" not in rcols:
        conn.execute("ALTER TABLE booking_rules ADD COLUMN vat_kind TEXT")
    dcols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if dcols and "suggest_tx" not in dcols:
        conn.execute("ALTER TABLE documents ADD COLUMN suggest_tx INTEGER")
    tcols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "amount_home" not in tcols:
        conn.execute("ALTER TABLE transactions ADD COLUMN amount_home REAL")
        conn.execute("ALTER TABLE transactions ADD COLUMN fx_rate REAL")
        # Bestand: EUR-Buchungen 1:1 als Heimwährung übernehmen
        conn.execute("UPDATE transactions SET amount_home = amount, fx_rate = 1.0 "
                     "WHERE currency = 'EUR'")
    conn.commit()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Kunden ───────────────────────────────────────────────────────────────
def list_customers(conn) -> list:
    return conn.execute("SELECT * FROM customers ORDER BY id").fetchall()


def get_customer(conn, cid: int):
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()


def create_customer(conn, name: str, address: str, vat_id: str | None,
                    number: str | None = None):
    number = _check_customer_number(conn, number)
    cur_ = conn.execute(
        "INSERT INTO customers (name, address, vat_id, number, created_at) VALUES (?,?,?,?,?)",
        (name, address, vat_id or None, number, _now()),
    )
    conn.commit()
    return get_customer(conn, cur_.lastrowid)


# Kundennummern (Debitoren): frei wählbar, aber eindeutig. Vorschlag folgt dem
# SKR03-Debitorenbereich 10000–69999.
CUSTOMER_NUMBER_START = 10001


def get_customer_by_number(conn, number: str):
    number = (number or "").strip()
    if not number:
        return None
    return conn.execute("SELECT * FROM customers WHERE number = ?", (number,)).fetchone()


def _check_customer_number(conn, number: str | None, *, exclude_id: int | None = None):
    """Leere Nummer -> None (keine Nummer). Sonst auf Eindeutigkeit prüfen."""
    number = (number or "").strip()
    if not number:
        return None
    row = get_customer_by_number(conn, number)
    if row is not None and row["id"] != exclude_id:
        raise ValueError(f"Kundennummer {number} ist schon an '{row['name']}' vergeben.")
    return number


def next_customer_number(conn) -> str:
    """Nächste freie rein numerische Kundennummer (ab 10001)."""
    used = set()
    for r in conn.execute("SELECT number FROM customers WHERE number IS NOT NULL"):
        n = (r["number"] or "").strip()
        if n.isdigit():
            used.add(int(n))
    nxt = max(used) + 1 if used else CUSTOMER_NUMBER_START
    while nxt in used:
        nxt += 1
    return str(nxt)


def set_customer_number(conn, cid: int, number: str | None) -> dict:
    """Kundennummer setzen oder (bei leerer Eingabe) entfernen."""
    c = get_customer(conn, cid)
    if c is None:
        raise ValueError(f"Kunde #{cid} nicht gefunden.")
    number = _check_customer_number(conn, number, exclude_id=cid)
    conn.execute("UPDATE customers SET number = ? WHERE id = ?", (number, cid))
    conn.commit()
    return {"customer_id": cid, "name": c["name"], "number": number}


def count_customer_invoices(conn, cid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM invoices WHERE customer_id = ?", (cid,)).fetchone()[0]


def list_customer_invoices(conn, cid: int) -> list:
    """Rechnungen eines Kunden – damit die Oberfläche belegen kann, WELCHE das sind."""
    return conn.execute(
        "SELECT number, issue_date, gross FROM invoices WHERE customer_id = ? "
        "ORDER BY issue_date, number", (cid,)).fetchall()


def delete_customer(conn, cid: int) -> dict:
    """Kunde aus dem Register löschen – unabhängig von Rechnungen und Buchungen.
    Die Rechnungen bleiben unverändert bestehen: sie haben Name, Adresse und USt-IdNr.
    beim Anlegen eingefroren, nur die Verknüpfung zum Register entfällt."""
    c = get_customer(conn, cid)
    if c is None:
        raise ValueError(f"Kunde #{cid} nicht gefunden.")
    n = count_customer_invoices(conn, cid)
    conn.execute("UPDATE invoices SET customer_id = NULL WHERE customer_id = ?", (cid,))
    conn.execute("DELETE FROM customers WHERE id = ?", (cid,))
    conn.commit()
    return {"deleted": c["name"], "rechnungen_entkoppelt": n}


def _customer_richness(c) -> tuple:
    """Sortierschlüssel: je mehr Stammdaten, desto eher wird dieser Datensatz behalten."""
    keys = c.keys()
    return (bool((c["address"] or "").strip()),
            bool((c["vat_id"] or "").strip()),
            bool(("number" in keys and c["number"] or "").strip()),
            bool(("email" in keys and c["email"] or "").strip()),
            -int(c["id"]))


def merge_customers(conn, keep_id: int, drop_id: int) -> dict:
    """Zwei Kundendatensätze zusammenführen: Rechnungen wandern zu keep_id, drop wird gelöscht.
    Fehlende Stammdaten (Adresse, USt-IdNr., E-Mail) werden vom gelöschten Datensatz übernommen."""
    keep, drop = get_customer(conn, keep_id), get_customer(conn, drop_id)
    if keep is None or drop is None:
        raise ValueError("Kunde nicht gefunden.")
    if keep_id == drop_id:
        raise ValueError("Kunde kann nicht mit sich selbst zusammengeführt werden.")
    cols = ["address", "vat_id"] + [c for c in ("email", "number") if c in keep.keys()]
    for col in cols:
        if not (keep[col] or "").strip() and (drop[col] or "").strip():
            conn.execute(f"UPDATE customers SET {col} = ? WHERE id = ?", (drop[col], keep_id))
    moved = count_customer_invoices(conn, drop_id)
    conn.execute("UPDATE invoices SET customer_id = ? WHERE customer_id = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM customers WHERE id = ?", (drop_id,))
    conn.commit()
    return {"behalten": keep["name"], "keep_id": keep_id, "rechnungen_umgehaengt": moved}


def duplicate_customers(conn) -> list:
    """Namensgleiche Kunden (case-insensitiv) gruppieren: [{'name', 'keep', 'drop': [ids]}]."""
    groups = {}
    for c in list_customers(conn):
        groups.setdefault(" ".join((c["name"] or "").split()).lower(), []).append(c)
    out = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=_customer_richness, reverse=True)
        out.append({"name": rows[0]["name"], "keep": rows[0]["id"],
                    "drop": [r["id"] for r in rows[1:]]})
    return out


def merge_duplicate_customers(conn) -> dict:
    """Alle namensgleichen Kunden zusammenführen – der Datensatz mit den meisten
    Stammdaten bleibt, die Rechnungen der anderen werden umgehängt."""
    merged = []
    for g in duplicate_customers(conn):
        for did in g["drop"]:
            r = merge_customers(conn, g["keep"], did)
            merged.append({"name": g["name"], **r})
    return {"zusammengefuehrt": len(merged), "details": merged}


def adhoc_customer(name: str, address: str = "", vat_id: str | None = None) -> dict:
    """Empfänger NUR für diese eine Rechnung – wird nicht ins Kundenregister aufgenommen.
    Die Rechnung friert Name/Adresse/USt-IdNr. ohnehin ein, deshalb reicht das aus."""
    return {"id": None, "name": (name or "").strip(),
            "address": _import_address(address), "vat_id": (vat_id or "").strip() or None}


def _customer_fields(customer) -> dict:
    """Kunde aus dem Register (sqlite3.Row) oder Ad-hoc-Empfänger (dict) einheitlich lesen."""
    get = customer.get if isinstance(customer, dict) else (lambda k, d=None: customer[k])
    keys = customer.keys() if not isinstance(customer, dict) else customer
    return {"id": get("id", None) if "id" in keys else None,
            "name": (get("name") or "").strip(),
            "address": get("address") or "",
            "vat_id": get("vat_id") or None}


# ── Rechnungen ───────────────────────────────────────────────────────────
def next_invoice_number(conn, year: int, month: int) -> tuple[str, int]:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM invoices WHERE year = ? AND month = ?",
        (year, month),
    ).fetchone()
    seq = row["m"] + 1
    return f"{year}-{month:02d}-{seq:04d}", seq


def parse_invoice_number(number: str) -> tuple[int, int, int]:
    """'2026-06-0001' -> (2026, 6, 1). Präfixe (Buchstaben) werden ignoriert."""
    import re
    m = re.search(r"(\d{4})-(\d{1,2})-(\d+)", number)
    if not m:
        raise ValueError(f"Rechnungsnummer '{number}' passt nicht ins Format JJJJ-MM-NNNN.")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


# EU-USt-Länderkürzel (ohne DE) – für die deterministische Reverse-Charge-Ableitung.
_EU_VAT_CC = {"AT", "BE", "BG", "CY", "CZ", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU",
              "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "XI"}


def customer_kind(cust) -> str | None:
    """Rechnungstyp STRENG aus dem Kunden ableiten – kein Raten, kein LLM-Spielraum:
      DE-USt-ID → domestic · EU-USt-ID → eu (Reverse Charge) · andere (Nicht-EU) USt-ID → third.
    Ohne USt-ID: Deutschland (Adresse) → domestic, sonst None (dann greift der übergebene Typ)."""
    vat = (cust.get("vat_id") if isinstance(cust, dict) else (cust["vat_id"] if cust else None)) or ""
    vat = vat.strip().upper()
    cc = vat[:2]
    if cc == "DE":
        return "domestic"
    if cc in _EU_VAT_CC:
        return "eu"
    if len(vat) >= 5 and cc.isalpha():        # ausländische Nicht-EU-USt-ID → Drittland
        return "third"
    addr = ((cust.get("address") if isinstance(cust, dict) else cust["address"]) or "").upper()
    if "GERMANY" in addr or "DEUTSCHLAND" in addr:
        return "domestic"
    return None


def create_invoice(conn, cfg, *, customer, kind: str, issue_date: str,
                   service_from: str, service_to: str | None,
                   items: list[dict], notes: str | None = None,
                   number: str | None = None, paid_date: str | None = None,
                   render: bool = True, supply_type: str | None = None):
    """items: [{description, quantity, unit, unit_price, vat_rate}] – Beträge als str/Decimal.
    number: bestehende Nummer übernehmen (Import); sonst automatisch je Monat.
    paid_date: falls schon bezahlt. render: PDF erzeugen (beim Import optional).
    supply_type: Ware oder sonstige Leistung (siehe SUPPLY_TYPES). Der Rechnungstyp sagt nur,
    WOHIN geliefert wird – für die richtige Kennzahl zählt auch, WAS geliefert wird."""
    if kind not in KINDS:
        raise ValueError(f"Ungültiger Rechnungstyp: {kind}")
    cust = _customer_fields(customer)
    kind = customer_kind(cust) or kind        # STRENG aus dem Kunden (USt-ID/Land) – kein Raten
    supply = _supply_type(supply_type)
    # Steuerfrei ohne Vorsteuerabzug und Auslandsumsätze werden ohne USt. ausgewiesen.
    reverse = kind in ("eu", "third") or supply == "exempt"

    norm = []
    for it in items:
        qty = Decimal(str(it.get("quantity", 1)).replace(",", "."))
        price = Decimal(str(it["unit_price"]).replace(",", "."))
        rate = 0 if reverse else int(it.get("vat_rate", cfg["invoice"]["default_vat_rate"]))
        desc = (it.get("description") or "").strip()
        if not desc:
            raise ValueError("Jede Rechnungsposition braucht eine Leistungsbeschreibung.")
        norm.append({
            "description": desc,
            "quantity": qty,
            "unit": (it.get("unit") or "-"),
            "unit_price": price,
            "vat_rate": rate,
            "line_net": money(qty * price),
        })
    if not norm:
        raise ValueError("Rechnung braucht mindestens eine Position.")

    # Keine Rechnung aus Platzhaltern/Defaults: ohne echten Empfänger und echten Betrag
    # wird NICHTS angelegt (gilt für Chat, Formular, CLI und MCP gleichermaßen).
    if not cust["name"]:
        raise ValueError("Rechnung braucht einen Empfänger.")

    net = money(sum(i["line_net"] for i in norm))
    if net <= 0:
        raise ValueError("Rechnung braucht einen Betrag größer 0,00 €.")
    vat = Decimal(0) if reverse else money(
        sum(i["line_net"] * Decimal(i["vat_rate"]) / 100 for i in norm))
    gross = money(net + vat)

    issue_d = datetime.strptime(issue_date, "%Y-%m-%d").date()
    terms = int(cfg["invoice"].get("payment_terms_days", 14))
    due = (issue_d + timedelta(days=terms)).isoformat()
    if number:
        year, month, seq = parse_invoice_number(number)
    else:
        number, seq = next_invoice_number(conn, issue_d.year, issue_d.month)
        year, month = issue_d.year, issue_d.month
    if conn.execute("SELECT 1 FROM invoices WHERE number = ?", (number,)).fetchone():
        raise ValueError(f"Rechnungsnummer {number} existiert bereits.")

    cur_ = conn.execute(
        """INSERT INTO invoices
           (number, year, month, seq, customer_id, customer_name, customer_address,
            customer_vat_id, issue_date, service_from, service_to, due_date, paid_date,
            kind, net, vat, gross, notes, pdf_path, created_at, supply_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
        (number, year, month, seq, cust["id"], cust["name"],
         cust["address"], cust["vat_id"], issue_date, service_from,
         service_to or None, due, paid_date or None, kind, float(net), float(vat),
         float(gross), notes or None, _now(), supply),
    )
    invoice_id = cur_.lastrowid
    for i in norm:
        conn.execute(
            """INSERT INTO invoice_items
               (invoice_id, description, quantity, unit, unit_price, vat_rate, line_net)
               VALUES (?,?,?,?,?,?,?)""",
            (invoice_id, i["description"], float(i["quantity"]), i["unit"],
             float(i["unit_price"]), i["vat_rate"], float(i["line_net"])),
        )
    conn.commit()

    invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if render:
        pdf_path = render_invoice_pdf(cfg, invoice, norm)
        conn.execute("UPDATE invoices SET pdf_path = ? WHERE id = ?", (rel_pfad(pdf_path), invoice_id))
        conn.commit()
    return conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()


def list_invoices(conn) -> list:
    return conn.execute("SELECT * FROM invoices ORDER BY year, month, seq").fetchall()


def get_invoice(conn, invoice_id: int):
    return conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()


def get_invoice_by_number(conn, number: str):
    return conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()


def get_invoice_items(conn, invoice_id: int) -> list:
    return conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (invoice_id,)
    ).fetchall()


def delete_invoice(conn, invoice_id: int) -> dict:
    """Rechnung endgültig löschen: Positionen, PDF-Datei und Zuordnungen von Zahlungen.
    Zugeordnete Bankbuchungen bleiben erhalten, werden aber wieder auf 'offen' gesetzt.
    Rechnungen aus festgeschriebenen Zeiträumen sind gesperrt."""
    inv = get_invoice(conn, invoice_id)
    if inv is None:
        raise ValueError(f"Rechnung #{invoice_id} nicht gefunden.")
    issue = str(inv["issue_date"])
    for token in (issue[:4], f"{issue[:4]}-Q{(int(issue[5:7]) - 1) // 3 + 1}", issue[:7]):
        if is_period_closed(token):
            raise ValueError(f"Zeitraum {token} ist festgeschrieben – "
                             f"Rechnung {inv['number']} kann nicht gelöscht werden.")
    freed = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
    conn.execute("UPDATE transactions SET invoice_id = NULL, category = 'offen' "
                 "WHERE invoice_id = ?", (invoice_id,))
    conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    conn.commit()
    if inv["pdf_path"]:
        try:
            abs_pfad(inv["pdf_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": inv["number"], "buchungen_geloest": freed}


def mark_invoice_paid(conn, invoice_id: int, paid_date: str) -> None:
    conn.execute("UPDATE invoices SET paid_date = ? WHERE id = ?", (paid_date, invoice_id))
    conn.commit()


def send_invoice_email(conn, invoice_id: int, to_email: str, subject: str, body: str,
                       *, cc: str | None = None, copy_self: bool = False) -> dict:
    """Rechnung per E-Mail versenden (PDF im Anhang) über die in den Einstellungen hinterlegten
    SMTP-Zugangsdaten. Markiert die Rechnung anschließend als versendet (sent_at)."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    s = load_settings()
    host = (s.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("Kein SMTP-Server hinterlegt. Bitte zuerst unter Einstellungen › E-Mail konfigurieren.")
    to_email = (to_email or "").strip()
    if not to_email:
        raise ValueError("Bitte eine Empfänger-Adresse angeben.")
    port = int(s.get("smtp_port") or 587)
    user = (s.get("smtp_user") or "").strip()
    pw = s.get("smtp_pass") or ""
    sender = (s.get("email_from") or user or "").strip()
    if not sender:
        raise ValueError("Keine Absender-Adresse (email_from) hinterlegt.")

    inv = get_invoice(conn, invoice_id)
    if not inv:
        raise ValueError("Rechnung nicht gefunden.")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    if cc:
        msg["Cc"] = cc
    if s.get("email_reply_to"):
        msg["Reply-To"] = s["email_reply_to"]
    if copy_self:
        msg["Bcc"] = sender
    msg["Subject"] = subject or f"Rechnung {inv['number']}"
    msg.set_content(body or f"Anbei die Rechnung {inv['number']}.")

    pdf_path = abs_pfad(inv["pdf_path"])
    if pdf_path and pdf_path.exists():
        msg.add_attachment(pdf_path.read_bytes(), maintype="application", subtype="pdf",
                           filename=f"Rechnung_{inv['number']}.pdf")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as srv:
            if user:
                srv.login(user, pw)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as srv:
            srv.ehlo()
            try:
                srv.starttls(context=ctx)
                srv.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
            if user:
                srv.login(user, pw)
            srv.send_message(msg)

    conn.execute("UPDATE invoices SET sent_at = ? WHERE id = ?", (_now(), invoice_id))
    conn.commit()
    return {"sent": True, "to": to_email, "number": inv["number"]}


def record_invoice_payment(conn, invoice_id: int, paid_date: str, *,
                           amount=None, currency: str | None = None,
                           method: str = "", create_tx: bool = True) -> dict:
    """Zahlung zu einer Rechnung erfassen: Rechnung bezahlt markieren und (optional)
    eine verknüpfte Einnahme-Buchung anlegen – für Zahlungen ohne Bank-CSV
    (Bar, anderes/altes Konto, Fremdwährung). Betrag in Rechnungs-/Zahlwährung,
    Umrechnung auf Heimwährung über EZB-Kurs am Zahltag."""
    import uuid
    inv = get_invoice(conn, invoice_id)
    result = {"tx": False, "amount_home": None}
    if create_tx:
        cur = (currency or inv["currency"] or home_currency()).upper()
        try:
            amt = float(str(amount).replace(",", ".")) if amount not in (None, "") else float(inv["gross"])
        except (TypeError, ValueError):
            amt = float(inv["gross"])
        amount_home, fx = to_home(conn, amt, cur, paid_date)
        desc = f"Zahlung Rechnung {inv['number']}" + (f" ({method})" if method else "")
        conn.execute(
            """INSERT INTO transactions
               (ref, date, amount, fee, currency, amount_home, fx_rate,
                description, tx_type, category, invoice_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'einnahme_rechnung', ?, ?)""",
            ("invpay-" + uuid.uuid4().hex, paid_date, amt, 0.0, cur, amount_home, fx,
             desc, method or "Zahlung", invoice_id, _now()),
        )
        result.update({"tx": True, "amount_home": amount_home, "currency": cur, "amount": amt})
    conn.execute("UPDATE invoices SET paid_date = ? WHERE id = ?", (paid_date, invoice_id))
    conn.commit()
    return result


# ── Ausgaben ─────────────────────────────────────────────────────────────
def create_expense(conn, *, date_: str, vendor: str, description: str | None,
                   category: str, gross, vat_rate: int, paid_date: str | None,
                   receipt_src: str | None = None, private_share=0, reverse_charge=0,
                   vat_kind=None):
    """private_share: %-Anteil der privat ist (z. B. Telefon 80). Nur der
    Geschäftsanteil zählt als Betriebsausgabe / abziehbare Vorsteuer.
    vat_kind: USt-Art (siehe EXPENSE_VAT_KINDS). Bei §13b und innergemeinschaftlichem Erwerb
    stellt der Lieferant ohne Steuer aus (Netto = Brutto); vat_rate ist dann der Satz, mit dem
    der Empfänger die Steuer selbst berechnet – sie ist zugleich wieder Vorsteuer, also
    EÜR-neutral. reverse_charge: Altparameter, entspricht vat_kind='rc_eu'."""
    kind = _vat_kind(vat_kind) if vat_kind else ("rc_unknown" if reverse_charge else "domestic")
    gross_full = Decimal(str(gross).replace(",", "."))
    rate = int(vat_rate)
    if kind in SELF_TAXED and rate == 0:
        rate = 19          # Regelsatz – sonst stünde die selbst geschuldete Steuer auf null
    ps = max(Decimal(0), min(Decimal(100), Decimal(str(private_share or 0))))
    frac = (Decimal(100) - ps) / Decimal(100)          # Geschäftsanteil
    if kind in SELF_TAXED:
        # Der Lieferant weist keine Steuer aus – der Zahlbetrag IST das Entgelt.
        net_full, vat_full = gross_full, Decimal(0)
    else:
        net_full = gross_full / (1 + Decimal(rate) / 100)
        vat_full = gross_full - net_full
    net = money(net_full * frac)
    vat = money(vat_full * frac)
    gross_business = money(net + vat)

    stored = None
    if receipt_src:
        src = Path(receipt_src).expanduser()
        if src.exists():
            dest = beleg_dir("eingang", date_) / f"{date_}_{vendor.replace(' ', '-')}{src.suffix}"
            dest.write_bytes(src.read_bytes())
            stored = rel_pfad(dest)

    cur_ = conn.execute(
        """INSERT INTO expenses
           (date, paid_date, vendor, description, category, net, vat_rate, vat, gross,
            receipt_path, created_at, private_share, gross_full, reverse_charge, vat_kind)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (date_, paid_date or None, vendor, description or None, category,
         float(net), rate, float(vat), float(gross_business), stored, _now(),
         float(ps), float(money(gross_full)), 1 if kind in RC_KINDS else 0, kind),
    )
    conn.commit()
    return {"id": cur_.lastrowid, "net": net, "vat": vat, "gross": gross_business,
            "gross_full": money(gross_full), "private_share": ps, "vat_kind": kind}


def list_expenses(conn) -> list:
    return conn.execute("SELECT * FROM expenses ORDER BY date").fetchall()


def mark_expense_paid(conn, expense_id: int, paid_date: str) -> None:
    conn.execute("UPDATE expenses SET paid_date = ? WHERE id = ?", (paid_date, expense_id))
    conn.commit()


def _guard_open_period(date_str: str, was: str) -> None:
    """Wirft, wenn der Zeitraum (Jahr/Quartal/Monat) festgeschrieben ist."""
    dt = str(date_str or "")
    if len(dt) < 7:
        return
    for token in (dt[:4], f"{dt[:4]}-Q{(int(dt[5:7]) - 1) // 3 + 1}", dt[:7]):
        if is_period_closed(token):
            raise ValueError(f"Zeitraum {token} ist festgeschrieben – {was} nicht möglich.")


def update_expense(conn, expense_id: int, *, category=None, vat_rate=None,
                   private_share=None, vat_kind=None) -> dict:
    """Buchungssatz einer Ausgabe ändern (Konto, USt-Satz, Privatanteil, USt-Art) und
    Netto/USt/Brutto aus dem gespeicherten Bruttobetrag neu berechnen.
    Gesperrt in festgeschriebenen Zeiträumen."""
    e = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if e is None:
        raise ValueError(f"Ausgabe #{expense_id} nicht gefunden.")
    _guard_open_period(e["date"] or e["paid_date"], "Bearbeiten")
    category = e["category"] if category is None else category
    kind = _vat_kind(_col(e, "vat_kind") if vat_kind is None else vat_kind)
    rate = int(e["vat_rate"] or 0) if vat_rate is None else int(vat_rate)
    # Beim Wechsel auf eine selbst zu versteuernde Art ohne eigenen Satz greift der Regelsatz –
    # sonst stünde die Steuerschuld auf 0 und der Beleg fehlte still in der USt-VA.
    if kind in SELF_TAXED and rate == 0:
        rate = 19
    ps_src = e["private_share"] if private_share is None else private_share
    ps = max(Decimal(0), min(Decimal(100), Decimal(str(ps_src or 0))))
    gross_full = Decimal(str(e["gross_full"] or e["gross"]))
    frac = (Decimal(100) - ps) / Decimal(100)
    if kind in SELF_TAXED:
        net_full, vat_full = gross_full, Decimal(0)
    else:
        net_full = gross_full / (1 + Decimal(rate) / 100)
        vat_full = gross_full - net_full
    net = money(net_full * frac)
    vat = money(vat_full * frac)
    gross_business = money(net + vat)
    conn.execute(
        "UPDATE expenses SET category=?, vat_rate=?, private_share=?, net=?, vat=?, gross=?, "
        "vat_kind=?, reverse_charge=? WHERE id=?",
        (category, rate, float(ps), float(net), float(vat), float(gross_business),
         kind, 1 if kind in RC_KINDS else 0, expense_id))
    conn.commit()
    return {"id": expense_id, "category": category, "net": net, "vat": vat,
            "gross": gross_business, "private_share": ps, "vat_kind": kind}


def delete_expense(conn, expense_id: int) -> dict:
    """Betriebsausgabe löschen. Verknüpfte Bankbuchung wird wieder 'offen', ein zugeordnetes
    Dokument wieder 'to_review'. Gesperrt in festgeschriebenen Zeiträumen."""
    e = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if e is None:
        raise ValueError(f"Ausgabe #{expense_id} nicht gefunden.")
    _guard_open_period(e["date"] or e["paid_date"], "Löschen")
    conn.execute("UPDATE transactions SET expense_id=NULL, category='offen' WHERE expense_id=?",
                 (expense_id,))
    conn.execute("UPDATE documents SET expense_id=NULL, status='to_review' WHERE expense_id=?",
                 (expense_id,))
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    conn.commit()
    return {"deleted": expense_id, "vendor": e["vendor"]}


def book_expense_as_category(conn, expense_id: int, category: str) -> dict:
    """Eine (fälschlich als Betriebsausgabe gebuchte) Zeile in eine Bank-/Privatkategorie umbuchen –
    z. B. 'privatentnahme' (Konto 1800, ohne USt), 'gebuehr', 'einnahme_sonstige', 'ignorieren'.
    Die Ausgabe wird entfernt; die zugrundeliegende Zahlung erhält die Kategorie (gibt es keine,
    wird eine angelegt). Es wird KEINE Auto-Regel gelernt (einmalige Korrektur)."""
    if category not in TX_CATEGORIES or category in ("offen", "ausgabe", "einnahme_rechnung"):
        raise ValueError(f"Nicht als Kategorie buchbar: {category}")
    e = conn.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if e is None:
        raise ValueError(f"Ausgabe #{expense_id} nicht gefunden.")
    _guard_open_period(e["date"] or e["paid_date"], "Umbuchen")
    tx_id = e["source_tx"]
    if tx_id and get_transaction(conn, tx_id):
        reset_tx(conn, tx_id)                            # löst u. a. diese Ausgabe
    else:
        amt = -abs(float(e["gross_full"] or e["gross"]))
        tx_id = create_manual_transaction(conn, e["paid_date"] or e["date"],
                                          e["vendor"] or "Umbuchung", amt, "EUR", method="Umbuchung")
    conn.execute("UPDATE documents SET expense_id=NULL, status='to_review' WHERE expense_id=?",
                 (expense_id,))
    conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))     # falls reset_tx sie nicht traf
    conn.execute("UPDATE transactions SET category=? WHERE id=?", (category, tx_id))  # ohne Regel
    conn.commit()
    return {"tx_id": tx_id, "category": category}


# ── Bank / Transaktionen ─────────────────────────────────────────────────
TX_CATEGORIES = {
    "offen": "offen",
    "einnahme_rechnung": "Einnahme (Rechnung)",
    "einnahme_sonstige": "Einnahme (sonstige)",
    "ausgabe": "Betriebsausgabe",
    "privatentnahme": "Privatentnahme / Barauszahlung",
    "gebuehr": "Bankgebühr",
    "ignorieren": "Ignorieren",
}


def _num(v) -> float:
    v = (str(v) or "").strip().replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return 0.0


def import_bank_csv(conn, text: str) -> dict:
    """Revolut-CSV importieren. Dedupe über ref; danach Auto-Abgleich mit Rechnungen."""
    import csv
    import hashlib
    import io

    try:
        delim = csv.Sniffer().sniff(text[:2048], delimiters=",;\t").delimiter
    except Exception:
        delim = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    fm = {(fn or "").strip().lower(): fn for fn in (reader.fieldnames or [])}

    def col(row, *names):
        for n in names:
            if n in fm:
                return (row.get(fm[n], "") or "").strip()
        return ""

    imported = dups = 0
    for row in reader:
        state = col(row, "state").upper()
        if state and state != "COMPLETED":
            continue
        date_ = (col(row, "completed date") or col(row, "started date") or col(row, "date"))[:10]
        if not date_:
            continue
        amount = _num(col(row, "amount"))
        fee = _num(col(row, "fee"))
        desc = col(row, "description")
        currency = col(row, "currency") or "EUR"
        txtype = col(row, "type")
        bal = col(row, "balance")
        ref = hashlib.md5(f"{date_}|{amount}|{desc}|{bal}|{txtype}".encode()).hexdigest()
        if conn.execute("SELECT 1 FROM transactions WHERE ref = ?", (ref,)).fetchone():
            dups += 1
            continue
        amount_home, fx = to_home(conn, amount, currency, date_)
        conn.execute(
            """INSERT INTO transactions
               (ref, date, amount, fee, currency, amount_home, fx_rate,
                description, tx_type, category, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'offen', ?)""",
            (ref, date_, amount, fee, currency, amount_home, fx, desc, txtype, _now()),
        )
        imported += 1
    conn.commit()
    matched = _auto_match_transactions(conn)
    return {"imported": imported, "duplicates": dups, "matched": matched}


def import_bank_rows(conn, rows: list) -> dict:
    """Fertig normalisierte Zahlungen aufnehmen (z. B. aus dem Wirex-Konto).

    Landen als 'offen' in den Zahlungen – bewusst OHNE Buchungssatz und ohne
    Festschreibung: die buchhalterische Zuordnung macht der Nutzer.
    'ref' ist UNIQUE, ein erneuter Abruf legt also nichts doppelt an."""
    imported = dups = 0
    for r in rows:
        ref = (r.get("ref") or "").strip()
        date_ = (r.get("date") or "")[:10]
        if not ref or not date_:
            continue
        if conn.execute("SELECT 1 FROM transactions WHERE ref = ?", (ref,)).fetchone():
            dups += 1
            continue
        amount = float(r.get("amount") or 0)
        currency = (r.get("currency") or "EUR").upper()
        amount_home, fx = to_home(conn, amount, currency, date_)
        conn.execute(
            """INSERT INTO transactions
               (ref, date, amount, fee, currency, amount_home, fx_rate,
                description, tx_type, category, created_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'offen', ?)""",
            (ref, date_, amount, float(r.get("fee") or 0), currency, amount_home, fx,
             r.get("description") or "", r.get("tx_type") or "", _now()),
        )
        imported += 1
    conn.commit()
    matched = _auto_match_transactions(conn) if imported else 0
    return {"imported": imported, "duplicates": dups, "matched": matched}


def _auto_match_transactions(conn) -> int:
    """Eingänge automatisch offenen Rechnungen zuordnen (gleicher Bruttobetrag, nächstes Datum)."""
    matched = 0
    txs = conn.execute(
        """SELECT * FROM transactions
           WHERE amount > 0 AND invoice_id IS NULL AND amount_home IS NOT NULL"""
    ).fetchall()
    for t in txs:
        cands = conn.execute(
            """SELECT * FROM invoices WHERE ABS(gross - ?) < 0.005
               AND id NOT IN (SELECT invoice_id FROM transactions WHERE invoice_id IS NOT NULL)""",
            (t["amount_home"],),
        ).fetchall()
        if not cands:
            continue

        def diff(inv):
            a = datetime.strptime(t["date"], "%Y-%m-%d").date()
            b = datetime.strptime(inv["issue_date"], "%Y-%m-%d").date()
            return abs((a - b).days)

        inv = min(cands, key=diff)
        link_tx_invoice(conn, t["id"], inv["id"])
        matched += 1
    return matched


def create_manual_transaction(conn, date_: str, description: str, amount,
                              currency: str = "EUR", method: str = "Manuell"):
    """Manuelle Buchung (z. B. Barzahlung), die nicht aus dem Bank-CSV kommt.
    amount signiert: + Eingang, - Ausgang. method = Zahlungsart (z. B. 'Kasse', 'Wallet / Krypto').
    Landet als 'offen' und wird normal kategorisiert. Gibt die neue tx-id zurück."""
    import uuid
    amt = float(str(amount).replace(",", "."))
    amount_home, fx = to_home(conn, amt, currency, date_)
    cur = conn.execute(
        """INSERT INTO transactions
           (ref, date, amount, fee, currency, amount_home, fx_rate,
            description, tx_type, category, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'offen', ?)""",
        ("manual-" + uuid.uuid4().hex, date_, amt, 0.0, currency, amount_home, fx,
         description or "Manuelle Buchung", (method or "Manuell").strip(), _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_transactions(conn, only_open: bool = False, query: str | None = None) -> list:
    q = "SELECT * FROM transactions"
    conds, params = [], []
    if only_open:
        conds.append("category = 'offen'")
    if query and query.strip():
        conds.append("(lower(description) LIKE ? OR lower(tx_type) LIKE ?)")
        like = f"%{query.strip().lower()}%"
        params += [like, like]
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY date DESC, id DESC"
    return conn.execute(q, params).fetchall()


def list_ledger(conn, lo: str, hi: str) -> list:
    """Vollständiges Buchungsjournal im Zeitraum (Ist): bezahlte Ausgaben, bezahlte Rechnungen
    UND Bank-Bewegungen, die nicht bereits als Ausgabe/Rechnung erfasst sind (keine Doppel-
    zählung). Vereinheitlichte Zeilen, nach Datum absteigend. Jede Zeile: kind (expense|invoice|
    bank), date, text, Buchungssatz (skr_no/skr_name), Betrag (Heimwährung, vorzeichenbehaftet),
    beleg (bool). Bank-Zeilen tragen zusätzlich die Roh-Transaktion in 'tx' für die Weiterverarbeitung."""
    d = lambda x: float(x or 0)

    def R(x):
        return round(x + 1e-9, 2)

    def tax_label_exp(vat, kind):
        if kind in RC_KINDS:
            return "§13b"
        if kind == "ig_erwerb":
            return "i.g. Erwerb"
        if kind == "import":
            return f"{int(vat)}% EUSt." if vat else "Einfuhr"
        return "ohne USt." if not vat else f"{int(vat)}% VSt."

    rows = []

    # ── Betriebsausgaben (Ist = paid_date) ── jede Zeile trägt ihren EÜR-Beitrag in '_euer';
    #    Privatanteil erzeugt zusätzlich eine sichtbare Nutzungsentnahme-Zeile (Gegenbuchung).
    for e in conn.execute(
            "SELECT * FROM expenses WHERE paid_date IS NOT NULL AND paid_date BETWEEN ? AND ? "
            "ORDER BY paid_date DESC, id DESC", (lo, hi)):
        parts = (e["category"] or "").split(" - ", 1)
        betrag = d(e["gross_full"]) or d(e["gross"])          # ungekürzter Zahlbetrag
        neutral = is_neutral_account(e["category"])           # Bestandskonto -> EÜR-neutral
        vk = expense_vat_kind(e)
        rc = vk in RC_KINDS
        gf = d(e["gross_full"]) or (d(e["net"]) + d(e["vat"]))
        eu, eig_net, eig_ust = {}, 0.0, 0.0
        if not neutral and vk in SELF_TAXED:
            # §13b / i.g. Erwerb: Steuer selbst berechnet und zugleich Vorsteuer -> EÜR-neutral.
            eu = {"aufwand_net": gf, "ust_13b": R(gf * expense_tax_rate(e) / 100),
                  "business_net": d(e["net"])}
        elif not neutral:
            rate = e["vat_rate"] if e["vat_rate"] is not None else 19   # 0 % bleibt 0 %, nur NULL→19
            nf = R(gf / (1 + rate / 100)); vf = R(gf - nf)
            eu = {"aufwand_net": nf, "vorsteuer": vf, "business_net": d(e["net"])}
            eig_net, eig_ust = R(nf - d(e["net"])), R(vf - d(e["vat"]))
        ps = int(round(e["private_share"] or 0))
        rows.append({
            "kind": "expense", "id": e["id"], "date": e["paid_date"],
            "text": e["vendor"] or e["description"] or "Ausgabe",
            "sub": e["description"] if e["vendor"] else None, "category": e["category"],
            "skr_no": parts[0].strip(), "skr_name": parts[1].strip() if len(parts) > 1 else "",
            "amount": -betrag, "is_income": False,
            "beleg": bool(e["receipt_path"]) and abs_pfad(e["receipt_path"]).exists(),
            "vat_rate": int(e["vat_rate"] or 0), "reverse_charge": rc, "vat_kind": vk,
            "private_share": ps,
            "tax": tax_label_exp(e["vat_rate"], vk), "_euer": eu})
        if ps > 0 or eig_net or eig_ust:                      # sichtbare Nutzungsentnahme
            rows.append({
                "kind": "nutzungsentnahme", "id": e["id"], "date": e["paid_date"],
                "text": "Nutzungsentnahme", "sub": e["vendor"] or e["description"],
                "skr_no": "8921", "skr_name": f"Privatnutzung {ps} %",
                "amount": R(eig_net + eig_ust), "is_income": True, "beleg": False,
                "vat_rate": int(e["vat_rate"] or 0), "reverse_charge": False, "derived": True,
                "tax": (f"{int(e['vat_rate'])}% USt." if e["vat_rate"] else "ohne USt."),
                "_euer": {"eig_net": eig_net, "eig_ust": eig_ust}})

    # ── Ausgangsrechnungen (Ist = paid_date) ──
    inv_konto = {"eu": ("8336", "Erlöse EU (§13b)", "steuerfrei"),
                 "third": ("8338", "Erlöse Drittland", "nicht steuerbar")}
    for i in conn.execute(
            "SELECT * FROM invoices WHERE paid_date IS NOT NULL AND paid_date BETWEEN ? AND ? "
            "ORDER BY paid_date DESC, id DESC", (lo, hi)):
        no, name, tax = inv_konto.get(i["kind"], ("8400", "Erlöse 19% USt.",
                                                  "19% USt." if d(i["vat"]) else "ohne USt."))
        rows.append({
            "kind": "invoice", "id": i["id"], "date": i["paid_date"],
            "text": i["customer_name"] or "Kunde", "sub": i["number"],
            "skr_no": no, "skr_name": name, "amount": d(i["gross"]), "is_income": True,
            "beleg": bool(i["pdf_path"]) and abs_pfad(i["pdf_path"]).exists(), "vat_rate": None,
            "reverse_charge": i["kind"] in ("eu", "third"), "tax": tax,
            "_euer": {"umsatz_net": d(i["net"]), "umsatz_ust": d(i["vat"]), "inv_kind": i["kind"]}})

    # ── Bank-Bewegungen ohne eigene Ausgabe/Rechnung (Rest: privat, Gebühren, offen …) ──
    used_tx = {r["source_tx"] for r in
               conn.execute("SELECT source_tx FROM expenses WHERE source_tx IS NOT NULL")}
    for t in conn.execute("SELECT * FROM transactions WHERE date BETWEEN ? AND ?", (lo, hi)):
        if t["expense_id"] or t["invoice_id"] or t["id"] in used_tx:
            continue                                          # schon als Ausgabe/Rechnung im Journal
        ah = t["amount_home"] if t["amount_home"] is not None else t["amount"]
        rows.append({"kind": "bank", "id": t["id"], "date": t["date"],
                     "text": t["description"] or "—", "sub": t["tx_type"], "skr_no": "",
                     "skr_name": "", "amount": ah, "is_income": t["amount"] > 0,
                     "beleg": bool(t["receipt_path"]) and abs_pfad(t["receipt_path"]).exists(),
                     "vat_rate": None, "reverse_charge": False, "category": t["category"], "tx": t,
                     "_euer": ({"sonstige": ah} if t["category"] == "einnahme_sonstige" else {})})

    # Nutzungsentnahme direkt unter ihre Ausgabe (gleiche id) einsortieren
    rows.sort(key=lambda r: (r["date"] or "", r["kind"] != "bank", r["id"],
                             0 if r["kind"] == "nutzungsentnahme" else 1), reverse=True)
    return rows


def bulk_set_category(conn, ids: list[int], category: str) -> int:
    if category not in TX_CATEGORIES:
        raise ValueError(f"Unbekannte Kategorie: {category}")
    n = 0
    for tid in ids:
        set_tx_category(conn, tid, category)   # setzt Kategorie + lernt Regel
        n += 1
    return n


def bulk_create_expense(conn, ids: list[int], category: str, vat_rate, private_share=0) -> int:
    """Für jede ausgewählte Ausgangs-Buchung eine Betriebsausgabe anlegen."""
    n = 0
    for tid in ids:
        t = get_transaction(conn, tid)
        if not t or t["amount"] >= 0:      # nur Abgänge werden zu Ausgaben
            continue
        create_expense_from_tx(conn, tid, category, vat_rate, private_share=private_share)
        n += 1
    return n


def get_transaction(conn, tx_id: int):
    return conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()


def transactions_open_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM transactions WHERE category = 'offen'"
    ).fetchone()["c"]


def reset_tx(conn, tx_id: int) -> None:
    """Zahlung auf 'offen' zurücksetzen: verknüpfte Ausgabe löschen, Rechnungs-
    Zuordnung lösen (Rechnung wieder offen). Basis für Neu-Zuordnen/Bearbeiten."""
    t = get_transaction(conn, tx_id)
    if not t:
        return
    exp_id, inv_id = t["expense_id"], t["invoice_id"]
    # zuerst die Verweise der Transaktion lösen (sonst FK-Verletzung beim Löschen)
    conn.execute("UPDATE transactions SET category = 'offen', expense_id = NULL, "
                 "invoice_id = NULL WHERE id = ?", (tx_id,))
    # alle aus dieser Buchung erzeugten Ausgaben löschen (auch Split-Positionen)
    conn.execute("DELETE FROM expenses WHERE source_tx = ?", (tx_id,))
    if exp_id:
        conn.execute("DELETE FROM expenses WHERE id = ?", (exp_id,))
    if inv_id:
        conn.execute("UPDATE invoices SET paid_date = NULL WHERE id = ?", (inv_id,))
    conn.commit()


def set_tx_category(conn, tx_id: int, category: str, note: str | None = None) -> None:
    if category not in TX_CATEGORIES:
        raise ValueError(f"Unbekannte Kategorie: {category}")
    conn.execute("UPDATE transactions SET category = ?, note = COALESCE(?, note) WHERE id = ?",
                 (category, note, tx_id))
    conn.commit()
    t = get_transaction(conn, tx_id)
    save_rule(conn, t["description"], category)


# ── Regeln (automatische Vorschläge für wiederkehrende Buchungen) ─────────
def _rule_key(description: str) -> str:
    return (description or "").strip().lower()


def save_rule(conn, description: str, action: str, category=None,
              vat_rate=None, private_share=None, vat_kind=None) -> None:
    """Merkt sich die Zuordnung je Beschreibung, um sie künftig vorzuschlagen. Die USt-Art
    gehört dazu: ein wiederkehrender §13b-Lieferant soll nicht jedes Mal neu eingestuft werden."""
    key = _rule_key(description)
    if not key or action in ("offen", "invoice"):
        return
    vr = int(vat_rate) if vat_rate not in (None, "") else None
    ps = float(private_share) if private_share not in (None, "") else None
    vk = _vat_kind(vat_kind) if vat_kind else None
    conn.execute(
        """INSERT INTO booking_rules
             (match_key, action, category, vat_rate, private_share, updated_at, vat_kind)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(match_key) DO UPDATE SET
             action=excluded.action, category=excluded.category,
             vat_rate=excluded.vat_rate, private_share=excluded.private_share,
             vat_kind=excluded.vat_kind, updated_at=excluded.updated_at""",
        (key, action, category, vr, ps, _now(), vk),
    )
    conn.commit()


def get_rules_map(conn) -> dict:
    return {r["match_key"]: r for r in conn.execute("SELECT * FROM booking_rules")}


def suggestion_for(rules_map: dict, description: str):
    return rules_map.get(_rule_key(description))


def link_tx_invoice(conn, tx_id: int, invoice_id: int) -> None:
    t = get_transaction(conn, tx_id)
    conn.execute("UPDATE transactions SET invoice_id = ?, category = 'einnahme_rechnung' WHERE id = ?",
                 (invoice_id, tx_id))
    conn.execute("UPDATE invoices SET paid_date = ? WHERE id = ?", (t["date"], invoice_id))
    conn.commit()


def create_expense_from_tx(conn, tx_id: int, category: str, vat_rate,
                           description: str | None = None, private_share=0,
                           vat_kind=None) -> None:
    t = get_transaction(conn, tx_id)
    signed = t["amount_home"] if t["amount_home"] is not None else t["amount"]
    # Abgang (−) → positive Betriebsausgabe; Eingang (+) → negative Ausgabe = Erstattung/Minderung
    gross_home = -signed
    r = create_expense(
        conn, date_=t["date"], vendor=(description or t["description"] or "Ausgabe")[:120],
        description=t["description"], category=(category or "Sonstiges"),
        gross=gross_home, vat_rate=int(vat_rate or 0), paid_date=t["date"],
        private_share=private_share, vat_kind=vat_kind)
    conn.execute("UPDATE expenses SET source_tx = ? WHERE id = ?", (tx_id, r["id"]))
    conn.execute("UPDATE transactions SET expense_id = ?, category = 'ausgabe' WHERE id = ?",
                 (r["id"], tx_id))
    if t["receipt_path"]:
        conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?",
                     (t["receipt_path"], r["id"]))
    conn.commit()
    save_rule(conn, t["description"], "expense", category, vat_rate, private_share, vat_kind)


def create_split_from_tx(conn, tx_id: int, lines: list) -> None:
    """Eine Zahlung auf mehrere Betriebsausgaben aufteilen (Split-Buchung).
    lines: [{category, amount, vat_rate, private_share}]. Erzeugt je Zeile eine
    Ausgabe (Datum/Zahldatum = Transaktionsdatum) und markiert die Buchung als 'ausgabe'."""
    reset_tx(conn, tx_id)
    t = get_transaction(conn, tx_id)
    first_eid = None
    for ln in lines:
        try:
            gross = abs(float(str(ln.get("amount")).replace(",", ".")))
        except (TypeError, ValueError):
            continue
        if gross <= 0:
            continue
        r = create_expense(
            conn, date_=t["date"], vendor=(t["description"] or "Ausgabe")[:120],
            description=t["description"], category=ln.get("category") or "Sonstiges",
            gross=gross, vat_rate=int(ln.get("vat_rate") or 0), paid_date=t["date"],
            private_share=int(ln.get("private_share") or 0))
        conn.execute("UPDATE expenses SET source_tx = ? WHERE id = ?", (tx_id, r["id"]))
        if first_eid is None:
            first_eid = r["id"]
    if first_eid is not None:
        conn.execute("UPDATE transactions SET category = 'ausgabe', expense_id = ? WHERE id = ?",
                     (first_eid, tx_id))
        if t["receipt_path"]:
            conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?",
                         (t["receipt_path"], first_eid))
        conn.commit()


def attach_receipt_to_tx(conn, tx_id: int, data: bytes, filename: str):
    safe = "".join(c for c in filename if c.isalnum() or c in "._- ").strip() or "beleg"
    t0 = get_transaction(conn, tx_id)
    dest = beleg_dir("eingang", t0["date"] if t0 else None) / f"tx{tx_id}_{safe}"
    dest.write_bytes(data)
    conn.execute("UPDATE transactions SET receipt_path = ? WHERE id = ?", (rel_pfad(dest), tx_id))
    t = get_transaction(conn, tx_id)
    if t["expense_id"]:
        conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?", (rel_pfad(dest), t["expense_id"]))
    conn.commit()
    return dest


# ── Import (CSV) ─────────────────────────────────────────────────────────
IMPORT_COLUMNS = [
    "number", "issue_date", "service_from", "service_to", "paid_date", "kind",
    "customer_name", "customer_address", "customer_vat_id",
    "description", "quantity", "unit", "unit_price", "vat_rate",
]

_KIND_ALIASES = {
    "": "domestic", "domestic": "domestic", "inland": "domestic", "de": "domestic",
    "deutschland": "domestic", "germany": "domestic",
    "eu": "eu", "eu-b2b": "eu", "reverse-charge": "eu", "reverse charge": "eu", "rc": "eu",
    "third": "third", "drittland": "third", "non-eu": "third", "3rd": "third",
    "uk": "third", "us": "third", "usa": "third", "ch": "third", "schweiz": "third",
}


def _norm_kind(v: str):
    return _KIND_ALIASES.get((v or "").strip().lower())


def _parse_date_any(v: str):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Datum '{v}' nicht erkannt (nutze JJJJ-MM-TT oder TT.MM.JJJJ).")


def _import_address(v: str) -> str:
    import re
    v = (v or "").strip()
    parts = v.split("\n") if "\n" in v else re.split(r"[;|]", v)
    return "\n".join(p.strip() for p in parts if p.strip())


def resolve_or_create_customer(conn, name: str, address: str, vat_id: str | None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Kunde ohne Namen.")
    vat_id = (vat_id or "").strip() or None
    for c in list_customers(conn):
        if c["name"].strip().lower() == name.lower():
            return c
    return create_customer(conn, name, _import_address(address), vat_id)


def set_customer_email(conn, customer_id: int, email: str) -> None:
    """Empfänger-Adresse am Kunden merken (für nächsten Versand)."""
    email = (email or "").strip()
    if not customer_id or not email:
        return
    conn.execute("UPDATE customers SET email = ? WHERE id = ?", (email, customer_id))
    conn.commit()


def import_invoices_csv(conn, cfg, text: str, *, render: bool = True) -> dict:
    """CSV einlesen. Zeilen mit gleicher 'number' werden zu einer Rechnung (mehrere Positionen)
    zusammengefasst; Zeilen ohne 'number' sind je eine eigene Rechnung."""
    import csv
    import io
    from collections import OrderedDict

    try:
        delim = csv.Sniffer().sniff(text[:2048], delimiters=",;\t").delimiter
    except Exception:
        delim = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        return {"created": [], "skipped": [], "errors": ["Leere oder ungültige CSV-Datei."]}
    # Header auf Kleinschreibung normalisieren
    fieldmap = {(fn or "").strip().lower(): fn for fn in reader.fieldnames}

    def g(row, key):
        src = fieldmap.get(key)
        return (row.get(src, "") if src else "").strip()

    groups = OrderedDict()
    loose = []
    for row in reader:
        num = g(row, "number")
        if num:
            groups.setdefault(num, []).append(row)
        else:
            loose.append([row])
    batches = list(groups.items()) + [("", grp) for grp in loose]

    created, skipped, errors = [], [], []
    for num, rows in batches:
        head = rows[0]
        try:
            kind = _norm_kind(g(head, "kind"))
            if kind is None:
                raise ValueError(f"Unbekannter Typ '{g(head, 'kind')}' "
                                 "(erlaubt: domestic/eu/third).")
            issue_date = _parse_date_any(g(head, "issue_date"))
            if not issue_date:
                raise ValueError("issue_date fehlt.")
            service_from = _parse_date_any(g(head, "service_from")) or issue_date
            service_to = _parse_date_any(g(head, "service_to"))
            paid_date = _parse_date_any(g(head, "paid_date"))
            customer = resolve_or_create_customer(
                conn, g(head, "customer_name"), g(head, "customer_address"),
                g(head, "customer_vat_id"))

            items = []
            for r in rows:
                desc = g(r, "description")
                if not desc:
                    continue
                items.append({
                    "description": desc,
                    "quantity": g(r, "quantity") or "1",
                    "unit": g(r, "unit"),
                    "unit_price": g(r, "unit_price") or "0",
                    "vat_rate": g(r, "vat_rate") or cfg["invoice"]["default_vat_rate"],
                })
            if not items:
                raise ValueError("keine Position (description) gefunden.")

            inv = create_invoice(
                conn, cfg, customer=customer, kind=kind, issue_date=issue_date,
                service_from=service_from, service_to=service_to, items=items,
                number=num or None, paid_date=paid_date, render=render)
            created.append(inv["number"])
        except ValueError as e:
            msg = str(e)
            if "existiert bereits" in msg:
                skipped.append(f"{num or '(auto)'}: {msg}")
            else:
                errors.append(f"{num or '(auto)'}: {msg}")
        except Exception as e:  # noqa
            errors.append(f"{num or '(auto)'}: {e}")
    return {"created": created, "skipped": skipped, "errors": errors}


# ── Auswertungen ─────────────────────────────────────────────────────────
def period_bounds(year: int, period: str, n: int) -> tuple[str, str, str]:
    if period == "month":
        start = date(year, n, 1)
        end = date(year + (n == 12), (n % 12) + 1, 1) - timedelta(days=1)
        return start.isoformat(), end.isoformat(), f"{year}-{n:02d}"
    start = date(year, (n - 1) * 3 + 1, 1)
    em = n * 3
    end = date(year + (em == 12), (em % 12) + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), f"{year} Q{n}"


def period_bounds_token(tok: str):
    """Zeitraum-Token -> (lo, hi, label, canonical_token). Formen: 'JJJJ' | 'JJJJ-Qn' | 'JJJJ-MM'.
    Fällt ohne/ungültigem Token auf den aktuellen Monat zurück."""
    import re
    tok = (tok or "").strip()
    m = re.match(r"^(\d{4})-Q([1-4])$", tok)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        lo, hi, _ = period_bounds(y, "quarter", q)
        return lo, hi, f"{q}. Quartal {y}", f"{y}-Q{q}"
    m = re.match(r"^(\d{4})-(\d{1,2})$", tok)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        lo, hi, _ = period_bounds(y, "month", mo)
        return lo, hi, f"{mo:02d}/{y}", f"{y}-{mo:02d}"
    m = re.match(r"^(\d{4})$", tok)
    if m:
        y = int(m.group(1))
        return f"{y}-01-01", f"{y}-12-31", f"Jahr {y}", str(y)
    t = date.today()
    lo, hi, _ = period_bounds(t.year, "month", t.month)
    return lo, hi, f"{t.month:02d}/{t.year}", f"{t.year}-{t.month:02d}"


def euer_method() -> str:
    """Aktive EÜR-Methode: 'brutto' (amtlich, wie Finanzamt – USt & Eigenverbrauch & §13b
    sind Betriebseinnahmen/-ausgaben) oder 'netto' (wirtschaftlich, USt neutral). Default brutto."""
    m = (load_settings().get("euer_method") or "brutto").lower()
    return "netto" if m == "netto" else "brutto"


def euer(conn, cfg, lo: str, hi: str) -> dict:
    """Zentrale EÜR-Berechnung (Ist) für einen Zeitraum – liefert Brutto- UND Netto-Sicht sowie
    die aktive Sicht (einnahmen/ausgaben/gewinn) je nach Schalter. Alle Beträge pro Buchung
    gerundet (deckt sich mit der DATEV/BHB-Aufstellung)."""
    def R(x):
        return round(x + 1e-9, 2)

    # EÜR = Aggregation GENAU der Journal-Zeilen (list_ledger). Keine zweite Datenlage:
    # was hier summiert wird, ist exakt das, was der Nutzer unter „Zahlungen" sieht.
    umsatz_net = umsatz_ust = eu_net = third_net = domestic_net = 0.0
    aufwand_net = vorsteuer = eig_net = eig_ust = ust_13b = business_net = sonstige = 0.0
    by_cat: dict[str, float] = {}
    for row in list_ledger(conn, lo, hi):
        b = row.get("_euer") or {}
        if not b:
            continue
        umsatz_net += b.get("umsatz_net", 0.0); umsatz_ust += b.get("umsatz_ust", 0.0)
        k = b.get("inv_kind")
        if k == "eu":
            eu_net += b.get("umsatz_net", 0.0)
        elif k == "third":
            third_net += b.get("umsatz_net", 0.0)
        elif k:
            domestic_net += b.get("umsatz_net", 0.0)
        aufwand_net += b.get("aufwand_net", 0.0); vorsteuer += b.get("vorsteuer", 0.0)
        ust_13b += b.get("ust_13b", 0.0)
        eig_net += b.get("eig_net", 0.0); eig_ust += b.get("eig_ust", 0.0)
        business_net += b.get("business_net", 0.0)
        sonstige += b.get("sonstige", 0.0)
        if "aufwand_net" in b and row["kind"] == "expense":
            by_cat[row["category"]] = by_cat.get(row["category"], 0.0) + b["aufwand_net"]

    ust_vereinnahmt = R(umsatz_ust + eig_ust + ust_13b)   # 1776 + 1787
    vorsteuer_ges = R(vorsteuer + ust_13b)                # 1576 + 1577
    betriebseinnahmen = R(umsatz_net + eig_net + sonstige + ust_vereinnahmt)
    betriebsausgaben = R(aufwand_net + vorsteuer_ges)
    ergebnis = R(betriebseinnahmen - betriebsausgaben)

    income_net = R(umsatz_net + sonstige)             # Netto-Sicht (USt neutral)
    expense_net = R(business_net)
    profit_net = R(income_net - expense_net)

    method = euer_method()
    return {
        "method": method,
        "umsatz_net": R(umsatz_net), "eu_net": R(eu_net), "third_net": R(third_net),
        "domestic_net": R(domestic_net), "umsatz_ust": R(umsatz_ust),
        "eigenverbrauch_net": R(eig_net), "eigenverbrauch_ust": R(eig_ust), "sonstige": R(sonstige),
        "aufwand_net": R(aufwand_net), "vorsteuer": R(vorsteuer), "ust_13b": R(ust_13b),
        "ust_vereinnahmt": ust_vereinnahmt, "vorsteuer_ges": vorsteuer_ges,
        "betriebseinnahmen": betriebseinnahmen, "betriebsausgaben": betriebsausgaben, "ergebnis": ergebnis,
        "income_net": income_net, "expense_net": expense_net, "profit_net": profit_net,
        "by_cat": sorted(by_cat.items(), key=lambda x: -x[1]),
        "einnahmen": betriebseinnahmen if method == "brutto" else income_net,
        "ausgaben": betriebsausgaben if method == "brutto" else expense_net,
        "gewinn": ergebnis if method == "brutto" else profit_net,
    }


def figures_for_period(conn, cfg, tok: str) -> dict:
    """Einnahmen/Ausgaben/Gewinn + USt-Zahllast (Ist) für einen beliebigen Zeitraum-Token.
    Einnahmen/Ausgaben/Gewinn folgen der aktiven EÜR-Methode (Brutto=amtlich / Netto)."""
    lo, hi, label, canon = period_bounds_token(tok)
    e = euer(conn, cfg, lo, hi)
    vf = _vat_figures(conn, cfg, lo, hi)
    return {"zeitraum": label, "token": canon, "methode": e["method"],
            # aktive Sicht (je nach Schalter) – das ist die "richtige" Antwort auf "Einnahmen/Gewinn"
            "einnahmen": e["einnahmen"], "ausgaben": e["ausgaben"], "gewinn": e["gewinn"],
            # beide Sichten explizit, damit NICHTS selbst gerechnet werden muss:
            "einnahmen_brutto": e["betriebseinnahmen"], "einnahmen_netto": e["income_net"],
            "ausgaben_brutto": e["betriebsausgaben"], "ausgaben_netto": e["expense_net"],
            "gewinn_brutto": e["ergebnis"], "gewinn_netto": e["profit_net"],
            "umsatzerloese_netto": e["umsatz_net"], "vereinnahmte_ust": e["ust_vereinnahmt"],
            "ust_zahllast": round(float(vf["zahllast"]), 2)}


def activity_years(conn) -> list:
    """Nur Jahre MIT Daten (Rechnung/Ausgabe/Zahlung), aufsteigend – keine leeren Jahre."""
    yrs = set()
    for tbl, col in (("invoices", "paid_date"), ("expenses", "paid_date"), ("transactions", "date")):
        for r in conn.execute(f"SELECT DISTINCT substr({col},1,4) AS y FROM {tbl} WHERE {col} IS NOT NULL"):
            if r["y"] and str(r["y"]).isdigit():
                yrs.add(int(r["y"]))
    return sorted(yrs)


def yearly_overview(conn, cfg) -> list:
    """Einnahmen/Ausgaben/Gewinn je Jahr (nur Jahre mit Daten) – für Jahr-für-Jahr-Ansicht/Graph."""
    out = []
    for y in activity_years(conn):
        e = euer(conn, cfg, f"{y}-01-01", f"{y}-12-31")
        out.append({"jahr": y, "einnahmen": e["einnahmen"], "ausgaben": e["ausgaben"],
                    "gewinn": e["gewinn"], "einnahmen_netto": e["income_net"],
                    "einnahmen_brutto": e["betriebseinnahmen"]})
    return out


# ── Steuerrücklage: Einkommensteuer (§32a EStG) + USt ────────────────────
# Amtliche Tarifformel je Veranlagungsjahr (Grundfreibetrag, Zonengrenzen, Koeffizienten).
# Quelle: §32a Abs. 1 EStG (gesetze-im-internet.de).
_EST_TARIF = {
    2026: dict(gfb=12348, z2=17799, z3=69878, z4=277825,
               z2a=914.51, z2b=1400.0, z3a=173.10, z3b=2397.0, z3c=1034.87,
               z4m=0.42, z4c=11135.63, z5m=0.45, z5c=19470.38),
    2025: dict(gfb=12096, z2=17443, z3=68480, z4=277825,
               z2a=932.30, z2b=1400.0, z3a=176.64, z3b=2397.0, z3c=1015.13,
               z4m=0.42, z4c=10911.92, z5m=0.45, z5c=19246.67),
    2024: dict(gfb=11604, z2=17005, z3=66760, z4=277825,
               z2a=922.98, z2b=1400.0, z3a=181.19, z3b=2397.0, z3c=1025.38,
               z4m=0.42, z4c=10602.13, z5m=0.45, z5c=18936.88),
}
# Soli-Freigrenze (Höhe der ESt) – Einzelveranlagung; Zusammen = 2×. Für Freiberufler i. d. R. 0.
_SOLI_FREIGRENZE = {2024: 18130, 2025: 19950, 2026: 20350}


def _est_grundtarif(zve: float, year: int) -> float:
    """Einkommensteuer im Grundtarif nach §32a Abs. 1 EStG. zvE wird auf volle Euro
    abgerundet, das Ergebnis ebenfalls (amtliche Rundung)."""
    t = _EST_TARIF.get(year) or _EST_TARIF[max(_EST_TARIF)]
    x = int(max(0.0, zve))                       # zvE auf vollen Euro abrunden
    if x <= t["gfb"]:
        return 0.0
    if x <= t["z2"]:
        y = (x - t["gfb"]) / 10000
        est = (t["z2a"] * y + t["z2b"]) * y
    elif x <= t["z3"]:
        z = (x - t["z2"]) / 10000
        est = (t["z3a"] * z + t["z3b"]) * z + t["z3c"]
    elif x <= t["z4"]:
        est = t["z4m"] * x - t["z4c"]
    else:
        est = t["z5m"] * x - t["z5c"]
    return float(int(est))                        # ESt auf vollen Euro abrunden


def est_tarif(zve: float, year: int, splitting: bool = False) -> float:
    """Tarifliche Einkommensteuer. Bei Zusammenveranlagung Splitting: 2 × Tarif(zvE/2)."""
    if splitting:
        return 2.0 * _est_grundtarif(zve / 2.0, year)
    return _est_grundtarif(zve, year)


def _soli(est: float, year: int, splitting: bool) -> float:
    """Solidaritätszuschlag: 5,5 % der ESt, erst über der Freigrenze (Milderungszone 11,9 %)."""
    if est <= 0:
        return 0.0
    fg = _SOLI_FREIGRENZE.get(year, _SOLI_FREIGRENZE[max(_SOLI_FREIGRENZE)]) * (2 if splitting else 1)
    if est <= fg:
        return 0.0
    return round(min(0.055 * est, 0.119 * (est - fg)), 2)


_HOCHRECHNUNG_AB_TAGEN = 60       # vorher ist die Basis zu dünn für eine Jahresprognose


def _jahresanteil(year: int, today: date | None = None) -> float:
    """Anteil des Jahres, der bisher verstrichen ist (1.0 für abgelaufene Jahre)."""
    today = today or date.today()
    if year != today.year:
        return 1.0
    tage = (today - date(year, 1, 1)).days + 1
    im_jahr = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    return tage / im_jahr


def income_tax_estimate(conn, cfg, year: int, gewinn_jahr: float | None = None) -> dict:
    """Geschätzte Einkommensteuer (+ optional Soli, Kirchensteuer) für das ganze Jahr.
    Beim laufenden Jahr wird der bisherige EÜR-Gewinn auf das Gesamtjahr hochgerechnet
    ('läuft weiter wie bisher'), weil der §32a-Tarif progressiv ist und eine Steuer auf den
    Zwischenstand die Jahreslast systematisch unterschätzt.
    'gewinn_jahr' überschreibt diese Prognose mit einem Planwert für das GANZE Jahr
    (z. B. "ab Juli monatlich 4.500 € netto") – dann wird nichts hochgerechnet und die
    Rücklage ist die volle Jahressteuer, nicht der bisher verdiente Anteil.
    Annahme: keine weiteren Einkünfte außer 'est_weitere_einkuenfte' aus den Einstellungen.
    zvE = Gewinn + weitere Einkünfte − Krankenversicherung − Sonderausgaben-Pauschbetrag."""
    s = load_settings()
    gewinn = euer(conn, cfg, f"{year}-01-01", f"{year}-12-31")["gewinn"]
    anteil = _jahresanteil(year)
    tage = round(anteil * 365)
    vorgabe = gewinn_jahr is not None
    hochgerechnet = not vorgabe and anteil < 1.0 and tage >= _HOCHRECHNUNG_AB_TAGEN
    if vorgabe:
        gewinn_jahr = round(float(gewinn_jahr), 2)        # Planwert, gilt fürs ganze Jahr
    else:
        gewinn_jahr = round(gewinn / anteil, 2) if hochgerechnet else gewinn
    splitting = (s.get("est_veranlagung") == "zusammen")
    kv = float(s.get("est_krankenversicherung") or 0)
    weitere = float(s.get("est_weitere_einkuenfte") or 0)
    pausch = 72 if splitting else 36              # Sonderausgaben-Pauschbetrag
    zve = max(0.0, gewinn_jahr + weitere - kv - pausch)
    est = est_tarif(zve, year, splitting)
    soli = _soli(est, year, splitting) if s.get("est_soli") else 0.0
    kist_satz = float(s.get("est_kirchensteuer") or 0)
    kist = round(est * kist_satz / 100, 2)
    gesamt = round(est + soli + kist, 2)
    # Anteilige Rücklage 'heute': der bisher verdiente Teil der prognostizierten Jahressteuer.
    ruecklage = round(gesamt * anteil, 2) if hochgerechnet else gesamt
    # Vom Finanzamt festgesetzte Vorauszahlung je Quartal (nicht berechenbar → aus Einstellungen)
    vz = s.get("est_vorauszahlung")
    vz_q = float(vz) if vz not in (None, "") else None
    vz_jahr = round(vz_q * 4, 2) if vz_q is not None else None
    # Differenz: was du laut §32a fürs Jahr schuldest − was du per Vorauszahlung abführst
    # > 0 = voraussichtliche Nachzahlung, < 0 = voraussichtliche Erstattung
    differenz = round(gesamt - vz_jahr, 2) if vz_jahr is not None else None
    return {"year": year, "gewinn": gewinn, "gewinn_jahr": gewinn_jahr, "zve": zve,
            "anteil": round(anteil, 4), "hochgerechnet": hochgerechnet, "tage": tage,
            "vorgabe": vorgabe,
            "splitting": splitting,
            "est": round(est, 2), "soli": round(soli, 2), "kirchensteuer": kist,
            "kist_satz": kist_satz, "krankenversicherung": kv, "weitere_einkuenfte": weitere,
            "gesamt": gesamt, "ruecklage": ruecklage,
            "vorauszahlung_q": vz_q, "vorauszahlung_jahr": vz_jahr,
            "differenz": differenz}


def tax_reserve(conn, cfg=None, year: int | None = None,
                gewinn_jahr: float | None = None) -> dict:
    """Empfohlene Steuerrücklage 'heute': USt-Zahllast des laufenden Quartals (exakt) +
    anteilige Einkommensteuer des laufenden Jahres (§32a auf den hochgerechneten Gewinn).
    'year' rechnet ein anderes Jahr (dann Q4 als USt-Quartal), 'gewinn_jahr' setzt einen
    geplanten Jahresgewinn statt der Hochrechnung aus den Buchungen."""
    cfg = cfg or load_config()
    today = date.today()
    year = int(year) if year else today.year
    q = (today.month - 1) // 3 + 1 if year == today.year else 4
    qlo, qhi, _ = period_bounds(year, "quarter", q)
    ust = round(float(_vat_figures(conn, cfg, qlo, qhi)["zahllast"]), 2)
    est = income_tax_estimate(conn, cfg, year, gewinn_jahr=gewinn_jahr)
    return {"year": year, "quarter": q, "ust": ust, "ust_label": f"Q{q} {year}",
            "est": est, "gesamt": round(ust + est["ruecklage"], 2)}


def upcoming_deadlines(conn, cfg=None, limit=3) -> list:
    """Nächste Steuer-Termine ab heute: USt-Voranmeldung (exakt, Zahllast des Quartals) und
    Einkommensteuer-Vorauszahlung (Betrag laut Finanzamt-Bescheid aus den Einstellungen, sonst
    geschätzt Jahres-ESt / 4; Termine 10.03/06/09/12). Je Termin: 'due_soon' (≤10 Tage) und
    'acked' (vom Nutzer abgehakt) für die Fälligkeits-Markierung."""
    cfg = cfg or load_config()
    s = load_settings()
    acked = set(s.get("tax_ack") or [])
    today = date.today()
    out = []

    def ustva_due(qy, q):                          # fällig 10. des Monats nach Quartalsende
        m = q * 3 + 1
        return date(qy + 1, m - 12, 10) if m > 12 else date(qy, m, 10)

    # USt-Voranmeldung: nächster Quartals-Stichtag
    cands = [(today.year, q) for q in (1, 2, 3, 4)] + [(today.year + 1, q) for q in (1, 2)]
    for qy, q in cands:
        due = ustva_due(qy, q)
        if due < today:
            continue
        qlo, qhi, _ = period_bounds(qy, "quarter", q)
        zahllast = round(float(_vat_figures(conn, cfg, qlo, qhi)["zahllast"]), 2)
        laeuft = qhi >= today.isoformat()          # Quartal noch nicht abgeschlossen
        out.append({"due": due, "kind": "ustva", "key": f"ustva:{qy}-Q{q}",
                    "title": "USt-Voranmeldung", "sub": f"Q{q} {qy}" + (" · läuft" if laeuft else ""),
                    "amount": zahllast, "estimate": laeuft})
        break

    # ESt-Vorauszahlung: nächster Termin; Betrag laut Finanzamt-Bescheid, sonst Schätzung
    vz = s.get("est_vorauszahlung")
    vz_q = float(vz) if vz not in (None, "") else None
    if vz_q is not None:
        est_amount, est_est = round(vz_q, 2), False
    else:
        est_amount = round(income_tax_estimate(conn, cfg, today.year)["gesamt"] / 4, 2)
        est_est = True
    est_due = next((date(today.year, m, 10) for m in (3, 6, 9, 12)
                    if date(today.year, m, 10) >= today), date(today.year + 1, 3, 10))
    out.append({"due": est_due, "kind": "est", "key": f"est:{est_due.isoformat()}",
                "title": "ESt-Vorauszahlung",
                "sub": f"{est_due.year} · " + ("Schätzung" if est_est else "laut Finanzamt"),
                "amount": est_amount, "estimate": est_est})

    out.sort(key=lambda x: x["due"])
    mon = ["JAN", "FEB", "MRZ", "APR", "MAI", "JUN", "JUL", "AUG", "SEP", "OKT", "NOV", "DEZ"]
    for it in out:
        d = it.pop("due")
        it["date"], it["day"], it["mon"] = d.isoformat(), f"{d.day:02d}", mon[d.month - 1]
        it["days_until"] = (d - today).days
        it["due_soon"] = 0 <= it["days_until"] <= 10
        it["acked"] = it["key"] in acked
    return out[:limit]


def ack_deadline(key: str, on: bool = True) -> list:
    """Steuertermin ab-/anhaken – entfernt bzw. setzt die rote Fälligkeits-Markierung.
    Gibt die aktuelle Liste abgehakter Termin-Keys zurück."""
    s = load_settings()
    acks = set(s.get("tax_ack") or [])
    acks.add(key) if on else acks.discard(key)
    s["tax_ack"] = sorted(acks)
    save_settings(s)
    return s["tax_ack"]


def sonstige_einnahmen(conn, lo: str, hi: str) -> float:
    """Summe der als 'einnahme_sonstige' kategorisierten Zahlungen (Bar/Wallet/ohne Rechnung)
    im Zeitraum, in Heimwährung. Zählt als Betriebseinnahme in der EÜR (i. d. R. ohne USt)."""
    r = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(amount_home, amount)), 0) AS s "
        "FROM transactions WHERE category = 'einnahme_sonstige' AND date BETWEEN ? AND ?",
        (lo, hi)).fetchone()
    return float(r["s"] or 0)


def report_data(conn, year: int) -> dict:
    """EÜR für ein Jahr – amtliche Brutto-Struktur (Betriebseinnahmen/-ausgaben/Ergebnis)
    plus Netto-Sicht. Deckt sich mit der Anlage EÜR / BuchhaltungsButler."""
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    e = euer(conn, None, lo, hi)
    return {
        "year": year, "method": e["method"],
        # Brutto (amtlich)
        "betriebseinnahmen": e["betriebseinnahmen"], "betriebsausgaben": e["betriebsausgaben"],
        "ergebnis": e["ergebnis"],
        "umsatz_net": e["umsatz_net"], "eigenverbrauch_net": e["eigenverbrauch_net"],
        "ust_vereinnahmt": e["ust_vereinnahmt"], "vorsteuer_ges": e["vorsteuer_ges"],
        "aufwand_net": e["aufwand_net"], "ust_13b": e["ust_13b"], "sonstige": e["sonstige"],
        "eu_net": e["eu_net"], "third_net": e["third_net"], "domestic_net": e["domestic_net"],
        "by_cat": e["by_cat"],
        # Netto (wirtschaftlich) + aktive Sicht
        "income_net": e["income_net"], "expense_net": e["expense_net"], "profit_net": e["profit_net"],
        "einnahmen": e["einnahmen"], "ausgaben": e["ausgaben"], "gewinn": e["gewinn"],
        # Kompat
        "profit": e["ergebnis"] if e["method"] == "brutto" else e["profit_net"],
    }


def _sondervorauszahlung(cfg, lo: str, hi: str):
    """Kz 39 – Anrechnung der Sondervorauszahlung. Die gibt es NUR bei monatlicher Abgabe mit
    Dauerfristverlängerung (Quartalszahler bekommen die Fristverlängerung ohne Sondervoraus-
    zahlung) und angerechnet wird sie in der Voranmeldung für Dezember."""
    tax = cfg.get("tax", {}) if cfg else {}
    if tax.get("va_period", "quarter") != "month" or not tax.get("dauerfrist"):
        return Decimal(0)
    if not (lo.endswith("-12-01") and hi.endswith("-12-31")):
        return Decimal(0)
    return money(Decimal(str(tax.get("sondervorauszahlung", 0) or 0)))


def _vat_figures(conn, cfg, lo: str, hi: str) -> dict:
    """USt-Kennzahlen für einen beliebigen Zeitraum (Ist: nach Zahlung, Soll: nach Datum).
    Die Schlüssel 'kz' enthalten die Felder der Voranmeldung mit ihrer amtlichen Kennzahl."""
    taxation = cfg["tax"].get("taxation", "ist")
    date_col = "paid_date" if taxation == "ist" else "issue_date"
    inv_where = f"i.{date_col} BETWEEN ? AND ?"
    if taxation == "ist":
        inv_where += " AND i.paid_date IS NOT NULL"

    # ── Ausgangsseite: steuerpflichtige Inlandsumsätze nach Steuersatz (Kz 81/86) ──
    rows = conn.execute(
        f"""SELECT it.vat_rate AS rate, SUM(it.line_net) AS net
            FROM invoice_items it JOIN invoices i ON i.id = it.invoice_id
            WHERE {inv_where} AND i.kind = 'domestic'
              AND COALESCE(i.supply_type, 'service') != 'exempt'
            GROUP BY it.vat_rate""",
        (lo, hi),
    ).fetchall()
    net_by_rate = {int(r["rate"]): money(r["net"] or 0) for r in rows}
    net19, net7 = net_by_rate.get(19, Decimal(0)), net_by_rate.get(7, Decimal(0))
    ust19, ust7 = money(net19 * Decimal("0.19")), money(net7 * Decimal("0.07"))

    # Steuerfreie / nicht steuerbare Umsätze – Kennzahl aus Rechnungstyp + Leistungsart.
    supply: dict[str, Decimal] = {}
    for r in conn.execute(
            f"""SELECT i.kind AS kind, COALESCE(i.supply_type, 'service') AS st,
                       COALESCE(SUM(i.net), 0) AS net
                FROM invoices i WHERE {inv_where} GROUP BY i.kind, st""", (lo, hi)):
        hit = SUPPLY_KZ.get((r["kind"], r["st"]))
        if hit:
            supply[hit[0]] = supply.get(hit[0], Decimal(0)) + money(r["net"])

    # ── Eingangsseite ──
    exp_col = "paid_date" if taxation == "ist" else "date"
    exp_where = f"{exp_col} BETWEEN ? AND ?"
    if taxation == "ist":
        exp_where += " AND paid_date IS NOT NULL"
    vorsteuer = import_vst = rc_vst = ig_vst = Decimal(0)
    rc = {k: [Decimal(0), Decimal(0)] for k in RC_KINDS}      # art -> [Bemessung, Steuer]
    ig = {19: [Decimal(0), Decimal(0)], 7: [Decimal(0), Decimal(0)]}
    for e in conn.execute(f"SELECT * FROM expenses WHERE {exp_where}", (lo, hi)):
        vk = expense_vat_kind(e)
        if vk == "domestic":
            vorsteuer += money(e["vat"] or 0)
            continue
        if vk == "import":
            # Die am Zoll entrichtete EUSt ist echte gezahlte Steuer – nur ein anderes Feld.
            import_vst += money(e["vat"] or 0)
            continue
        gf = money(e["gross_full"] or (Decimal(str(e["net"] or 0)) + Decimal(str(e["vat"] or 0))))
        rate = expense_tax_rate(e)
        # Steuer entsteht auf das volle Entgelt; abziehbar ist nur der betriebliche Anteil.
        frac = (Decimal(100) - Decimal(str(e["private_share"] or 0))) / Decimal(100)
        tax = money(gf * Decimal(rate) / 100)
        if vk in RC_KINDS:
            rc[vk][0] += gf
            rc[vk][1] += tax
            rc_vst += money(tax * frac)
        else:                                                  # ig_erwerb
            slot = ig.get(rate) or ig.setdefault(rate, [Decimal(0), Decimal(0)])
            slot[0] += gf
            slot[1] += tax
            ig_vst += money(tax * frac)

    rc_tax = money(sum(v[1] for v in rc.values()))
    ig_tax = money(sum(v[1] for v in ig.values()))
    kz39 = _sondervorauszahlung(cfg, lo, hi)
    zahllast = money(ust19 + ust7 + rc_tax + ig_tax
                     - vorsteuer - rc_vst - ig_vst - import_vst - kz39)
    eu_net, third_net = supply.get("21", Decimal(0)), supply.get("45", Decimal(0))
    return {
        "taxation": taxation, "net19": net19, "ust19": ust19, "net7": net7, "ust7": ust7,
        "eu_net": eu_net, "third_net": third_net, "vorsteuer": vorsteuer, "zahllast": zahllast,
        # Ausgangsseite, steuerfrei / nicht steuerbar
        "ig_lieferung_net": supply.get("41", Decimal(0)),
        "ausfuhr_net": supply.get("43", Decimal(0)),
        "steuerfrei_net": supply.get("48", Decimal(0)),
        # § 13b als Leistungsempfänger
        "rc_eu_base": rc["rc_eu"][0], "rc_eu_tax": rc["rc_eu"][1],
        "rc_other_base": rc["rc_other"][0], "rc_other_tax": rc["rc_other"][1],
        "rc_open_base": rc["rc_unknown"][0], "rc_open_tax": rc["rc_unknown"][1],
        "rc_vst": rc_vst,
        # Innergemeinschaftliche Erwerbe
        "ig19_base": ig[19][0], "ig19_tax": ig[19][1],
        "ig7_base": ig[7][0], "ig7_tax": ig[7][1], "ig_vst": ig_vst,
        "import_vst": import_vst, "sondervorauszahlung": kz39,
    }


def vat_open_rc(conn, lo: str, hi: str) -> list:
    """§13b-Belege, denen die Zuordnung EU (Kz 46/47) vs. Drittland (Kz 84/85) noch fehlt.
    Ohne sie lässt sich die Voranmeldung nicht vollständig ausfüllen."""
    return conn.execute(
        "SELECT id, date, paid_date, vendor, gross_full, vat_rate FROM expenses "
        "WHERE vat_kind = 'rc_unknown' AND COALESCE(paid_date, date) BETWEEN ? AND ? "
        "ORDER BY COALESCE(paid_date, date), id", (lo, hi)).fetchall()


def vat_kz_rows(fig: dict) -> list:
    """Die USt-VA als Liste von Zeilen (Kennzahl, Bezeichnung, Bemessung, Kz-Steuer, Steuer).
    Eine Quelle für CLI, Web und Chat – damit überall dieselben Felder erscheinen.

    Bemessung ist None, wo das Formularfeld gar keine Bemessungsgrundlage kennt: die
    Vorsteuerfelder (66/61/62/67) und die Sondervorauszahlung (39) sind reine Betragsfelder."""
    z = Decimal(0)
    out = [
        ("81", "Steuerpflichtige Umsätze 19 %", fig["net19"], "", fig["ust19"]),
        ("86", "Steuerpflichtige Umsätze 7 %", fig["net7"], "", fig["ust7"]),
        ("41", "Innergemeinschaftliche Lieferungen", fig["ig_lieferung_net"], "", None),
        ("43", "Steuerfreie Ausfuhrlieferungen", fig["ausfuhr_net"], "", None),
        ("48", "Steuerfreie Umsätze ohne Vorsteuerabzug", fig["steuerfrei_net"], "", None),
        ("21", "Nicht steuerbare sonstige Leistungen (EU, ZM)", fig["eu_net"], "", None),
        ("45", "Übrige nicht steuerbare Umsätze (Drittland)", fig["third_net"], "", None),
        ("46", "Leistungen EU-Unternehmer § 13b Abs. 1", fig["rc_eu_base"], "47", fig["rc_eu_tax"]),
        ("84", "Andere Leistungen § 13b Abs. 2", fig["rc_other_base"], "85", fig["rc_other_tax"]),
        # Der Betrag steht fest, nur das Feld ist offen (EU und Drittland kosten dasselbe).
        # Als eigene Zeile sichtbar, damit die Aufstellung zur Zahllast aufgeht.
        ("46/84", "§ 13b – Feld noch offen (EU oder Drittland?)",
         fig["rc_open_base"], "47/85", fig["rc_open_tax"]),
        ("89", "Innergem. Erwerbe 19 %", fig["ig19_base"], "", fig["ig19_tax"]),
        ("93", "Innergem. Erwerbe 7 %", fig["ig7_base"], "", fig["ig7_tax"]),
        # Reine Betragsfelder: keine Bemessungsgrundlage, der Wert IST der Steuerbetrag.
        ("66", "Vorsteuer aus Rechnungen", None, "", fig["vorsteuer"]),
        ("61", "Vorsteuer aus innergem. Erwerben", None, "", fig["ig_vst"]),
        ("62", "Entrichtete Einfuhrumsatzsteuer", None, "", fig["import_vst"]),
        ("67", "Vorsteuer aus Leistungen § 13b", None, "", fig["rc_vst"]),
        ("39", "Anrechnung Sondervorauszahlung", None, "", fig["sondervorauszahlung"]),
    ]
    # Kz 81/86 und 66 immer zeigen (das Grundgerüst), den Rest nur wenn er Werte trägt.
    keep = {"81", "86", "66"}
    return [r for r in out if r[0] in keep or (r[2] or z) != z or (r[4] or z) != z]


def vat_data(conn, cfg, year: int, n: int) -> dict:
    period = cfg["tax"].get("va_period", "quarter")
    lo, hi, label = period_bounds(year, period, n)
    fig = _vat_figures(conn, cfg, lo, hi)
    fig.update({"label": label, "lo": lo, "hi": hi})
    return fig


def vat_annual(conn, cfg, year: int) -> dict:
    """USt-Jahresabschluss: Jahressummen + Vergleich mit den vier Quartals-Vorauszahlungen."""
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    fig = _vat_figures(conn, cfg, lo, hi)
    quarters, prepaid = [], Decimal(0)
    for q in range(1, 5):
        qlo, qhi, _ = period_bounds(year, "quarter", q)
        z = _vat_figures(conn, cfg, qlo, qhi)["zahllast"]
        quarters.append({"q": q, "zahllast": z})
        prepaid += z
    fig.update({"year": year, "lo": lo, "hi": hi, "quarters": quarters,
                "prepaid": money(prepaid), "final": money(fig["zahllast"] - prepaid)})
    return fig


def set_invoice_pdf(conn, invoice_id: int, data: bytes, filename: str = "") -> "Path":
    inv = get_invoice(conn, invoice_id)
    dest = beleg_dir("ausgang", inv["paid_date"] or inv["issue_date"]) / f"Invoice-{inv['number']}.pdf"
    dest.write_bytes(data)
    conn.execute("UPDATE invoices SET pdf_path = ? WHERE id = ?", (rel_pfad(dest), invoice_id))
    conn.commit()
    return dest


def regenerate_invoice_pdf(conn, cfg, invoice_id: int) -> "Path":
    """PDF einer bestehenden Rechnung neu erzeugen (z. B. nach geändertem Briefkopf)."""
    inv = get_invoice(conn, invoice_id)
    if not inv:
        raise ValueError("Rechnung nicht gefunden.")
    items = get_invoice_items(conn, invoice_id)
    if not items:
        raise ValueError(f"Rechnung {inv['number']} hat keine Positionen.")
    pdf_path = render_invoice_pdf(cfg, inv, items)
    conn.execute("UPDATE invoices SET pdf_path = ? WHERE id = ?", (rel_pfad(pdf_path), invoice_id))
    conn.commit()
    return pdf_path


def zm_data(conn, year: int, n: int) -> dict:
    """Zusammenfassende Meldung. Warenlieferungen und sonstige Leistungen sind getrennte
    Meldearten ('L' bzw. 'S') und werden deshalb je Kunde getrennt ausgewiesen."""
    lo, hi, label = period_bounds(year, "quarter", n)
    rows = conn.execute(
        """SELECT customer_name, customer_vat_id,
                  CASE WHEN COALESCE(supply_type,'service') = 'goods' THEN 'L' ELSE 'S' END AS art,
                  COALESCE(SUM(net),0) AS net, COUNT(*) AS cnt
           FROM invoices WHERE kind = 'eu' AND COALESCE(supply_type,'service') != 'exempt'
             AND issue_date BETWEEN ? AND ?
           GROUP BY customer_vat_id, customer_name, art ORDER BY customer_name, art""",
        (lo, hi),
    ).fetchall()
    total = money(sum(Decimal(str(r["net"])) for r in rows)) if rows else Decimal(0)
    missing = any(not r["customer_vat_id"] for r in rows)
    deadline = {1: "25.04.", 2: "25.07.", 3: "25.10.", 4: "25.01. (Folgejahr)"}[n]
    return {"label": label, "lo": lo, "hi": hi, "rows": rows,
            "total": total, "missing": missing, "deadline": deadline}


# ── PDF-Rendering ────────────────────────────────────────────────────────
LABELS = {
    "en": dict(headers=["Pos.", "Service", "Amount", "Unit", "Unit Price (Net)", "VAT", "Total"],
               total="Total", invoice="Invoice", cust_id="Customer ID",
               payment="Payment within {days} days of receipt of the invoice without "
                       "deductions to the bank account provided below."),
    "de": dict(headers=["Pos.", "Leistung", "Menge", "Einheit", "Einzelpreis (netto)", "USt", "Gesamt"],
               total="Gesamt", invoice="Rechnung", cust_id="Kundennr.",
               payment="Zahlbar innerhalb von {days} Tagen nach Rechnungserhalt ohne Abzug "
                       "auf das unten genannte Konto."),
}


def render_invoice_pdf(cfg, invoice, items) -> Path:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    from fpdf.fonts import FontFace

    biz, bank, inv = cfg["business"], cfg["bank"], cfg["invoice"]
    lang = inv.get("language", "en")
    L = LABELS.get(lang, LABELS["en"])
    kind = invoice["kind"]
    supply = _supply_type(_col(invoice, "supply_type"))
    reverse = kind in ("eu", "third") or supply == "exempt"

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(20, 18, 20)
    family, fallback = _register_font(pdf)

    def T(s) -> str:
        return _sanitize(str(s)) if fallback else str(s)

    def at(x, y, text, size=10, style="", align="L", w=0, h=5):
        pdf.set_xy(x, y)
        pdf.set_font(family, style, size)
        pdf.cell(w, h, T(text), align=align)

    def para(h, text, size=9, style=""):
        pdf.set_x(pdf.l_margin)
        pdf.set_font(family, style, size)
        pdf.multi_cell(0, h, T(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    lm = pdf.l_margin
    right_x = 120

    sender_line = f"{biz.get('owner', biz['name'])}, " + ", ".join(biz["address_lines"])
    at(lm, 20, sender_line, size=7, style="U")

    y = 30
    at(lm, y, invoice["customer_name"], size=11); y += 6
    for line in (invoice["customer_address"] or "").split("\n"):
        at(lm, y, line, size=11); y += 6
    if invoice["customer_vat_id"]:
        vat_label = "USt-IdNr." if kind == "eu" else "VAT No."
        at(lm, y, f"{vat_label}: {invoice['customer_vat_id']}", size=9); y += 6

    ry = 28
    at(right_x, ry, biz["name"], size=9, style="B"); ry += 6
    for line in biz["address_lines"]:
        at(right_x, ry, line, size=10); ry += 5
    if biz.get("email"):
        at(right_x, ry, f"E-Mail: {biz['email']}", size=9); ry += 5
    if biz.get("vat_id"):
        at(right_x, ry, f"USt-IdNr.: {biz['vat_id']}", size=9); ry += 5

    band_y = max(y, ry) + 10
    if invoice["service_to"] and invoice["service_to"] != invoice["service_from"]:
        service_str = f"{de_date(invoice['service_from'])}-{de_date(invoice['service_to'])}"
    else:
        service_str = de_date(invoice["service_from"])

    for x, label, value in [
        (lm, L["cust_id"], str(invoice["customer_id"] or "-")),
        (lm + 55, "Liefer-/Leistungsdatum /\nDelivered by", service_str),
        (lm + 115, "Rechnungsdatum / Date", de_date(invoice["issue_date"])),
    ]:
        pdf.set_xy(x, band_y)
        pdf.set_font(family, "B", 9)
        pdf.multi_cell(52, 4.5, T(label), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_x(x)
        pdf.set_font(family, "", 9)
        pdf.multi_cell(52, 4.5, T(value))

    pdf.set_xy(lm, band_y + 22)
    para(5, inv.get("intro", ""), size=10)
    pdf.ln(2)
    para(8, f"{L['invoice']} {invoice['number']}", size=15, style="B")
    pdf.ln(1)

    headings = FontFace(emphasis="BOLD")
    pdf.set_font(family, "", 9)
    with pdf.table(
        col_widths=(9, 42, 12, 10, 22, 13, 20),
        text_align=("CENTER", "LEFT", "CENTER", "CENTER", "RIGHT", "CENTER", "RIGHT"),
        headings_style=headings, line_height=6,
    ) as table:
        table.row([T(h) for h in L["headers"]])
        for idx, i in enumerate(items, 1):
            rate = i["vat_rate"]
            if reverse or rate == 0:
                vat_cell, line_total = "0%", i["line_net"]
            else:
                vat_amt = money(Decimal(str(i["line_net"])) * Decimal(rate) / 100)
                vat_cell = f"{rate}%\n{cur(vat_amt)}"
                line_total = money(Decimal(str(i["line_net"])) + vat_amt)
            table.row([
                T(f"{idx}."), T(str(i["description"])),
                T(f"{Decimal(str(i['quantity'])):g}"), T(str(i["unit"] or "-")),
                T(cur(i["unit_price"])), T(vat_cell), T(cur(line_total)),
            ])

    pdf.ln(1)
    pdf.set_font(family, "B", 10)
    pdf.cell(pdf.epw - 40, 6, T(L["total"]), align="R")
    pdf.cell(40, 6, T(cur(invoice["gross"])), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)

    para(5, L["payment"].format(days=cfg["invoice"].get("payment_terms_days", 14)), size=9)
    pdf.ln(1)
    # Der Pflichthinweis hängt am Tatbestand: Warenlieferung ins EU-Ausland ist eine steuerfreie
    # innergemeinschaftliche Lieferung, keine Reverse-Charge-Leistung.
    if supply == "exempt":
        para(5, inv.get("exempt_note", "Steuerfrei nach § 4 UStG."), size=9); pdf.ln(1)
    elif kind == "eu" and supply == "goods":
        para(5, inv.get("ig_delivery_note",
                        "Steuerfreie innergemeinschaftliche Lieferung (§ 4 Nr. 1b i. V. m. § 6a UStG)."),
             size=9); pdf.ln(1)
    elif kind == "eu":
        para(5, inv.get("reverse_charge_note", "Reverse charge applies."), size=9); pdf.ln(1)
    elif kind == "third" and supply == "goods":
        para(5, inv.get("export_note",
                        "Steuerfreie Ausfuhrlieferung (§ 4 Nr. 1a i. V. m. § 6 UStG)."),
             size=9); pdf.ln(1)
    elif kind == "third":
        para(5, inv.get("third_country_note", "Not subject to German VAT."), size=9); pdf.ln(1)
    if inv.get("closing"):
        para(5, inv["closing"], size=9)

    pdf.ln(6)
    para(5, inv.get("signoff", "Best regards,"), size=9)
    sig = inv.get("signature_path")
    if sig and Path(sig).expanduser().exists():
        pdf.image(str(Path(sig).expanduser()), w=35); pdf.ln(1)
    else:
        pdf.ln(8)
    para(5, biz.get("owner", biz["name"]), size=9)

    pdf.set_auto_page_break(auto=False)
    fy = max(pdf.get_y() + 8, 248)
    at(lm, fy, biz["name"], size=8); fy2 = fy + 4
    for line in biz["address_lines"]:
        at(lm, fy2, line, size=8); fy2 += 4
    if biz.get("email"):
        at(lm, fy2, f"E-Mail: {biz['email']}", size=8); fy2 += 4
    if biz.get("vat_id"):
        at(lm, fy2, f"USt-IdNr.: {biz['vat_id']}", size=8); fy2 += 4

    by = fy
    for tag in ("Local", "SWIFT"):
        at(right_x, by, tag, size=8, style="B"); by += 4
        at(right_x, by, bank["holder"], size=8); by += 4
        at(right_x, by, f"IBAN: {bank['iban']}", size=8); by += 4
        at(right_x, by, f"BIC: {bank['bic']}", size=8); by += 5

    _idate = invoice["paid_date"] or invoice["issue_date"] if hasattr(invoice, "keys") else None
    out = beleg_dir("ausgang", _idate) / f"Invoice-{invoice['number']}.pdf"
    pdf.output(str(out))
    return out
