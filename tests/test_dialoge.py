"""Keine nativen Browser-Dialoge.

In der Desktop-App rendert WebKit confirm()/alert() als Systemfenster mit
Python-Icon. Das sieht falsch aus und blockiert obendrein jede Automatisierung,
weil ein offener Modaldialog alle weiteren Ereignisse anhält.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
# confirm( / alert( / prompt( ohne vorangestelltes "klar" oder "."
NATIV = re.compile(r"(?<![\w.])(?:window\.)?(confirm|alert|prompt)\s*\(")


def _js_zeilen(pfad: Path):
    """Zeilen ohne Jinja- und HTML-Kommentare (auch mehrzeilige)."""
    text = pfad.read_text(encoding="utf-8")
    # Kommentare durch gleich viele Zeilenumbrüche ersetzen, damit die
    # Zeilennummern in der Fehlermeldung stimmen bleiben.
    text = re.sub(r"\{#.*?#\}|<!--.*?-->",
                  lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    for nr, zeile in enumerate(text.splitlines(), 1):
        yield nr, re.sub(r"//.*$", "", zeile)


@pytest.mark.parametrize("vorlage", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_keine_nativen_dialoge(vorlage):
    treffer = [f"{vorlage.name}:{nr}: {z.strip()[:90]}"
               for nr, z in _js_zeilen(vorlage) if NATIV.search(z)]
    assert not treffer, "Native Dialoge gefunden:\n" + "\n".join(treffer)


def test_ersatz_wird_ueberall_eingebunden():
    """Jede Vorlage, die klarConfirm/klarAlert nutzt, muss den Dialog haben."""
    quelle = (TEMPLATES / "_dialog.html").read_text(encoding="utf-8")
    assert "window.klarConfirm" in quelle and "window.klarAlert" in quelle

    basen = {"base.html", "base_klar.html"}
    for vorlage in TEMPLATES.glob("*.html"):
        text = vorlage.read_text(encoding="utf-8")
        if not re.search(r"klar(Confirm|Alert|ConfirmSubmit)\s*\(", text):
            continue
        if vorlage.name == "_dialog.html":
            continue
        hat_direkt = '{% include "_dialog.html" %}' in text
        erbt = any(f'extends "{b}"' in text for b in basen)
        assert hat_direkt or erbt, f"{vorlage.name} nutzt klarConfirm, bindet den Dialog aber nicht ein"


def test_dialog_liegt_in_der_top_layer():
    """Als <dialog> + showModal(), sonst läge er unter anderen Modalen."""
    quelle = (TEMPLATES / "_dialog.html").read_text(encoding="utf-8")
    assert "<dialog" in quelle and "showModal()" in quelle


def test_escape_gilt_als_abbruch():
    quelle = (TEMPLATES / "_dialog.html").read_text(encoding="utf-8")
    assert "'cancel'" in quelle, "Escape muss als Abbruch aufgelöst werden"
