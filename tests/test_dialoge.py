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


# ── Chat-Karten ──────────────────────────────────────────────────────────
def test_vollseite_zeichnet_dieselben_karten_wie_das_widget():
    """Die Vollseite /chat zeigte nur Text; Diagramme fielen still unter den Tisch.

    Beide Ansichten müssen dieselbe Funktion benutzen, sonst laufen sie
    wieder auseinander.
    """
    basis = (TEMPLATES / "base_klar.html").read_text(encoding="utf-8")
    seite = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    assert "window.klarRenderBlock=renderBlock" in basis, "Widget gibt den Zeichner frei"
    assert "klarRenderBlock" in seite, "Vollseite benutzt ihn"
    assert "m.blocks" in seite, "Vollseite liest die Karten aus der Antwort"


def test_karten_koennen_nicht_aus_dem_widget_laufen():
    """Flexbox: ohne min-width:0 greift text-overflow nie – genau das ist passiert."""
    basis = (TEMPLATES / "base_klar.html").read_text(encoding="utf-8")
    zeilen = [z for z in basis.splitlines()
              if "display:flex" in z and "flex:1" in z and "min-width:0" not in z
              and "btn" not in z]
    assert not zeilen, "Flex-Zeile mit flex:1-Kind ohne min-width:0:\n" + "\n".join(
        z.strip()[:120] for z in zeilen)


def test_vollseite_ist_aus_dem_widget_erreichbar():
    """Ohne Verweis war /chat nur über die Befehlspalette auffindbar."""
    basis = (TEMPLATES / "base_klar.html").read_text(encoding="utf-8")
    kopf = basis[basis.index('class="assistant__head"'):]
    kopf = kopf[:kopf.index('id="ask-warn"')]      # bis zum Ende des Kopfbereichs
    assert "url_for('chat')" in kopf, "Widget-Kopf verlinkt die Vollseite"
