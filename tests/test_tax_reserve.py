"""Steuerrücklage: §32a-Einkommensteuer, ESt-Schätzung, USt+ESt-Rücklage, Termine."""
import core


# ── §32a EStG Tarif 2026 ─────────────────────────────────────────────────
def test_est_tarif_grundfreibetrag_2026():
    assert core.est_tarif(12348, 2026) == 0.0        # bis Grundfreibetrag steuerfrei
    assert core.est_tarif(5000, 2026) == 0.0


def test_est_tarif_proportionalzonen_2026():
    # 42 %-Zone: 0,42·x − 11.135,63
    assert core.est_tarif(100000, 2026) == int(0.42 * 100000 - 11135.63)   # 30864
    # 45 %-Zone: 0,45·x − 19.470,38
    assert core.est_tarif(300000, 2026) == int(0.45 * 300000 - 19470.38)   # 115529


def test_est_tarif_progressionszone_2026():
    # zvE 60.000 -> Zone 3: (173,10·z + 2.397)·z + 1.034,87, z=(zvE-17.799)/1e4
    assert core.est_tarif(60000, 2026) == 14233.0


def test_est_tarif_monoton_steigend():
    werte = [core.est_tarif(z, 2026) for z in (10000, 20000, 40000, 80000, 300000)]
    assert werte == sorted(werte) and werte[0] == 0.0


def test_est_tarif_splitting_ist_doppelter_halbtarif():
    assert core.est_tarif(200000, 2026, splitting=True) == 2 * core.est_tarif(100000, 2026)


# ── ESt-Schätzung aus Einstellungen ──────────────────────────────────────
def _profit_invoice(conn, cfg, customer, net):
    core.create_invoice(conn, cfg, customer=customer, kind="domestic",
                        issue_date="2026-05-01", service_from="2026-05-01", service_to=None,
                        items=[{"description": "X", "quantity": "1", "unit": "h",
                                "unit_price": str(net), "vat_rate": 19}],
                        paid_date="2026-05-01", render=False)


def test_income_tax_estimate_zieht_vorsorge_ab_und_splittet(conn, cfg, customer):
    _profit_invoice(conn, cfg, customer, 60000)
    core.update_settings(est_veranlagung="zusammen", est_krankenversicherung=10000,
                         est_weitere_einkuenfte=0, est_kirchensteuer="9", est_soli=False)
    r = core.income_tax_estimate(conn, cfg, 2026)
    assert r["splitting"] is True
    assert r["zve"] == r["gewinn"] + 0 - 10000 - 72          # Splitting-Pauschbetrag 72
    assert r["est"] == core.est_tarif(r["zve"], 2026, True)
    assert r["kirchensteuer"] == round(r["est"] * 0.09, 2)
    assert r["gesamt"] == round(r["est"] + r["soli"] + r["kirchensteuer"], 2)


def test_income_tax_estimate_ohne_gewinn_ist_null(conn, cfg):
    r = core.income_tax_estimate(conn, cfg, 2026)
    assert r["est"] == 0.0 and r["gesamt"] == 0.0


# ── Rücklage (USt laufendes Quartal + ESt laufendes Jahr) ─────────────────
def test_tax_reserve_setzt_sich_aus_ust_und_est_zusammen(conn, cfg):
    r = core.tax_reserve(conn, cfg)
    assert r["est"]["year"] == r["year"]
    assert r["gesamt"] == round(r["ust"] + r["est"]["gesamt"], 2)
    assert r["ust_label"].startswith("Q")


# ── Anstehende Termine ───────────────────────────────────────────────────
def test_upcoming_deadlines_liefert_ustva_und_est(conn, cfg):
    ups = core.upcoming_deadlines(conn, cfg)
    assert ups and ups == sorted(ups, key=lambda x: x["date"])
    kinds = {u["kind"] for u in ups}
    assert "ustva" in kinds and "est" in kinds
    for u in ups:
        assert u["day"].isdigit() and len(u["mon"]) == 3
        assert isinstance(u["amount"], float)
        assert "key" in u and "days_until" in u and "due_soon" in u and "acked" in u


def test_upcoming_nutzt_finanzamt_vorauszahlung_exakt(conn, cfg):
    core.update_settings(est_vorauszahlung=1200)
    est = next(u for u in core.upcoming_deadlines(conn, cfg) if u["kind"] == "est")
    assert est["amount"] == 1200.0 and est["estimate"] is False
    assert "Finanzamt" in est["sub"]


def test_upcoming_ohne_vorauszahlung_schaetzt(conn, cfg):
    est = next(u for u in core.upcoming_deadlines(conn, cfg) if u["kind"] == "est")
    assert est["estimate"] is True and "Schätzung" in est["sub"]


# ── Differenz Finanzamt vs. §32a ─────────────────────────────────────────
def test_income_tax_estimate_differenz(conn, cfg, customer):
    _profit_invoice(conn, cfg, customer, 60000)
    core.update_settings(est_vorauszahlung=500, est_krankenversicherung=0,
                         est_weitere_einkuenfte=0, est_kirchensteuer="0", est_soli=False)
    r = core.income_tax_estimate(conn, cfg, 2026)
    assert r["vorauszahlung_jahr"] == 2000.0                 # 4 × 500
    assert r["differenz"] == round(r["gesamt"] - 2000.0, 2)  # >0 Nachzahlung, <0 Erstattung


def test_income_tax_estimate_ohne_vorauszahlung_keine_differenz(conn, cfg):
    r = core.income_tax_estimate(conn, cfg, 2026)
    assert r["vorauszahlung_jahr"] is None and r["differenz"] is None


# ── Termine abhaken (rote Fälligkeit entfernen) ──────────────────────────
def test_ack_deadline_setzt_und_entfernt(sandbox):
    core.ack_deadline("ustva:2026-Q2")
    assert "ustva:2026-Q2" in core.load_settings()["tax_ack"]
    core.ack_deadline("ustva:2026-Q2", on=False)
    assert "ustva:2026-Q2" not in (core.load_settings().get("tax_ack") or [])


def test_upcoming_spiegelt_abgehakten_termin(conn, cfg):
    ust = next(u for u in core.upcoming_deadlines(conn, cfg) if u["kind"] == "ustva")
    assert ust["acked"] is False
    core.ack_deadline(ust["key"])
    ust2 = next(u for u in core.upcoming_deadlines(conn, cfg) if u["key"] == ust["key"])
    assert ust2["acked"] is True
