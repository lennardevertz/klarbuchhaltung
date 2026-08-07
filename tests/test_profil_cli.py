"""Profilwahl für CLI und MCP über BB_PROFILE."""
import pytest

import core


@pytest.fixture
def raum(sandbox, monkeypatch):
    monkeypatch.delenv("BB_PROFILE", raising=False)
    core.CONFIG_PATH.unlink()          # kein Altlayout
    return sandbox


def test_altlayout_wird_beim_ersten_aufruf_uebernommen(sandbox, monkeypatch):
    """CLI auf einer alten Installation: migriert und wählt das Profil."""
    monkeypatch.delenv("BB_PROFILE", raising=False)
    assert core.profil_aus_umgebung() == "testfirma"
    assert core.DATA_DIR == core.PROFILES_DIR / "testfirma" / "data"


def test_ganz_ohne_alles_bleibt_ungebunden(sandbox, monkeypatch):
    monkeypatch.delenv("BB_PROFILE", raising=False)
    core.CONFIG_PATH.unlink()
    assert core.profil_aus_umgebung() is None


def test_einziges_profil_wird_genommen(raum):
    core.create_profile("Einzig")
    assert core.profil_aus_umgebung() == "einzig"
    assert core.AKTIVES_PROFIL == "einzig"


def test_mehrere_profile_ohne_variable_brechen_ab(raum):
    core.create_profile("Eins")
    core.create_profile("Zwei")
    with pytest.raises(SystemExit, match="BB_PROFILE"):
        core.profil_aus_umgebung()


def test_bb_profile_waehlt_aus(raum, monkeypatch):
    core.create_profile("Eins")
    core.create_profile("Zwei")
    monkeypatch.setenv("BB_PROFILE", "zwei")
    assert core.profil_aus_umgebung() == "zwei"
    assert core.DATA_DIR == core.PROFILES_DIR / "zwei" / "data"


def test_unbekanntes_bb_profile_bricht_ab(raum, monkeypatch):
    core.create_profile("Eins")
    monkeypatch.setenv("BB_PROFILE", "gibtsnicht")
    with pytest.raises(SystemExit, match="Unbekanntes Profil"):
        core.profil_aus_umgebung()


def test_leeres_bb_profile_wird_ignoriert(raum, monkeypatch):
    core.create_profile("Einzig")
    monkeypatch.setenv("BB_PROFILE", "   ")
    assert core.profil_aus_umgebung() == "einzig"


def test_cli_main_bindet_das_profil(raum, monkeypatch, capsys):
    """`bb customers list` muss im gewählten Profil arbeiten."""
    import bookkeeping
    p = core.create_profile("Eins")
    core.use_profile(p["slug"])
    core.write_config(business={"name": "Eins", "owner": "E", "address_lines": ["A"],
                                "vat_id": "", "tax_number": "", "email": "", "phone": ""},
                      bank={"holder": "E", "iban": "", "bic": ""},
                      invoice={"currency": "EUR", "default_vat_rate": 19,
                               "payment_terms_days": 14, "language": "de"},
                      tax={"taxation": "ist", "va_period": "quarter", "small_business": False})
    core.create_customer(core.db(), "Kunde im Profil", "Berlin", None)
    core.use_profile(None)             # Bindung lösen, main muss selbst wählen

    monkeypatch.setenv("BB_PROFILE", "eins")
    bookkeeping.main(["customers"])
    assert "Kunde im Profil" in capsys.readouterr().out
