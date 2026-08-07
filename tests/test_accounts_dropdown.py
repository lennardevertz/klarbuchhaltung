"""Regressions-Schutz: die Konten im Kategorien-Tab müssen exakt denen in den
Dropdowns (Buchungssatz-Auswahl) entsprechen – sonst kann man ein Konto sehen,
aber nicht buchen (oder umgekehrt)."""
import re
from contextlib import contextmanager

from flask import template_rendered

import app as flask_app
import core


@contextmanager
def _captured_context(app_):
    rec = []

    def record(sender, template, context, **extra):
        rec.append((template.name, context))

    template_rendered.connect(record, app_, weak=False)
    try:
        yield rec
    finally:
        template_rendered.disconnect(record, app_)


def _nums(accounts) -> set:
    """Buchungszahlen (SKR-Kontonummern) aus 'NNNN - Bezeichnung'-Einträgen."""
    out = set()
    for a in accounts or []:
        m = re.match(r"\s*(\d{3,4})", a or "")
        if m:
            out.add(m.group(1))
    return out


def test_kategorien_konten_stimmen_mit_dropdown_ueberein(sandbox):
    """Union aus Standard- + Eigenen Konten (Kategorien-Tab) == Dropdown-Kontenliste."""
    core.add_custom_account("1800 - Privatentnahmen allgemein")
    with _captured_context(flask_app.app) as rec:
        r = flask_app.app.test_client().get("/categories")
        assert r.status_code == 200
    ctx = next(c for name, c in rec if name == "categories.html")

    kategorien = _nums(ctx["accounts"]) | _nums(ctx["custom"])   # was der Tab anzeigt
    dropdown = _nums(ctx["expense_accounts"])                    # was die Dropdowns anbieten
    fehlt_im_dropdown = kategorien - dropdown
    fehlt_in_kategorien = dropdown - kategorien
    assert kategorien == dropdown, (
        f"Kontenlisten weichen ab – nur im Kategorien-Tab: {sorted(fehlt_im_dropdown)}; "
        f"nur im Dropdown: {sorted(fehlt_in_kategorien)}")
    assert "1800" in dropdown, "neu angelegtes Konto muss im Dropdown erscheinen"


def test_neutrales_privatkonto_wird_nicht_als_aufwand_gezaehlt(sandbox):
    """1800 (Bestandskonto) darf den EÜR-Gewinn nicht als Betriebsausgabe mindern."""
    assert core.is_neutral_account("1800 - Privatentnahmen allgemein")
