"""Belegpfade: relativ in der DB, damit ein Ordner-/Profilwechsel sie nicht kappt."""
import core


# ── rel_pfad ─────────────────────────────────────────────────────────────
def test_rel_pfad_kuerzt_auf_den_datenordner(sandbox):
    """Immer Schrägstriche – sonst wäre ein Profil nicht portabel."""
    p = core.DOC_DIR / "eingang" / "2026" / "Q1" / "beleg.pdf"
    assert core.rel_pfad(p) == "documents/eingang/2026/Q1/beleg.pdf"
    assert "\\" not in core.rel_pfad(p)


def test_rel_pfad_laesst_fremde_pfade_absolut(sandbox, tmp_path):
    fremd = tmp_path / "woanders" / "beleg.pdf"
    assert core.rel_pfad(fremd) == str(fremd)


def test_rel_pfad_nimmt_auch_strings(sandbox):
    p = core.DOC_DIR / "ausgang" / "r.pdf"
    assert core.rel_pfad(str(p)) == "documents/ausgang/r.pdf"


# ── abs_pfad ─────────────────────────────────────────────────────────────
def test_abs_pfad_loest_relativ_gegen_den_datenordner(sandbox):
    assert core.abs_pfad("documents/eingang/b.pdf") == core.DATA_DIR / "documents/eingang/b.pdf"


def test_abs_pfad_laesst_alte_absolute_werte_gelten(sandbox, tmp_path):
    """Bestände von vor der Umstellung müssen weiter funktionieren."""
    alt = tmp_path / "alt" / "beleg.pdf"
    assert core.abs_pfad(str(alt)) == alt


def test_abs_pfad_bei_leer(sandbox):
    assert core.abs_pfad(None) is None and core.abs_pfad("") is None


def test_hin_und_zurueck(sandbox):
    p = core.DOC_DIR / "eingang" / "2026" / "Q3" / "x.pdf"
    assert core.abs_pfad(core.rel_pfad(p)) == p


# ── der eigentliche Zweck ────────────────────────────────────────────────
def test_beleg_ueberlebt_umzug_des_datenordners(sandbox, conn, tmp_path, monkeypatch):
    """Genau der Fall, der beim Repo-Umzug alle Belege gekappt hat."""
    quelle = tmp_path / "rechnung.pdf"
    quelle.write_bytes(b"%PDF-1.4 beleg")
    core.create_expense(conn, date_="2026-03-05", vendor="Telekom", description="Mobilfunk",
                        category="4920 - Telefon", gross="119", vat_rate=19,
                        paid_date=None, receipt_src=str(quelle))
    gespeichert = core.list_expenses(conn)[0]["receipt_path"]
    assert not gespeichert.startswith("/"), "in der DB steht ein relativer Pfad"
    assert core.abs_pfad(gespeichert).exists()

    # Datenordner umziehen – wie bei einem Profilwechsel oder verschobenem Repo.
    # Vorher schliessen: Windows verschiebt keinen Ordner mit offener Datei.
    conn.close()
    neu = tmp_path / "woanders"
    core.DATA_DIR.rename(neu)
    monkeypatch.setattr(core, "DATA_DIR", neu)

    assert core.abs_pfad(gespeichert).exists(), "Beleg bleibt auffindbar"
    assert core.abs_pfad(gespeichert).read_bytes() == b"%PDF-1.4 beleg"


def test_windows_bestand_mit_backslashes_wird_gelesen(sandbox):
    """Werte, die eine ältere Windows-Fassung geschrieben hat."""
    ziel = core.DOC_DIR / "eingang" / "b.pdf"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(b"x")
    assert core.abs_pfad(r"documents\eingang\b.pdf") == ziel
    assert core.abs_pfad(r"documents\eingang\b.pdf").exists()
