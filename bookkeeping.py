#!/usr/bin/env python3
"""
Kommandozeilen-Buchhaltung für Freelancer.
Dünne CLI über core.py – dieselbe Logik nutzt auch die Web-App (app.py)
und der MCP-Server (mcp_server.py).

Übersicht:
  Rechnungen   invoice · list invoices · pay · send · pdf · payment · import
  Ausgaben     expense · list expenses
  Kunden       customers · customer add|email
  Bank         bank-import · tx list|show|category|expense|split|link|reset|
               delete|receipt|manual|bulk-category|bulk-expense|rules
  Belege       doc add|list|show|suggest|match|confirm|split|delete
  Auswertung   report · figures · vat · vat-annual · zm · period
  Stammdaten   accounts · recurring · dunning · settings · fx
  Werkzeuge    tools · call   (identische Schnittstelle wie Chat/MCP)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import core
from core import eur


# ── Ausgabe-Helfer ───────────────────────────────────────────────────────
def _plain(obj):
    """sqlite3.Row / Decimal / Path in JSON-fähige Strukturen wandeln."""
    if hasattr(obj, "keys") and not isinstance(obj, dict):
        return {k: _plain(obj[k]) for k in obj.keys()}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def emit(args, obj, text=None) -> None:
    """Bei --json maschinenlesbar, sonst den Text (oder JSON als Fallback)."""
    if getattr(args, "json", False):
        print(json.dumps(_plain(obj), ensure_ascii=False, indent=2, default=str))
    elif text is not None:
        print(text)
    else:
        print(json.dumps(_plain(obj), ensure_ascii=False, indent=2, default=str))


def fail(msg: str):
    sys.exit(f"✗ {msg}")


def need_cfg(cfg):
    if cfg is None:
        fail("Keine config.toml gefunden – cp config.example.toml config.toml")
    return cfg


def read_text_file(path: str) -> str:
    raw = Path(path).expanduser().read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_lines(specs: list[str]) -> list[dict]:
    """Split-Positionen: 'KONTO|BETRAG|USTSATZ|PRIVATANTEIL' (die letzten zwei optional)."""
    out = []
    for spec in specs or []:
        parts = [p.strip() for p in spec.split("|")]
        if len(parts) < 2:
            fail(f"Position '{spec}' braucht mindestens KONTO|BETRAG.")
        out.append({"category": parts[0] or "Sonstiges", "amount": parts[1],
                    "vat_rate": int(parts[2]) if len(parts) > 2 and parts[2] else 0,
                    "private_share": float(parts[3]) if len(parts) > 3 and parts[3] else 0})
    return out


def parse_items(specs: list[str], default_rate) -> list[dict]:
    """Rechnungspositionen: 'BESCHREIBUNG|MENGE|EINHEIT|EINZELPREIS|USTSATZ'."""
    out = []
    for spec in specs or []:
        p = [x.strip() for x in spec.split("|")]
        if len(p) < 4:
            fail(f"Position '{spec}' braucht BESCHREIBUNG|MENGE|EINHEIT|EINZELPREIS[|USTSATZ].")
        out.append({"description": p[0], "quantity": p[1] or "1", "unit": p[2] or "",
                    "unit_price": p[3],
                    "vat_rate": int(p[4]) if len(p) > 4 and p[4] else int(default_rate)})
    return out


def find_invoice(conn, ident: str):
    """Rechnung über Nummer oder numerische ID finden."""
    inv = core.get_invoice_by_number(conn, ident)
    if inv is None and str(ident).isdigit():
        inv = core.get_invoice(conn, int(ident))
    if inv is None:
        fail(f"Rechnung '{ident}' nicht gefunden.")
    return inv


# ── Eingabe-Helfer (interaktiver Modus) ──────────────────────────────────
def ask(prompt, default=None, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw or not required:
            return raw
        print("  ! Pflichtfeld, bitte ausfüllen.")


def ask_date(prompt, default=None):
    d = default or date.today()
    while True:
        raw = ask(prompt + " (JJJJ-MM-TT)", default=d.isoformat())
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            print("  ! Format: JJJJ-MM-TT")


def ask_multiline(prompt):
    print(f"{prompt} (mehrzeilig, leere Zeile = fertig):")
    lines = []
    while True:
        line = input("  ").rstrip()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


# ── invoice ──────────────────────────────────────────────────────────────
def cmd_invoice(args, cfg):
    cfg = need_cfg(cfg)
    conn = core.db()
    default_rate = str(cfg["invoice"].get("default_vat_rate", 19))

    if args.item:                       # nicht-interaktiv (skript-/testbar)
        if not args.customer:
            fail("--customer ist im nicht-interaktiven Modus Pflicht.")
        cust = core.resolve_or_create_customer(conn, args.customer, args.address or "",
                                               args.vat_id)
        issue_date = args.date or date.today().isoformat()
        inv = core.create_invoice(
            conn, cfg, customer=cust, kind=args.kind, issue_date=issue_date,
            service_from=args.service_from or issue_date, service_to=args.service_to,
            items=parse_items(args.item, default_rate), notes=args.notes,
            number=args.number, paid_date=args.paid, render=not args.no_pdf)
        emit(args, inv,
             f"✓ Rechnung {inv['number']} angelegt\n"
             f"  Netto {eur(inv['net'])} | USt {eur(inv['vat'])} | Brutto {eur(inv['gross'])}\n"
             f"  PDF: {inv['pdf_path'] or '(nicht erzeugt)'}")
        return

    print("\n── Neue Ausgangsrechnung ───────────────────────────────")
    customers = core.list_customers(conn)
    if customers:
        print("Bestehende Kunden:")
        for c in customers:
            print(f"  [{c['id']}] {c['name']} – {(c['address'] or '').splitlines()[0]}")
    cid = ask("Customer-ID wählen (Enter = neuer Kunde)", required=False)
    if cid and core.get_customer(conn, int(cid)):
        cust = core.get_customer(conn, int(cid))
    else:
        name = ask("Kunde (Name/Firma)")
        address = ask_multiline("Kundenadresse")
        vat_id = ask("USt-IdNr. des Kunden (EU/Reverse-Charge, sonst Enter)", required=False)
        cust = core.create_customer(conn, name, address, vat_id)
    print(f"→ Kunde [{cust['id']}] {cust['name']}")

    print("\nRechnungstyp:  1) Inland   2) EU (Reverse Charge)   3) Drittland (UK/US/CH)")
    kind = {"1": "domestic", "2": "eu", "3": "third"}[ask("Auswahl", default="1")]
    if kind == "eu" and not cust["vat_id"]:
        print("  ⚠ Für EU-Reverse-Charge muss die USt-IdNr. des Kunden hinterlegt sein.")

    issue_date = ask_date("Rechnungsdatum")
    issue_d = datetime.strptime(issue_date, "%Y-%m-%d").date()
    service_from = ask_date("Leistungsdatum von", issue_d)
    service_to = ask("Leistungsdatum bis (JJJJ-MM-TT, Enter = einzelner Tag)", required=False) or None

    items = []
    print("\nPositionen (leere Beschreibung = fertig):")
    while True:
        desc = ask("  Leistung/Service", required=False)
        if not desc:
            if items:
                break
            print("  ! Mindestens eine Position nötig.")
            continue
        qty = ask("  Menge/Amount", default="1")
        unit = ask("  Einheit/Unit (Enter = keine)", required=False)
        price = ask("  Netto-Einzelpreis")
        rate = 0 if kind in ("eu", "third") else int(ask("  USt-Satz %", default=default_rate))
        items.append({"description": desc, "quantity": qty, "unit": unit,
                      "unit_price": price, "vat_rate": rate})
    notes = ask("Interne Notiz (nicht auf Rechnung)", required=False)

    invoice = core.create_invoice(
        conn, cfg, customer=cust, kind=kind, issue_date=issue_date,
        service_from=service_from, service_to=service_to, items=items, notes=notes)

    tag = {"domestic": "", "eu": "  [EU Reverse Charge]", "third": "  [Drittland]"}[kind]
    print(f"\n✓ Rechnung {invoice['number']} angelegt{tag}")
    print(f"  Netto {eur(invoice['net'])} | USt {eur(invoice['vat'])} | Brutto {eur(invoice['gross'])}")
    print(f"  PDF: {invoice['pdf_path']}")


# ── expense ──────────────────────────────────────────────────────────────
def cmd_expense(args, cfg):
    conn = core.db()
    default_rate = (need_cfg(cfg)["invoice"].get("default_vat_rate", 19)
                    if cfg else 19)

    if args.vendor and args.gross is not None:      # nicht-interaktiv
        d = args.date or date.today().isoformat()
        r = core.create_expense(
            conn, date_=d, vendor=args.vendor, description=args.description,
            category=args.category, gross=args.gross,
            vat_rate=args.vat_rate if args.vat_rate is not None else default_rate,
            paid_date=(args.paid if args.paid is not None else d) or None,
            receipt_src=args.receipt, private_share=args.private_share,
            reverse_charge=args.reverse_charge, vat_kind=getattr(args, "vat_kind", None))
        emit(args, r,
             f"✓ Ausgabe #{r['id']} erfasst: {args.vendor} · {args.category}\n"
             f"  Brutto {eur(r['gross'])} | Netto {eur(r['net'])} | Vorsteuer {eur(r['vat'])}")
        return

    print("\n── Neue Ausgabe / Eingangsbeleg ────────────────────────")
    d = ask_date("Belegdatum")
    vendor = ask("Lieferant/Anbieter")
    description = ask("Beschreibung", required=False)
    category = ask("Kategorie", default="Sonstiges")
    gross = ask("Bruttobetrag")
    rate = int(ask("Vorsteuersatz % (0 wenn keine)", default=str(default_rate)))
    paid = ask_date("Bezahlt am", datetime.strptime(d, "%Y-%m-%d").date())
    receipt = ask("Pfad zur Belegdatei (optional)", required=False)

    r = core.create_expense(conn, date_=d, vendor=vendor, description=description,
                            category=category, gross=gross, vat_rate=rate,
                            paid_date=paid, receipt_src=receipt or None)
    print(f"\n✓ Ausgabe erfasst: {vendor} · {category}")
    print(f"  Brutto {eur(r['gross'])} | Netto {eur(r['net'])} | Vorsteuer {eur(r['vat'])}")


# ── pay / send / pdf / payment ───────────────────────────────────────────
def cmd_pay(args, cfg):
    conn = core.db()
    paid = args.date or date.today().isoformat()
    if args.kind == "invoice":
        inv = find_invoice(conn, args.ident)
        core.mark_invoice_paid(conn, inv["id"], paid)
        emit(args, {"number": inv["number"], "paid_date": paid},
             f"✓ Rechnung {inv['number']} als bezahlt am {paid} markiert.")
    else:
        if not core.list_expenses(conn) or not any(
                e["id"] == int(args.ident) for e in core.list_expenses(conn)):
            fail(f"Ausgabe #{args.ident} nicht gefunden.")
        core.mark_expense_paid(conn, int(args.ident), paid)
        emit(args, {"expense_id": int(args.ident), "paid_date": paid},
             f"✓ Ausgabe #{args.ident} als bezahlt am {paid} markiert.")


def cmd_payment(args, cfg):
    """Zahlungseingang zu einer Rechnung erfassen (Bar, Fremdwährung, anderes Konto)."""
    conn = core.db()
    inv = find_invoice(conn, args.number)
    paid = args.date or date.today().isoformat()
    r = core.record_invoice_payment(conn, inv["id"], paid, amount=args.amount,
                                    currency=args.currency, method=args.method,
                                    create_tx=not args.no_tx)
    txt = f"✓ Zahlung zu {inv['number']} am {paid} erfasst."
    if r.get("tx"):
        txt += (f"\n  Buchung: {r['amount']} {r['currency']}"
                f" → {eur(r['amount_home'])} (Heimwährung)"
                if r.get("amount_home") is not None else "\n  Buchung angelegt.")
    emit(args, r, txt)


def cmd_send(args, cfg):
    conn = core.db()
    inv = find_invoice(conn, args.number)
    to = args.to
    if not to and inv["customer_id"]:
        cust = core.get_customer(conn, inv["customer_id"])
        to = cust and cust["email"]
    if not to:
        fail("Keine Empfänger-Adresse: --to angeben oder E-Mail am Kunden hinterlegen.")
    s = core.load_settings()
    subject = args.subject or (s.get("email_subject_tpl") or "").replace(
        "{number}", inv["number"]) or f"Rechnung {inv['number']}"
    body = args.body or (s.get("email_body_tpl") or "").replace(
        "{number}", inv["number"]) or f"Anbei die Rechnung {inv['number']}."
    try:
        r = core.send_invoice_email(conn, inv["id"], to, subject, body,
                                    cc=args.cc, copy_self=args.copy_self)
    except Exception as e:
        fail(str(e))
    if args.remember and inv["customer_id"]:
        core.set_customer_email(conn, inv["customer_id"], to)
    emit(args, r, f"✓ Rechnung {inv['number']} an {to} versendet.")


def cmd_pdf(args, cfg):
    conn = core.db()
    inv = find_invoice(conn, args.number)
    if args.set:
        dest = core.set_invoice_pdf(conn, inv["id"],
                                    Path(args.set).expanduser().read_bytes(),
                                    Path(args.set).name)
        emit(args, {"number": inv["number"], "pdf_path": str(dest)},
             f"✓ PDF für {inv['number']} ersetzt: {dest}")
        return
    if args.regen:
        try:
            dest = core.regenerate_invoice_pdf(conn, need_cfg(cfg), inv["id"])
        except ValueError as e:
            fail(str(e))
        emit(args, {"number": inv["number"], "pdf_path": str(dest)},
             f"✓ PDF für {inv['number']} neu erzeugt: {dest}")
        return
    emit(args, {"number": inv["number"], "pdf_path": inv["pdf_path"]},
         inv["pdf_path"] or "(kein PDF hinterlegt)")


# ── customers ────────────────────────────────────────────────────────────
def cmd_customers(args, cfg):
    conn = core.db()
    rows = core.list_customers(conn)
    if getattr(args, "json", False):
        emit(args, rows)
        return
    if not rows:
        print("Noch keine Kunden erfasst.")
        return
    print("\nID   Nr.        Name                          USt-IdNr.        E-Mail")
    print("─" * 90)
    for r in rows:
        print(f"{r['id']:<4} {(r['number'] or '-'):<10} {r['name'][:28]:<28}  "
              f"{(r['vat_id'] or '-'):<15}  {(r['email'] or '-')[:24]}")


def cmd_customer(args, cfg):
    conn = core.db()
    if args.action == "add":
        c = core.create_customer(conn, args.name, (args.address or "").replace(";", "\n"),
                                 args.vat_id, getattr(args, "number", None))
        if args.email:
            core.set_customer_email(conn, c["id"], args.email)
            c = core.get_customer(conn, c["id"])
        emit(args, c, f"✓ Kunde [{c['id']}] {c['name']} angelegt.")
    elif args.action == "email":
        if not core.get_customer(conn, int(args.id)):
            fail(f"Kunde #{args.id} nicht gefunden.")
        core.set_customer_email(conn, int(args.id), args.email)
        emit(args, core.get_customer(conn, int(args.id)),
             f"✓ E-Mail für Kunde #{args.id} gesetzt: {args.email}")
    elif args.action == "number":
        r = core.set_customer_number(conn, int(args.id), args.number)
        emit(args, core.get_customer(conn, int(args.id)),
             f"✓ Kundennummer für {r['name']}: {r['number'] or '(entfernt)'}")
    else:                                        # show
        c = core.get_customer(conn, int(args.id))
        if not c:
            fail(f"Kunde #{args.id} nicht gefunden.")
        emit(args, c, f"[{c['id']}] {c['name']} (Nr. {c['number'] or '-'})\n{c['address']}\n"
                      f"USt-IdNr.: {c['vat_id'] or '-'}   E-Mail: {c['email'] or '-'}")


# ── list ─────────────────────────────────────────────────────────────────
def cmd_list(args, cfg):
    conn = core.db()
    if args.kind == "invoices":
        rows = core.list_invoices(conn)
        if getattr(args, "json", False):
            emit(args, rows)
            return
        print("\nNr.             Datum       Kunde                   Brutto      Typ  Status")
        print("─" * 84)
        marker = {"domestic": "DE ", "eu": "EU ", "third": "3rd"}
        for r in rows:
            status = f"bezahlt {r['paid_date']}" if r["paid_date"] else "OFFEN"
            print(f"{r['number']:<15} {r['issue_date']}  {r['customer_name'][:22]:<22} "
                  f"{eur(r['gross']):>13}  {marker.get(r['kind'], '?  ')}  {status}")
    else:
        rows = core.list_expenses(conn)
        if getattr(args, "json", False):
            emit(args, rows)
            return
        print("\n#     Datum       Lieferant             Kategorie        Brutto       Vorsteuer")
        print("─" * 80)
        for r in rows:
            print(f"{r['id']:<5} {r['date']}  {r['vendor'][:20]:<20} {r['category'][:15]:<15} "
                  f"{eur(r['gross']):>12} {eur(r['vat']):>12}")


# ── report / figures / vat / zm ──────────────────────────────────────────
def cmd_report(args, cfg):
    r = core.report_data(core.db(), args.year)
    if getattr(args, "json", False):
        emit(args, r)
        return
    method = "Brutto (amtlich)" if r["method"] == "brutto" else "Netto (wirtschaftlich)"
    print(f"\n════ EÜR {r['year']}  (Zufluss-/Abflussprinzip · {method}) ════\n")
    print(f"Betriebseinnahmen                {eur(r['einnahmen']):>16}")
    print(f"  Umsatzerlöse (netto)           {eur(r['umsatz_net']):>16}")
    if r["eu_net"]:
        print(f"  davon EU (Reverse Charge)      {eur(r['eu_net']):>16}")
    if r["third_net"]:
        print(f"  davon Drittland (nicht steuerb.){eur(r['third_net']):>16}")
    if r["method"] == "brutto":
        print(f"  vereinnahmte USt               {eur(r['ust_vereinnahmt']):>16}")
        if r["eigenverbrauch_net"]:
            print(f"  Eigenverbrauch (Privatanteil)  {eur(r['eigenverbrauch_net']):>16}")
    if r["sonstige"]:
        print(f"  sonstige Einnahmen             {eur(r['sonstige']):>16}")
    print(f"\nBetriebsausgaben                 {eur(r['ausgaben']):>16}")
    print(f"  Aufwand (netto)                {eur(r['aufwand_net']):>16}")
    if r["method"] == "brutto":
        print(f"  gezahlte Vorsteuer / §13b      {eur(r['vorsteuer_ges']):>16}")
    print("─" * 50)
    print(f"Ergebnis (zu versteuern)         {eur(r['gewinn']):>16}\n")
    print("Ausgaben nach Kategorie (netto):")
    for cat, amt in r["by_cat"]:
        print(f"  {cat[:28]:<28} {eur(amt):>16}")
    print("\nHinweis: Orientierung für die Anlage EÜR. Keine Steuerberatung.")


def cmd_figures(args, cfg):
    f = core.figures_for_period(core.db(), need_cfg(cfg), args.period)
    method = "brutto" if f["methode"] == "brutto" else "netto"
    emit(args, f,
         f"\n════ {f['zeitraum']}  (EÜR {method}) ════\n"
         f"  Einnahmen           {eur(f['einnahmen']):>16}\n"
         f"  Ausgaben            {eur(f['ausgaben']):>16}\n"
         f"  Gewinn              {eur(f['gewinn']):>16}\n"
         f"  USt-Zahllast        {eur(f['ust_zahllast']):>16}")


def cmd_figures_yearly(args, cfg):
    rows = core.yearly_overview(core.db(), need_cfg(cfg))
    if getattr(args, "json", False):
        emit(args, rows)
        return
    if not rows:
        print("Noch keine Daten – keine Jahresübersicht.")
        return
    print("\nJahr      Einnahmen         Ausgaben           Gewinn")
    print("─" * 56)
    for r in rows:
        print(f"{r['jahr']:<6} {eur(r['einnahmen']):>16} {eur(r['ausgaben']):>16} "
              f"{eur(r['gewinn']):>16}")


def cmd_vat(args, cfg):
    v = core.vat_data(core.db(), need_cfg(cfg), args.year, args.n)
    if getattr(args, "json", False):
        emit(args, v)
        return
    print(f"\n════ USt-Voranmeldung {v['label']}  ({v['taxation']}-Besteuerung) ════")
    print(f"Zeitraum: {v['lo']} bis {v['hi']}\n")
    print("ELSTER-Kennziffern:")
    for kz_, label, base, kz_tax, tax in core.vat_kz_rows(v):
        # Bemessung leer = reines Betragsfeld (Vorsteuer/Sondervorauszahlung).
        line = f"  Kz {kz_:<6} {label:<44} {eur(base) if base is not None else '':>13}"
        if tax is not None:
            line += f"   {('Kz ' + kz_tax) if kz_tax else 'Steuer':<9} {eur(tax):>11}"
        print(line)
    print("─" * 94)
    kz = "Kz 83     Zahllast" if v["zahllast"] >= 0 else "Kz 83     Erstattung"
    print(f"  {kz:<54} {eur(abs(v['zahllast'])):>13}")
    open_rc = core.vat_open_rc(core.db(), v["lo"], v["hi"])
    if open_rc:
        print(f"\n⚠ {len(open_rc)} §13b-Beleg(e) ohne Zuordnung EU (Kz 46/47) oder Drittland "
              f"(Kz 84/85) –\n  die Steuer fehlt sonst in der Anmeldung:")
        for r in open_rc[:10]:
            print(f"    #{r['id']:<5} {r['paid_date'] or r['date']}  {(r['vendor'] or '')[:34]:<34} "
                  f"{eur(r['gross_full'] or 0):>10}")
        if len(open_rc) > 10:
            print(f"    … und {len(open_rc) - 10} weitere")
        print("  Zuordnen:  ./bb expense-vat-kind <id> rc_eu|rc_other")
    if v["eu_net"] or v["ig_lieferung_net"]:
        print(f"\n⚠ EU-Umsätze zusätzlich in der ZM melden:  ./bb zm {args.year} <Quartal>")
    print("\nHinweis: Beträge für ELSTER (Mein ELSTER › USt-VA). Keine Steuerberatung.")


def cmd_expense_vat_kind(args, cfg):
    r = core.update_expense(core.db(), args.id, vat_kind=args.vat_kind,
                            vat_rate=getattr(args, "vat_rate", None))
    emit(args, r, f"✓ Ausgabe #{r['id']}: {core.EXPENSE_VAT_KINDS[r['vat_kind']]}")


def cmd_vat_annual(args, cfg):
    v = core.vat_annual(core.db(), need_cfg(cfg), args.year)
    if getattr(args, "json", False):
        emit(args, v)
        return
    print(f"\n════ USt-Jahreserklärung {v['year']}  ({v['taxation']}-Besteuerung) ════")
    print(f"Zeitraum: {v['lo']} bis {v['hi']}\n")
    print(f"  Umsätze 19 % (netto)             {eur(v['net19']):>16}   USt {eur(v['ust19'])}")
    print(f"  Umsätze  7 % (netto)             {eur(v['net7']):>16}   USt {eur(v['ust7'])}")
    if v["eu_net"]:
        print(f"  innergem. sonst. Leistungen (EU) {eur(v['eu_net']):>16}")
    if v["third_net"]:
        print(f"  nicht steuerbar (Drittland)      {eur(v['third_net']):>16}")
    print(f"  Vorsteuer                        {eur(v['vorsteuer']):>16}")
    print("─" * 55)
    print(f"  USt-Schuld Jahr                  {eur(v['zahllast']):>16}")
    for q in v["quarters"]:
        print(f"    Q{q['q']} vorangemeldet               {eur(q['zahllast']):>16}")
    print(f"  Summe Vorauszahlungen            {eur(v['prepaid']):>16}")
    print("─" * 55)
    label = "Nachzahlung" if v["final"] >= 0 else "Erstattung"
    print(f"  {label:<32} {eur(abs(v['final'])):>16}")
    print("\nHinweis: Keine Steuerberatung.")


def cmd_zm(args, cfg):
    z = core.zm_data(core.db(), args.year, args.n)
    if getattr(args, "json", False):
        emit(args, dict(z, rows=[_plain(r) for r in z["rows"]]))
        return
    print(f"\n════ Zusammenfassende Meldung (ZM) {z['label']} ════")
    print(f"Meldezeitraum: {z['lo']} bis {z['hi']}  (nach Rechnungs-/Leistungsdatum)\n")
    if not z["rows"]:
        print("Keine EU-Umsätze in diesem Quartal – keine ZM erforderlich.")
        return
    print("USt-IdNr.          Kunde                         Netto        Art")
    print("─" * 72)
    for r in z["rows"]:
        vid = r["customer_vat_id"] or "!! USt-IdNr. FEHLT !!"
        print(f"{vid:<18} {r['customer_name'][:28]:<28} {eur(r['net']):>13}   sonst. Leistung")
    print("─" * 72)
    print(f"{'Summe':<47} {eur(z['total']):>13}")
    print(f"\nAbgabe: elektronisch über Mein ELSTER bis zum {z['deadline']}")
    if z["missing"]:
        print("\n⚠ Bei mindestens einem Kunden fehlt die USt-IdNr. – für die ZM zwingend nötig!")
    print("\nHinweis: Keine Steuerberatung. USt-IdNr. vorab auf Gültigkeit prüfen (BZSt/VIES).")


# ── import ───────────────────────────────────────────────────────────────
def cmd_import(args, cfg):
    conn = core.db()
    res = core.import_invoices_csv(conn, need_cfg(cfg), read_text_file(args.file),
                                   render=not args.no_pdf)
    if getattr(args, "json", False):
        emit(args, res)
        return
    print(f"\n✓ {len(res['created'])} Rechnung(en) importiert"
          + (": " + ", ".join(res["created"]) if res["created"] else ""))
    if res["skipped"]:
        print(f"⏭  {len(res['skipped'])} übersprungen:")
        for s in res["skipped"]:
            print("   ", s)
    if res["errors"]:
        print(f"✗ {len(res['errors'])} Fehler:")
        for e in res["errors"]:
            print("   ", e)


def cmd_bank_import(args, cfg):
    res = core.import_bank_csv(core.db(), read_text_file(args.file))
    emit(args, res,
         f"✓ {res['imported']} Buchung(en) importiert, "
         f"{res['duplicates']} Dublette(n) übersprungen, "
         f"{res['matched']} automatisch einer Rechnung zugeordnet.")


# ── tx (Bank / Zahlungen) ────────────────────────────────────────────────
def _print_txs(rows):
    print("\n#      Datum       Beschreibung                        Betrag  Kategorie      Beleg")
    print("─" * 92)
    for t in rows:
        amt = t["amount_home"] if t["amount_home"] is not None else t["amount"]
        print(f"{t['id']:<6} {t['date']}  {(t['description'] or '')[:32]:<32} "
              f"{eur(amt):>13}  {t['category'][:13]:<13}  {'ja' if t['receipt_path'] else '-'}")


def cmd_tx(args, cfg):
    conn = core.db()
    a = args.action

    if a == "list":
        rows = core.list_transactions(conn, only_open=args.open, query=args.query)
        if args.missing_receipt:
            rows = [t for t in rows if not t["receipt_path"]]
        if getattr(args, "json", False):
            emit(args, rows)
        else:
            _print_txs(rows)
            print(f"\n{len(rows)} Buchung(en) · offen gesamt: {core.transactions_open_count(conn)}")
        return

    if a == "manual":
        tid = core.create_manual_transaction(conn, args.date or date.today().isoformat(),
                                             args.description, args.amount,
                                             args.currency, args.method)
        emit(args, core.get_transaction(conn, tid), f"✓ Buchung #{tid} angelegt.")
        return

    if a == "rules":
        rules = core.get_rules_map(conn)
        if getattr(args, "json", False):
            emit(args, {k: _plain(v) for k, v in rules.items()})
            return
        if not rules:
            print("Noch keine Regeln gelernt.")
            return
        print("\nBeschreibung                              Aktion       Konto")
        print("─" * 82)
        for k, r in sorted(rules.items()):
            print(f"{k[:40]:<40}  {r['action'][:11]:<11}  {(r['category'] or '-')[:26]}")
        return

    if a in ("bulk-category", "bulk-expense"):
        ids = [int(i) for i in args.ids]
        if a == "bulk-category":
            n = core.bulk_set_category(conn, ids, args.category)
            emit(args, {"updated": n}, f"✓ {n} Buchung(en) auf '{args.category}' gesetzt.")
        else:
            n = core.bulk_create_expense(conn, ids, args.category, args.vat_rate,
                                         private_share=args.private_share)
            emit(args, {"created": n}, f"✓ {n} Betriebsausgabe(n) aus Buchungen erzeugt.")
        return

    # ab hier: Aktionen auf genau einer Buchung
    tid = int(args.id)
    t = core.get_transaction(conn, tid)
    if not t:
        fail(f"Buchung #{tid} nicht gefunden.")

    if a == "show":
        emit(args, t, "\n".join(f"{k:<14} {t[k]}" for k in t.keys()))
    elif a == "category":
        try:
            core.set_tx_category(conn, tid, args.category, note=args.note)
        except ValueError as e:
            fail(str(e))
        emit(args, core.get_transaction(conn, tid),
             f"✓ Buchung #{tid} → {core.TX_CATEGORIES[args.category]}")
    elif a == "expense":
        if t["amount"] >= 0:
            fail(f"Buchung #{tid} ist ein Eingang – daraus wird keine Ausgabe.")
        core.create_expense_from_tx(conn, tid, args.category, args.vat_rate,
                                    description=args.description,
                                    private_share=args.private_share)
        emit(args, core.get_transaction(conn, tid),
             f"✓ Buchung #{tid} als Betriebsausgabe gebucht ({args.category}).")
    elif a == "split":
        core.create_split_from_tx(conn, tid, parse_lines(args.line))
        emit(args, core.get_transaction(conn, tid),
             f"✓ Buchung #{tid} auf {len(args.line)} Position(en) aufgeteilt.")
    elif a == "link":
        inv = find_invoice(conn, args.invoice)
        core.link_tx_invoice(conn, tid, inv["id"])
        emit(args, core.get_transaction(conn, tid),
             f"✓ Buchung #{tid} mit Rechnung {inv['number']} verknüpft (bezahlt am {t['date']}).")
    elif a == "reset":
        core.reset_tx(conn, tid)
        emit(args, core.get_transaction(conn, tid), f"✓ Buchung #{tid} auf 'offen' zurückgesetzt.")
    elif a == "delete":
        core.delete_transaction(conn, tid)
        emit(args, {"deleted": tid}, f"✓ Buchung #{tid} gelöscht.")
    elif a == "receipt":
        p = Path(args.file).expanduser()
        if not p.exists():
            fail(f"Datei nicht gefunden: {p}")
        dest = core.attach_receipt_to_tx(conn, tid, p.read_bytes(), p.name)
        emit(args, {"tx": tid, "receipt_path": str(dest)},
             f"✓ Beleg an Buchung #{tid} gehängt: {dest}")


# ── doc (Belege / Posteingang) ───────────────────────────────────────────
def cmd_doc(args, cfg):
    conn = core.db()
    a = args.action

    if a == "list":
        rows = core.list_documents(conn)
        if getattr(args, "json", False):
            emit(args, rows)
            return
        print("\n#      Datei                              Anbieter          Betrag  Datum       Status")
        print("─" * 96)
        for d in rows:
            amt = eur(d["amount"]) if d["amount"] is not None else "-"
            print(f"{d['id']:<6} {(d['filename'] or '')[:34]:<34} {(d['vendor'] or '-')[:16]:<16} "
                  f"{amt:>12}  {(d['doc_date'] or '-'):<10}  {d['status']}")
        return

    if a == "add":
        p = Path(args.file).expanduser()
        if not p.exists():
            fail(f"Datei nicht gefunden: {p}")
        did = core.add_document(conn, p.read_bytes(), p.name, vendor=args.vendor,
                                amount=args.amount, doc_date=args.date)
        if args.suggest:
            tx = core.suggest_tx_for_document(conn, args.amount, args.date, args.vendor)
            if tx:
                conn.execute("UPDATE documents SET suggest_tx = ? WHERE id = ?", (tx, did))
                conn.commit()
        emit(args, core.get_document(conn, did), f"✓ Beleg #{did} aufgenommen: {p.name}")
        return

    did = int(args.id)
    doc = core.get_document(conn, did)
    if not doc:
        fail(f"Beleg #{did} nicht gefunden.")

    if a == "show":
        emit(args, doc, "\n".join(f"{k:<12} {doc[k]}" for k in doc.keys()))
    elif a == "suggest":
        tx = core.suggest_tx_for_document(conn, doc["amount"], doc["doc_date"], doc["vendor"])
        if not tx:
            emit(args, {"suggest_tx": None}, "Keine passende offene Zahlung gefunden.")
            return
        conn.execute("UPDATE documents SET suggest_tx = ? WHERE id = ?", (tx, did))
        conn.commit()
        t = core.get_transaction(conn, tx)
        emit(args, {"suggest_tx": tx, "transaction": _plain(t)},
             f"→ Vorschlag: Buchung #{tx} · {t['date']} · {t['description']}")
    elif a == "match":
        core.match_document_to_tx(conn, did, int(args.tx), category=args.category,
                                  vat_rate=args.vat_rate, private_share=args.private_share)
        emit(args, core.get_document(conn, did),
             f"✓ Beleg #{did} an Buchung #{args.tx} zugeordnet und als {args.category} gebucht.")
    elif a == "confirm":
        core.confirm_document(conn, did, category=args.category, vat_rate=args.vat_rate,
                              private_share=args.private_share)
        emit(args, core.get_document(conn, did),
             f"✓ Beleg #{did} als eigenständige Ausgabe gebucht ({args.category}).")
    elif a == "split":
        core.split_document(conn, did, parse_lines(args.line),
                            tx_id=int(args.tx) if args.tx else None)
        emit(args, core.get_document(conn, did),
             f"✓ Beleg #{did} auf {len(args.line)} Position(en) aufgeteilt.")
    elif a == "delete":
        core.delete_document(conn, did)
        emit(args, {"deleted": did}, f"✓ Beleg #{did} gelöscht.")


# ── accounts / recurring / dunning / period / settings / fx ──────────────
def cmd_accounts(args, cfg):
    if args.action == "add":
        core.add_custom_account(args.name)
        emit(args, core.load_settings().get("custom_accounts", []),
             f"✓ Konto '{args.name}' hinzugefügt.")
    elif args.action == "remove":
        core.remove_custom_account(args.name)
        emit(args, core.load_settings().get("custom_accounts", []),
             f"✓ Konto '{args.name}' entfernt.")
    else:
        accounts = core.expense_accounts(cfg or {})
        emit(args, accounts, "\n".join(accounts))


def cmd_recurring(args, cfg):
    a = args.action
    if a == "add":
        it = core.add_recurring(args.client, args.service, args.interval,
                                args.amount, args.next_run)
        emit(args, it, f"✓ Vorlage [{it['id']}] {it['client']} · {it['service']} "
                       f"({it['interval']}, nächste am {it['next_run']})")
    elif a == "toggle":
        state = core.toggle_recurring(args.id)
        if state is None:
            fail(f"Vorlage '{args.id}' nicht gefunden.")
        emit(args, {"id": args.id, "active": state},
             f"✓ Vorlage {args.id} {'aktiv' if state else 'pausiert'}.")
    elif a == "delete":
        if not core.delete_recurring(args.id):
            fail(f"Vorlage '{args.id}' nicht gefunden.")
        emit(args, {"deleted": args.id}, f"✓ Vorlage {args.id} gelöscht.")
    elif a == "advance":
        new = core.advance_recurring(args.id)
        if new is None:
            fail(f"Vorlage '{args.id}' nicht gefunden.")
        emit(args, {"id": args.id, "next_run": new}, f"✓ Nächster Lauf: {new}")
    else:
        items = core.list_recurring()
        if getattr(args, "json", False):
            emit(args, items)
            return
        if not items:
            print("Keine wiederkehrenden Rechnungen hinterlegt.")
            return
        print("\nID        Kunde                 Leistung                Intervall      Nächste      Status")
        print("─" * 96)
        for it in items:
            print(f"{it['id']:<9} {it['client'][:20]:<20}  {it['service'][:22]:<22}  "
                  f"{it['interval']:<13}  {it['next_run']:<11}  "
                  f"{'aktiv' if it.get('active', True) else 'pausiert'}")


def cmd_dunning(args, cfg):
    conn = core.db()
    if args.action == "advance":
        stage = core.advance_dunning(args.number)
        emit(args, {"number": args.number, "stage": stage,
                    "stage_name": core.DUNNING_STAGES[stage]},
             f"✓ {args.number} → Stufe {stage}: {core.DUNNING_STAGES[stage]}")
    elif args.action == "reset":
        core.reset_dunning(args.number)
        emit(args, {"number": args.number, "stage": 0}, f"✓ {args.number} zurückgesetzt.")
    else:
        cases = core.dunning_cases(conn)
        if getattr(args, "json", False):
            emit(args, cases)
            return
        if not cases:
            print("Keine offenen Rechnungen – nichts anzumahnen.")
            return
        print("\nNummer          Kunde                     Brutto   Datum       Stufe")
        print("─" * 80)
        for c in cases:
            print(f"{c['number']:<15} {c['client'][:24]:<24} {eur(c['gross']):>12}  "
                  f"{c['issue']}  {c['stage']} · {c['stage_name']}")


def cmd_period(args, cfg):
    if args.action == "close":
        cp = core.close_period(args.token)
        emit(args, cp, f"✓ Periode {args.token} festgeschrieben.")
    elif args.action == "reopen":
        cp = core.reopen_period(args.token)
        emit(args, cp, f"✓ Periode {args.token} wieder geöffnet.")
    else:
        cp = core.closed_periods()
        emit(args, cp, "\n".join(cp) if cp else "Keine Periode festgeschrieben.")


def cmd_settings(args, cfg):
    s = core.load_settings()
    if args.action == "set":
        changes = {}
        for pair in args.pair:
            if "=" not in pair:
                fail(f"'{pair}' ist kein KEY=VALUE.")
            k, v = pair.split("=", 1)
            changes[k.strip()] = _coerce(v)
        s = core.update_settings(**changes)
        emit(args, {k: s.get(k) for k in changes},
             "\n".join(f"{k} = {s.get(k)}" for k in changes))
    elif args.action == "unset":
        for k in args.key:
            s.pop(k, None)
        core.save_settings(s)
        emit(args, {"removed": args.key}, f"✓ Entfernt: {', '.join(args.key)}")
    else:
        masked = dict(s)
        for k in ("api_key", "smtp_pass"):
            if masked.get(k):
                masked[k] = "········"
        keys = getattr(args, "key", None)
        if keys:
            masked = {k: masked.get(k) for k in keys}
        emit(args, masked, "\n".join(f"{k} = {v}" for k, v in sorted(masked.items())))


def _coerce(v: str):
    low = v.strip().lower()
    if low in ("true", "ja", "yes"):
        return True
    if low in ("false", "nein", "no"):
        return False
    if low == "null":
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def cmd_fx(args, cfg):
    conn = core.db()
    try:
        rate = core.fx_rate(conn, args.base, args.quote, args.date)
    except Exception as e:
        fail(f"Kurs nicht abrufbar: {e}")
    on = args.date or date.today().isoformat()
    emit(args, {"base": args.base.upper(), "quote": args.quote.upper(),
                "date": on, "rate": rate},
         f"1 {args.base.upper()} = {rate} {args.quote.upper()}  ({on})")


# ── tools / call (identische Schnittstelle wie Chat & MCP) ───────────────
def cmd_tools(args, cfg):
    import assistant
    names = assistant.tool_names()
    emit(args, names, "\n".join(names))


def cmd_call(args, cfg):
    import assistant
    payload = {}
    if args.args:
        try:
            payload = json.loads(args.args)
        except json.JSONDecodeError as e:
            fail(f"Ungültiges JSON: {e}")
    if args.tool not in assistant.tool_names():
        fail(f"Unbekanntes Tool: {args.tool}. './bb tools' zeigt alle.")
    print(assistant.dispatch(args.tool, payload))


# ── Parser ───────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog="bb", description="Schlanke Buchhaltung für Freelancer")
    p.add_argument("--json", action="store_true", help="Ausgabe maschinenlesbar als JSON")
    sub = p.add_subparsers(dest="command", required=True)

    # invoice
    si = sub.add_parser("invoice", help="Ausgangsrechnung erstellen")
    si.add_argument("--customer", help="Kundenname (legt bei Bedarf an)")
    si.add_argument("--address", help="Adresse, Zeilen mit ; trennen")
    si.add_argument("--vat-id", dest="vat_id")
    si.add_argument("--kind", choices=list(core.KINDS), default="domestic")
    si.add_argument("--date", help="Rechnungsdatum JJJJ-MM-TT")
    si.add_argument("--service-from", dest="service_from")
    si.add_argument("--service-to", dest="service_to")
    si.add_argument("--item", action="append",
                    help="BESCHREIBUNG|MENGE|EINHEIT|EINZELPREIS[|USTSATZ] (mehrfach)")
    si.add_argument("--notes")
    si.add_argument("--number", help="Nummer vorgeben (statt automatisch)")
    si.add_argument("--paid", help="Zahldatum, falls schon bezahlt")
    si.add_argument("--no-pdf", action="store_true")

    # expense
    se = sub.add_parser("expense", help="Ausgabe erfassen")
    se.add_argument("--date")
    se.add_argument("--vendor")
    se.add_argument("--description")
    se.add_argument("--category", default="Sonstiges")
    se.add_argument("--gross")
    se.add_argument("--vat-rate", dest="vat_rate", type=int)
    se.add_argument("--paid", help="Zahldatum ('' = unbezahlt)")
    se.add_argument("--receipt", help="Pfad zur Belegdatei")
    se.add_argument("--private-share", dest="private_share", type=float, default=0)
    se.add_argument("--reverse-charge", dest="reverse_charge", action="store_true",
                    help="ausländische Eingangsrechnung nach §13b (Herkunft offen)")
    se.add_argument("--vat-kind", dest="vat_kind", choices=sorted(core.EXPENSE_VAT_KINDS),
                    help="USt-Art: " + " · ".join(f"{k}={v}" for k, v in core.EXPENSE_VAT_KINDS.items()))

    # USt-Art einer erfassten Ausgabe nachtragen (z. B. §13b EU vs. Drittland)
    sk = sub.add_parser("expense-vat-kind", help="USt-Art einer Ausgabe setzen")
    sk.add_argument("id", type=int)
    sk.add_argument("vat_kind", choices=sorted(core.EXPENSE_VAT_KINDS))
    sk.add_argument("--vat-rate", dest="vat_rate", type=int, help="Steuersatz (Standard 19)")

    # Kunden
    sub.add_parser("customers", help="Kundenregister anzeigen")
    sc = sub.add_parser("customer", help="Kunden anlegen/bearbeiten").add_subparsers(
        dest="action", required=True)
    ca = sc.add_parser("add"); ca.add_argument("--name", required=True)
    ca.add_argument("--address", default=""); ca.add_argument("--vat-id", dest="vat_id")
    ca.add_argument("--email"); ca.add_argument("--number", help="Kundennummer (eindeutig)")
    ce = sc.add_parser("email"); ce.add_argument("id"); ce.add_argument("email")
    cn = sc.add_parser("number", help="Kundennummer setzen (leer = entfernen)")
    cn.add_argument("id"); cn.add_argument("number", nargs="?", default="")
    cs = sc.add_parser("show"); cs.add_argument("id")

    # Rechnungs-Aktionen
    sp = sub.add_parser("pay", help="als bezahlt markieren")
    sp.add_argument("kind", choices=["invoice", "expense"])
    sp.add_argument("ident"); sp.add_argument("--date")

    spm = sub.add_parser("payment", help="Zahlungseingang zu einer Rechnung erfassen")
    spm.add_argument("number"); spm.add_argument("--date")
    spm.add_argument("--amount"); spm.add_argument("--currency")
    spm.add_argument("--method", default="")
    spm.add_argument("--no-tx", action="store_true", help="nur bezahlt markieren, keine Buchung")

    ss = sub.add_parser("send", help="Rechnung per E-Mail versenden")
    ss.add_argument("number"); ss.add_argument("--to"); ss.add_argument("--cc")
    ss.add_argument("--subject"); ss.add_argument("--body")
    ss.add_argument("--copy-self", dest="copy_self", action="store_true")
    ss.add_argument("--remember", action="store_true", help="E-Mail am Kunden speichern")

    sf = sub.add_parser("pdf", help="Rechnungs-PDF anzeigen/neu erzeugen/ersetzen")
    sf.add_argument("number")
    sf.add_argument("--regen", action="store_true", help="PDF neu rendern")
    sf.add_argument("--set", help="vorhandene PDF-Datei übernehmen")

    sl = sub.add_parser("list", help="Rechnungen/Ausgaben auflisten")
    sl.add_argument("kind", choices=["invoices", "expenses"])

    # Auswertungen
    sr = sub.add_parser("report", help="EÜR für ein Jahr"); sr.add_argument("year", type=int)
    sg = sub.add_parser("figures", help="Kennzahlen für JJJJ | JJJJ-Qn | JJJJ-MM")
    sg.add_argument("period")
    sub.add_parser("figures-yearly", help="Jahresübersicht: Einnahmen/Ausgaben/Gewinn je Jahr")
    sv = sub.add_parser("vat", help="USt-Voranmeldung")
    sv.add_argument("year", type=int); sv.add_argument("n", type=int)
    sva = sub.add_parser("vat-annual", help="USt-Jahreserklärung")
    sva.add_argument("year", type=int)
    sz = sub.add_parser("zm", help="Zusammenfassende Meldung")
    sz.add_argument("year", type=int); sz.add_argument("n", type=int)

    # Import
    sim = sub.add_parser("import", help="Rechnungen aus CSV importieren")
    sim.add_argument("file"); sim.add_argument("--no-pdf", action="store_true")
    sbi = sub.add_parser("bank-import", help="Bank-/Zahlungs-CSV importieren")
    sbi.add_argument("file")

    # tx
    tx = sub.add_parser("tx", help="Bankbuchungen").add_subparsers(dest="action", required=True)
    tl = tx.add_parser("list")
    tl.add_argument("--open", action="store_true", help="nur unkategorisierte")
    tl.add_argument("--query"); tl.add_argument("--missing-receipt", dest="missing_receipt",
                                                action="store_true")
    tx.add_parser("show").add_argument("id")
    tc = tx.add_parser("category"); tc.add_argument("id")
    tc.add_argument("category", choices=list(core.TX_CATEGORIES)); tc.add_argument("--note")
    te = tx.add_parser("expense"); te.add_argument("id")
    te.add_argument("--category", default="Sonstiges")
    te.add_argument("--vat-rate", dest="vat_rate", type=int, default=0)
    te.add_argument("--private-share", dest="private_share", type=float, default=0)
    te.add_argument("--description")
    tsp = tx.add_parser("split"); tsp.add_argument("id")
    tsp.add_argument("--line", action="append", required=True,
                     help="KONTO|BETRAG[|USTSATZ][|PRIVATANTEIL] (mehrfach)")
    tk = tx.add_parser("link"); tk.add_argument("id"); tk.add_argument("invoice")
    tx.add_parser("reset").add_argument("id")
    tx.add_parser("delete").add_argument("id")
    tr = tx.add_parser("receipt"); tr.add_argument("id"); tr.add_argument("file")
    tm = tx.add_parser("manual")
    tm.add_argument("--date"); tm.add_argument("--description", required=True)
    tm.add_argument("--amount", required=True, help="signiert: + Eingang, - Ausgang")
    tm.add_argument("--currency", default="EUR"); tm.add_argument("--method", default="Manuell")
    tbc = tx.add_parser("bulk-category"); tbc.add_argument("ids", nargs="+")
    tbc.add_argument("--category", required=True, choices=list(core.TX_CATEGORIES))
    tbe = tx.add_parser("bulk-expense"); tbe.add_argument("ids", nargs="+")
    tbe.add_argument("--category", default="Sonstiges")
    tbe.add_argument("--vat-rate", dest="vat_rate", type=int, default=0)
    tbe.add_argument("--private-share", dest="private_share", type=float, default=0)
    tx.add_parser("rules", help="gelernte Zuordnungsregeln")

    # doc
    doc = sub.add_parser("doc", help="Belege / Posteingang").add_subparsers(
        dest="action", required=True)
    doc.add_parser("list")
    da = doc.add_parser("add"); da.add_argument("file")
    da.add_argument("--vendor"); da.add_argument("--amount"); da.add_argument("--date")
    da.add_argument("--suggest", action="store_true", help="passende Zahlung gleich suchen")
    doc.add_parser("show").add_argument("id")
    doc.add_parser("suggest").add_argument("id")
    dm = doc.add_parser("match"); dm.add_argument("id"); dm.add_argument("tx")
    dm.add_argument("--category", default="Sonstiges")
    dm.add_argument("--vat-rate", dest="vat_rate", type=int, default=0)
    dm.add_argument("--private-share", dest="private_share", type=float, default=0)
    dc = doc.add_parser("confirm"); dc.add_argument("id")
    dc.add_argument("--category", default="Sonstiges")
    dc.add_argument("--vat-rate", dest="vat_rate", type=int, default=0)
    dc.add_argument("--private-share", dest="private_share", type=float, default=0)
    ds = doc.add_parser("split"); ds.add_argument("id")
    ds.add_argument("--line", action="append", required=True)
    ds.add_argument("--tx", help="optional: Zahlung, der alle Positionen zugeordnet werden")
    doc.add_parser("delete").add_argument("id")

    # Stammdaten
    ac = sub.add_parser("accounts", help="Aufwandskonten").add_subparsers(dest="action")
    aca = ac.add_parser("add"); aca.add_argument("name")
    acr = ac.add_parser("remove"); acr.add_argument("name")
    ac.add_parser("list")

    rc = sub.add_parser("recurring", help="wiederkehrende Rechnungen").add_subparsers(
        dest="action")
    rc.add_parser("list")
    rca = rc.add_parser("add"); rca.add_argument("--client", required=True)
    rca.add_argument("--service", required=True)
    rca.add_argument("--interval", default="monatlich", choices=list(core.RECURRING_INTERVALS))
    rca.add_argument("--amount", default=""); rca.add_argument("--next-run", dest="next_run")
    rc.add_parser("toggle").add_argument("id")
    rc.add_parser("delete").add_argument("id")
    rc.add_parser("advance").add_argument("id")

    dn = sub.add_parser("dunning", help="Mahnwesen").add_subparsers(dest="action")
    dn.add_parser("list")
    dn.add_parser("advance").add_argument("number")
    dn.add_parser("reset").add_argument("number")

    pe = sub.add_parser("period", help="Perioden festschreiben").add_subparsers(dest="action")
    pe.add_parser("list")
    pe.add_parser("close").add_argument("token", help="z. B. 2026-Q1")
    pe.add_parser("reopen").add_argument("token")

    st = sub.add_parser("settings", help="App-Einstellungen").add_subparsers(dest="action")
    stg = st.add_parser("get"); stg.add_argument("key", nargs="*")
    sts = st.add_parser("set"); sts.add_argument("pair", nargs="+", help="KEY=VALUE")
    stu = st.add_parser("unset"); stu.add_argument("key", nargs="+")

    fx = sub.add_parser("fx", help="EZB-Wechselkurs abfragen")
    fx.add_argument("base"); fx.add_argument("quote", nargs="?", default=None)
    fx.add_argument("--date")

    # Tool-Schnittstelle (wie Chat/MCP)
    sub.add_parser("tools", help="verfügbare Tools (Chat/MCP) auflisten")
    sca = sub.add_parser("call", help="Tool direkt aufrufen: call <tool> '<json>'")
    sca.add_argument("tool"); sca.add_argument("args", nargs="?", default="")

    return p


COMMANDS = {
    "invoice": cmd_invoice, "expense": cmd_expense, "expense-vat-kind": cmd_expense_vat_kind,
    "customers": cmd_customers,
    "customer": cmd_customer, "pay": cmd_pay, "payment": cmd_payment, "send": cmd_send,
    "pdf": cmd_pdf, "list": cmd_list, "report": cmd_report, "figures": cmd_figures,
    "figures-yearly": cmd_figures_yearly,
    "vat": cmd_vat, "vat-annual": cmd_vat_annual, "zm": cmd_zm, "import": cmd_import,
    "bank-import": cmd_bank_import, "tx": cmd_tx, "doc": cmd_doc, "accounts": cmd_accounts,
    "recurring": cmd_recurring, "dunning": cmd_dunning, "period": cmd_period,
    "settings": cmd_settings, "fx": cmd_fx, "tools": cmd_tools, "call": cmd_call,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Profil festlegen, bevor irgendein Pfad benutzt wird. Bei mehreren
    # Profilen bricht das ab, statt in der falschen Buchhaltung zu landen.
    core.profil_aus_umgebung()
    # Gruppen ohne Unterbefehl (z. B. 'bb accounts') auf 'list' verstehen
    if getattr(args, "action", "sentinel") is None:
        args.action = "list"
    if args.command == "fx" and args.quote is None:
        args.base, args.quote = args.base, core.home_currency()
    try:
        cfg = core.load_config()
    except FileNotFoundError:
        cfg = None
    COMMANDS[args.command](args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(1)
