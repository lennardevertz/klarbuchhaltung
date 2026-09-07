#!/usr/bin/env python3
"""
Anbieter-unabhängiger LLM-Chat für die Buchhaltung.

Spricht das OpenAI-kompatible /chat/completions-Format (OpenAI, OpenRouter,
DeepSeek, Groq, lokale Server wie Ollama/LM Studio ...). Der Zugang (Base-URL,
API-Key, Modell) kommt aus den Einstellungen (data/settings.json).

Der Chat *bedient* die App über Tools (Rechnungen, Ausgaben, Bank, Berichte).
Mit aktiviertem Entwickler-Modus zusätzlich Shell- und Datei-Tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import core
import wirex


# ── Konfiguration aus den Einstellungen ──────────────────────────────────
def settings() -> dict:
    return core.load_settings()


def is_configured() -> bool:
    s = settings()
    return bool(s.get("api_key") and s.get("base_url") and s.get("model"))


def dev_enabled() -> bool:
    return bool(settings().get("developer_mode"))


# ── Tool-Definitionen (OpenAI-Function-Format) ───────────────────────────
def _fn(name, description, properties, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required or [], "additionalProperties": False}}}


BOOKKEEPING_TOOLS = [
    _fn("list_invoices", "Alle Rechnungen auflisten (Nummer, Datum, Kunde, Netto, Brutto, Typ, bezahlt).", {}),
    _fn("list_expenses", "Alle Ausgaben auflisten.", {}),
    _fn("list_customers", "Alle Kunden auflisten.", {}),
    _fn("list_transactions", "Bankbuchungen/Zahlungen auflisten. Jede Zeile zeigt Betrag, Kategorie und ob "
        "ein Beleg zugeordnet ist ('beleg': true/false). Filter kombinierbar.",
        {"query": {"type": "string", "description": "Suchbegriff in der Beschreibung (optional)"},
         "only_open": {"type": "boolean", "description": "nur noch nicht kategorisierte Buchungen"},
         "missing_receipt": {"type": "boolean", "description": "nur Zahlungen OHNE zugeordneten Beleg"}}),
    _fn("list_documents", "Hochgeladene Belege im Posteingang auflisten. Status: 'to_review' = noch zu "
        "prüfen/buchen, 'matched' = bereits verbucht. Zeigt Dateiname, Anbieter, Betrag, Datum und – falls "
        "vorhanden – eine vorgeschlagene passende Zahlung (vorschlag_tx_id) zum Zuordnen. Nutze dies IMMER, "
        "wenn der Nutzer nach hochgeladenen Dateien/Belegen/Dokumenten/Rechnungen im Posteingang fragt.", {}),
    _fn("book_document", "Einen hochgeladenen Beleg (id aus list_documents) verbuchen. MIT tx_id wird der "
        "Beleg an eine offene Zahlung ZUGEORDNET – die Zahlung wird dadurch zur Betriebsausgabe (nutze das, "
        "wenn eine passende Zahlung existiert, z. B. vorschlag_tx_id). OHNE tx_id wird eine eigenständige "
        "Ausgabe gebucht (für Belege ohne passende Bankzahlung). category = SKR03-Konto, private_share = "
        "%-Privatanteil (Vodafone-Handy üblicherweise 80).",
        {"document_id": {"type": "integer"}, "category": {"type": "string", "description": "SKR03-Konto, z. B. '4920 - Telefon'"},
         "vat_rate": {"type": "integer", "description": "19 / 7 / 0"}, "private_share": {"type": "number"},
         "tx_id": {"type": "integer", "description": "optional: offene Zahlung, an die zugeordnet wird"}},
        ["document_id", "category"]),
    _fn("figures", "Einnahmen/Ausgaben/Gewinn + USt-Zahllast für einen BELIEBIGEN Zeitraum. "
        "Nutze DIESES Tool für Monats-, Quartals- UND Jahresfragen zu Zahlen. period-Token: "
        "'JJJJ' | 'JJJJ-Qn' | 'JJJJ-MM'. WICHTIG: Nenne ausschließlich die Felder aus dem Rückgabewert "
        "WÖRTLICH – rechne NIE selbst Summen aus Rechnungen. Für 'Einnahmen' nimm 'einnahmen' (aktive "
        "Methode); brauchst du beide, nutze 'einnahmen_brutto' und 'einnahmen_netto' (nicht selbst berechnen). "
        "'umsatzerloese_netto' = reine Rechnungssummen netto.",
        {"period": {"type": "string", "description": "z. B. '2026-07', '2026-Q2', '2026'"}}, ["period"]),
    _fn("figures_yearly", "Jahresübersicht: Einnahmen/Ausgaben/Gewinn für ALLE Jahre mit Daten (Jahr für Jahr). "
        "Nutze DIES bei 'jahr für jahr', 'pro Jahr', 'alle Jahre', 'Jahresvergleich', 'Verlauf' – rufe NICHT "
        "'figures' pro Jahr einzeln auf. Die App zeigt danach AUTOMATISCH einen Balken-Graphen. Danach nur EIN "
        "kurzer Satz OHNE Zahlen, kein erneuter Aufruf.", {}),
    _fn("report", "EÜR / Gewinnermittlung für ein Jahr.",
        {"year": {"type": "integer"}}, ["year"]),
    _fn("vat", "USt-Voranmeldung für ein Quartal (Kennziffern).",
        {"year": {"type": "integer"}, "quarter": {"type": "integer", "description": "1-4"}}, ["year", "quarter"]),
    _fn("zm", "Zusammenfassende Meldung (EU-Umsätze) für ein Quartal.",
        {"year": {"type": "integer"}, "quarter": {"type": "integer"}}, ["year", "quarter"]),
    _fn("vat_annual", "USt-Jahresabschluss (Jahressummen + Vorauszahlungen).",
        {"year": {"type": "integer"}}, ["year"]),
    _fn("create_invoice",
        "Neue Ausgangsrechnung anlegen (erzeugt PDF). Der Rechnungstyp (Inland/EU-Reverse-Charge/"
        "Drittland) wird AUTOMATISCH aus der USt-IdNr./dem Land des Kunden bestimmt – NICHT selbst "
        "setzen oder raten. 'kind' nur angeben, wenn ein Kunde weder USt-ID noch Land hat.",
        {"customer_name": {"type": "string"},
         "customer_address": {"type": "string", "description": "Adresse, Zeilen mit ; trennen (optional bei bestehendem Kunden)"},
         "customer_vat_id": {"type": "string", "description": "USt-IdNr. (bestimmt den Rechnungstyp automatisch: DE→Inland, EU→Reverse-Charge, sonst Drittland)"},
         "kind": {"type": "string", "enum": ["domestic", "eu", "third"],
                  "description": "Fallback – wird ignoriert, sobald der Kunde eine USt-ID/ein Land hat."},
         "save_customer": {"type": "boolean", "description": "false = Empfänger nur für diese "
                           "Rechnung verwenden, nicht ins Kundenregister aufnehmen"},
         "issue_date": {"type": "string", "description": "JJJJ-MM-TT"},
         "service_from": {"type": "string", "description": "Leistungsdatum von (optional)"},
         "service_to": {"type": "string", "description": "Leistungsdatum bis (optional)"},
         "items": {"type": "array", "description": "Positionen",
                   "items": {"type": "object", "properties": {
                       "description": {"type": "string"}, "quantity": {"type": "number"},
                       "unit": {"type": "string"}, "unit_price": {"type": "number"},
                       "vat_rate": {"type": "integer"}}, "required": ["description", "unit_price"]}},
         "supply_type": {"type": "string", "enum": ["service", "goods", "exempt"],
                         "description": "Was wird abgerechnet: service = Dienstleistung (Standard), "
                                        "goods = Warenlieferung, exempt = steuerfrei nach § 4 Nr. 8–29 "
                                        "UStG. Entscheidet über die USt-VA-Kennzahl (EU: 21 vs. 41, "
                                        "Drittland: 45 vs. 43) und die ZM-Meldeart."}},
        ["customer_name", "issue_date", "items"]),
    _fn("create_expense", "Neue Ausgabe/Betriebsausgabe erfassen. private_share = %-Privatanteil (z. B. Telefon 80). "
        "vat_kind = umsatzsteuerliche Art der Eingangsrechnung; NICHT raten, im Zweifel weglassen "
        "oder beim Nutzer nachfragen (EU vs. Drittland ist eine andere Kennzahl).",
        {"date": {"type": "string"}, "vendor": {"type": "string"}, "category": {"type": "string"},
         "gross": {"type": "number"}, "vat_rate": {"type": "integer"},
         "private_share": {"type": "number"}, "paid_date": {"type": "string"},
         "reverse_charge": {"type": "boolean", "description": "veraltet – entspricht vat_kind='rc_unknown'"},
         "vat_kind": {"type": "string", "enum": sorted(core.EXPENSE_VAT_KINDS),
                      "description": " · ".join(f"{k}: {v}" for k, v in core.EXPENSE_VAT_KINDS.items())}},
        ["date", "vendor", "gross", "vat_rate"]),
    _fn("set_expense_vat_kind",
        "USt-Art einer bereits erfassten Ausgabe setzen – vor allem die §13b-Zuordnung EU "
        "(Kz 46/47) vs. Drittland (Kz 84/85), die die USt-VA sonst offen lässt.",
        {"expense_id": {"type": "integer"},
         "vat_kind": {"type": "string", "enum": sorted(core.EXPENSE_VAT_KINDS)},
         "vat_rate": {"type": "integer", "description": "Steuersatz, Standard 19"}},
        ["expense_id", "vat_kind"]),
    _fn("categorize_transactions",
        "Offene Bankbuchungen, die zum Suchbegriff passen, kategorisieren. "
        "action: expense(Betriebsausgabe), privatentnahme, gebuehr, einnahme_sonstige, ignorieren.",
        {"query": {"type": "string"}, "action": {"type": "string",
            "enum": ["expense", "privatentnahme", "gebuehr", "einnahme_sonstige", "ignorieren"]},
         "category": {"type": "string", "description": "Konto/Kategorie bei action=expense"},
         "vat_rate": {"type": "integer"}, "private_share": {"type": "number"}},
        ["query", "action"]),
    _fn("mark_invoice_paid", "Rechnung als bezahlt markieren.",
        {"number": {"type": "string"}, "paid_date": {"type": "string"}}, ["number", "paid_date"]),
    _fn("present",
        "Zeige dem Nutzer eine visuelle KARTE statt/zusätzlich zu Text. IMMER nutzen, wenn ein Ergebnis "
        "besser als Karte passt: Ausgaben-/Kategorie-Aufschlüsselung -> chart_donut oder chart_bar; einzelne "
        "Kennzahl (Umsatz, Gewinn, Zahllast) -> stat; Aufzählung (Rechnungen, Buchungen) -> list; etwas, das "
        "der Nutzer bestätigen soll (buchen, senden, Mahnung, anlegen) -> confirm_action; will der Nutzer eine "
        "RECHNUNG ERSTELLEN -> invoice_form (zeigt ein ausfüllbares Formular, KEINE Prosa-Rückfrage nach den Daten!). "
        "Einzelne Kennzahl MIT Trendverlauf (z. B. Umsatz-Entwicklung) -> kpi (mit 'spark'-Zahlenreihe). "
        "Zwei Zeiträume/Werte gegenüberstellen (Q1 vs. Q2) -> compare. Nutzer soll zwischen Optionen WÄHLEN "
        "(z. B. auto vs. manuell kategorisieren) -> choice. Analysierter Beleg/Rechnungs-Upload -> doc_card. "
        "Rufe VORHER die "
        "Daten-Tools (report/list_expenses/…) auf und fülle echte, formatierte Zahlen ein (z. B. '€2.400,00'). "
        "Setze IMMER ein 'link'-Objekt, wenn es dazu einen vollständigen App-Screen gibt (z. B. Bericht -> "
        "Button 'Vollständigen Bericht ansehen'). 'delta' NUR bei echtem Vergleich (z. B. vs. Vormonat), sonst weglassen.",
        {"type": {"type": "string", "enum": ["stat", "chart_bar", "chart_donut", "list", "confirm_action",
                                             "invoice_form", "kpi", "compare", "choice", "doc_card"]},
         "title": {"type": "string"},
         "spark": {"type": "array", "description": "kpi: Zahlenreihe für die Trendlinie (chronologisch, z. B. Monatsumsätze)",
                   "items": {"type": "number"}},
         "left": {"type": "object", "description": "compare: linke (Vergleichs-)Karte",
                  "properties": {"label": {"type": "string"}, "value": {"type": "string"}}},
         "right": {"type": "object", "description": "compare: rechte (hervorgehobene) Karte",
                   "properties": {"label": {"type": "string"}, "value": {"type": "string"}}},
         "options": {"type": "array", "description": "choice: Auswahloptionen; bei Klick auf den CTA geht 'message' "
                     "der gewählten Option als Antwort an dich zurück",
                     "items": {"type": "object", "properties": {"title": {"type": "string"},
                               "sub": {"type": "string"}, "message": {"type": "string"}}}},
         "details": {"type": "array", "description": "doc_card: zusätzliche Beleg-Felder, die erst per 'Details' "
                     "aufklappen (z. B. USt-IdNr., Netto, Zahlart, SKR-Vorschlag)",
                     "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}},
         "invoice": {"type": "object", "description": "invoice_form: Vorbelegung des Rechnungs-Formulars. "
                     "Übernimm alles, was der Nutzer schon genannt hat (sonst leer lassen – der Nutzer füllt es aus).",
                     "properties": {
                         "customer_name": {"type": "string"},
                         "amount": {"type": "number", "description": "Netto-Betrag (USt kommt oben drauf)"},
                         "currency": {"type": "string", "description": "z. B. 'EUR', 'GBP' (Standard EUR)"},
                         "term_days": {"type": "integer", "description": "Zahlungsziel in Tagen (14/30)"},
                         "service": {"type": "string", "description": "Leistungsbeschreibung / Positionstext"},
                         "kind": {"type": "string", "enum": ["domestic", "eu", "third"]},
                         "vat_rate": {"type": "integer", "description": "19 / 7 / 0"}}},
         "subtitle": {"type": "string", "description": "kurze Erklärzeile über der Karte"},
         "value": {"type": "string", "description": "stat: die große Zahl, formatiert"},
         "delta": {"type": "string", "description": "stat: Veränderung, z. B. '+18%'"},
         "total": {"type": "string", "description": "chart_donut: Gesamtsumme, formatiert"},
         "bars": {"type": "array", "description": "chart_bar",
                  "items": {"type": "object", "properties": {"label": {"type": "string"},
                            "value": {"type": "number"}, "amount": {"type": "string"}}}},
         "segments": {"type": "array", "description": "chart_donut",
                      "items": {"type": "object", "properties": {"name": {"type": "string"},
                                "value": {"type": "number"}, "amount": {"type": "string"}}}},
         "items": {"type": "array", "description": "list",
                   "items": {"type": "object", "properties": {"title": {"type": "string"},
                             "sub": {"type": "string"}, "amount": {"type": "string"}, "status": {"type": "string"}}}},
         "fields": {"type": "array", "description": "confirm_action / doc_card: Zeilen Label/Wert (beim Beleg z. B. "
                    "Händler, Betrag, USt, Datum)",
                    "items": {"type": "object", "properties": {"label": {"type": "string"}, "value": {"type": "string"}}}},
         "confirm_label": {"type": "string", "description": "confirm_action/doc_card/choice: Text des Aktions-Buttons "
                           "(Beleg z. B. 'Zuordnen & buchen', choice z. B. 'Los')"},
         "confirm_message": {"type": "string", "description": "confirm_action/doc_card: diese Nachricht geht bei Klick als "
                             "Bestätigung an dich zurück; DU führst dann die Aktion mit deinen Tools aus."},
         "link": {"type": "object", "description": "Optionaler Button, der zum vollständigen Screen in der App "
                  "führt. Setze ihn IMMER, wenn es einen passenden Screen gibt. Deep-Links: USt-VA -> "
                  "'/reports?p=JAHR-Qn&gen=ustva' (EÜR gen=eur, BWA gen=bwa, ZM gen=zm, SuSa gen=susa); "
                  "Rechnungen -> '/invoices?dir=out&year=JAHR'; Zahlungen -> '/zahlungen?p=JAHR' (oder JAHR-Qn); "
                  "Dokumente -> '/files'; Berichte-Übersicht -> '/reports'; Overview -> '/'.",
                  "properties": {"label": {"type": "string"}, "href": {"type": "string"}}}},
        ["type"]),
]

# Erweiterte Werkzeuge: decken die übrigen Funktionen der App ab, damit Chat, CLI
# (./bb call) und MCP denselben Funktionsumfang haben wie die Weboberfläche.
_ACCOUNT = {"type": "string", "description": "SKR03-Konto, z. B. '4920 - Telefon'"}
_RATE = {"type": "integer", "description": "19 / 7 / 0"}
_SHARE = {"type": "number", "description": "%-Privatanteil"}
_LINES = {"type": "array", "description": "Positionen der Aufteilung",
          "items": {"type": "object", "properties": {
              "category": {"type": "string"}, "amount": {"type": "number"},
              "vat_rate": {"type": "integer"}, "private_share": {"type": "number"}},
              "required": ["category", "amount"]}}

EXTENDED_TOOLS = [
    # ── Rechnungen ──
    _fn("record_payment", "Zahlungseingang zu einer Rechnung erfassen (Bar, Fremdwährung, anderes "
        "Konto). Legt zusätzlich eine Einnahme-Buchung an, sofern create_tx nicht false ist.",
        {"number": {"type": "string"}, "paid_date": {"type": "string"},
         "amount": {"type": "number"}, "currency": {"type": "string"},
         "method": {"type": "string"}, "create_tx": {"type": "boolean"}},
        ["number", "paid_date"]),
    _fn("send_invoice", "Rechnung als PDF per E-Mail versenden (SMTP aus den Einstellungen).",
        {"number": {"type": "string"}, "to": {"type": "string"}, "cc": {"type": "string"},
         "subject": {"type": "string"}, "body": {"type": "string"},
         "copy_self": {"type": "boolean"}}, ["number"]),
    _fn("regenerate_invoice_pdf", "PDF einer bestehenden Rechnung neu erzeugen.",
        {"number": {"type": "string"}}, ["number"]),
    _fn("delete_invoice", "Rechnung endgültig löschen (inkl. PDF). Zugeordnete Zahlungen bleiben "
        "erhalten und werden wieder auf 'offen' gesetzt.",
        {"number": {"type": "string"}}, ["number"]),
    _fn("import_invoices_csv", "Rechnungen aus einer CSV-Datei importieren (Pfad im Projektordner).",
        {"path": {"type": "string"}, "render": {"type": "boolean", "description": "PDFs erzeugen"}},
        ["path"]),
    # ── Kunden ──
    _fn("add_customer", "Neuen Kunden anlegen (bestehender Name wird wiederverwendet).",
        {"name": {"type": "string"}, "address": {"type": "string", "description": "Zeilen mit ; trennen"},
         "vat_id": {"type": "string"}, "email": {"type": "string"},
         "number": {"type": "string", "description": "Kundennummer (optional, muss eindeutig sein)"}},
        ["name"]),
    _fn("set_customer_number", "Kundennummer setzen (leer = entfernen). Muss eindeutig sein.",
        {"customer_id": {"type": "integer"}, "number": {"type": "string"}}, ["customer_id"]),
    _fn("next_customer_number", "Nächste freie Kundennummer vorschlagen (ab 10001).", {}),
    _fn("set_customer_email", "E-Mail-Adresse eines Kunden setzen.",
        {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
        ["customer_id", "email"]),
    _fn("delete_customer", "Kunde löschen. Nur möglich, wenn ihm keine Rechnung zugeordnet ist.",
        {"customer_id": {"type": "integer"}}, ["customer_id"]),
    _fn("merge_duplicate_customers", "Namensgleiche Kunden zusammenführen: Rechnungen wandern zum "
        "Datensatz mit den meisten Stammdaten, die Doppel werden gelöscht.", {}),
    # ── Ausgaben ──
    _fn("mark_expense_paid", "Ausgabe als bezahlt markieren.",
        {"expense_id": {"type": "integer"}, "paid_date": {"type": "string"}},
        ["expense_id", "paid_date"]),
    # ── Bank / Zahlungen ──
    _fn("import_bank_csv", "Bank-/Zahlungs-CSV importieren (Pfad im Projektordner). Dedupliziert "
        "und ordnet Eingänge automatisch offenen Rechnungen zu.",
        {"path": {"type": "string"}}, ["path"]),
    _fn("create_transaction", "Manuelle Buchung erfassen (Bar, Wallet …). amount signiert: "
        "+ Eingang, - Ausgang.",
        {"date": {"type": "string"}, "description": {"type": "string"},
         "amount": {"type": "number"}, "currency": {"type": "string"},
         "method": {"type": "string"}}, ["date", "description", "amount"]),
    _fn("set_transaction_category", "Eine einzelne Bankbuchung kategorisieren.",
        {"tx_id": {"type": "integer"}, "category": {"type": "string",
            "enum": ["offen", "einnahme_rechnung", "einnahme_sonstige", "ausgabe",
                     "privatentnahme", "gebuehr", "ignorieren"]},
         "note": {"type": "string"}}, ["tx_id", "category"]),
    _fn("book_transaction_as_expense", "Eine Ausgangs-Buchung als Betriebsausgabe verbuchen.",
        {"tx_id": {"type": "integer"}, "category": _ACCOUNT, "vat_rate": _RATE,
         "private_share": _SHARE}, ["tx_id", "category"]),
    _fn("split_transaction", "Eine Zahlung auf mehrere Betriebsausgaben aufteilen.",
        {"tx_id": {"type": "integer"}, "lines": _LINES}, ["tx_id", "lines"]),
    _fn("link_transaction_to_invoice", "Eine Bankbuchung einer Rechnung zuordnen "
        "(markiert die Rechnung als bezahlt).",
        {"tx_id": {"type": "integer"}, "number": {"type": "string"}}, ["tx_id", "number"]),
    _fn("reset_transaction", "Zuordnung einer Buchung lösen und wieder auf 'offen' setzen.",
        {"tx_id": {"type": "integer"}}, ["tx_id"]),
    _fn("delete_transaction", "Bankbuchung endgültig löschen (inkl. abhängiger Buchungen).",
        {"tx_id": {"type": "integer"}}, ["tx_id"]),
    _fn("list_rules", "Gelernte Zuordnungsregeln (Beschreibung → Konto/Aktion) auflisten.", {}),
    # ── Belege ──
    _fn("add_document", "Datei als Beleg in den Posteingang aufnehmen (Pfad im Projektordner).",
        {"path": {"type": "string"}, "vendor": {"type": "string"},
         "amount": {"type": "number"}, "doc_date": {"type": "string"}}, ["path"]),
    _fn("suggest_document_match", "Passende offene Zahlung zu einem Beleg vorschlagen.",
        {"document_id": {"type": "integer"}}, ["document_id"]),
    _fn("split_document", "Einen Beleg auf mehrere Buchungssätze aufteilen; mit tx_id werden alle "
        "Positionen einer Zahlung zugeordnet.",
        {"document_id": {"type": "integer"}, "lines": _LINES, "tx_id": {"type": "integer"}},
        ["document_id", "lines"]),
    _fn("delete_document", "Beleg aus dem Posteingang löschen.",
        {"document_id": {"type": "integer"}}, ["document_id"]),
    # ── Stammdaten & Auswertung ──
    _fn("tax_reserve", "Empfohlene STEUERRÜCKLAGE und geschätzte EINKOMMENSTEUER: USt-Zahllast des "
        "laufenden Quartals (exakt) + Einkommensteuer nach §32a EStG (inkl. Soli/Kirchensteuer und "
        "Splitting laut Einstellungen) auf den Jahresgewinn. Im laufenden Jahr wird der bisherige "
        "EÜR-Gewinn aufs Jahr hochgerechnet. Nutze dies IMMER bei Fragen nach Einkommensteuer, "
        "Steuerlast, Nachzahlung oder 'wie viel muss ich zurücklegen'.",
        {"year": {"type": "integer", "description": "Jahr, Standard: laufendes Jahr"},
         "gewinn_jahr": {"type": "number", "description": "geplanter Jahresgewinn in Euro statt der "
                         "Hochrechnung – z. B. wenn der Nutzer künftige Einnahmen vorgibt"}}),
    _fn("list_accounts", "Alle verfügbaren Aufwandskonten (SKR03) auflisten.", {}),
    _fn("add_account", "Eigenes Aufwandskonto ergänzen.",
        {"account": {"type": "string"}}, ["account"]),
    _fn("remove_account", "Eigenes Aufwandskonto entfernen.",
        {"account": {"type": "string"}}, ["account"]),
    _fn("list_recurring", "Vorlagen für wiederkehrende Rechnungen auflisten.", {}),
    _fn("add_recurring", "Vorlage für eine wiederkehrende Rechnung anlegen.",
        {"client": {"type": "string"}, "service": {"type": "string"},
         "interval": {"type": "string", "enum": ["monatlich", "quartalsweise", "jährlich"]},
         "amount": {"type": "number"}, "next_run": {"type": "string"}},
        ["client", "service"]),
    _fn("toggle_recurring", "Vorlage aktivieren/pausieren.", {"id": {"type": "string"}}, ["id"]),
    _fn("delete_recurring", "Vorlage löschen.", {"id": {"type": "string"}}, ["id"]),
    _fn("advance_recurring", "next_run einer Vorlage um ein Intervall weiterschreiben.",
        {"id": {"type": "string"}}, ["id"]),
    _fn("list_dunning", "Offene Rechnungen mit ihrer Mahnstufe auflisten.", {}),
    _fn("advance_dunning", "Mahnstufe einer Rechnung erhöhen.",
        {"number": {"type": "string"}}, ["number"]),
    _fn("reset_dunning", "Mahnstufe einer Rechnung zurücksetzen.",
        {"number": {"type": "string"}}, ["number"]),
    _fn("list_closed_periods", "Festgeschriebene Zeiträume auflisten.", {}),
    _fn("close_period", "Zeitraum festschreiben, z. B. '2026-Q1'.",
        {"token": {"type": "string"}}, ["token"]),
    _fn("reopen_period", "Festgeschriebenen Zeitraum wieder öffnen.",
        {"token": {"type": "string"}}, ["token"]),
    _fn("get_settings", "App-Einstellungen lesen (Geheimnisse werden maskiert).",
        {"keys": {"type": "array", "items": {"type": "string"}}}),
    _fn("set_settings", "App-Einstellungen setzen (nur die übergebenen Schlüssel).",
        {"values": {"type": "object", "description": "z. B. {\"home_currency\": \"EUR\"}"}},
        ["values"]),
    _fn("fx_rate", "EZB-Wechselkurs abfragen (1 base = ? quote).",
        {"base": {"type": "string"}, "quote": {"type": "string"}, "date": {"type": "string"}},
        ["base"]),
    _fn("bank_import_transactions", "Karten- und Kontoumsätze des Nuri/Wirex-Kontos abrufen und als "
        "OFFENE Zahlungen einpflegen (kein Buchungssatz, keine Festschreibung – der Nutzer ordnet sie "
        "danach selbst zu). Braucht eine aktive Passkey-Session: vorher wirex_session_start aufrufen "
        "und den Nutzer den Link bestätigen lassen. Erneuter Aufruf legt nichts doppelt an.",
        {"page_size": {"type": "integer", "description": "Umsätze pro Rail, Standard 100"}}),
]

DEV_TOOLS = [
    _fn("run_shell", "Shell-Befehl im Projektordner ausführen und Ausgabe zurückgeben. NUR Entwickler-Modus.",
        {"command": {"type": "string"}}, ["command"]),
    _fn("read_file", "Datei aus dem Projektordner lesen.",
        {"path": {"type": "string"}}, ["path"]),
    _fn("write_file", "Datei im Projektordner schreiben/überschreiben.",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
]


def tools_for() -> list:
    # Die wirex_*-Tools kommen live vom MCP-Server (tools/list), nicht aus einer
    # Liste hier — neue Tools dort sind ohne Codeänderung im Chat verfügbar.
    return (BOOKKEEPING_TOOLS + EXTENDED_TOOLS
            + (DEV_TOOLS if dev_enabled() else []) + wirex.openai_tools())


def tool_names() -> list:
    return [t["function"]["name"] for t in tools_for()]


# ── Terminal-Modus: direkte Tool-Aufrufe ohne LLM ────────────────────────
def terminal_run(command: str) -> str:
    """Ein Befehl gegen die Tool-Schnittstelle. Formen:
       help | <tool> {json} | Kurzformen (report 2026, vat 2026 2, tx <text>, invoices ...)."""
    cmd = (command or "").strip()
    if not cmd:
        return ""
    parts = cmd.split(None, 1)
    name = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    if name in ("help", "?", "tools"):
        return ("Tools: " + ", ".join(tool_names()) +
                "\n\nNutzung:\n  <tool> {\"arg\": ...}     z. B.  report {\"year\": 2026}\n"
                "Kurzformen:\n  report 2026 | vat 2026 2 | zm 2026 2 | vat_annual 2026\n"
                "  invoices | expenses | customers | tx <suchbegriff>\n"
                + ("  shell <befehl> | cat <pfad>\n" if dev_enabled() else ""))

    args: dict = {}
    nums = [int(x) for x in rest.split() if x.strip().lstrip("-").isdigit()]
    aliases = {"invoices": "list_invoices", "expenses": "list_expenses",
               "customers": "list_customers", "tx": "list_transactions",
               "transactions": "list_transactions", "shell": "run_shell", "cat": "read_file"}
    name = aliases.get(name, name)

    if rest.startswith("{"):
        try:
            args = json.loads(rest)
        except Exception as e:
            return f"Ungültiges JSON: {e}"
    elif name == "list_transactions" and rest:
        args = {"query": rest, "only_open": True}
    elif name in ("report", "vat_annual") and nums:
        args = {"year": nums[0]}
    elif name in ("vat", "zm") and len(nums) >= 2:
        args = {"year": nums[0], "quarter": nums[1]}
    elif name == "run_shell" and rest:
        args = {"command": rest}
    elif name == "read_file" and rest:
        args = {"path": rest}

    if name not in tool_names():
        return f"Unbekannter Befehl: {name}. 'help' zeigt alle Tools."

    result = dispatch(name, args)
    try:
        return json.dumps(json.loads(result), ensure_ascii=False, indent=2)
    except Exception:
        return result


# ── Tool-Ausführung ──────────────────────────────────────────────────────
def _rows(rows):
    return [{k: r[k] for k in r.keys()} for r in rows]


def _tx_view(t) -> dict:
    """Kompakte Zahlungs-Sicht fürs LLM: inkl. ob ein Beleg zugeordnet ist."""
    amt = t["amount_home"] if t["amount_home"] is not None else t["amount"]
    return {"id": t["id"], "datum": t["date"], "beschreibung": t["description"],
            "betrag": round(float(amt or 0), 2), "kategorie": t["category"],
            "beleg": bool(t["receipt_path"])}


def _doc_view(conn, d) -> dict:
    """Kompakte Beleg-Sicht fürs LLM: inkl. vorgeschlagener passender Zahlung."""
    v = {"id": d["id"], "datei": d["filename"], "anbieter": d["vendor"],
         "betrag": d["amount"], "datum": d["doc_date"], "status": d["status"]}
    if d["suggest_tx"]:
        t = core.get_transaction(conn, d["suggest_tx"])
        if t:
            amt = t["amount_home"] if t["amount_home"] is not None else t["amount"]
            v["vorschlag_tx_id"] = t["id"]
            v["vorschlag"] = f"{t['date']} · {t['description']} · {round(float(amt or 0), 2)}"
    return v


def _need_invoice(conn, ident):
    """Rechnung über Nummer oder ID holen – wirft, damit dispatch einen Fehler zurückgibt."""
    inv = core.get_invoice_by_number(conn, str(ident))
    if inv is None and str(ident).isdigit():
        inv = core.get_invoice(conn, int(ident))
    if inv is None:
        raise ValueError(f"Rechnung '{ident}' nicht gefunden.")
    return inv


def _need_tx(conn, tx_id: int):
    t = core.get_transaction(conn, int(tx_id))
    if t is None:
        raise ValueError(f"Buchung #{tx_id} nicht gefunden.")
    return t


def _safe_path(path: str) -> Path:
    p = (core.ROOT / path).resolve()
    if core.ROOT not in p.parents and p != core.ROOT:
        raise ValueError("Pfad außerhalb des Projektordners nicht erlaubt.")
    return p


def dispatch(name: str, args: dict) -> str:
    conn = core.db()
    cfg = core.load_config()
    try:
        if name == "list_invoices":
            return _j(_rows(core.list_invoices(conn)))
        if name == "list_expenses":
            return _j(_rows(core.list_expenses(conn)))
        if name == "list_customers":
            return _j(_rows(core.list_customers(conn)))
        if name == "list_transactions":
            txs = core.list_transactions(conn, only_open=bool(args.get("only_open")),
                                         query=args.get("query"))
            if args.get("missing_receipt"):
                txs = [t for t in txs if not t["receipt_path"]]
            return _j([_tx_view(t) for t in txs])
        if name == "list_documents":
            return _j([_doc_view(conn, d) for d in core.list_documents(conn)])
        if name == "book_document":
            did = int(args["document_id"])
            doc = core.get_document(conn, did)
            if not doc:
                return _j({"error": "Beleg nicht gefunden"})
            if doc["status"] == "matched":
                return _j({"error": f"Beleg '{doc['filename']}' ist bereits verbucht."})
            cat = args.get("category") or "Sonstiges"
            vat = int(args.get("vat_rate", 19) or 0)
            ps = int(float(args.get("private_share", 0) or 0))
            if args.get("tx_id"):
                core.match_document_to_tx(conn, did, int(args["tx_id"]), category=cat,
                                          vat_rate=vat, private_share=ps)
                return _j({"gebucht": doc["filename"], "zugeordnet_an_tx": int(args["tx_id"]),
                           "konto": cat, "privatanteil": ps})
            core.confirm_document(conn, did, category=cat, vat_rate=vat, private_share=ps)
            return _j({"gebucht": doc["filename"], "als": "eigenständige Ausgabe",
                       "konto": cat, "privatanteil": ps})
        if name == "figures":
            return _j(core.figures_for_period(conn, cfg, args.get("period")))
        if name == "figures_yearly":
            return _j(core.yearly_overview(conn, cfg))
        if name == "report":
            return _j(core.report_data(conn, int(args["year"])))
        if name == "vat":
            v = dict(core.vat_data(conn, cfg, int(args["year"]), int(args["quarter"])))
            v["kennzahlen"] = [{"kz": kz_, "feld": lbl, "bemessung": str(base),
                                "kz_steuer": kz_tax or None,
                                "steuer": None if tax is None else str(tax)}
                               for kz_, lbl, base, kz_tax, tax in core.vat_kz_rows(v)]
            # Ohne EU/Drittland-Zuordnung fehlt die §13b-Steuer in der Anmeldung – sichtbar machen.
            v["offene_13b_belege"] = _rows(core.vat_open_rc(conn, v["lo"], v["hi"]))
            return _j(v)
        if name == "zm":
            z = core.zm_data(conn, int(args["year"]), int(args["quarter"]))
            z = dict(z); z["rows"] = _rows(z["rows"]); return _j(z)
        if name == "tax_reserve":
            g = args.get("gewinn_jahr")
            return _j(core.tax_reserve(conn, cfg, year=args.get("year"),
                                       gewinn_jahr=None if g in (None, "") else float(g)))
        if name == "vat_annual":
            return _j(core.vat_annual(conn, cfg, int(args["year"])))
        if name == "create_invoice":
            if args.get("save_customer") is False:
                cust = core.adhoc_customer(args["customer_name"],
                                           args.get("customer_address", ""),
                                           args.get("customer_vat_id"))
            else:
                cust = core.resolve_or_create_customer(
                    conn, args["customer_name"], args.get("customer_address", ""),
                    args.get("customer_vat_id"))
            inv = core.create_invoice(
                conn, cfg, customer=cust, kind=args["kind"], issue_date=args["issue_date"],
                service_from=args.get("service_from") or args["issue_date"],
                service_to=args.get("service_to"), items=args["items"],
                supply_type=args.get("supply_type"))
            return _j({"created": inv["number"], "net": str(inv["net"]),
                       "vat": str(inv["vat"]), "gross": str(inv["gross"])})
        if name == "create_expense":
            r = core.create_expense(
                conn, date_=args["date"], vendor=args["vendor"],
                description=args.get("description"), category=args.get("category", "Sonstiges"),
                gross=args["gross"], vat_rate=int(args["vat_rate"]),
                paid_date=args.get("paid_date") or args["date"],
                private_share=args.get("private_share", 0),
                reverse_charge=bool(args.get("reverse_charge")),
                vat_kind=args.get("vat_kind"))
            return _j({"created_expense": True, "net": str(r["net"]), "vat": str(r["vat"]),
                       "vat_kind": r["vat_kind"]})
        if name == "set_expense_vat_kind":
            r = core.update_expense(conn, int(args["expense_id"]), vat_kind=args["vat_kind"],
                                    vat_rate=args.get("vat_rate"))
            return _j({"expense_id": r["id"], "vat_kind": r["vat_kind"],
                       "bedeutung": core.EXPENSE_VAT_KINDS[r["vat_kind"]]})
        if name == "categorize_transactions":
            txs = core.list_transactions(conn, only_open=True, query=args["query"])
            action = args["action"]
            n = 0
            for t in txs:
                if action == "expense":
                    if t["amount"] < 0:
                        core.create_expense_from_tx(conn, t["id"], args.get("category", "Sonstiges"),
                                                    args.get("vat_rate", 0),
                                                    private_share=args.get("private_share", 0))
                        n += 1
                else:
                    core.set_tx_category(conn, t["id"], action); n += 1
            return _j({"kategorisiert": n})
        if name == "mark_invoice_paid":
            inv = core.get_invoice_by_number(conn, args["number"])
            if not inv:
                return _j({"error": "Rechnung nicht gefunden"})
            core.mark_invoice_paid(conn, inv["id"], args["paid_date"])
            return _j({"ok": True})

        # ── Rechnungen ──
        if name == "record_payment":
            inv = _need_invoice(conn, args["number"])
            r = core.record_invoice_payment(
                conn, inv["id"], args["paid_date"], amount=args.get("amount"),
                currency=args.get("currency"), method=args.get("method", ""),
                create_tx=args.get("create_tx", True) is not False)
            return _j(dict(r, number=inv["number"]))
        if name == "send_invoice":
            inv = _need_invoice(conn, args["number"])
            to = args.get("to")
            if not to and inv["customer_id"]:
                c = core.get_customer(conn, inv["customer_id"])
                to = c and c["email"]
            return _j(core.send_invoice_email(
                conn, inv["id"], to or "", args.get("subject") or f"Rechnung {inv['number']}",
                args.get("body") or f"Anbei die Rechnung {inv['number']}.",
                cc=args.get("cc"), copy_self=bool(args.get("copy_self"))))
        if name == "regenerate_invoice_pdf":
            inv = _need_invoice(conn, args["number"])
            return _j({"number": inv["number"],
                       "pdf_path": str(core.regenerate_invoice_pdf(conn, cfg, inv["id"]))})
        if name == "delete_invoice":
            inv = _need_invoice(conn, args["number"])
            return _j(core.delete_invoice(conn, inv["id"]))
        if name == "import_invoices_csv":
            text = _safe_path(args["path"]).read_text(encoding="utf-8", errors="replace")
            return _j(core.import_invoices_csv(
                conn, cfg, text, render=args.get("render", True) is not False))

        # ── Kunden ──
        if name == "add_customer":
            c = core.resolve_or_create_customer(
                conn, args["name"], args.get("address", ""), args.get("vat_id"))
            if args.get("email"):
                core.set_customer_email(conn, c["id"], args["email"])
            if args.get("number"):
                core.set_customer_number(conn, c["id"], args["number"])
            c = core.get_customer(conn, c["id"])
            return _j({k: c[k] for k in c.keys()})
        if name == "set_customer_number":
            return _j(core.set_customer_number(conn, int(args["customer_id"]),
                                               args.get("number", "")))
        if name == "next_customer_number":
            return _j({"vorschlag": core.next_customer_number(conn)})
        if name == "set_customer_email":
            core.set_customer_email(conn, int(args["customer_id"]), args["email"])
            return _j({"ok": True, "customer_id": int(args["customer_id"]), "email": args["email"]})
        if name == "delete_customer":
            return _j(core.delete_customer(conn, int(args["customer_id"])))
        if name == "merge_duplicate_customers":
            return _j(core.merge_duplicate_customers(conn))

        # ── Ausgaben ──
        if name == "mark_expense_paid":
            core.mark_expense_paid(conn, int(args["expense_id"]), args["paid_date"])
            return _j({"ok": True})

        # ── Bank / Zahlungen ──
        if name == "import_bank_csv":
            text = _safe_path(args["path"]).read_text(encoding="utf-8", errors="replace")
            return _j(core.import_bank_csv(conn, text))
        if name == "create_transaction":
            tid = core.create_manual_transaction(
                conn, args["date"], args["description"], args["amount"],
                args.get("currency", "EUR"), args.get("method", "Manuell"))
            return _j(_tx_view(core.get_transaction(conn, tid)))
        if name == "set_transaction_category":
            core.set_tx_category(conn, int(args["tx_id"]), args["category"], args.get("note"))
            return _j(_tx_view(core.get_transaction(conn, int(args["tx_id"]))))
        if name == "book_transaction_as_expense":
            tid = int(args["tx_id"])
            t = _need_tx(conn, tid)
            if t["amount"] >= 0:
                return _j({"error": f"Buchung #{tid} ist ein Eingang – daraus wird keine Ausgabe."})
            core.create_expense_from_tx(conn, tid, args["category"], args.get("vat_rate", 0),
                                        private_share=args.get("private_share", 0))
            return _j(_tx_view(core.get_transaction(conn, tid)))
        if name == "split_transaction":
            tid = int(args["tx_id"])
            _need_tx(conn, tid)
            core.create_split_from_tx(conn, tid, args["lines"])
            return _j(_tx_view(core.get_transaction(conn, tid)))
        if name == "link_transaction_to_invoice":
            tid = int(args["tx_id"])
            _need_tx(conn, tid)
            inv = _need_invoice(conn, args["number"])
            core.link_tx_invoice(conn, tid, inv["id"])
            return _j({"ok": True, "tx_id": tid, "number": inv["number"]})
        if name == "reset_transaction":
            tid = int(args["tx_id"])
            _need_tx(conn, tid)
            core.reset_tx(conn, tid)
            return _j(_tx_view(core.get_transaction(conn, tid)))
        if name == "delete_transaction":
            core.delete_transaction(conn, int(args["tx_id"]))
            return _j({"deleted": int(args["tx_id"])})
        if name == "list_rules":
            return _j({k: {kk: r[kk] for kk in r.keys()}
                       for k, r in core.get_rules_map(conn).items()})

        # ── Belege ──
        if name == "add_document":
            p = _safe_path(args["path"])
            did = core.add_document(conn, p.read_bytes(), p.name, vendor=args.get("vendor"),
                                    amount=args.get("amount"), doc_date=args.get("doc_date"))
            return _j(_doc_view(conn, core.get_document(conn, did)))
        if name == "suggest_document_match":
            did = int(args["document_id"])
            doc = core.get_document(conn, did)
            if not doc:
                return _j({"error": "Beleg nicht gefunden"})
            tx = core.suggest_tx_for_document(conn, doc["amount"], doc["doc_date"], doc["vendor"])
            if tx:
                conn.execute("UPDATE documents SET suggest_tx = ? WHERE id = ?", (tx, did))
                conn.commit()
            return _j(_doc_view(conn, core.get_document(conn, did)))
        if name == "split_document":
            did = int(args["document_id"])
            if not core.get_document(conn, did):
                return _j({"error": "Beleg nicht gefunden"})
            core.split_document(conn, did, args["lines"],
                                tx_id=int(args["tx_id"]) if args.get("tx_id") else None)
            return _j(_doc_view(conn, core.get_document(conn, did)))
        if name == "delete_document":
            core.delete_document(conn, int(args["document_id"]))
            return _j({"deleted": int(args["document_id"])})

        # ── Stammdaten ──
        if name == "list_accounts":
            return _j(core.expense_accounts(cfg))
        if name == "add_account":
            return _j({"custom_accounts": core.add_custom_account(args["account"])})
        if name == "remove_account":
            return _j({"custom_accounts": core.remove_custom_account(args["account"])})
        if name == "list_recurring":
            return _j(core.list_recurring())
        if name == "add_recurring":
            return _j(core.add_recurring(args["client"], args["service"],
                                         args.get("interval", "monatlich"),
                                         args.get("amount", ""), args.get("next_run")))
        if name == "toggle_recurring":
            state = core.toggle_recurring(args["id"])
            if state is None:
                return _j({"error": "Vorlage nicht gefunden"})
            return _j({"id": args["id"], "active": state})
        if name == "delete_recurring":
            return _j({"deleted": core.delete_recurring(args["id"])})
        if name == "advance_recurring":
            new = core.advance_recurring(args["id"])
            if new is None:
                return _j({"error": "Vorlage nicht gefunden"})
            return _j({"id": args["id"], "next_run": new})
        if name == "list_dunning":
            return _j(core.dunning_cases(conn))
        if name == "advance_dunning":
            stage = core.advance_dunning(args["number"])
            return _j({"number": args["number"], "stage": stage,
                       "stage_name": core.DUNNING_STAGES[stage]})
        if name == "reset_dunning":
            core.reset_dunning(args["number"])
            return _j({"number": args["number"], "stage": 0})
        if name == "list_closed_periods":
            return _j(core.closed_periods())
        if name == "close_period":
            return _j({"closed_periods": core.close_period(args["token"])})
        if name == "reopen_period":
            return _j({"closed_periods": core.reopen_period(args["token"])})
        if name == "get_settings":
            s = dict(core.load_settings())
            for k in ("api_key", "smtp_pass"):
                if s.get(k):
                    s[k] = "········"
            keys = args.get("keys")
            return _j({k: s.get(k) for k in keys} if keys else s)
        if name == "set_settings":
            vals = args["values"]
            if not isinstance(vals, dict):
                return _j({"error": "values muss ein Objekt sein."})
            core.update_settings(**vals)
            return _j({"ok": True, "updated": sorted(vals)})
        if name == "fx_rate":
            quote = args.get("quote") or core.home_currency()
            return _j({"base": args["base"].upper(), "quote": quote.upper(),
                       "date": args.get("date") or core.date.today().isoformat(),
                       "rate": core.fx_rate(conn, args["base"], quote, args.get("date"))})

        # ── Entwickler-Tools ──
        if name in ("run_shell", "read_file", "write_file"):
            if not dev_enabled():
                return _j({"error": "Entwickler-Modus ist deaktiviert."})
            if name == "run_shell":
                p = subprocess.run(args["command"], shell=True, cwd=str(core.ROOT),
                                   capture_output=True, text=True, timeout=120)
                return _j({"stdout": p.stdout[-6000:], "stderr": p.stderr[-3000:], "code": p.returncode})
            if name == "read_file":
                return _safe_path(args["path"]).read_text(encoding="utf-8")[:20000]
            if name == "write_file":
                dest = _safe_path(args["path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(args["content"], encoding="utf-8")
                return _j({"written": args["path"]})
        if name == "bank_import_transactions":
            return _j(wirex.import_transactions(int(args.get("page_size") or 50)))
        if wirex.handles(name):
            return wirex.call(name, args)
        return _j({"error": f"Unbekanntes Tool: {name}"})
    except Exception as e:  # noqa
        return _j({"error": str(e)})


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _money_de(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    s = f"{v:,.2f}".replace(",", "␟").replace(".", ",").replace("␟", ".")
    return "€" + s


def _figures_card(r: dict, link: dict | None) -> dict:
    """Deterministische Zahlen-Karte aus dem figures-Ergebnis – die App füllt die Werte,
    NICHT das LLM. So kann keine Zahl mehr halluziniert werden."""
    netto, brutto = r.get("einnahmen_netto"), r.get("einnahmen_brutto")
    items = []
    if netto is not None and brutto is not None and abs(float(netto) - float(brutto)) >= 0.01:
        items.append({"title": "Einnahmen (netto)", "amount": _money_de(netto)})
        items.append({"title": "Einnahmen (brutto)", "amount": _money_de(brutto)})
    else:
        items.append({"title": "Einnahmen", "amount": _money_de(r.get("einnahmen"))})
    items.append({"title": "Ausgaben", "amount": "−" + _money_de(r.get("ausgaben"))})
    items.append({"title": "Gewinn", "amount": _money_de(r.get("gewinn"))})
    card = {"type": "list", "_auto": True, "title": r.get("zeitraum", ""), "items": items}
    if link:
        card["link"] = link
    return card


_PASSKEY_TEXTS = {
    "wirex_onboard_start": ("Konto einrichten", "Lege deinen Passkey an – danach werden IBAN und Karte erstellt."),
    "wirex_session_start": ("Konto entsperren", "Einmal bestätigen, dann kann ich Guthaben und Umsätze lesen."),
    "wirex_card_details_start": ("Kartendaten anzeigen", "Bestätige, um Nummer und CVV zu sehen."),
    "wirex_card_pin_start": ("PIN anzeigen", "Bestätige, um die PIN deiner Karte zu sehen."),
    "wirex_card_action_start": ("Karte ändern", "Bestätige die Änderung an deiner Karte."),
    "wirex_sepa_transfer_start": ("Überweisung freigeben", "Bestätige die SEPA-Überweisung."),
    "wirex_withdraw_start": ("Auszahlung freigeben", "Bestätige die Auszahlung."),
}


def _passkey_card(tool: str, out: str) -> dict | None:
    """Aus einer Tool-Antwort mit approval_url eine Karte bauen. None, wenn keine drin ist.
       Das zugehörige *_result-Tool ruft das Frontend nach dem Tap selbst auf."""
    try:
        data = json.loads(out)
    except Exception:  # noqa
        return None
    if not isinstance(data, dict):
        return None
    d = data.get("data") if isinstance(data.get("data"), dict) else data
    url = d.get("approval_url") or data.get("approval_url")
    if not url:
        return None
    title, subtitle = _PASSKEY_TEXTS.get(tool, ("Mit Passkey bestätigen",
                                                "Bestätige den Vorgang mit Face ID oder Fingerabdruck."))
    return {"type": "passkey", "title": title, "subtitle": subtitle, "url": url,
            "session_id": d.get("session_id") or data.get("session_id") or "",
            "result_tool": tool[:-6] + "_result" if tool.endswith("_start") else ""}


def _invoice_form_card(args: dict) -> dict:
    """Aus einem (gesperrten) create_invoice-Versuch eine vorbelegte Formular-Karte bauen."""
    items = args.get("items") or []
    total = 0.0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            total += float(it.get("unit_price") or 0) * float(it.get("quantity") or 1)
        except (TypeError, ValueError):
            pass
    first = items[0] if items and isinstance(items[0], dict) else {}
    iv = {"customer_name": args.get("customer_name") or "",
          "service": first.get("description") or "",
          "kind": args.get("kind") if args.get("kind") in ("domestic", "eu", "third") else "domestic"}
    if total > 0:
        iv["amount"] = round(total, 2)
    try:
        if first.get("vat_rate") is not None:
            iv["vat_rate"] = int(first["vat_rate"])
    except (TypeError, ValueError):
        pass
    return {"type": "invoice_form", "_auto": True, "invoice": iv}


def _yearly_card(rows: list, link: dict | None) -> dict:
    """Deterministischer Balken-Graph Einnahmen (brutto) pro Jahr – App füllt die Werte."""
    bars = [{"label": str(r.get("jahr")), "value": float(r.get("einnahmen") or 0),
             "amount": _money_de(r.get("einnahmen"))} for r in rows]
    card = {"type": "chart_bar", "_auto": True, "subtitle": "Einnahmen pro Jahr", "bars": bars}
    if link:
        card["link"] = link
    return card


# ── Chat-Schleife ────────────────────────────────────────────────────────
def _banking_state() -> str:
    """Das lokal gespeicherte Wirex-Konto in den Prompt geben. Ohne das fragt das
       Modell nach jedem Neustart wieder nach Name und E-Mail, obwohl beides bekannt ist."""
    acc = wirex.account()
    envtag = ("UMGEBUNG: SANDBOX (Base Sepolia, Testgeld – nutze wirex_mint_sandbox für Guthaben, "
              "kein echtes Geld). " if wirex.is_sandbox() else "UMGEBUNG: LIVE (echtes Geld). ")
    if not acc.get("wallet"):
        return (envtag + "BANKING-STAND: Es ist noch KEIN Konto registriert. Das Einrichten läuft "
                "über den Button 'Konto einrichten' im Banking-Tab (deterministischer Ablauf mit "
                "beiden Passkey-Signaturen) – rufe wirex_onboard_start NICHT selbst auf, sondern "
                "verweise den Nutzer auf diesen Button. ")
    known = ", ".join(f"{k}={acc[k]}" for k in ("username", "email", "wallet", "credential_id")
                      if acc.get(k))
    out = (envtag + f"BANKING-STAND: Lokal bekannt ({known}). Frage diese Daten NIE erneut ab. "
           "Guthaben, IBAN, Karte und Umsätze sind NICHT lokal. "
           "WICHTIG zur Deutung von wirex_status: `status:\"not_registered\"` bedeutet, das Konto "
           "ist bei Wirex NOCH NICHT angelegt (z. B. Onboarding/KYC abgebrochen) – das ist KEIN "
           "Session-Ablauf und KEIN Fehler. Sage dann NICHT 'Session abgelaufen' und starte KEINE "
           "neue Session in Schleife; verweise stattdessen auf den Button 'Konto einrichten' im "
           "Banking-Tab, um das Onboarding abzuschliessen. Nur der Text `session_missing_or_expired` "
           "heisst wirklich abgelaufen. ")
    if wirex.session():
        out += ("Es läuft bereits eine aktive Lese-Session: rufe wirex_status/wirex_wallet DIREKT "
                "auf und starte KEIN neues wirex_session_start (das würde nur eine überflüssige "
                "Passkey-Abfrage erzeugen). ")
    else:
        out += ("Für Reads zuerst EINMAL wirex_session_start; danach reicht die Session für "
                "alle weiteren Reads – nicht wiederholen. ")
    if wirex.kyc_done():
        return out + "Die Verifizierung ist abgeschlossen. "
    link = acc.get("kyc_url")
    out += (f"WICHTIG – die KYC ist NOCH OFFEN (Stand: {wirex.kyc_label()}). Ohne sie gibt es "
            "KEINE IBAN und KEINE Karte; melde niemals 'keine Konten vorhanden', ohne diesen "
            "Grund zu nennen. ")
    out += (f"Der gespeicherte Verifizierungs-Link ist: {link} – gib ihn dem Nutzer, statt zu "
            "sagen, es sei keiner verfügbar. " if link else
            "Es ist noch kein Link gespeichert: hol ihn mit wirex_status (die E-Mail wird "
            "automatisch mitgeschickt) und nenne ihn dem Nutzer. ")
    return out


def _system_prompt() -> str:
    cfg = core.load_config()
    biz = cfg.get("business", {}).get("name", "der Nutzer")
    base = (
        f"Du bist der Assistent einer lokalen Buchhaltungs-App für {biz} (Freelancer, Deutschland, "
        f"regelbesteuert). Heute ist {date.today().isoformat()}. "
        "Du bedienst die App über die bereitgestellten Tools – erfinde keine Daten, sondern rufe Tools auf. "
        "ABSOLUTE REGEL für ZAHLEN: Nenne NIE einen Geldbetrag, eine Summe oder Kennzahl, die du nicht GERADE "
        "über ein Tool (figures/report/list_invoices/list_expenses/vat/…) erhalten hast. NIE schätzen, runden, "
        "aus dem Gedächtnis nehmen oder plausibel wirkende Zahlen erfinden. Für Einnahmen/Ausgaben/Gewinn eines "
        "Zeitraums rufe 'figures' GENAU EINMAL auf (ein Aufruf liefert netto UND brutto). Danach zeigt die App "
        "AUTOMATISCH eine exakte Kennzahl-Karte – ANTWORTE dann SOFORT mit HÖCHSTENS EINEM kurzen Satz OHNE "
        "Beträge (z. B. 'Hier deine Zahlen für 2025.') und rufe 'figures' NICHT erneut auf. Baue KEINE eigene "
        "Zahlen-Karte. Widersprechen sich zwei Zahlen, rechne per Tool nach und nenne den ECHTEN Grund – DICHTE "
        "NIEMALS eine Erklärung dazu. Kennst du eine Zahl nicht, ruf das Tool auf oder sag es ehrlich. "
        "Bei 'JAHR FÜR JAHR' / 'pro Jahr' / 'alle Jahre' / 'Verlauf' rufe 'figures_yearly' auf (GENAU EINMAL) – "
        "es liefert nur Jahre MIT Daten und die App zeigt einen Balken-Graphen; NICHT 'figures' pro Jahr aufrufen. "
        "Fasse dich kurz und antworte auf Deutsch. Bevor du etwas anlegst oder änderst, nenne kurz was du tust. "
        "Beträge sind in Euro; EÜR und USt-VA folgen der Ist-Besteuerung. "
        "STEUERN: Fragen nach EINKOMMENSTEUER, Steuerlast, Nachzahlung oder Rücklage beantwortest du mit "
        "'tax_reserve' – die App rechnet den §32a-Tarif selbst, sag NIE, du könntest keine Einkommensteuer "
        "berechnen. Gibt der Nutzer künftige Einnahmen vor, rechne den geplanten Jahresgewinn aus "
        "(figures/euer für den Ist-Stand) und übergib ihn als 'gewinn_jahr'. Nenne das Ergebnis als "
        "Schätzung ohne Steuerberatung und weise auf die Annahmen hin (keine weiteren Einkünfte, "
        "Kranken-/Rentenversicherung nur soweit in den Einstellungen hinterlegt). "
        "Zeige Ergebnisse als KARTEN über das present-Tool, wann immer es passt: Kennzahlen als stat, "
        "Aufschlüsselungen als chart_donut/chart_bar, Aufzählungen als list. Alles, was der Nutzer bestätigen "
        "soll (buchen, senden, Mahnung), IMMER als confirm_action-Karte – handle NIE ungefragt, "
        "sondern lass den Nutzer per Karte bestätigen. "
        "Will der Nutzer eine RECHNUNG ERSTELLEN, frage NICHT in Prosa nach den Daten, sondern zeige SOFORT eine "
        "invoice_form-Karte (übernimm vorbelegt, was er schon genannt hat). Das Formular erledigt die Erfassung. "
        "Wenn ein BELEG/eine Rechnung HOCHGELADEN wurde, zeige das Ergebnis als doc_card (fields = Händler, Betrag, "
        "USt, Datum; details = USt-IdNr., Netto, Zahlart, SKR-Kontovorschlag; confirm_label 'Zuordnen & buchen', "
        "confirm_message = Anweisung, die passende Buchung/Zuordnung auszuführen) – prüfe vorher mit "
        "list_transactions, ob es eine passende offene Zahlung gibt. "
        "BELEGE & ZUORDNEN: Hochgeladene Belege im Posteingang siehst du mit list_documents (Status "
        "'to_review' = noch offen, 'matched' = schon verbucht) – rate NIE über hochgeladene Dateien, ruf das "
        "Tool auf. Zahlungen OHNE zugeordneten Beleg findest du mit list_transactions(missing_receipt=true). "
        "Einen Beleg verbuchst du mit book_document: gibt es eine passende offene Zahlung (siehe vorschlag_tx_id, "
        "gleicher Betrag/Anbieter), ordne den Beleg mit tx_id ZU; sonst buche ihn OHNE tx_id als eigenständige "
        "Ausgabe. Wähle ein sinnvolles SKR03-Konto (Telefon 4920, Internet 4925) und Steuersatz (i. d. R. 19). "
        "Handelt es sich um mehrere gleichartige Belege, arbeite sie der Reihe nach ab und melde am Ende die Summe. "
        "BANKING (Nuri-Karte, EUR-IBAN, Guthaben, SEPA): läuft AUSSCHLIESSLICH über die wirex_*-Tools. "
        "set_settings ist dafür NIE zuständig – erfinde KEINE Settings-Keys wie 'banking_enabled'. "
        "'Banking freischalten' / 'Konto einrichten' / 'Karte beantragen' -> wirex_onboard_start (braucht "
        "username + email; frag kurz danach, wenn du sie nicht hast). Lesen (Kontostand, IBAN, Karte, "
        "Kartenumsätze) braucht eine Session: wirex_session_start OHNE Argumente aufrufen, dann erst "
        "wirex_status/wirex_wallet/wirex_card_transactions. Jedes wirex-Tool, das eine approval_url "
        "zurückgibt, verlangt einen Passkey-Tap: zeig dem Nutzer den Link und warte, bis er bestätigt hat, "
        "bevor du das zugehörige *_result-Tool aufrufst. Geld bewegen (SEPA, Withdraw) NUR nach "
        "confirm_action-Karte. "
        + _banking_state()
        + "STIL: extrem knapp. KEIN Vorspann wie 'Ich werde…', KEINE Ankündigung von Tool-Aufrufen, KEINE "
        "Wiederholung der Frage, KEINE Zusammenfassung. Wenn du eine Karte zeigst, schreib HÖCHSTENS EINEN "
        "kurzen Satz dazu (oft gar keinen). Verweise NIE auf den Button ('Details unten') – er ist selbsterklärend."
    )
    if dev_enabled():
        base += (" Entwickler-Modus ist AKTIV: du kannst mit run_shell/read_file/write_file den Code der App "
                 "im Projektordner ändern. Sei vorsichtig, erkläre Änderungen und teste sie.")
    return base


def _post(base_url: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "http://localhost", "X-Title": "Buchhaltung"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _clean_msg(m: dict) -> dict:
    """Nur API-gültige Felder ans LLM schicken (interne Zusätze wie _display entfernen)."""
    keep = ("role", "content", "tool_calls", "tool_call_id", "name")
    return {k: v for k, v in m.items() if k in keep and v is not None}


# ── PDF-Belege im Chat: Text extrahieren und vom LLM einordnen lassen ─────
def extract_pdf_text(data: bytes) -> str:
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(parts).strip()


_DOC_ANALYSIS = (
    "Ich habe ein PDF-Dokument hochgeladen (Dateiname: {filename}). Analysiere den Text unten "
    "und ordne es korrekt ein:\n"
    "1) Ist es eine EINGEHENDE Rechnung / ein Beleg AN MICH (z. B. Handyrechnung von Vodafone/Telekom, "
    "Software/SaaS, Hosting, Domain, Fachliteratur)? Dann als Betriebsausgabe mit create_expense buchen: "
    "vendor (Anbieter), gross (Bruttobetrag), vat_rate (meist 19), date (Rechnungsdatum JJJJ-MM-TT), "
    "ein passendes SKR03-Konto als category. WICHTIG: Bei Telefon/Handy private_share=80 setzen "
    "(nur 20 % betrieblich absetzbar). Nenne mir vorher in EINEM Satz, was du buchst.\n"
    "2) Ist es eine AUSGEHENDE Rechnung von MIR ({absender}) an einen Kunden? "
    "Dann NICHT neu anlegen – sag mir nur kurz, um welche Rechnung es geht (Nummer/Kunde/Betrag).\n"
    "3) Rein privater Beleg? Dann nicht buchen, nur kurz sagen.\n"
    "Extrahiere Datum, Anbieter, Netto/Brutto und USt-Satz aus dem Text.\n\n"
    "--- PDF-TEXT ---\n{text}"
)


def extract_document(data: bytes, filename: str) -> dict:
    """Best-effort OCR-Extraktion aus PDF-Text: {vendor, amount, date}. {} wenn nicht möglich."""
    try:
        text = extract_pdf_text(data)
    except Exception:
        return {}
    if not text or not is_configured():
        return {}
    s = settings()
    prompt = ("Extrahiere aus diesem Beleg/Rechnungstext AUSSCHLIESSLICH JSON in genau dieser Form: "
              '{"vendor":"Anbietername","amount":Bruttobetrag_als_Zahl,"date":"JJJJ-MM-TT"}. '
              "Keine Erklärung, nur das JSON.\n\n--- TEXT ---\n" + text[:6000])
    try:
        data_r = _post(s["base_url"], s["api_key"],
                       {"model": s["model"], "messages": [{"role": "user", "content": prompt}]})
        content = data_r["choices"][0]["message"]["content"]
        import re
        m = re.search(r"\{.*\}", content, re.S)
        obj = json.loads(m.group(0)) if m else {}
        return {"vendor": obj.get("vendor"), "amount": obj.get("amount"), "date": obj.get("date")}
    except Exception:
        return {}


def analyze_pdf(data: bytes, filename: str) -> dict:
    """Baut aus einem hochgeladenen PDF die LLM-Nachricht (voller Text) + kurze Anzeige."""
    try:
        text = extract_pdf_text(data)
    except Exception as e:
        return {"empty": True, "reply": f"PDF konnte nicht gelesen werden: {e}"}
    if not text:
        return {"empty": True,
                "reply": "Die PDF enthält keinen auslesbaren Text (vermutlich ein Scan/Bild). "
                         "Bitte die Ausgabe manuell erfassen oder eine Text-PDF hochladen."}
    try:
        biz = core.load_config().get("business", {})
        absender = biz.get("name") or biz.get("owner") or "mir selbst"
    except Exception:
        absender = "mir selbst"
    content = _DOC_ANALYSIS.format(filename=filename, absender=absender, text=text[:8000])
    return {"empty": False, "content": content, "display": f"📎 {filename} hochgeladen"}


def _mit_fehler(history: list, text: str, log: list) -> dict:
    """Fehler als Assistenten-Nachricht in den Verlauf legen.

    Die Oberflaeche zeigt nur den Verlauf, nicht 'reply' – ohne das bliebe der
    Chat bei einem API-Fehler einfach stumm, und niemand weiss, warum.
    """
    return {"reply": text, "messages": list(history) + [{"role": "assistant", "content": text}],
            "log": log}


def run(history: list) -> dict:
    """history: OpenAI-Nachrichten (ohne System). Gibt reply, neue history, tool-log zurück."""
    s = settings()
    if not is_configured():
        return {"reply": "Bitte zuerst unter Einstellungen einen LLM-Zugang hinterlegen.",
                "messages": history, "log": []}
    msgs = [{"role": "system", "content": _system_prompt()}] + history
    log = []
    blocks = []
    report_link = None
    auto_figures = False
    seen_fig = set()
    fig_calls = 0
    invoice_form_shown = False
    try:
        for _ in range(10):
            data = _post(s["base_url"], s["api_key"], {
                "model": s["model"], "messages": [_clean_msg(m) for m in msgs],
                "tools": tools_for(), "tool_choice": "auto",
                # Ohne Angabe reservieren manche Anbieter das Modellmaximum und
                # lehnen die Anfrage bei knappem Guthaben ab. Antworten hier sind
                # kurz; 4096 reicht auch fuer lange Tool-Ketten.
                "max_tokens": int(s.get("max_tokens") or 4096)})
            if "choices" not in data:
                return {"reply": f"Antwort ohne Ergebnis: {json.dumps(data)[:500]}",
                        "messages": msgs[1:], "log": log}
            msg = data["choices"][0]["message"]
            msgs.append(msg)
            calls = msg.get("tool_calls")
            if not calls:
                if auto_figures:
                    # LLM-eigene Kennzahl-/Vergleichsgrafiken verwerfen – es gilt NUR die App-Karte
                    blocks = [b for b in blocks if b.get("_auto")
                              or b.get("type") not in ("stat", "kpi", "chart_bar", "compare")]
                if blocks:
                    msg["_blocks"] = blocks
                return {"reply": msg.get("content") or "", "messages": msgs[1:], "log": log}
            for c in calls:
                fn = c["function"]["name"]
                try:
                    a = json.loads(c["function"].get("arguments") or "{}")
                except Exception:
                    a = {}
                if fn == "present":
                    # Deterministischen Report-Deeplink IMMER erzwingen (das LLM setzt sonst
                    # gern falsche Perioden wie Q4 statt des ganzen Jahres):
                    if report_link:
                        a["link"] = report_link
                    if a.get("type") == "invoice_form":
                        if invoice_form_shown:
                            out = "Das Rechnungs-Formular ist bereits sichtbar."
                            log.append({"tool": fn, "args": a})
                            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
                            continue
                        invoice_form_shown = True
                    blocks.append(a)
                    out = "Karte angezeigt."
                elif fn == "create_invoice":
                    # HARTE SPERRE: im Chat wird NIE eine Rechnung direkt angelegt. Egal was das
                    # Modell aufruft – es erscheint immer die Maske, die der Nutzer selbst absendet.
                    if not invoice_form_shown:
                        blocks.append(_invoice_form_card(a))
                        invoice_form_shown = True
                    out = _j({"nicht_ausgefuehrt": True,
                              "grund": "Rechnungen werden im Chat NIE direkt angelegt.",
                              "stattdessen": "Dem Nutzer wurde das ausfüllbare Rechnungs-Formular "
                                             "angezeigt; er erstellt die Rechnung dort selbst.",
                              "jetzt": "Antworte mit HÖCHSTENS einem kurzen Satz und rufe weder "
                                       "create_invoice noch send_invoice/regenerate_invoice_pdf auf."})
                else:
                    out = dispatch(fn, a)
                    # Passkey-Links NIE als roher Text: die App hängt selbst eine
                    # Karte an, das Modell soll die URL gar nicht erst nennen.
                    if wirex.handles(fn):
                        card = _passkey_card(fn, out)
                        if card:
                            blocks.append(card)
                            out = _j({"approval_url_angezeigt": True,
                                      "hinweis": "Dem Nutzer wurde eine Passkey-Karte mit Button "
                                                 "gezeigt. Nenne die URL NICHT und rufe das "
                                                 "*_result-Tool NICHT auf – die Karte erledigt das. "
                                                 "Antworte mit HÖCHSTENS einem kurzen Satz."})
                    if fn == "figures_yearly":
                        report_link = {"label": "Berichte öffnen", "href": "/reports"}
                        try:
                            if "yearly" not in seen_fig:
                                blocks.append(_yearly_card(json.loads(out), report_link))
                                seen_fig.add("yearly")
                            auto_figures = True
                            fig_calls += 1
                        except Exception:  # noqa
                            pass
                    # Deterministischer Report-Deeplink aus dem Tool-Aufruf merken
                    try:
                        y = int(a.get("year")) if a.get("year") else None
                        q = int(a.get("quarter")) if a.get("quarter") else None
                    except (TypeError, ValueError):
                        y = q = None
                    if fn == "figures" and a.get("period"):
                        _lo, _hi, _lbl, tok = core.period_bounds_token(a.get("period"))
                        _gen = "bwa" if "-Q" not in tok and "-" in tok else "eur"
                        report_link = {"label": "Im Bericht ansehen",
                                       "href": f"/reports?p={tok}&gen={_gen}"}
                        # Zahlen-Karte deterministisch SELBST anhängen (nicht das LLM), 1x pro Zeitraum:
                        try:
                            if tok not in seen_fig:
                                blocks.append(_figures_card(json.loads(out), report_link))
                                seen_fig.add(tok)
                            auto_figures = True
                        except Exception:  # noqa
                            pass
                    elif fn == "vat" and y and q:
                        report_link = {"label": "Vollständigen Bericht ansehen",
                                       "href": f"/reports?p={y}-Q{q}&gen=ustva"}
                    elif fn == "zm" and y and q:
                        report_link = {"label": "ZM ansehen", "href": f"/reports?p={y}-Q{q}&gen=zm"}
                    elif fn == "report" and y:
                        report_link = {"label": "EÜR ansehen", "href": f"/reports?p={y}&gen=eur"}
                    elif fn == "vat_annual" and y:
                        report_link = {"label": "USt-Jahresabschluss ansehen",
                                       "href": f"/reports?p={y}&gen=ustva"}
                if fn == "figures":
                    fig_calls += 1
                log.append({"tool": fn, "args": a})
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": out})
            # Schleifen-Schutz: ruft das Modell figures mehrfach, ohne fertig zu werden ->
            # mit der bereits erzeugten App-Karte beenden (keine Endlosschleife).
            if auto_figures and fig_calls >= 2:
                final = {"role": "assistant", "content": "Hier deine Zahlen."}
                if blocks:
                    final["_blocks"] = [b for b in blocks if b.get("_auto")]
                msgs.append(final)
                return {"reply": final["content"], "messages": msgs[1:], "log": log}
        return {"reply": "(Abbruch: zu viele Tool-Schritte)", "messages": msgs[1:], "log": log}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        return _mit_fehler(history, f"API-Fehler {e.code}: {detail}", log)
    except Exception as e:  # noqa
        return _mit_fehler(history, f"Fehler: {e}", log)


# ── Claude Code als Agent-Backend (für das Chat-Bubble-Widget) ───────────
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def claude_available() -> bool:
    return bool(CLAUDE_BIN) and os.path.exists(CLAUDE_BIN)


def run_claude(message: str, session_id: str | None = None) -> dict:
    """Eine Nachricht an Claude Code (non-interactive) im Projektordner.
    Antwortet als Text und darf die App ändern. Kontext via --resume."""
    if not claude_available():
        return {"text": "Claude Code ('claude') ist nicht installiert/gefunden.",
                "session_id": session_id, "error": True}
    harness = (
        "Antworte extrem knapp – wie ein TL;DR. Kein Vorwort, keine Wiederholung der Frage, "
        "keine abschließende Zusammenfassung, keine Tabellen. Am liebsten 1–3 Sätze, nur das Nötigste. "
        "Wenn du Code geändert hast: EIN Satz – was, und ob ein Server-Neustart nötig ist."
    )
    model = settings().get("claude_model") or "sonnet"   # schneller als opus; per Einstellung überschreibbar
    cmd = [CLAUDE_BIN, "-p", message, "--output-format", "json",
           "--dangerously-skip-permissions", "--model", model,
           "--append-system-prompt", harness]
    if session_id:
        cmd += ["--resume", session_id]
    try:
        p = subprocess.run(cmd, cwd=str(core.ROOT), capture_output=True,
                           text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"text": "(Zeitüberschreitung nach 30 min)", "session_id": session_id, "error": True}
    out = (p.stdout or "").strip()
    try:
        data = json.loads(out)
        return {"text": data.get("result") or "(keine Antwort)",
                "session_id": data.get("session_id") or session_id,
                "error": bool(data.get("is_error"))}
    except Exception:
        return {"text": (out or p.stderr or "(keine Ausgabe)")[-4000:],
                "session_id": session_id, "error": True}
