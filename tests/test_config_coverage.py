"""Konfiguration + Meta-Test: jede öffentliche core-Funktion wird irgendwo getestet."""
import ast
import inspect
from pathlib import Path

import pytest

import core

ROOT = Path(__file__).resolve().parent.parent


# ── load_config ──────────────────────────────────────────────────────────
def test_load_config(sandbox):
    cfg = core.load_config()
    assert cfg["invoice"]["default_vat_rate"] == 19
    assert cfg["tax"]["taxation"] == "ist"


def test_load_config_ohne_datei_wirft(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "fehlt.toml")
    with pytest.raises(FileNotFoundError):
        core.load_config()


def test_save_statement_legt_datei_ab(sandbox):
    dest = core.save_statement(b"a;b;c", "revolut export.csv")
    assert dest.exists() and dest.parent == core.STATEMENT_DIR
    assert dest.read_bytes() == b"a;b;c"


def test_now_format(sandbox):
    now = core._now()
    assert "T" in now and len(now) == 19


# ── Meta-Abdeckung ───────────────────────────────────────────────────────
def _public_core_functions():
    src = (ROOT / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    return [n.name for n in tree.body
            if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]


def _all_test_source():
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in (ROOT / "tests").glob("test_*.py"))


# Funktionen, die nicht direkt per Namen aufgerufen, sondern nur indirekt oder gar
# nicht sinnvoll unit-testbar sind (I/O-lastig, in anderen Tests mit abgedeckt).
_INDIRECT = {
    "db",              # in jeder conn-Fixture benutzt
    "init_schema",     # via db()
    "render_invoice_pdf",   # via create_invoice(render=True) / regenerate (pdf-Tests)
    "send_invoice_email",   # SMTP – in test_email separat gemockt
}


def test_jede_public_core_funktion_wird_getestet():
    funcs = _public_core_functions()
    src = _all_test_source()
    missing = [f for f in funcs
               if f not in _INDIRECT and f"core.{f}" not in src and f"\"{f}\"" not in src]
    assert not missing, f"Ohne Test: {missing}"


def test_indirekte_funktionen_existieren_noch():
    """Schützt die _INDIRECT-Liste vor Karteileichen."""
    for name in _INDIRECT:
        assert hasattr(core, name), f"{name} existiert nicht mehr in core"


def test_bankverbindung_aus_settings_ueberschreibt_config(sandbox):
    core.update_settings(bank_holder="Neuer Inhaber", bank_iban="DE12345678", bank_bic="NEWBICXX")
    cfg = core.load_config()
    assert cfg["bank"]["holder"] == "Neuer Inhaber"
    assert cfg["bank"]["iban"] == "DE12345678"
    assert cfg["bank"]["bic"] == "NEWBICXX"
    # leere Settings -> config.toml bleibt maßgeblich
    core.update_settings(bank_holder="", bank_iban="", bank_bic="")
    assert core.load_config()["bank"]["holder"] == "Test Owner"


def test_steuer_stammdaten_aus_settings_ueberschreibt_config(sandbox):
    core.update_settings(business_vat_id="DE999999999", business_tax_number="12/345/67890")
    cfg = core.load_config()
    assert cfg["business"]["vat_id"] == "DE999999999"
    assert cfg["business"]["tax_number"] == "12/345/67890"
    core.update_settings(business_vat_id="", business_tax_number="")   # leer -> config bleibt
    assert core.load_config()["business"]["vat_id"] == "DE123456789"   # aus CONFIG_TOML
