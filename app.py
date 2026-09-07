#!/usr/bin/env python3
"""
Lokale Web-App für die Buchhaltung (Flask).
Nutzt dieselbe Logik wie die CLI (core.py). Start:  ./bb-web
"""
from __future__ import annotations

import json as _json
import os
import sys
import select
import signal
import struct
import subprocess
import threading
import time
import traceback
import webbrowser
from datetime import date, timedelta as _timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from flask_sock import Sock

import core
import assistant
import wirex

# Das eingebaute Terminal braucht ein Unix-PTY. Unter Windows gibt es die
# Module nicht – dort entfällt der Reiter, statt den Import zu sprengen.
try:
    import fcntl
    import pty
    import termios
    TERMINAL_MOEGLICH = True
except ImportError:                                   # pragma: no cover - nur Windows
    fcntl = pty = termios = None
    TERMINAL_MOEGLICH = False

# Vorlagen und statische Dateien explizit aus dem Bündel: in der gepackten App
# liegt der Code in einem Temporärverzeichnis, das Flask von sich aus nicht kennt.
app = Flask(__name__,
            template_folder=str(core.BUNDLE / "templates"),
            static_folder=str(core.BUNDLE / "static"))
app.secret_key = "local-bookkeeping"  # nur für Flash-Messages, rein lokal
app.config["TEMPLATES_AUTO_RELOAD"] = True  # Template-Änderungen ohne Neustart
sock = Sock(app)
CHAT_MESSAGES: list = []  # LLM-Verlauf (lokal, rein im Speicher)
AGENT = {"session": None, "display": []}  # Claude-Code-Agent (Chat-Bubble-Widget)

# Jinja-Filter fürs Geld-Format
app.jinja_env.filters["eur"] = core.eur
app.jinja_env.filters["de_date"] = lambda s: core.de_date(s) if s else "—"


# ── Profile: aktives Profil je Anfrage binden ────────────────────────────
# core hält die Pfade in Modulglobals. Deshalb wird vor JEDER Anfrage neu
# gebunden – sonst würde ein Profilwechsel in andere Anfragen durchschlagen.
_PROFIL_FREI = {"profile_waehlen", "profile_neu", "profile_oeffnen", "profile_wechseln",
                "profile_umbenennen", "profile_entfernen", "static"}
_SETUP_FREI = _PROFIL_FREI | {"setup", "restart"}


@app.before_request
def _profil_binden():
    if request.path.startswith("/static/"):
        return None
    endpoint = request.endpoint or ""
    profile = core.list_profiles()

    if not profile:
        # Noch keine Profile. Liegt eine config da (Altlayout), damit weiterarbeiten
        # und die Pfade NICHT umbiegen – sonst überschriebe das eine Sandbox.
        if core.config_exists():
            return None
        return None if endpoint in _PROFIL_FREI else redirect(url_for("profile_waehlen"))

    gewaehlt = session.get("profil")
    if gewaehlt and not any(p["slug"] == gewaehlt for p in profile):
        session.pop("profil", None)          # Profil wurde entfernt
        gewaehlt = None
    if not gewaehlt and len(profile) == 1:
        gewaehlt = profile[0]["slug"]        # nur eines da -> direkt hinein
        session["profil"] = gewaehlt

    if not gewaehlt:
        return None if endpoint in _PROFIL_FREI else redirect(url_for("profile_waehlen"))

    core.use_profile(gewaehlt)
    if not core.config_exists() and endpoint not in _SETUP_FREI:
        return redirect(url_for("setup"))
    return None


@app.context_processor
def inject_globals():
    try:
        open_tx = core.transactions_open_count(core.db())
    except Exception:
        open_tx = 0
    s = core.load_settings()
    try:
        accounts = list(core.expense_accounts(core.load_config()))
    except Exception:
        accounts = list(core.DEFAULT_EXPENSE_ACCOUNTS)
    for a in s.get("custom_accounts", []):
        if a not in accounts:
            accounts.append(a)
    try:
        biz = core.load_config().get("business", {})
    except Exception:
        biz = {}
    # Absenderzeile: Ort aus der letzten Adresszeile vor dem Land, sonst leer
    adr = [a.strip() for a in (biz.get("address_lines") or []) if a.strip()]
    profil = core.active_profile()
    return {"today": date.today().isoformat(), "current_year": date.today().year,
            "nav_open_tx": open_tx, "expense_accounts": accounts,
            "home_currency": core.home_currency(), "fx_currencies": core.FX_CURRENCIES,
            "accent": s.get("accent", "green"), "density": s.get("density", "airy"),
            "biz_name": biz.get("owner") or biz.get("name") or "",
            "biz_place": adr[-2] if len(adr) >= 2 else (adr[0] if adr else ""),
            "terminal_moeglich": TERMINAL_MOEGLICH,
            "profil": profil,
            "profil_initialen": _initialen(profil["name"]) if profil else "",
            "profil_anzahl": len(core.list_profiles())}


# ── 500 auffangen: einmal automatisch neu starten (kein Dauer-Loop) ───────
_AUTORESTART_MARKER = core.DATA_DIR / ".autorestart"

_RESTARTING_HTML = """<!doctype html><meta charset=utf-8><title>Neustart…</title>
<body style="font:16px -apple-system,sans-serif;background:#0f1720;color:#d7e0ea;
display:flex;height:100vh;margin:0;align-items:center;justify-content:center">
<div style="text-align:center">Fehler erkannt – Server wird neu gestartet…
<br><small style="color:#7c8896">die Seite lädt gleich automatisch neu</small></div>
<script>
let t=0;function poll(){fetch('/healthz',{cache:'no-store'}).then(r=>r.ok?location.reload():retry()).catch(retry);}
function retry(){if(++t>80){location.reload();return;}setTimeout(poll,250);}setTimeout(poll,900);
</script>"""


def _error_html(msg, tb):
    import html as _h
    return (
        "<!doctype html><meta charset=utf-8><title>Serverfehler</title>"
        "<body style='font:14px -apple-system,sans-serif;max-width:900px;margin:40px auto;padding:0 20px'>"
        "<h1 style='color:#b42318'>Serverfehler</h1>"
        "<p>Ein automatischer Neustart wurde bereits versucht – der Fehler besteht weiter, "
        "daher kein weiterer Auto-Neustart (kein Loop).</p>"
        f"<pre style='background:#0f1720;color:#d7e0ea;padding:14px;border-radius:8px;white-space:pre-wrap'>{_h.escape(msg)}</pre>"
        f"<details><summary>Details</summary><pre style='white-space:pre-wrap;overflow:auto'>{_h.escape(tb)}</pre></details>"
        "<p><a href='#' onclick='location.reload();return false'>neu laden</a> · "
        "<a href='#' onclick='fetch(\"/restart\",{method:\"POST\"}).catch(()=>{});"
        "setTimeout(()=>location.reload(),1200);return false'>trotzdem neu starten</a></p>")


@app.errorhandler(Exception)
def _handle_exc(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException) and (e.code or 500) < 500:
        return e  # 404/409 etc. normal behandeln
    tb = traceback.format_exc()
    app.logger.error("Serverfehler:\n%s", tb)
    try:
        recent = (time.time() - _AUTORESTART_MARKER.stat().st_mtime) < 20
    except OSError:
        recent = False
    if os.environ.get("BB_SUPERVISED") and not recent:
        try:
            core.DATA_DIR.mkdir(exist_ok=True)
            _AUTORESTART_MARKER.write_text(str(time.time()))
        except Exception:
            pass
        threading.Timer(0.3, lambda: os._exit(42)).start()  # Supervisor startet neu
        return _RESTARTING_HTML, 200
    return _error_html(str(e), tb), 500


# ── Dashboard ────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    conn = core.db()
    year = int(request.args.get("year", date.today().year))
    period = request.args.get("period", "year")            # year | q1..q4
    if period.startswith("q") and period[1:].isdigit():
        n = int(period[1:])
        lo, hi, _ = core.period_bounds(year, "quarter", n)
        label = f"Q{n} {year}"
    else:
        period = "year"
        lo, hi = f"{year}-01-01", f"{year}-12-31"
        label = f"Jahr {year}"
    d = lambda x: float(x or 0)
    _eu = core.euer(conn, None, lo, hi)   # EÜR-Methode (Brutto amtlich / Netto)
    revenue, expense, profit = _eu["einnahmen"], _eu["ausgaben"], _eu["gewinn"]
    margin = round(profit / revenue * 100) if revenue else 0
    invoices = core.list_invoices(conn)
    open_inv = [i for i in invoices if not i["paid_date"]]
    outstanding = sum(float(i["gross"]) for i in open_inv)
    txs = core.list_transactions(conn)
    from collections import defaultdict
    cl = defaultdict(float)
    for i in invoices:
        if i["paid_date"] and lo <= str(i["paid_date"]) <= hi:
            cl[i["customer_name"]] += float(i["gross"])
    ranked = sorted(cl.items(), key=lambda x: -x[1])[:4]
    cmax = ranked[0][1] if ranked else 1
    top_clients = [{"name": n, "amount": a, "pct": round(a / cmax * 100) if cmax else 0}
                   for n, a in ranked]
    kpi = {"revenue": revenue, "expense": expense, "profit": profit,
           "outstanding": outstanding, "margin": margin}
    # Monatsreihe (Balkendiagramm Einnahmen vs. Ausgaben) im Zeitraum
    series = []
    cy, cm = int(lo[:4]), int(lo[5:7])
    ey, em = int(hi[:4]), int(hi[5:7])
    while (cy, cm) <= (ey, em):
        mlo, mhi, _ = core.period_bounds(cy, "month", cm)
        _m = core.euer(conn, None, mlo, mhi)
        series.append({"label": _MONTHS_DE[cm][:3], "inc": _m["einnahmen"], "exp": _m["ausgaben"]})
        cy, cm = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
    smax = max([s["inc"] for s in series] + [s["exp"] for s in series] + [1.0])
    # Donut: Ausgaben nach Kategorie
    palette = ["#3B6EF5", "#1FA971", "#F0A31A", "#F5573B", "#9B7BE5", "#16171D", "#5C5F6E"]
    catrows = conn.execute(
        """SELECT category, COALESCE(SUM(net),0) AS net FROM expenses
           WHERE paid_date BETWEEN ? AND ? GROUP BY category ORDER BY net DESC LIMIT 7""",
        (lo, hi)).fetchall()
    cat_total = sum(d(r["net"]) for r in catrows) or 1.0
    donut, off = [], 0.0
    C = 2 * 3.14159265 * 40   # Umfang r=40
    for idx, r in enumerate(catrows):
        frac = d(r["net"]) / cat_total
        donut.append({"name": r["category"], "amount": d(r["net"]),
                      "color": palette[idx % len(palette)],
                      "dash": f"{frac * C:.2f} {C:.2f}", "off": f"{-off * C:.2f}"})
        off += frac
    reserve = core.tax_reserve(conn)                  # immer laufendes Quartal/Jahr
    upcoming = core.upcoming_deadlines(conn)
    return render_template("dashboard.html", active="dashboard", year=year, period=period,
                           label=label, years=list(range(date.today().year, 2022, -1)),
                           kpi=kpi, recent=txs[:6], open_inv=open_inv[:5],
                           open_count=len(open_inv), top_clients=top_clients,
                           cats=core.TX_CATEGORIES, series=series, smax=smax,
                           donut=donut, donut_total=expense,
                           reserve=reserve, upcoming=upcoming)


# ── Rechnungen ───────────────────────────────────────────────────────────
@app.route("/invoices")
def invoices():
    conn = core.db()
    tab = request.args.get("tab", "invoices")
    direction = request.args.get("dir")          # out | in
    year = request.args.get("year")
    from collections import defaultdict
    invs = core.list_invoices(conn)[::-1]
    exps = core.list_expenses(conn)[::-1]
    dirs = {
        "out": {"count": len(invs), "total": sum(float(i["gross"]) for i in invs)},
        "in": {"count": len(exps), "total": sum(float(e["gross"]) for e in exps)},
    }
    items = invs if direction == "out" else exps if direction == "in" else []
    keyf = (lambda i: str(i["issue_date"])[:4]) if direction == "out" else (lambda e: str(e["date"])[:4])
    byyear = defaultdict(list)
    for it in items:
        byyear[keyf(it)].append(it)
    years = [{"year": y, "count": len(byyear[y]),
              "total": sum(float(x["gross"]) for x in byyear[y])}
             for y in sorted(byyear, reverse=True)]
    rows = byyear.get(year, []) if year else []
    custs = core.list_customers(conn)
    return render_template("invoices.html", active="invoices", tab=tab, direction=direction,
                           year=year, years=years, rows=rows, dirs=dirs,
                           customers=custs, today=date.today().isoformat(),
                           cust_invoices={c["id"]: core.list_customer_invoices(conn, c["id"])
                                          for c in custs},
                           dupes=core.duplicate_customers(conn),
                           next_cust_number=core.next_customer_number(conn))


@app.route("/invoices/customer", methods=["POST"])
def invoices_customer():
    f = request.form
    if f.get("name", "").strip():
        try:
            c = core.create_customer(core.db(), f["name"].strip(),
                                     f.get("address", "").strip(),
                                     f.get("vat_id", "").strip() or None,
                                     f.get("number", "").strip() or None)
            nr = c["number"]
            flash("Kunde angelegt." if not nr else f"Kunde angelegt (Nr. {nr}).", "ok")
        except Exception as e:  # noqa
            flash(f"Fehler: {e}", "error")
    return redirect(url_for("invoices", tab="customers"))


@app.route("/customers/<int:cid>/number", methods=["POST"])
def customer_number(cid):
    try:
        r = core.set_customer_number(core.db(), cid, request.form.get("number", ""))
        flash(f"Kundennummer für {r['name']} " +
              (f"auf {r['number']} gesetzt." if r["number"] else "entfernt."), "ok")
    except Exception as e:  # noqa
        flash(f"Fehler: {e}", "error")
    return redirect(url_for("invoices", tab="customers"))


@app.route("/customers/<int:cid>/delete", methods=["POST"])
def customer_delete(cid):
    try:
        r = core.delete_customer(core.db(), cid)
        n = r["rechnungen_entkoppelt"]
        flash(f"Kunde {r['deleted']} gelöscht."
              + (f" {n} Rechnung(en) bleiben bestehen." if n else ""), "ok")
    except Exception as e:  # noqa
        flash(f"Fehler: {e}", "error")
    return redirect(url_for("invoices", tab="customers"))


@app.route("/customers/merge-duplicates", methods=["POST"])
def customers_merge_duplicates():
    try:
        r = core.merge_duplicate_customers(core.db())
        flash(f"{r['zusammengefuehrt']} doppelte(n) Kunden zusammengeführt." if r["zusammengefuehrt"]
              else "Keine doppelten Kunden gefunden.", "ok")
    except Exception as e:  # noqa
        flash(f"Fehler: {e}", "error")
    return redirect(url_for("invoices", tab="customers"))


@app.route("/invoices/new", methods=["GET", "POST"])
def invoice_new():
    conn = core.db()
    cfg = core.load_config()
    if request.method == "POST":
        f = request.form
        cid = f.get("customer_id", "").strip()
        if cid:
            cust = core.get_customer(conn, int(cid))
        else:
            if not f.get("new_name", "").strip():
                flash("Bitte einen Kunden wählen oder anlegen.", "error")
                return redirect(url_for("invoice_new"))
            if f.get("no_save"):
                # Empfänger nur für diese Rechnung – kein Eintrag im Kundenregister
                cust = core.adhoc_customer(f["new_name"].strip(),
                                           f.get("new_address", "").strip(),
                                           f.get("new_vat_id", "").strip() or None)
            else:
                cust = core.create_customer(
                    conn, f["new_name"].strip(),
                    f.get("new_address", "").strip(),
                    f.get("new_vat_id", "").strip() or None)

        items = []
        for desc, qty, unit, price, rate in zip(
                f.getlist("item_desc"), f.getlist("item_qty"), f.getlist("item_unit"),
                f.getlist("item_price"), f.getlist("item_rate")):
            if not desc.strip():
                continue
            items.append({"description": desc.strip(), "quantity": qty or "1",
                          "unit": unit.strip(), "unit_price": price or "0",
                          "vat_rate": rate or cfg["invoice"]["default_vat_rate"]})
        if not items:
            flash("Mindestens eine Position mit Beschreibung nötig.", "error")
            return redirect(url_for("invoice_new"))

        pdf = request.files.get("pdf")
        has_pdf = bool(pdf and pdf.filename)
        try:
            inv = core.create_invoice(
                conn, cfg, customer=cust, kind=f.get("kind", "domestic"),
                issue_date=f["issue_date"], service_from=f["service_from"],
                service_to=f.get("service_to") or None, items=items,
                notes=f.get("notes") or None,
                number=f.get("number", "").strip() or None,
                paid_date=f.get("paid_date") or None,
                supply_type=f.get("supply_type"),
                render=not has_pdf)  # eigenes PDF hochgeladen -> nicht generieren
        except Exception as e:  # z. B. ungültige Eingaben / Nummer existiert
            flash(f"Fehler: {e}", "error")
            return redirect(url_for("invoice_new"))
        if has_pdf:
            core.set_invoice_pdf(conn, inv["id"], pdf.read(), pdf.filename)
        flash(f"Rechnung {inv['number']} erstellt.", "ok")
        return redirect(url_for("invoices"))

    return render_template("invoice_new.html", active="invoices",
                           customers=core.list_customers(conn),
                           default_rate=cfg["invoice"].get("default_vat_rate", 19))


@app.route("/invoices/<int:iid>/pdf-upload", methods=["POST"])
def invoice_pdf_upload(iid):
    conn = core.db()
    pdf = request.files.get("pdf")
    if pdf and pdf.filename:
        core.set_invoice_pdf(conn, iid, pdf.read(), pdf.filename)
        flash("PDF ersetzt.", "ok")
    return redirect(url_for("invoices"))


@app.route("/invoices/<int:iid>/delete", methods=["POST"])
def invoice_delete(iid):
    conn = core.db()
    inv = core.get_invoice(conn, iid)
    year = str(inv["issue_date"])[:4] if inv else None
    try:
        r = core.delete_invoice(conn, iid)
        flash(f"Rechnung {r['deleted']} gelöscht."
              + (f" {r['buchungen_geloest']} Zahlung(en) wieder offen." if r["buchungen_geloest"] else ""),
              "ok")
    except Exception as e:  # noqa
        flash(f"Fehler: {e}", "error")
    return redirect(url_for("invoices", dir="out", year=year) if year else url_for("invoices"))


@app.route("/invoices/<int:iid>/pdf")
def invoice_pdf(iid):
    conn = core.db()
    inv = core.get_invoice(conn, iid)
    pfad = core.abs_pfad(inv["pdf_path"]) if inv else None
    if not pfad or not pfad.exists():
        abort(404)
    return send_file(pfad, mimetype="application/pdf",
                     download_name=f"Invoice-{inv['number']}.pdf")


@app.route("/invoices/<int:iid>/pay", methods=["POST"])
def invoice_pay(iid):
    conn = core.db()
    f = request.form
    pd = f.get("paid_date") or date.today().isoformat()
    create_tx = bool(f.get("create_tx"))
    res = core.record_invoice_payment(
        conn, iid, pd,
        amount=f.get("amount"), currency=f.get("currency"),
        method=(f.get("method") or "").strip(), create_tx=create_tx)
    if res.get("tx"):
        hc = core.home_currency()
        ah = res.get("amount_home")
        extra = (f" – Buchung angelegt ({res['amount']:.2f} {res['currency']}"
                 + (f" = {ah:.2f} {hc}" if res.get("currency") != hc and ah is not None else "")
                 + ")") if ah is not None else " – Buchung angelegt (Umrechnung offline fehlgeschlagen)"
        flash("Zahlung erfasst" + extra + ".", "ok")
    else:
        flash("Rechnung als bezahlt markiert (ohne Buchung).", "ok")
    return redirect(url_for("invoices"))


# ── Kunden ───────────────────────────────────────────────────────────────
@app.route("/customers", methods=["GET", "POST"])
def customers():
    conn = core.db()
    if request.method == "POST":
        f = request.form
        if f.get("name", "").strip():
            core.create_customer(conn, f["name"].strip(), f.get("address", "").strip(),
                                 f.get("vat_id", "").strip() or None)
            flash("Kunde angelegt.", "ok")
        return redirect(url_for("invoices", tab="customers"))
    return redirect(url_for("invoices", tab="customers"))


# ── Ausgaben ─────────────────────────────────────────────────────────────
@app.route("/expenses", methods=["GET", "POST"])
def expenses():
    conn = core.db()
    cfg = core.load_config()
    if request.method == "GET":
        return redirect(url_for("zahlungen"))
    if request.method == "POST":
        f = request.form
        if f.get("vendor", "").strip():
            r = core.create_expense(
                conn, date_=f["date"], vendor=f["vendor"].strip(),
                description=f.get("description", "").strip(),
                category=f.get("category", "Sonstiges").strip() or "Sonstiges",
                gross=f.get("gross", "0"), vat_rate=int(f.get("vat_rate", 19) or 19),
                paid_date=f.get("paid_date") or f["date"],
                private_share=f.get("private_share", 0) or 0,
                vat_kind=f.get("vat_kind"))
            file = request.files.get("receipt")
            if file and file.filename:
                safe = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip() or "beleg"
                dest = core.beleg_dir("eingang", f["date"]) / f"exp{r['id']}_{safe}"
                dest.write_bytes(file.read())
                conn.execute("UPDATE expenses SET receipt_path = ? WHERE id = ?", (str(dest), r["id"]))
                conn.commit()
            flash("Ausgabe erfasst.", "ok")
        return redirect(url_for("expenses"))
    return render_template("expenses.html", expenses=core.list_expenses(conn)[::-1],
                           default_rate=cfg["invoice"].get("default_vat_rate", 19),
                           vat_kinds=[(k, v) for k, v in core.EXPENSE_VAT_KINDS.items()
                                      if k != "rc_unknown"])


@app.route("/expenses/<int:eid>/pay", methods=["POST"])
def expense_pay(eid):
    conn = core.db()
    core.mark_expense_paid(conn, eid, request.form.get("paid_date") or date.today().isoformat())
    flash("Zahlung eingetragen.", "ok")
    return redirect(url_for("expenses"))


@app.route("/expenses/<int:eid>/update", methods=["POST"])
def expense_update(eid):
    f = request.form
    action = f.get("action", "expense")
    conn = core.db()
    try:
        if action in ("privatentnahme", "gebuehr", "einnahme_sonstige", "ignorieren"):
            core.book_expense_as_category(conn, eid, action)
            flash(f"Als „{core.TX_CATEGORIES[action]}“ umgebucht.", "ok")
        else:
            core.update_expense(conn, eid, category=f.get("category") or None,
                                vat_rate=f.get("vat_rate") or None,
                                private_share=f.get("private_share") or None,
                                vat_kind=f.get("vat_kind") or None)
            flash("Buchung aktualisiert.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("zahlungen"))


@app.route("/expenses/<int:eid>/vat-kind", methods=["POST"])
def expense_vat_kind(eid):
    """USt-Art nachtragen – vor allem die §13b-Zuordnung EU (Kz 46/47) vs. Drittland (84/85)."""
    f = request.form
    try:
        core.update_expense(core.db(), eid, vat_kind=f.get("vat_kind"))
        flash("USt-Art gesetzt.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("reports", p=f.get("back"), gen="ustva") if f.get("back")
                    else request.referrer or url_for("reports"))


@app.route("/expenses/<int:eid>/delete", methods=["POST"])
def expense_delete(eid):
    try:
        r = core.delete_expense(core.db(), eid)
        flash(f"Ausgabe „{r['vendor']}“ gelöscht.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("zahlungen"))


@app.route("/expenses/<int:eid>/receipt")
def expense_receipt(eid):
    row = core.db().execute("SELECT receipt_path FROM expenses WHERE id = ?", (eid,)).fetchone()
    pfad = core.abs_pfad(row["receipt_path"]) if row else None
    if not pfad or not pfad.exists():
        abort(404)
    return send_file(pfad)


# ── Berichte ─────────────────────────────────────────────────────────────
_MONTHS_DE = ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]


def _period_from_token(tok):
    """Zeitraum-Token -> (lo, hi, label, year, quarter|None). Formen:
    'JJJJ' | 'JJJJ-Qn' | 'JJJJ-MM'."""
    import re
    tok = (tok or "").strip()
    m = re.match(r"^(\d{4})-Q([1-4])$", tok)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        lo, hi, _ = core.period_bounds(y, "quarter", q)
        return lo, hi, f"{q}. Quartal {y}", y, q
    m = re.match(r"^(\d{4})-(\d{2})$", tok)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        lo, hi, _ = core.period_bounds(y, "month", mo)
        return lo, hi, f"{_MONTHS_DE[mo]} {y}", y, (mo - 1) // 3 + 1
    m = re.match(r"^(\d{4})$", tok)
    if m:
        y = int(m.group(1))
        return f"{y}-01-01", f"{y}-12-31", f"Gesamtes Jahr {y}", y, None
    t = date.today()
    lo, hi, _ = core.period_bounds(t.year, "month", t.month)
    return lo, hi, f"{_MONTHS_DE[t.month]} {t.year}", t.year, (t.month - 1) // 3 + 1


def _activity_years():
    """Alle Jahre mit Aktivität (früheste Buchung/Rechnung/Ausgabe … bis heute), absteigend."""
    conn = core.db()
    yrs = set()
    for tbl, col in (("transactions", "date"), ("invoices", "issue_date"), ("expenses", "date")):
        for r in conn.execute(f"SELECT DISTINCT substr({col},1,4) AS y FROM {tbl}"):
            if r["y"] and str(r["y"]).isdigit():
                yrs.add(int(r["y"]))
    yrs.add(date.today().year)
    return sorted(yrs, reverse=True)


def _period_list(_year=None):
    out = []
    for y in _activity_years():
        out.append((str(y), f"Gesamtes Jahr {y}"))
        for q in (4, 3, 2, 1):
            out.append((f"{y}-Q{q}", f"{q}. Quartal {y}"))
        for mo in range(12, 0, -1):
            out.append((f"{y}-{mo:02d}", f"{_MONTHS_DE[mo]} {y}"))
    return out


@app.route("/reports")
def reports():
    conn = core.db()
    cfg = core.load_config()
    s = core.load_settings()
    today = date.today()
    p = request.args.get("p") or f"{today.year}-{today.month:02d}"
    lo, hi, label, year, quarter = _period_from_token(p)
    gen = request.args.get("gen", "")
    d = lambda x: float(x or 0)
    # Readiness (für den gewählten Zeitraum)
    open_pay = core.transactions_open_count(conn)
    missing = conn.execute("SELECT COUNT(*) c FROM expenses WHERE receipt_path IS NULL").fetchone()["c"]
    closed = p in s.get("closed_periods", [])
    festz = 0 if closed else (
        conn.execute("SELECT COUNT(*) c FROM transactions WHERE category != 'offen' "
                     "AND date BETWEEN ? AND ?", (lo, hi)).fetchone()["c"]
        + conn.execute("SELECT COUNT(*) c FROM expenses WHERE paid_date BETWEEN ? AND ?",
                       (lo, hi)).fetchone()["c"]
        + conn.execute("SELECT COUNT(*) c FROM invoices WHERE paid_date BETWEEN ? AND ?",
                       (lo, hi)).fetchone()["c"])
    total_tx = len(core.list_transactions(conn))
    progress = 100 if total_tx == 0 else round(100 * (total_tx - open_pay) / total_tx)
    # Zahlen für den Zeitraum – EÜR (Brutto amtlich / Netto) über die zentrale Engine
    eu = core.euer(conn, cfg, lo, hi)
    vf = core._vat_figures(conn, cfg, lo, hi)
    zm_rows = conn.execute(
        """SELECT customer_name, customer_vat_id,
                  CASE WHEN COALESCE(supply_type,'service')='goods' THEN 'L' ELSE 'S' END AS art,
                  COALESCE(SUM(net),0) AS net
           FROM invoices WHERE kind='eu' AND COALESCE(supply_type,'service') != 'exempt'
             AND issue_date BETWEEN ? AND ?
           GROUP BY customer_vat_id, customer_name, art ORDER BY customer_name, art""",
        (lo, hi)).fetchall()
    zm_total = sum(d(r["net"]) for r in zm_rows)
    susa = conn.execute(
        """SELECT category, COALESCE(SUM(net),0) AS net FROM expenses
           WHERE paid_date BETWEEN ? AND ? GROUP BY category ORDER BY net DESC""", (lo, hi)).fetchall()
    fig = {"income": eu["einnahmen"], "expense": eu["ausgaben"], "profit": eu["gewinn"],
           "label": label, "method": eu["method"], "euer": eu,
           "net19": vf["net19"], "ust19": vf["ust19"], "net7": vf["net7"], "ust7": vf["ust7"],
           "eu_net": vf["eu_net"], "third_net": vf["third_net"], "vorsteuer": vf["vorsteuer"],
           "zahllast": vf["zahllast"], "zm_rows": zm_rows, "zm_total": zm_total, "susa": susa,
           # Vollständige USt-VA: eine Zeile je belegtem Feld, plus die noch offenen §13b-Belege.
           "kz_rows": core.vat_kz_rows(vf), "open_rc": core.vat_open_rc(conn, lo, hi)}
    return render_template("reports.html", active="reports", p=p, label=label, year=year,
                           quarter=quarter, periods=_period_list(year), gen=gen, fig=fig,
                           open_pay=open_pay, missing=missing, festz=festz, closed=closed,
                           progress=progress)


@app.route("/reports/close", methods=["POST"])
def reports_close():
    p = request.form.get("p")
    if p:
        core.close_period(p)
        flash("Periode festgeschrieben.", "ok")
    return redirect(url_for("reports", p=p))


# ── Bank / Zahlungen ─────────────────────────────────────────────────────
def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _tax_label(cat, vat):
    if cat == "ausgabe":
        return f"{int(vat)}% VSt." if vat else "ohne USt./VSt."
    if cat == "einnahme_rechnung":
        return "ohne USt./VSt."
    if cat == "einnahme_sonstige":
        return "19% USt."
    return "—"


def _day_label(iso):
    today = date.today()
    d = date.fromisoformat(iso[:10]) if iso else today
    if d == today:
        return "Heute · " + core.de_date(iso)
    if (today.toordinal() - d.toordinal()) == 1:
        return "Gestern · " + core.de_date(iso)
    return core.de_date(iso)


@app.route("/zahlungen")
def zahlungen():
    conn = core.db()
    p = request.args.get("p") or str(date.today().year)
    lo, hi, plabel, py, _pq = _period_from_token(p)
    entries = core.list_ledger(conn, lo, hi)          # Ausgaben + Rechnungen + Bank-Bewegungen
    inv_list = core.list_invoices(conn)
    linked = {t["invoice_id"] for t in core.list_transactions(conn) if t["invoice_id"]}
    inv_options = [i for i in inv_list if i["id"] not in linked]
    inv_cust = {i["id"]: (i["customer_name"] or "") for i in inv_list}
    doc_vendor_by_exp = {d["expense_id"]: (d["vendor"] or "")
                         for d in core.list_documents(conn) if d["expense_id"] and d["vendor"]}
    rules = core.get_rules_map(conn)
    groups, cur_day, cur_rows = [], None, None
    money_in = money_out = 0.0
    for r in entries:
        kind, ah = r["kind"], r["amount"]
        if not r.get("derived"):                       # Nutzungsentnahme ist kein Zahlungsfluss
            money_in += ah if ah > 0 else 0
            money_out += ah if ah < 0 else 0
        row = dict(r)
        if kind == "bank":
            t = r["tx"]
            cat = r["category"]
            st = {"action": "", "category": "", "vat_rate": 19, "private_share": 0,
                  "vat_kind": "domestic"}
            skr_no = skr_name = ""
            if cat in ("privatentnahme", "gebuehr", "einnahme_sonstige", "ignorieren"):
                st["action"] = cat
                # feste SKR-Konten für die Bank-Kategorien anzeigen (wie BuchhaltungsButler)
                _skr = {"privatentnahme": ("1800", "Privatentnahmen allgemein"),
                        "gebuehr": ("4970", "Nebenkosten des Geldverkehrs")}
                skr_no, skr_name = _skr.get(cat, ("", core.TX_CATEGORIES.get(cat, cat)))
                if skr_no:                                   # Modal-Vorbelegung (Konto + ohne USt)
                    st["category"], st["vat_rate"] = f"{skr_no} - {skr_name}", 0
            elif cat == "offen":
                s = core.suggestion_for(rules, t["description"])
                if s:
                    st = {"action": s["action"], "category": s.get("category") or "",
                          "vat_rate": int(s["vat_rate"]) if s.get("vat_rate") is not None else 19,
                          "private_share": int(round(s["private_share"] or 0)),
                          "vat_kind": core.expense_vat_kind(s)}
            tax_label = _tax_label(cat, st["vat_rate"])
            row.update({"st": st, "skr_no": skr_no, "skr_name": skr_name, "tax": tax_label,
                        "posted": cat != "offen", "categorized": bool(skr_no or skr_name),
                        "ref": t["ref"], "note": t["note"], "tx_type": t["tx_type"],
                        "currency": t["currency"], "amount_home": ah,
                        "beleg_url": url_for("zahlungen_receipt", tid=t["id"]) if t["receipt_path"] else None,
                        "open_url": None})
            search = " ".join(filter(None, [
                t["description"] or "", skr_name, tax_label, t["tx_type"] or "", t["note"] or "",
                core.eur(ah), f"{abs(ah):.2f}"])).lower()
        else:                                          # expense | invoice | nutzungsentnahme
            if kind == "expense":
                beleg_url = url_for("expense_receipt", eid=r["id"]) if r["beleg"] else None
                open_url = url_for("expenses")
            elif kind == "invoice":
                beleg_url = url_for("invoice_pdf", iid=r["id"]) if r["beleg"] else None
                open_url = url_for("invoices", dir="out", year=(r["date"] or "")[:4])
            else:                                      # nutzungsentnahme (abgeleitete Gegenbuchung)
                beleg_url = open_url = None
            row.update({"st": {"action": kind, "category": "", "vat_rate": r.get("vat_rate") or 0,
                               "private_share": r.get("private_share", 0)},
                        "posted": True, "categorized": True, "ref": None, "note": None,
                        "tx_type": r.get("sub"), "currency": core.home_currency(), "amount_home": ah,
                        "beleg_url": beleg_url, "open_url": open_url})
            search = " ".join(filter(None, [
                r["text"], r.get("sub") or "", r["skr_no"], r["skr_name"], r["tax"],
                core.eur(ah), f"{abs(ah):.2f}"])).lower()
        row.update({"description": r["text"], "search": search, "is_income": r["is_income"]})
        if r["date"] != cur_day:
            cur_day = r["date"]
            cur_rows = []
            groups.append({"label": _day_label(r["date"]), "iso": r["date"], "rows": cur_rows})
        cur_rows.append(row)
    for g in groups:
        g["total"] = sum(x["amount"] for x in g["rows"] if not x.get("derived"))
    summary = {"money_in": money_in, "money_out": money_out, "net": money_in + money_out}
    return render_template("zahlungen.html", active="zahlungen", groups=groups,
                           inv_options=inv_options, cats=core.TX_CATEGORIES,
                           summary=summary, count=len(entries), p=p, plabel=plabel,
                           periods=_period_list(py),
                           open_count=core.transactions_open_count(conn))


@app.route("/zahlungen/export")
def zahlungen_export():
    import csv
    import io
    from flask import Response
    conn = core.db()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Datum", "Beschreibung", "Betrag", "Währung", "Betrag_EUR",
                "Kategorie", "Typ"])
    for t in core.list_transactions(conn):
        w.writerow([t["date"], t["description"], t["amount"], t["currency"],
                    t["amount_home"], t["category"], t["tx_type"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=zahlungen.csv"})


@app.route("/zahlungen/manual", methods=["POST"])
def zahlungen_manual():
    f = request.form
    d = f.get("date") or date.today().isoformat()
    desc = (f.get("description") or "").strip()
    try:
        amt = abs(float((f.get("amount") or "0").replace(",", ".")))
    except ValueError:
        amt = 0.0
    if f.get("direction") == "out":
        amt = -amt
    cur = (f.get("currency") or core.home_currency()).upper()
    method = (f.get("method") or "Kasse").strip()
    if amt:
        core.create_manual_transaction(core.db(), d, desc, amt, cur, method=method)
        hc = core.home_currency()
        if cur != hc:
            ah, _ = core.to_home(core.db(), amt, cur, d)
            if ah is None:
                flash(f"Buchung angelegt, aber Umrechnung {cur}→{hc} fehlgeschlagen "
                      "(offline?). Betrag in Heimwährung fehlt – später erneut speichern.", "error")
            else:
                flash(f"Manuelle Buchung angelegt ({cur} → {ah:.2f} {hc}) – jetzt kategorisieren.", "ok")
        else:
            flash("Manuelle Buchung angelegt – jetzt unten kategorisieren.", "ok")
    else:
        flash("Betrag fehlt.", "error")
    return redirect(url_for("zahlungen"))


@app.route("/zahlungen/bulk", methods=["POST"])
def zahlungen_bulk():
    f = request.form
    ids = [int(x) for x in f.get("ids", "").split(",") if x.strip().isdigit()]
    action = f.get("action", "")
    conn = core.db()
    if not ids:
        flash("Keine Buchung ausgewählt.", "error")
    elif action == "expense":
        n = core.bulk_create_expense(conn, ids, f.get("category", "Sonstiges"),
                                     f.get("vat_rate", 0), f.get("private_share", 0))
        flash(f"{n} Buchung(en) als Betriebsausgabe erfasst.", "ok")
    else:
        n = core.bulk_set_category(conn, ids, action)
        label = core.TX_CATEGORIES.get(action, action)
        flash(f"{n} Buchung(en) auf '{label}' gesetzt.", "ok")
    return redirect(url_for("zahlungen", filter=f.get("back", "all"), q=f.get("q") or None))


@app.route("/zahlungen/import", methods=["POST"])
def zahlungen_import():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Bitte eine CSV-Datei (z. B. Revolut-Export) auswählen.", "error")
        return redirect(url_for("zahlungen"))
    raw = file.read()
    core.save_statement(raw, file.filename)
    res = core.import_bank_csv(core.db(), _decode(raw))
    flash(f"{res['imported']} Buchung(en) importiert, {res['duplicates']} Duplikat(e) übersprungen, "
          f"{res['matched']} automatisch Rechnungen zugeordnet.", "ok")
    return redirect(url_for("zahlungen"))


@app.route("/zahlungen/tx/<int:tid>/link", methods=["POST"])
def zahlungen_link(tid):
    inv_id = request.form.get("invoice_id")
    if inv_id:
        core.link_tx_invoice(core.db(), tid, int(inv_id))
        flash("Zahlung der Rechnung zugeordnet und als bezahlt markiert.", "ok")
    return redirect(url_for("zahlungen", filter=request.form.get("back", "all")))


@app.route("/zahlungen/tx/<int:tid>/expense", methods=["POST"])
def zahlungen_expense(tid):
    f = request.form
    core.create_expense_from_tx(core.db(), tid, f.get("category", "Sonstiges"),
                                f.get("vat_rate", 0), f.get("description") or None)
    flash("Als Betriebsausgabe erfasst.", "ok")
    return redirect(url_for("zahlungen", filter=f.get("back", "all")))


@app.route("/zahlungen/tx/<int:tid>/category", methods=["POST"])
def zahlungen_category(tid):
    f = request.form
    core.set_tx_category(core.db(), tid, f.get("category", "ignorieren"), f.get("note"))
    flash("Buchung kategorisiert.", "ok")
    return redirect(url_for("zahlungen", filter=f.get("back", "all")))


@app.route("/zahlungen/tx/<int:tid>/set", methods=["POST"])
def zahlungen_set(tid):
    """Einheitliche Zuordnung einer einzelnen Buchung per Dropdown."""
    f = request.form
    action = f.get("action", "")
    conn = core.db()
    if action == "expense":
        core.reset_tx(conn, tid)   # bestehende Zuordnung lösen -> Um-/Neubuchen
        core.create_expense_from_tx(conn, tid, f.get("category", "Sonstiges"),
                                    f.get("vat_rate", 0), private_share=f.get("private_share", 0),
                                    vat_kind=f.get("vat_kind"))
        flash("Als Betriebsausgabe erfasst.", "ok")
    elif action == "invoice":
        inv_id = f.get("invoice_id")
        if inv_id:
            core.reset_tx(conn, tid)
            core.link_tx_invoice(conn, tid, int(inv_id))
            flash("Rechnung zugeordnet und als bezahlt markiert.", "ok")
        else:
            flash("Keine Rechnung gewählt.", "error")
    elif action in core.TX_CATEGORIES:
        core.reset_tx(conn, tid)
        core.set_tx_category(conn, tid, action)
        flash(f"Buchung auf '{core.TX_CATEGORIES[action]}' gesetzt.", "ok")
    else:
        flash("Keine Aktion gewählt.", "error")
    return redirect(url_for("zahlungen", filter=f.get("back", "all"), q=f.get("q") or None))


@app.route("/zahlungen/tx/<int:tid>/split", methods=["POST"])
def zahlungen_split(tid):
    f = request.form
    lines = [{"category": c, "amount": a, "vat_rate": v}
             for c, a, v in zip(f.getlist("split_category"), f.getlist("split_amount"),
                                 f.getlist("split_vat")) if a.strip()]
    if lines:
        core.create_split_from_tx(core.db(), tid, lines)
        flash(f"Zahlung in {len(lines)} Positionen gesplittet.", "ok")
    else:
        flash("Keine Split-Positionen angegeben.", "error")
    return redirect(url_for("zahlungen"))


@app.route("/zahlungen/tx/<int:tid>/upload", methods=["POST"])
def zahlungen_upload(tid):
    file = request.files.get("file")
    if file and file.filename:
        core.attach_receipt_to_tx(core.db(), tid, file.read(), file.filename)
        flash("Beleg angehängt.", "ok")
    return redirect(url_for("zahlungen", filter=request.form.get("back", "all")))


@app.route("/zahlungen/tx/<int:tid>/receipt")
def zahlungen_receipt(tid):
    t = core.get_transaction(core.db(), tid)
    pfad = core.abs_pfad(t["receipt_path"]) if t else None
    if not pfad or not pfad.exists():
        abort(404)
    return send_file(pfad)


# ── Import ───────────────────────────────────────────────────────────────
@app.route("/import", methods=["GET", "POST"])
def import_csv():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Bitte eine CSV-Datei auswählen.", "error")
            return redirect(url_for("import_csv"))
        raw = file.read()
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        conn = core.db()
        cfg = core.load_config()
        res = core.import_invoices_csv(conn, cfg, text,
                                       render=not request.form.get("no_pdf"))
        if res["created"]:
            flash(f"{len(res['created'])} Rechnung(en) importiert: "
                  + ", ".join(res["created"]), "ok")
        for s in res["skipped"]:
            flash("Übersprungen – " + s, "error")
        for e in res["errors"]:
            flash("Fehler – " + e, "error")
        if not res["created"] and not res["skipped"] and not res["errors"]:
            flash("Nichts importiert – ist die CSV leer?", "error")
        return redirect(url_for("invoices"))
    return render_template("import.html", active="import", columns=core.IMPORT_COLUMNS)


@app.route("/import/template")
def import_template():
    path = core.ROOT / "import_template.csv"
    return send_file(path, mimetype="text/csv",
                     as_attachment=True, download_name="import_template.csv")


# ── Dokumente: Rechnungen, Belege & Zahlungs-CSVs (schön sortiert) ────────
def _stmt_dirs():
    return [core.STATEMENT_DIR]


def _group_by_year_quarter(items, iso_of, split_threshold=6):
    """Nach Jahr gruppieren (neueste zuerst); Jahre mit mehr als N Einträgen
    zusätzlich in Quartale unterteilen. iso_of(item) -> 'JJJJ-MM-TT'."""
    by_year = {}
    for it in items:
        y = (iso_of(it) or "")[:4] or "ohne Datum"
        by_year.setdefault(y, []).append(it)
    groups = []
    for y in sorted(by_year, reverse=True):
        yitems = by_year[y]
        node = {"year": y, "count": len(yitems), "quarters": None, "items": yitems}
        if len(yitems) > split_threshold and y.isdigit():
            byq = {}
            for it in yitems:
                mm = (iso_of(it) or "")[5:7]
                q = (int(mm) - 1) // 3 + 1 if mm.isdigit() and mm != "00" else 0
                byq.setdefault(q, []).append(it)
            node["quarters"] = [{"q": q, "items": byq[q]} for q in sorted(byq, reverse=True)]
            node["items"] = None
        groups.append(node)
    return groups


@app.route("/files")
def files():
    conn = core.db()
    # Vorschläge für offene Belege mit aktueller Logik neu berechnen (fixt alte Fehl-Matches)
    for d in core.list_documents(conn):
        if d["status"] == "to_review":
            stx = core.suggest_tx_for_document(conn, d["amount"], d["doc_date"], d["vendor"])
            if (stx or None) != (d["suggest_tx"] or None):
                conn.execute("UPDATE documents SET suggest_tx = ? WHERE id = ?", (stx, d["id"]))
    conn.commit()
    docs = core.list_documents(conn)
    counts = {"all": len(docs), "to_review": 0, "matched": 0, "processing": 0}
    for d in docs:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    open_txs = [t for t in core.list_transactions(conn, only_open=True) if t["amount"] < 0]
    tx_map = {t["id"]: {"date": core.de_date(t["date"]), "desc": t["description"] or "—",
                        "amount": core.eur(t["amount_home"] if t["amount_home"] is not None else t["amount"])}
              for t in open_txs}
    # Gelernte Anbieter-Regeln je Beleg (Konto/Steuersatz/Privatanteil vorbefüllen)
    doc_rules = {d["id"]: _vendor_rule(d["vendor"]) for d in docs if d["vendor"]}
    return render_template("files.html", active="files", docs=docs, counts=counts,
                           open_txs=open_txs, tx_map=tx_map, doc_rules=doc_rules,
                           llm_ok=assistant.is_configured())


@app.route("/documents/upload", methods=["POST"])
def documents_upload():
    files = request.files.getlist("file")
    conn = core.db()
    n = 0
    for file in files:
        if not file or not file.filename:
            continue
        data = file.read()
        ex = assistant.extract_document(data, file.filename)   # {} wenn kein LLM/Text
        did = core.add_document(conn, data, file.filename, vendor=ex.get("vendor"),
                                amount=ex.get("amount"), doc_date=ex.get("date"), status="to_review")
        stx = core.suggest_tx_for_document(conn, ex.get("amount"), ex.get("date"), ex.get("vendor"))
        if stx:
            conn.execute("UPDATE documents SET suggest_tx = ? WHERE id = ?", (stx, did))
            conn.commit()
        n += 1
    flash(f"{n} Dokument(e) hochgeladen." if n else "Keine Datei ausgewählt.",
          "ok" if n else "error")
    return redirect(url_for("files"))


@app.route("/documents/<int:did>/file")
def document_file(did):
    d = core.get_document(core.db(), did)
    pfad = core.abs_pfad(d["file_path"]) if d else None
    if not pfad or not pfad.exists():
        abort(404)
    return send_file(pfad)


def _wants_json():
    return "application/json" in (request.headers.get("Accept") or "") or request.args.get("json")


_VENDOR_SUFFIXES = {"gmbh", "ag", "kg", "ug", "mbh", "se", "co", "ohg", "ek", "ltd",
                    "inc", "llc", "oy", "oü", "ou", "bv", "sa", "srl", "&"}


def _vendor_key(vendor):
    """Anbieter auf einen stabilen Schlüssel normalisieren, z. B. 'Vodafone GmbH' -> 'vodafone'."""
    if not vendor:
        return None
    toks = [t for t in "".join(c if c.isalnum() or c.isspace() else " "
                               for c in vendor.lower()).split()
            if t not in _VENDOR_SUFFIXES]
    return " ".join(toks[:2]) or None


def _vendor_rule(vendor):
    key = _vendor_key(vendor)
    if not key:
        return None
    return core.load_settings().get("vendor_defaults", {}).get(key)


def _learn_vendor_rule(vendor, category, vat_rate, private_share):
    key = _vendor_key(vendor)
    if not key:
        return
    s = core.load_settings()
    rules = s.get("vendor_defaults", {})
    rules[key] = {"category": category, "vat_rate": int(vat_rate or 0),
                  "private_share": int(float(private_share or 0))}
    s["vendor_defaults"] = rules
    core.save_settings(s)


@app.route("/documents/<int:did>/confirm", methods=["POST"])
def document_confirm(did):
    f = request.form
    conn = core.db()
    doc = core.get_document(conn, did)
    tx_id = f.get("tx_id")
    lines_raw = f.get("lines")
    lines = []
    if lines_raw:
        try:
            lines = [l for l in (_json.loads(lines_raw) or []) if l]
        except (ValueError, TypeError):
            lines = []
    if lines:
        core.split_document(conn, did, lines, int(tx_id) if tx_id else None)
        msg = "Beleg auf mehrere Buchungssätze aufgeteilt und verbucht."
    elif tx_id:
        core.match_document_to_tx(conn, did, int(tx_id), category=f.get("category", "Sonstiges"),
                                  vat_rate=f.get("vat_rate", 19), private_share=f.get("private_share", 0))
        msg = "Beleg an Zahlung zugeordnet und verbucht."
    else:
        core.confirm_document(conn, did, category=f.get("category", "Sonstiges"),
                              vat_rate=f.get("vat_rate", 19), private_share=f.get("private_share", 0))
        msg = "Beleg als eigenständige Ausgabe verbucht (keine passende Zahlung)."
    # Anbieter-Regel merken (nur bei Einzelbuchung, nicht bei Splits)
    if doc and doc["vendor"] and not lines:
        _learn_vendor_rule(doc["vendor"], f.get("category", "Sonstiges"),
                           f.get("vat_rate", 19), f.get("private_share", 0))
    if _wants_json():
        return jsonify(ok=True, id=did, status="matched",
                       vendor=(doc and doc["vendor"]) or "verbucht",
                       doc_vendor=(doc and doc["vendor"]) or "",
                       rule=_vendor_rule(doc["vendor"]) if doc and doc["vendor"] else None)
    flash(msg, "ok")
    return redirect(url_for("files"))


@app.route("/documents/<int:did>/delete", methods=["POST"])
def document_delete(did):
    core.delete_document(core.db(), did)
    if _wants_json():
        return jsonify(ok=True, id=did)
    flash("Dokument gelöscht.", "ok")
    return redirect(url_for("files"))


# ── Kategorien / SKR03-Kontenrahmen ──────────────────────────────────────
@app.route("/banking")
def banking():
    # Einzige Grundlage für empty/onboarded: das lokal gespeicherte Konto
    # (settings.json -> "wirex"). Es entsteht beim Onboarding über die wirex_*-Tools.
    acc = wirex.account()

    def _eur_de(v, cur="EUR"):
        sym = "€" if cur == "EUR" else (cur + " ")
        return sym + f"{float(v):,.2f}".replace(",", "␟").replace(".", ",").replace("␟", ".")

    # Letzter bekannter Stand aus dem Konto – NIE auf "—" zurückfallen, wenn schon
    # einmal ein Wert bekannt war. "—" nur, solange noch nie etwas geladen wurde.
    cur = acc.get("balance_currency") or "EUR"
    bal = acc.get("balance")
    ld = acc.get("limit_daily")
    lu = acc.get("limit_usage") or 0
    unlimited = ld is None or (isinstance(ld, (int, float)) and ld < 0)
    iban_disp = acc.get("iban") or ("wird bereitgestellt…"
                                    if acc.get("iban_status") not in (None, "none") else "—")
    bank = {"last4": acc.get("last4") or "••••",
            "holder": (core.load_config().get("business", {}).get("name") or "").upper(),
            "expiry": acc.get("expiry") or "––/––",
            "pan": acc.get("pan") or "•••• •••• •••• ••••",
            "iban": iban_disp,
            "balance": _eur_de(bal, cur) if bal is not None else "—",
            "limit_label": ("kein Limit" if unlimited else _eur_de(ld, cur)) if ld is not None or bal is not None else "—",
            "limit_used": _eur_de(lu, cur) if bal is not None else "—",
            "limit_left": ("unbegrenzt" if unlimited else _eur_de(max(0, (ld or 0) - lu), cur)) if bal is not None else "—",
            "pending_amount": "", "pending_merchant": ""}
    return render_template("banking.html", active="banking", bank=bank, account=acc,
                           # drei Zustände: kein Konto -> KYC offen -> nutzbar
                           activated=wirex.is_onboarded() and wirex.kyc_done(),
                           kyc_pending=wirex.is_onboarded() and not wirex.kyc_done(),
                           kyc_label=wirex.kyc_label(), kyc_url=acc.get("kyc_url", ""),
                           wirex_env=wirex.env(), wirex_sandbox=wirex.is_sandbox())


@app.route("/banking/env", methods=["POST"])
def banking_env():
    """Zwischen Prod und Sandbox umschalten. Konten bleiben pro Umgebung getrennt."""
    target = (request.get_json(silent=True) or {}).get("env", "")
    try:
        wirex.set_env(target)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, env=wirex.env())


def _needs_passkey():
    """Gemeinsamer Einstieg für Aktionen, die eine Session brauchen."""
    try:
        start = _json.loads(wirex.call("wirex_session_start", {}))
    except Exception as e:  # noqa
        return jsonify(ok=False, error=str(e)), 502
    d = start.get("data") if isinstance(start.get("data"), dict) else start
    url = d.get("approval_url")
    if not url:
        return jsonify(ok=False, error=start.get("error") or "kein approval_url"), 502
    return jsonify(ok=False, needs_passkey=True, approval_url=url,
                   session_id=d.get("session_id"))


@app.route("/banking/signin", methods=["POST"])
def banking_signin():
    """Bestandsnutzer-Anmeldung: NUR Passkey, kein Onboarding, kein Name/E-Mail.
       wirex_session_start ohne Argumente -> die Signatur enthüllt das Konto."""
    try:
        start = _json.loads(wirex.call("wirex_session_start", {}))
    except Exception as e:  # noqa
        return jsonify(ok=False, error=str(e)), 502
    d = start.get("data") if isinstance(start.get("data"), dict) else start
    url = d.get("approval_url")
    if not url:
        return jsonify(ok=False, error=start.get("error") or "kein approval_url"), 502
    return jsonify(ok=True, approval_url=url, session_id=d.get("session_id"))


@app.route("/banking/signin/result", methods=["POST"])
def banking_signin_result():
    """Nach dem Anmelde-Tap: Session holen und das echte Konto einlesen
       (wirex_status füllt wallet/cards/verification lokal)."""
    sid = (request.get_json(silent=True) or {}).get("session_id")
    if not sid:
        return jsonify(ok=False, error="session_id fehlt"), 400
    wirex.call("wirex_session_result", {"session_id": sid})
    if not wirex.session():
        return jsonify(ok=False, pending=True)
    # Konto real einlesen -> wallet/cards/verification/iban werden übernommen.
    wirex.call("wirex_status", {})
    acc = wirex.account()
    return jsonify(ok=True, wallet=acc.get("wallet"),
                   verification=acc.get("verification_status"),
                   has_card=bool(acc.get("last4") or acc.get("card_id")))


@app.route("/banking/onboard/start", methods=["POST"])
def banking_onboard_start():
    """Deterministisches Onboarding starten (nicht über den Chat). Liefert die
       erste approval_url zum Passkey-Anlegen."""
    f = request.get_json(silent=True) or {}
    username = (f.get("username") or "").strip()
    email = (f.get("email") or "").strip()
    if not username or not email:
        return jsonify(ok=False, error="Username und E-Mail nötig"), 400
    # country ist optional – nur durchreichen, wenn der Nutzer eins angibt (z. B.
    # Nicht-DE); sonst nutzt Wirex seinen Default. Kein hartkodiertes "DE".
    res = wirex.onboard_start(username, email, f.get("country"))
    return (jsonify(res), 200 if res.get("ok") else 502)


@app.route("/banking/onboard/poll", methods=["POST"])
def banking_onboard_poll():
    """Ergebnis nach einem Passkey-Tap abfragen. phase='onboard' -> onboard_result
       (kann eine 2. Register-Signatur verlangen), phase='register' -> register_result."""
    f = request.get_json(silent=True) or {}
    sid = f.get("session_id")
    if not sid:
        return jsonify(state="error", error="session_id fehlt"), 400
    try:
        if f.get("phase") == "register":
            return jsonify(wirex.register_poll(sid))
        return jsonify(wirex.onboard_poll(sid))
    except Exception as e:  # noqa
        return jsonify(state="pending", error=str(e)[:200])


def _card_start(tool, extra=None):
    """Karten-Aktion starten: _start-Tool aufrufen, approval_url + session_id
       zurückgeben. credential_id/card_id hängt wirex.call selbst an."""
    acc = wirex.account()
    if not acc.get("card_id"):
        return jsonify(ok=False, error="keine Karte"), 400
    # Karten-Tools brauchen die EOA (Signer). Die kommt aus session_result – ist
    # sie noch nicht erfasst, zuerst eine Session minten (ein Passkey-Tap).
    if not acc.get("eoa"):
        return _needs_passkey()
    args = {"card_id": acc["card_id"]}
    if extra:
        args.update(extra)
    d = wirex._payload(wirex.call(tool, args))
    url = d.get("approval_url")
    sid = d.get("session_id")
    if not url or not sid:
        return jsonify(ok=False, error=d.get("error") or f"{tool} ohne approval_url"), 502
    return jsonify(ok=True, approval_url=url, session_id=sid,
                   result_tool=tool[:-6] + "_result")


@app.route("/banking/card/freeze", methods=["POST"])
def banking_card_freeze():
    action = (request.get_json(silent=True) or {}).get("action", "freeze")
    if action not in ("freeze", "unfreeze"):
        return jsonify(ok=False, error="ungültige Aktion"), 400
    return _card_start("wirex_card_action_start", {"action": action})


@app.route("/banking/card/limit", methods=["POST"])
def banking_card_limit():
    limit = (request.get_json(silent=True) or {}).get("limit")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Limit fehlt"), 400
    return _card_start("wirex_card_action_start", {"action": "set-limit", "limit": limit})


@app.route("/banking/card/reveal", methods=["POST"])
def banking_card_reveal():
    kind = (request.get_json(silent=True) or {}).get("kind")
    tool = {"details": "wirex_card_details_start"}.get(kind)
    if not tool:
        return jsonify(ok=False, error="ungültig"), 400
    return _card_start(tool)


@app.route("/banking/reset", methods=["POST"])
def banking_reset():
    """Das Konto der AKTUELLEN Umgebung lokal verwerfen und neu starten.
       Löscht nur den lokalen Zustand (Wirex-seitig bleibt es bestehen)."""
    wirex.clear_account()
    wirex.forget_session()
    return jsonify(ok=True, env=wirex.env())


@app.route("/banking/kyc/continue", methods=["POST"])
def banking_kyc_continue():
    """KYC fortsetzen/neu einreichen wie nuri-expo: onboard erneut aufrufen ->
       liefert nach der Signatur einen FRISCHEN Sumsub-Link. Braucht den lokal
       gespeicherten username + email (Bestandskonto)."""
    acc = wirex.account()
    username, email = acc.get("username"), acc.get("email")
    if not username or not email:
        return jsonify(ok=False, error="Username/E-Mail fehlen im Konto"), 400
    try:
        r = _json.loads(wirex.call("wirex_onboard_start", {
            "username": username, "email": email, "use_existing_passkey": True}))
    except Exception as e:  # noqa
        return jsonify(ok=False, error=str(e)), 502
    d = r.get("data") if isinstance(r.get("data"), dict) else r
    url = d.get("approval_url")
    if not url:
        return jsonify(ok=False, error=r.get("error") or "kein approval_url"), 502
    return jsonify(ok=True, approval_url=url, session_id=d.get("session_id"))


@app.route("/banking/kyc/recheck", methods=["POST"])
def banking_kyc_recheck():
    """KYC-Stand bei Wirex nachschlagen. wirex.call hängt die E-Mail selbst an,
       nur damit liefert wirex_status eine frische kyc_url."""
    if not wirex.session():
        return _needs_passkey()
    wirex.call("wirex_status", {})          # Ergebnis wird in den Konto-Block übernommen
    return jsonify(ok=True, kyc_done=wirex.kyc_done(), label=wirex.kyc_label(),
                   kyc_url=wirex.account().get("kyc_url", ""))


@app.route("/banking/status", methods=["POST"])
def banking_status():
    """Roher Status inkl. capabilities – für den stillen IBAN-Poller + Konsole."""
    if not wirex.session():
        return jsonify(ok=False, needs_session=True)
    res = wirex.status_debug()
    if res.get("error"):
        return jsonify(ok=False, error=res["error"]), 502
    return jsonify(ok=True, **res)


@app.route("/banking/live", methods=["POST"])
def banking_live():
    """Guthaben/IBAN/Karte live nachladen, wenn eine Session aktiv ist.
       Ohne Session ein sanftes needs_session (kein Passkey erzwingen beim Laden)."""
    if not wirex.session():
        return jsonify(ok=False, needs_session=True)
    res = wirex.live_summary()
    if res.get("error"):
        return jsonify(ok=False, error=res["error"]), 502
    return jsonify(ok=True, **res)


def _mark_imported(rows):
    """Jede Zeile markieren, ob ihr ref schon in den Zahlungen liegt."""
    existing = {r[0] for r in core.db().execute(
        "SELECT ref FROM transactions WHERE ref LIKE 'wirex:%'").fetchall()}
    for r in rows:
        r["imported"] = r.get("ref") in existing
    return rows


@app.route("/banking/transactions", methods=["POST"])
def banking_transactions():
    """Wirex-Umsätze anzeigen (nicht importieren). Zeigt bei Bedarf den lokalen
       Cache (ohne Passkey); force=true zieht frisch von Wirex (Session nötig)."""
    force = bool((request.get_json(silent=True) or {}).get("force"))
    if wirex.session():
        res = wirex.fetch_transactions(50)
        if res.get("error"):
            cached = wirex.cached_transactions()
            if cached:
                return jsonify(ok=True, rows=_mark_imported(cached), stale=True, error=res["error"])
            return jsonify(ok=False, error=res["error"]), 502
        wirex.cache_transactions(res["rows"])
        return jsonify(ok=True, rows=_mark_imported(res["rows"]))
    # Keine Session: Cache zeigen (kein Passkey), nur ein erzwungener Refresh
    # (oder ein leerer Cache) verlangt eine Anmeldung.
    cached = wirex.cached_transactions()
    if cached and not force:
        return jsonify(ok=True, rows=_mark_imported(cached), stale=True, needs_refresh=True)
    return _needs_passkey()


@app.route("/banking/transactions/import", methods=["POST"])
def banking_transactions_import():
    """Nur die ausgewählten Umsätze (per ref) in die Zahlungen übernehmen –
       als OFFENE Zahlungen, kein Buchungssatz, keine Festschreibung."""
    refs = set((request.get_json(silent=True) or {}).get("refs") or [])
    if not refs:
        return jsonify(ok=False, error="nichts ausgewählt"), 400
    # Aus dem lokalen Cache übernehmen – die Zeilen enthalten alles Nötige,
    # kein erneuter Wirex-Call (und damit kein Passkey) nötig.
    rows = wirex.cached_transactions()
    if not rows and wirex.session():
        rows = wirex.fetch_transactions(50).get("rows") or []
    selected = [r for r in rows if r.get("ref") in refs]
    if not selected:
        return jsonify(ok=False, error="Umsätze nicht im Cache – bitte aktualisieren"), 409
    out = core.import_bank_rows(core.db(), selected)
    return jsonify(ok=True, **out)


# Nur eindeutige Ergebnis-Felder. 'status' wäre auch bei "pending" wahr und hat
# den Abschluss früher fälschlich gemeldet.
_PASSKEY_DONE_FIELDS = ("session", "wallet", "kyc_url", "pan", "pin", "transfer_id")


def _read_str(src, keys):
    if isinstance(src, dict):
        for k in keys:
            v = src.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _parse_card_details(d):
    """card_details_result -> {pan, expiry, cvv}. details/cvv können String
       ODER Objekt sein (nuri-expo parseWirexCardDetails)."""
    details, cvv = d.get("details"), d.get("cvv")
    pan = details if isinstance(details, str) else _read_str(details, ["card_number", "pan", "number"])
    exp = _read_str(details, ["expiry_date", "expiry", "exp"])
    cvv_v = cvv if isinstance(cvv, str) else _read_str(cvv, ["cvv", "code"])
    return {"pan": pan, "expiry": exp, "cvv": cvv_v}




def _passkey_summary(d: dict) -> str:
    """Kurzfassung fürs Chat-Fenster – nie das rohe JSON."""
    if d.get("kyc_url"):
        return f"Passkey bestätigt. KYC ist noch offen: {d['kyc_url']}"
    bits = []
    if d.get("session"):
        bits.append("Session aktiv")
    if d.get("wallet"):
        bits.append(f"Konto {d['wallet'][:10]}…")
    if d.get("verification_status"):
        bits.append(f"KYC: {d['verification_status']}")
    if d.get("pan") or d.get("pin"):
        bits.append("Kartendaten liegen vor")
    return "Passkey bestätigt." + (" " + ", ".join(bits) + "." if bits else "")


@app.route("/banking/passkey/result", methods=["POST"])
def banking_passkey_result():
    """Nach dem Passkey-Tap das zugehörige *_result-Tool aufrufen. Wird gepollt:
       solange nicht signiert wurde, meldet Wirex noch kein Ergebnis."""
    f = request.get_json(silent=True) or {}
    tool, sid = f.get("tool"), f.get("session_id")
    if not tool or not str(tool).startswith("wirex_") or not str(tool).endswith("_result"):
        return jsonify(ok=False, error="ungültiges Tool"), 400
    try:
        payload = _json.loads(wirex.call(tool, {"session_id": sid} if sid else {}))
    except Exception as e:  # noqa
        return jsonify(ok=False, pending=True, error=str(e))
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if payload.get("error"):
        return jsonify(ok=False, pending=True, error=str(payload["error"])[:200])
    # Karten-Details/PIN liefern die Daten VERSCHACHTELT (details/cvv/pin als
    # String oder Objekt) – normalisieren und daran den Abschluss festmachen.
    result = d
    if tool == "wirex_card_details_result":
        parsed = _parse_card_details(d)
        done = bool(parsed.get("pan") or parsed.get("cvv"))
        result = parsed
    elif tool == "wirex_card_action_result":
        # freeze/limit liefern nur ok:true – fertig, sobald nicht mehr pending.
        done = bool(payload.get("ok") and d.get("status") not in ("pending", "waiting"))
    else:
        # Fresh-Onboarding ist mehrstufig: register_call_data zählt als Fortschritt.
        done = any(d.get(k) for k in _PASSKEY_DONE_FIELDS) or bool(d.get("register_call_data"))
    # Terminaler Status ohne Payload = Wirex hat abgelehnt.
    term = d.get("status") not in (None, "", "pending", "waiting")
    return jsonify(ok=done, pending=not done and not term,
                   status=d.get("status") or "", intent=d.get("intent") or "",
                   error=(f"Wirex-Status: {d.get('status')}" if term and not done else None),
                   summary=_passkey_summary(d) if done and result is d else "",
                   result=result if done else None)


@app.route("/banking/session/result", methods=["POST"])
def banking_session_result():
    """Nach dem Passkey-Tap das Session-Token abholen (wirex.call merkt es sich)."""
    sid = (request.get_json(silent=True) or {}).get("session_id")
    if not sid:
        return jsonify(ok=False, error="session_id fehlt"), 400
    wirex.call("wirex_session_result", {"session_id": sid})
    return jsonify(ok=bool(wirex.session()))


@app.route("/categories", methods=["GET", "POST"])
def categories():
    if request.method == "POST":
        acc = (request.form.get("account") or "").strip()
        if acc:
            core.add_custom_account(acc)
            flash("Konto hinzugefügt.", "ok")
        return redirect(url_for("categories"))
    # gleiche Basis wie die Dropdowns (config.toml oder Default), damit beides identisch ist
    base = (core.load_config().get("bookkeeping", {}) or {}).get("expense_accounts") \
        or core.DEFAULT_EXPENSE_ACCOUNTS
    return render_template("categories.html", active="categories",
                           accounts=list(base),
                           custom=core.load_settings().get("custom_accounts", []))


# ── Wiederkehrend ────────────────────────────────────────────────────────
@app.route("/recurring", methods=["GET", "POST"])
def recurring():
    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            core.add_recurring(request.form.get("client"), request.form.get("service"),
                               request.form.get("interval") or "monatlich",
                               request.form.get("amount"), request.form.get("next_run"))
        elif act == "toggle":
            core.toggle_recurring(request.form.get("id"))
        elif act == "delete":
            core.delete_recurring(request.form.get("id"))
        return redirect(url_for("recurring"))
    return render_template("recurring.html", active="recurring", items=core.list_recurring(),
                           customers=core.list_customers(core.db()),
                           today=date.today().isoformat())


# ── Mahnwesen ────────────────────────────────────────────────────────────
_DUNNING_STAGES = core.DUNNING_STAGES


@app.route("/dunning", methods=["GET", "POST"])
def dunning():
    if request.method == "POST":
        num, act = request.form.get("number"), request.form.get("act")
        if act == "advance":
            core.advance_dunning(num)
        elif act == "reset":
            core.reset_dunning(num)
        return redirect(url_for("dunning"))
    return render_template("dunning.html", active="dunning",
                           cases=core.dunning_cases(core.db()),
                           stage_names=_DUNNING_STAGES)


# ── Profile ──────────────────────────────────────────────────────────────
def _initialen(name: str) -> str:
    teile = [t for t in (name or "").split() if t[:1].isalnum()]
    return ("".join(t[0] for t in teile[:2]) or (name or "?")[:1]).upper()


@app.route("/profile")
def profile_waehlen():
    """Startbildschirm: Kachel je Profil."""
    profile = [{**p, "initialen": _initialen(p["name"])} for p in core.list_profiles()]
    return render_template("profile.html", profile=profile)


@app.route("/profile/<slug>/oeffnen", methods=["POST"])
def profile_oeffnen(slug):
    if not core.get_profile(slug):
        abort(404)
    session["profil"] = slug
    return redirect(url_for("dashboard"))


@app.route("/profile/wechseln", methods=["POST"])
def profile_wechseln():
    session.pop("profil", None)
    return redirect(url_for("profile_waehlen"))


@app.route("/profile/neu", methods=["POST"])
def profile_neu():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Bitte einen Namen für das Profil angeben.", "error")
        return redirect(url_for("profile_waehlen"))
    eintrag = core.create_profile(name)
    session["profil"] = eintrag["slug"]
    return redirect(url_for("setup"))


@app.route("/profile/<slug>/umbenennen", methods=["POST"])
def profile_umbenennen(slug):
    try:
        core.rename_profile(slug, request.form.get("name", ""))
        flash("Profil umbenannt.", "ok")
    except (KeyError, ValueError):
        flash("Profil konnte nicht umbenannt werden.", "error")
    return redirect(url_for("profile_waehlen"))


@app.route("/profile/<slug>/entfernen", methods=["POST"])
def profile_entfernen(slug):
    """Nimmt das Profil aus der Liste. Die Daten bleiben auf der Platte."""
    try:
        core.delete_profile(slug)
        if session.get("profil") == slug:
            session.pop("profil", None)
        flash("Profil entfernt. Die Daten liegen weiter unter profiles/.", "ok")
    except KeyError:
        flash("Profil nicht gefunden.", "error")
    return redirect(url_for("profile_waehlen"))


# ── Ersteinrichtung ──────────────────────────────────────────────────────
@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Legt beim ersten Start die config.toml an. Danach nicht mehr erreichbar."""
    if core.config_exists():
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("setup.html", setup_modus=True)

    f = request.form
    def t(name):
        return (f.get(name) or "").strip()

    fehler = []
    if not t("name"):
        fehler.append("Firma / Anzeigename fehlt.")
    if not t("owner"):
        fehler.append("Inhaber fehlt.")
    if not t("street") or not t("city"):
        fehler.append("Adresse (Straße und PLZ/Ort) fehlt.")
    if fehler:
        for m in fehler:
            flash(m, "error")
        return render_template("setup.html", form=f, setup_modus=True), 400

    address = [t("street"), t("city")]
    if t("country"):
        address.append(t("country"))

    try:
        vat_rate = int(t("default_vat_rate") or 19)
    except ValueError:
        vat_rate = 19
    try:
        terms = int(t("payment_terms_days") or 14)
    except ValueError:
        terms = 14

    core.write_config(
        business={"name": t("name"), "owner": t("owner"), "address_lines": address,
                  "vat_id": t("vat_id"), "tax_number": t("tax_number"),
                  "email": t("email"), "phone": t("phone")},
        bank={"holder": t("bank_holder") or t("owner"), "iban": t("iban"), "bic": t("bic")},
        invoice={"currency": t("currency") or "EUR", "default_vat_rate": vat_rate,
                 "payment_terms_days": terms, "language": t("language") or "de"},
        tax={"taxation": t("taxation") or "ist", "va_period": t("va_period") or "quarter",
             "small_business": bool(f.get("small_business"))},
    )
    core.db()  # DB/Ordner anlegen, damit das Dashboard direkt trägt
    flash("Einrichtung abgeschlossen. Du kannst alles später in den Einstellungen ändern.", "ok")
    return redirect(url_for("dashboard"))


# ── Einstellungen ────────────────────────────────────────────────────────
@app.route("/settings", methods=["GET", "POST"])
def settings():
    s = core.load_settings()
    if request.method == "POST":
        f = request.form
        section = f.get("section", "llm")
        if section == "llm":
            s["base_url"] = f.get("base_url", "").strip()
            s["model"] = f.get("model", "").strip()
            new_key = f.get("api_key", "").strip()
            if new_key and new_key != "········":
                s["api_key"] = new_key
            s["claude_model"] = f.get("claude_model", "").strip()
            s["developer_mode"] = bool(f.get("developer_mode"))
        elif section == "appearance":
            s["accent"] = f.get("accent", "green")
            s["density"] = f.get("density", "airy")
        elif section == "preferences":
            s["home_currency"] = (f.get("home_currency") or "EUR").strip().upper()
            s["euer_method"] = "netto" if f.get("euer_method") == "netto" else "brutto"
        elif section == "steuer":
            s["est_veranlagung"] = "zusammen" if f.get("est_veranlagung") == "zusammen" else "einzel"
            s["est_kirchensteuer"] = f.get("est_kirchensteuer", "0")
            s["est_soli"] = bool(f.get("est_soli"))
            s["est_krankenversicherung"] = core._num(f.get("est_krankenversicherung", "0"))
            s["est_weitere_einkuenfte"] = core._num(f.get("est_weitere_einkuenfte", "0"))
            vz = f.get("est_vorauszahlung", "").strip()
            s["est_vorauszahlung"] = core._num(vz) if vz else None
            s["business_vat_id"] = f.get("business_vat_id", "").strip().upper()
            s["business_tax_number"] = f.get("business_tax_number", "").strip()
            s["ust_dauerfrist"] = bool(f.get("ust_dauerfrist"))
            svz = f.get("ust_sondervorauszahlung", "").strip()
            s["ust_sondervorauszahlung"] = core._num(svz) if svz else None
        elif section == "email":
            for k in ("email_provider", "email_from", "email_reply_to",
                      "smtp_host", "smtp_port", "smtp_user"):
                s[k] = f.get(k, "").strip()
            # Standard-Nachricht (Betreff/Text/Signatur) – Zeilenumbrüche & Platzhalter erhalten
            s["email_subject_tpl"] = f.get("email_subject_tpl", "").strip()
            s["email_body_tpl"] = (f.get("email_body_tpl", "") or "").replace("\r\n", "\n").strip("\n")
            s["email_signature"] = (f.get("email_signature", "") or "").replace("\r\n", "\n").strip("\n")
            new_pw = f.get("smtp_pass", "").strip()
            if new_pw and new_pw != "········":
                s["smtp_pass"] = new_pw
            s["auto_send_invoices"] = bool(f.get("auto_send_invoices"))
            s["auto_send_dunning"] = bool(f.get("auto_send_dunning"))
        elif section == "bank":
            s["bank_holder"] = f.get("bank_holder", "").strip()
            s["bank_iban"] = f.get("bank_iban", "").strip().replace(" ", "").upper()
            s["bank_bic"] = f.get("bank_bic", "").strip().upper()
        core.save_settings(s)
        flash("Gespeichert.", "ok")
        tab = {"bank": "profile"}.get(section, section)   # bank-Formular liegt im Profil-Tab
        return redirect(url_for("settings", tab=tab))
    biz, bank, va_period = {}, {}, "quarter"
    try:
        cfg = core.load_config()          # bank enthält bereits die Settings-Overrides
        biz, bank = cfg.get("business", {}), cfg.get("bank", {})
        va_period = cfg.get("tax", {}).get("va_period", "quarter")
    except Exception:
        pass
    return render_template("settings.html", active="settings", s=s,
                           has_key=bool(s.get("api_key")), has_pw=bool(s.get("smtp_pass")),
                           biz=biz, bank=bank, cfg_va_period=va_period,
                           tab=request.args.get("tab", "appearance"))


@app.route("/tax/ack", methods=["POST"])
def tax_ack():
    """Steuertermin ab-/anhaken (rote Fälligkeits-Markierung)."""
    key = request.form.get("key", "").strip()
    if key:
        core.ack_deadline(key, on=not request.form.get("undo"))
    return redirect(request.referrer or url_for("dashboard"))


# ── Chat ─────────────────────────────────────────────────────────────────
def _chat_display() -> list:
    out = []
    for m in CHAT_MESSAGES:
        role = m.get("role")
        if role == "user" and isinstance(m.get("content"), str):
            if m.get("_file"):
                out.append({"role": "user", "text": m.get("_display") or "", "file": m["_file"]})
            else:
                out.append({"role": "user", "text": m.get("_display") or m["content"]})
        elif role == "assistant":
            if m.get("content") or m.get("_blocks"):
                out.append({"role": "assistant", "text": m.get("content") or "",
                            "blocks": m.get("_blocks") or []})
            for c in (m.get("tool_calls") or []):
                name = c.get("function", {}).get("name", "tool")
                if name != "present":
                    out.append({"role": "tool", "text": name})
    return out


@app.route("/chat")
def chat():
    return render_template("chat.html", active="chat", display=_chat_display(),
                           configured=assistant.is_configured(), dev=assistant.dev_enabled(),
                           model=core.load_settings().get("model", ""))


@app.route("/chat/send", methods=["POST"])
def chat_send():
    text = request.form.get("message", "").strip()
    if text:
        CHAT_MESSAGES.append({"role": "user", "content": text})
        res = assistant.run(list(CHAT_MESSAGES))
        CHAT_MESSAGES.clear()
        CHAT_MESSAGES.extend(res["messages"])
    return redirect(url_for("chat"))


@app.route("/chat/reset", methods=["POST"])
def chat_reset():
    CHAT_MESSAGES.clear()
    return redirect(url_for("chat"))


# ── Chat-Widget: JSON-API (für das schwebende Panel auf jeder Seite) ──────
@app.route("/chat/api")
def chat_api():
    return jsonify(messages=_chat_display(),
                   configured=assistant.is_configured(), dev=assistant.dev_enabled())


@app.route("/chat/api/send", methods=["POST"])
def chat_api_send():
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    if text:
        CHAT_MESSAGES.append({"role": "user", "content": text})
        res = assistant.run(list(CHAT_MESSAGES))
        CHAT_MESSAGES.clear()
        CHAT_MESSAGES.extend(res["messages"])
    return jsonify(messages=_chat_display())


@app.route("/chat/api/upload", methods=["POST"])
def chat_api_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(messages=_chat_display(), error="Keine Datei erhalten."), 400
    if not assistant.is_configured():
        return jsonify(messages=_chat_display(),
                       error="Bitte zuerst unter Einstellungen einen LLM-Zugang hinterlegen."), 400
    data = file.read()
    note = (request.form.get("message") or "").strip()
    res = assistant.analyze_pdf(data, file.filename)
    # Der Anhang wird als Pill gerendert – die Bubble zeigt nur die Anmerkung.
    meta = {"name": file.filename, "size": len(data)}
    if res.get("empty"):
        CHAT_MESSAGES.append({"role": "user", "content": f"[Beleg hochgeladen: {file.filename}]",
                              "_display": note, "_file": meta})
        CHAT_MESSAGES.append({"role": "assistant", "content": res["reply"]})
        return jsonify(messages=_chat_display())
    content = res["content"] + (f"\n\nNutzer-Anmerkung: {note}" if note else "")
    CHAT_MESSAGES.append({"role": "user", "content": content, "_display": note, "_file": meta})
    out = assistant.run(list(CHAT_MESSAGES))
    CHAT_MESSAGES.clear()
    CHAT_MESSAGES.extend(out["messages"])
    return jsonify(messages=_chat_display())


@app.route("/chat/api/reset", methods=["POST"])
def chat_api_reset():
    CHAT_MESSAGES.clear()
    return jsonify(messages=[])


def _eur(x) -> str:
    s = f"{float(x):,.2f}"
    return "€" + s.replace(",", "␟").replace(".", ",").replace("␟", ".")


_DEFAULT_SUBJECT_TPL = "Rechnung {number}"
_DEFAULT_BODY_TPL = ("Hallo {customer},\n\nanbei die Rechnung {number} über {amount}, "
                     "zahlbar bis {due}.\n\nVielen Dank")


def _email_texts(customer, number, amount, due):
    """Betreff + Text aus den in den Einstellungen hinterlegten Vorlagen füllen (mit Signatur)."""
    s = core.load_settings()
    biz = core.load_config().get("business", {}).get("name", "")
    ctx = {"customer": customer or "", "number": number or "", "amount": amount or "",
           "due": due or "", "business": biz}

    def fill(t):
        for k, v in ctx.items():
            t = t.replace("{" + k + "}", str(v))
        return t
    subject = fill((s.get("email_subject_tpl") or _DEFAULT_SUBJECT_TPL))
    body = fill((s.get("email_body_tpl") or _DEFAULT_BODY_TPL))
    sig = (s.get("email_signature") or "").strip()
    if sig:
        body = body.rstrip() + "\n\n" + fill(sig)
    return subject, body


@app.route("/chat/api/customers")
def chat_api_customers():
    """Kundenliste für das Rechnungs-Formular im Chat (Empfänger-Dropdown)."""
    conn = core.db()
    out = []
    for c in core.list_customers(conn):
        name = c["name"] or ""
        parts = [p for p in name.replace("-", " ").split() if p]
        initials = "".join(p[0] for p in parts[:2]).upper() or (name[:2].upper() if name else "?")
        out.append({"id": c["id"], "name": name, "initials": initials,
                    "address": c["address"] or "", "vat_id": c["vat_id"] or ""})
    return jsonify(customers=out)


@app.route("/chat/api/invoice", methods=["POST"])
def chat_api_invoice():
    """Rechnung aus dem Chat-Formular erstellen. Betrag ist BRUTTO; Netto wird herausgerechnet."""
    data = request.get_json(silent=True) or {}
    conn = core.db()
    cfg = core.load_config()
    name = (data.get("customer_name") or "").strip()
    if not name:
        return jsonify(error="Bitte einen Empfänger angeben."), 400
    try:
        net = float(str(data.get("amount")).replace(",", "."))  # Eingabe ist NETTO
    except (TypeError, ValueError):
        net = 0.0
    if net <= 0:
        return jsonify(error="Bitte einen gültigen Betrag angeben."), 400
    kind = data.get("kind") or "domestic"
    if kind not in core.KINDS:
        kind = "domestic"
    supply = data.get("supply_type") or "service"
    reverse = kind in ("eu", "third") or supply == "exempt"
    rate = 0 if reverse else int(data.get("vat_rate") or cfg["invoice"]["default_vat_rate"])
    net = round(net, 2)
    service = (data.get("service") or "Leistung").strip()
    cid = data.get("customer_id")
    cust = core.get_customer(conn, int(cid)) if cid else None
    if not cust:
        if data.get("no_save"):
            cust = core.adhoc_customer(name, data.get("address") or "",
                                       data.get("vat_id") or None)
        else:
            cust = core.resolve_or_create_customer(conn, name, data.get("address") or "",
                                                   data.get("vat_id") or None)
    term = int(data.get("term_days") or cfg["invoice"].get("payment_terms_days", 14))
    icfg = dict(cfg)
    icfg["invoice"] = {**cfg["invoice"], "payment_terms_days": term}
    today = date.today().isoformat()
    try:
        inv = core.create_invoice(
            conn, icfg, customer=cust, kind=kind, issue_date=today,
            service_from=today, service_to=None,
            items=[{"description": service, "quantity": 1, "unit": "Pauschal",
                    "unit_price": net, "vat_rate": rate}], supply_type=supply)
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 400
    due = (date.today() + _timedelta(days=term)).strftime("%d.%m.%Y")
    subject, body = _email_texts(cust["name"], inv["number"], _eur(inv["gross"]), due)
    block = {"type": "invoice_created", "number": inv["number"],
             "customer_name": cust["name"], "amount": _eur(inv["gross"]),
             "due": due,
             "pdf": url_for("invoice_pdf", iid=inv["id"]),
             "send_subject": subject,
             "send_body": body,
             "link": {"label": "Rechnung ansehen",
                      "href": url_for("invoices") + f"?dir=out&year={inv['year']}"}}
    CHAT_MESSAGES.append({"role": "assistant",
                          "content": f"Rechnung {inv['number']} für {cust['name']} erstellt.",
                          "_blocks": [block]})
    return jsonify(messages=_chat_display(), number=inv["number"])


@app.route("/chat/api/invoice/send", methods=["POST"])
def chat_api_invoice_send():
    """Rechnung aus dem Chat per E-Mail versenden (PDF im Anhang)."""
    data = request.get_json(silent=True) or {}
    conn = core.db()
    number = (data.get("number") or "").strip()
    inv = core.get_invoice_by_number(conn, number) if number else None
    if not inv:
        return jsonify(error="Rechnung nicht gefunden."), 400
    try:
        core.send_invoice_email(
            conn, inv["id"], (data.get("to") or "").strip(),
            (data.get("subject") or "").strip(),
            data.get("body") or "",
            cc=(data.get("cc") or "").strip() or None,
            copy_self=bool(data.get("copy_self")))
    except Exception as e:  # noqa
        return jsonify(error=str(e)), 400
    # Empfänger am Kunden merken (fürs nächste Mal)
    if inv["customer_id"] and (data.get("to") or "").strip():
        core.set_customer_email(conn, inv["customer_id"], data.get("to").strip())
    block = {"type": "invoice_sent", "number": inv["number"],
             "customer_name": inv["customer_name"], "to": (data.get("to") or "").strip(),
             "link": {"label": "Zahlungen öffnen", "href": url_for("zahlungen")}}
    CHAT_MESSAGES.append({"role": "assistant",
                          "content": f"Rechnung {inv['number']} versendet.",
                          "_blocks": [block]})
    return jsonify(messages=_chat_display())


@app.route("/chat/api/invoice/compose")
def chat_api_invoice_compose():
    """Vorschau- und Vorlagendaten für das Rechnung-senden-Modal."""
    conn = core.db()
    cfg = core.load_config()
    number = (request.args.get("number") or "").strip()
    inv = core.get_invoice_by_number(conn, number) if number else None
    if not inv:
        return jsonify(error="Rechnung nicht gefunden."), 404
    items = core.get_invoice_items(conn, inv["id"])
    cust = core.get_customer(conn, inv["customer_id"]) if inv["customer_id"] else None
    reverse = inv["kind"] in ("eu", "third")
    vat_amt = float(inv["gross"]) - float(inv["net"])
    rate = 0
    for it in items:
        if it["vat_rate"]:
            rate = int(it["vat_rate"]); break
    status = "Bezahlt" if inv["paid_date"] else ("Versendet" if inv["sent_at"] else "Entwurf")
    due = inv["due_date"]
    try:
        due = date.fromisoformat(inv["due_date"]).strftime("%d.%m.%Y")
    except Exception:  # noqa
        pass
    subject, body = _email_texts(inv["customer_name"], inv["number"], _eur(inv["gross"]), due)
    biz = cfg.get("business", {})
    return jsonify(
        number=inv["number"], status=status,
        from_name=biz.get("name", ""), from_city=biz.get("city", ""),
        to_name=inv["customer_name"], to_email=(cust and cust["email"]) or "",
        lines=[{"desc": it["description"], "amount": _eur(it["line_net"])} for it in items],
        net=_eur(inv["net"]), vat_amount=_eur(vat_amt),
        vat_label=("Reverse Charge" if reverse else f"USt {rate}%"),
        gross=_eur(inv["gross"]), reverse=reverse,
        subject=subject, body=body,
        pdf=url_for("invoice_pdf", iid=inv["id"]))


# ── Terminal: dauerhafte Shell-Session (überlebt Reload/Seitenwechsel) ────
_TERM = {"fd": None, "pid": None, "buf": bytearray(), "client": None,
         "lock": threading.Lock()}


def _term_alive() -> bool:
    if not _TERM["pid"]:
        return False
    try:
        os.kill(_TERM["pid"], 0)
        return True
    except OSError:
        return False


def _term_reader(fd, pid):
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            data = os.read(fd, 8192)
            if not data:
                break
        except OSError:
            break
        with _TERM["lock"]:
            _TERM["buf"].extend(data)
            if len(_TERM["buf"]) > 200_000:      # Scrollback begrenzen
                del _TERM["buf"][:-200_000]
            client = _TERM["client"]
        if client is not None:
            try:
                client.send(data)
            except Exception:
                pass
    with _TERM["lock"]:
        if _TERM["pid"] == pid:
            _TERM["fd"], _TERM["pid"] = None, None


def _ensure_term():
    if _TERM["fd"] is not None and _term_alive():
        return
    pid, fd = pty.fork()
    if pid == 0:  # Kind -> Login-Shell im Projektordner
        os.environ["TERM"] = "xterm-256color"
        try:
            os.chdir(str(core.ROOT))
        except Exception:
            pass
        shell = os.environ.get("SHELL", "/bin/zsh")
        os.execvp(shell, [shell, "-l"])
        os._exit(1)
    with _TERM["lock"]:
        _TERM["fd"], _TERM["pid"], _TERM["buf"] = fd, pid, bytearray()
    threading.Thread(target=_term_reader, args=(fd, pid), daemon=True).start()


@app.route("/terminal")
def terminal():
    if not TERMINAL_MOEGLICH:
        abort(404)
    return render_template("terminal.html")


@app.route("/terminal/kill", methods=["POST"])
def terminal_kill():
    if not TERMINAL_MOEGLICH:
        abort(404)
    pid = _TERM["pid"]
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return ("", 204)


@app.route("/healthz")
def healthz():
    return "ok"


# ── Claude-Code-Agent (Chat-Bubble-Widget, backend = claude) ─────────────
@app.route("/agent/api")
def agent_api():
    return jsonify({"messages": AGENT["display"], "available": assistant.claude_available()})


@app.route("/agent/send", methods=["POST"])
def agent_send():
    msg = (request.get_json(silent=True) or {}).get("message", "").strip()
    if not msg:
        return jsonify({"messages": AGENT["display"]})
    AGENT["display"].append({"role": "user", "text": msg})
    res = assistant.run_claude(msg, AGENT["session"])
    if res.get("session_id"):
        AGENT["session"] = res["session_id"]
    AGENT["display"].append({"role": "assistant", "text": res.get("text") or "(keine Antwort)"})
    return jsonify({"messages": AGENT["display"]})


@app.route("/agent/reset", methods=["POST"])
def agent_reset():
    AGENT["session"] = None
    AGENT["display"] = []
    return jsonify({"messages": []})


@app.route("/agent/upload", methods=["POST"])
def agent_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"messages": AGENT["display"]}), 400
    note = (request.form.get("message") or "").strip()
    from datetime import datetime
    core.DOC_DIR.mkdir(exist_ok=True)
    safe = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip() or "upload"
    dest = core.DOC_DIR / f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{safe}"
    dest.write_bytes(file.read())
    AGENT["display"].append({"role": "user",
                             "text": f"📎 {file.filename}" + (f" · {note}" if note else "")})
    msg = (f"Ich habe eine Datei hochgeladen: {dest} (Original: {file.filename}). "
           + (note + " " if note else "")
           + "Bitte lies sie und ordne/verbuche sie passend ein (nutze ./bb oder die App-Logik).")
    res = assistant.run_claude(msg, AGENT["session"])
    if res.get("session_id"):
        AGENT["session"] = res["session_id"]
    AGENT["display"].append({"role": "assistant", "text": res.get("text") or "(keine Antwort)"})
    return jsonify({"messages": AGENT["display"]})


def _adopt_term_from_env():
    """Nach einem Neustart eine übergebene, noch laufende Shell übernehmen."""
    if not TERMINAL_MOEGLICH:
        return
    fds = os.environ.pop("BB_TERM_FD", None)
    pids = os.environ.pop("BB_TERM_PID", None)
    if not fds or not pids:
        return
    fd, pid = int(fds), int(pids)
    try:
        os.kill(pid, 0)  # lebt die Shell noch?
    except OSError:
        try:
            os.close(fd)
        except Exception:
            pass
        return
    with _TERM["lock"]:
        _TERM["fd"], _TERM["pid"], _TERM["buf"] = fd, pid, bytearray()
    threading.Thread(target=_term_reader, args=(fd, pid), daemon=True).start()


@app.route("/restart", methods=["POST"])
def restart():
    """Server manuell neu starten (übernimmt Code-Änderungen). Nur auf Knopfdruck.
    Eine laufende Terminal-Shell wird an den neuen Prozess übergeben und überlebt."""
    if not os.environ.get("BB_SUPERVISED"):
        return ("Neustart nur verfügbar, wenn per ./bb-web gestartet.", 409)
    # Prozess mit Code 42 beenden -> der bb-web-Supervisor startet frisch.
    threading.Timer(0.3, lambda: os._exit(42)).start()
    return ("", 204)


@sock.route("/pty")
def pty_ws(ws):
    if not TERMINAL_MOEGLICH:
        return
    """Verbindet xterm.js mit der dauerhaften Shell; liefert den Scrollback nach."""
    _ensure_term()
    with _TERM["lock"]:
        fd = _TERM["fd"]
        snapshot = bytes(_TERM["buf"])
        _TERM["client"] = ws
    if snapshot:
        try:
            ws.send(snapshot)
        except Exception:
            pass
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, str) and msg.startswith("\x01"):
                try:
                    d = _json.loads(msg[1:])
                    winsize = struct.pack("HHHH", int(d["rows"]), int(d["cols"]), 0, 0)
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                    continue
                except Exception:
                    pass
            try:
                os.write(fd, msg.encode() if isinstance(msg, str) else msg)
            except OSError:
                break
    except Exception:
        pass
    finally:
        with _TERM["lock"]:
            if _TERM["client"] is ws:
                _TERM["client"] = None
        # Shell NICHT killen -> Session bleibt für den nächsten Besuch erhalten


def _open_browser(url):
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    # Vor allem anderen: eine Altinstallation als erstes Profil uebernehmen.
    # Das verschiebt Ordner und muss passieren, BEVOR irgendwo eine Verbindung
    # zur Datenbank offen ist – Windows verschiebt sonst nichts.
    core.migrate_wurzel_zu_profil()
    core.db()  # DB/Ordner sicherstellen
    _adopt_term_from_env()  # nach Neustart: laufende Shell übernehmen
    port = 8765
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Buchhaltung läuft auf  {url}\n  (Beenden mit Strg+C)\n")
    if not os.environ.pop("BB_NO_BROWSER", ""):
        _open_browser(url)
    app.run(port=port, debug=False, threaded=True)
