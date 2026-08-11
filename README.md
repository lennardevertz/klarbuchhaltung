# Buchhaltung

Lokale Buchhaltung für Freelancer und kleine Betriebe — statt Cloud-Abo.

### Für Agenten

Diesen Text an Claude Code, Codex o. Ä. schicken, dann läuft die App:

```text
Installiere https://github.com/lennardevertz/klarbuchhaltung und starte es.
Voraussetzung ist Python 3.11 oder neuer.

1. Repo klonen und hineinwechseln.
2. Virtuelle Umgebung anlegen: python3 -m venv .venv
3. Abhängigkeiten installieren: .venv/bin/pip install -r requirements.txt
4. Web-App starten: ./bb-web  (läuft auf http://127.0.0.1:8765, öffnet den Browser)
   Alternativ als eigenes Fenster: .venv/bin/python desktop.py

Danach führt die App selbst durch die Einrichtung (Firma, Adresse, USt-IdNr.,
Bank, Steuervorgaben) — die musst du nicht ausfüllen, sag mir nur Bescheid,
wenn die Seite läuft. Es laufen keine Daten irgendwo hin: alles bleibt in einer
lokalen SQLite-Datei, und der Server hört ausschließlich auf 127.0.0.1.
```

Ausgelegt auf deutsche **Regelbesteuerung**, **EÜR** und **USt-Voranmeldung über
ELSTER**, inklusive **Reverse Charge** für EU-Kunden und Drittland.

**Alle Daten bleiben auf deinem Rechner.** Kein Konto, kein Server, keine
laufenden Kosten — eine SQLite-Datei und deine Belege als PDF.

<p align="center">
  <img src="packaging/Buchhaltung.png" width="96" alt="">
</p>

![Übersicht](docs/screenshots/uebersicht.jpg)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/zahlungen.jpg" alt="Zahlungen"></td>
<td width="50%"><img src="docs/screenshots/rechnungen.jpg" alt="Rechnungen"></td>
</tr>
<tr>
<td>Jede Buchung mit Konto und Steuerbehandlung — 19 % USt, steuerfrei, § 13b</td>
<td>Rechnungen nach Jahr und Richtung, PDF direkt daneben</td>
</tr>
</table>

<p align="center">
  <img src="docs/screenshots/berichte.jpg" width="85%" alt="Berichte">
</p>
<p align="center"><sub>EÜR, USt-Voranmeldung, Zusammenfassende Meldung, DATEV-Export</sub></p>

---

## Installieren

Fertige Pakete liegen unter [Releases](../../releases).

| System | Datei | Start |
|--------|-------|-------|
| macOS | `Buchhaltung-*-macOS.dmg` | Öffnen, App nach *Programme* ziehen |
| Windows | `Buchhaltung-*-Windows.zip` | Entpacken, `Buchhaltung.exe` starten |
| Linux | `Buchhaltung-*-Linux.tar.gz` | Entpacken, `Buchhaltung` starten |

### Beim ersten Start kommt eine Warnung

Die Pakete sind **nicht signiert** — dafür bräuchte es kostenpflichtige
Zertifikate von Apple bzw. einer Zertifizierungsstelle. Das ist normal für
freie Software, sieht beim ersten Start aber unschön aus:

- **macOS:** „…kann nicht geöffnet werden, da der Entwickler nicht verifiziert
  werden kann." → Rechtsklick auf die App → **Öffnen** → im Dialog nochmals
  **Öffnen**. Nur einmal nötig.
- **Windows:** SmartScreen meldet einen unbekannten Herausgeber. →
  **Weitere Informationen** → **Trotzdem ausführen**.
- **Linux:** braucht WebKitGTK (`sudo apt install gir1.2-webkit2-4.1`).

Wem das nicht geheuer ist: aus dem Quellcode betreiben, siehe unten.

---

## Selbst betreiben (aus dem Quellcode)

Voraussetzung ist Python 3.11 oder neuer.

```bash
git clone https://github.com/lennardevertz/klarbuchhaltung.git
cd klarbuchhaltung
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python desktop.py     # eigenes Fenster
# oder
./bb-web                        # im Browser auf http://127.0.0.1:8765
```

Beim ersten Start führt die App durch die Einrichtung: Firma, Adresse,
USt-IdNr., Bankverbindung sowie die Rechnungs- und Steuervorgaben. Daraus
entsteht eine `config.toml`, die du später jederzeit von Hand oder über die
Einstellungen ändern kannst.

Der Server hört ausschließlich auf `127.0.0.1` — er ist von außen nicht
erreichbar. Wer ihn dennoch im Heimnetz erreichbar machen will, sollte einen
Reverse Proxy mit Zugriffsschutz davorsetzen; die App bringt **keine**
Anmeldung mit.

### Selbst bauen

```bash
.venv/bin/pip install pyinstaller pillow
.venv/bin/python packaging/make_icon.py
./packaging/build.sh                     # Ergebnis in dist/release/
```

---

## Profile (mehrere Buchhaltungen)

Beim Start erscheint eine Profilauswahl. Jedes Profil ist eine **eigenständige
Buchhaltung** mit eigener Konfiguration, Datenbank und Belegablage — zwischen
Profilen wird nichts geteilt. Praktisch für mehrere Firmen oder um
Vorjahre getrennt zu halten.

- Gibt es nur ein Profil, springt die App direkt hinein.
- Wechseln über die Kachel unten links in der Seitenleiste.
- „Aus der Liste nehmen" löscht **nichts** — die Daten bleiben liegen.

### Wo die Daten liegen

| Betrieb | Ort |
|---------|-----|
| Installierte App (macOS) | `~/Library/Application Support/Buchhaltung/` |
| Installierte App (Windows) | `%APPDATA%\Buchhaltung\` |
| Installierte App (Linux) | `~/.local/share/buchhaltung/` |
| Aus dem Quellcode | im Projektordner |

Mit der Umgebungsvariable `BB_HOME` lässt sich ein beliebiger Ort erzwingen.
Darunter liegt je Profil `profiles/<name>/` mit `config.toml` und `data/`.

---

## Was die App kann

### Buchhaltung

- **Rechnungen** schreiben als PDF, zweisprachig DE/EN, mit fortlaufender
  Nummer je Monat. Inland mit USt, EU-Geschäftskunden mit Reverse Charge,
  Drittland nicht steuerbar — der Rechnungstyp folgt zwingend dem Kunden,
  nicht der Auswahl im Formular.
- **Zahlungen** erfassen, **Mahnwesen**, **wiederkehrende Rechnungen**
- **Ausgaben** mit Belegablage nach Jahr und Quartal, SKR03-Konten und
  **Privatanteil** (Telefon zu 80 % privat: nur 20 % zählen als Betriebsausgabe)
- **Berichte**: EÜR nach Zufluss-/Abflussprinzip, USt-Voranmeldung mit den
  ELSTER-Kennziffern 81/86/66/83/21/45, Zusammenfassende Meldung,
  USt-Jahresabschluss, SuSa-Liste, DATEV-/CSV-Export für den Steuerberater

### Banking

- **CSV-Import** des Bankexports. Die komplette Historie darf jedes Mal rein —
  Duplikate erkennt die App per Prüfsumme.
- **Automatischer Abgleich**: Eingänge werden offenen Rechnungen mit gleichem
  Betrag zugeordnet, die Rechnung gilt damit als bezahlt.
- **Regeln, die sich selbst lernen**: Sobald du eine Buchung zuordnest, merkt
  sich die App die Zuordnung je Beschreibung und schlägt sie beim nächsten Mal
  vor — bestätigen musst du weiterhin selbst.
![Banking](docs/screenshots/banking.jpg)

- **Nuri-/Wirex-Konto direkt angebunden** (optional): Karten- und
  Kontoumsätze, IBAN und SEPA laufen über den MCP-Server von Wirex. Die
  Anmeldung erfolgt per **Passkey**; das Sitzungs-Token gilt rund eine Stunde
  und wird nur im Arbeitsspeicher gehalten, nie auf die Platte geschrieben.

### Chat und KI — mit dem Anbieter deiner Wahl

Der eingebaute Chat spricht **jede OpenAI-kompatible Schnittstelle**. Du
trägst in den Einstellungen Base-URL, Modell und Schlüssel ein; der Schlüssel
bleibt lokal in deiner Profilablage.

| Anbieter | Base-URL |
|---|---|
| **Lokal (Ollama)** | `http://localhost:11434/v1` |
| **Lokal (LM Studio)** | `http://localhost:1234/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Anthropic (Claude) | `https://api.anthropic.com/v1` |

Mit Ollama oder LM Studio verlässt **kein Byte den Rechner** — dieselbe Zusage
wie beim Rest der App. Ohne eingetragenen Zugang bleibt der Chat einfach aus,
alles andere funktioniert unverändert.

![Chat](docs/screenshots/chat.jpg)

Antworten kommen nicht nur als Text: Zu Auswertungen zeichnet der Assistent
Karten — Ringdiagramm für Kategorien, Balken für Verläufe, Kennzahl mit
Trendlinie, oder gleich ein ausfüllbares Rechnungsformular.

Was der Chat kann: Rechnungen und Ausgaben anlegen, Zahlungen zuordnen,
Auswertungen abfragen — und **hochgeladene PDF-Belege auslesen**: Anbieter,
Betrag, Datum und ein passendes SKR03-Konto werden vorgeschlagen, gebucht wird
erst nach deiner Bestätigung.

> Der **Entwickler-Modus** (standardmäßig aus) gibt dem Modell zusätzlich
> Shell- und Dateizugriff. Praktisch zum Entwickeln — aber ein präpariertes
> PDF könnte darüber Anweisungen einschleusen. Lass ihn aus, wenn du Belege
> von Dritten verarbeitest.

### Weitere Zugänge

- **MCP-Server** für Claude Code, Codex oder Cursor: `./bb-mcp` — dieselben
  Werkzeuge wie im Chat, erzeugt aus derselben Quelle, also nie veraltet
- **CLI** für alles ohne Oberfläche: `./bb <befehl>`
- **Terminal** im Browser: eine echte Shell im Projektordner

Bei mehreren Profilen brauchen CLI und MCP die Angabe, welches gemeint ist:

```bash
BB_PROFILE=meine-firma ./bb report 2026
BB_PROFILE=meine-firma ./bb-mcp
```

---

## Bedienung

### Buchung festsetzen

Jede offene Buchung im Reiter **Zahlungen** lässt sich festsetzen als
Rechnungszahlung, Betriebsausgabe (mit Konto, USt-Satz und optionalem
Privatanteil), Privatentnahme, Bankgebühr oder „ignorieren". An jede
festgesetzte Buchung kann ein Beleg.

Über das Suchfeld filtern (z. B. „Vodafone"), dann „alle auswählen" und in
einem Rutsch festsetzen.

### Kontenrahmen

Ausgaben bekommen ein Konto aus einem SKR03-orientierten Katalog. Der Katalog
steht in der `config.toml` unter `[bookkeeping] expense_accounts` im Format
`"Nr - Bezeichnung"` und lässt sich frei anpassen; die EÜR gruppiert danach.

### Rechnungen importieren (CSV)

Für Altbestände: Reiter **Import** (Vorlage dort herunterladbar) oder
`./bb import datei.csv` (mit `--no-pdf`, wenn die Original-PDFs schon
existieren). Eine Zeile je Position; mehrere Zeilen mit derselben `number`
ergeben eine Rechnung mit mehreren Positionen. Vorhandene Rechnungsnummern
werden übersprungen, nichts wird überschrieben.

### Wichtigste CLI-Befehle

| Befehl | Zweck |
|--------|-------|
| `./bb invoice` | Rechnung erstellen (interaktiv) |
| `./bb expense` | Ausgabe erfassen |
| `./bb pay invoice 2026-06-0001 --date 2026-07-20` | Zahlung buchen |
| `./bb report 2026` | EÜR fürs Jahr |
| `./bb vat 2026 3` | USt-Voranmeldung Q3 |
| `./bb zm 2026 3` | Zusammenfassende Meldung Q3 |

Rechnungsnummern laufen je Monat fortlaufend: `JJJJ-MM-NNNN`.

---

## Wie die Zahlen zustande kommen

- **EÜR** folgt dem **Zufluss-/Abflussprinzip** (§4 Abs. 3 EStG): gezählt wird nach
  *Zahlungsdatum*, nicht Rechnungsdatum. Nur bezahlte Vorgänge fließen ein.
  Gewinn = Netto-Einnahmen − Netto-Ausgaben (die USt ist für dich durchlaufend).
- **USt-Voranmeldung** bei `taxation = "ist"`: Umsätze zählen im Zeitraum des
  *Zahlungseingangs*, Vorsteuer im Zeitraum der *Zahlung* der Ausgabe. Ausgegebene
  ELSTER-Kennziffern: **81** (Umsatz 19 %), **86** (Umsatz 7 %), **66** (Vorsteuer),
  **83** (Zahllast/Erstattung), **21** (EU, innergem. sonstige Leistungen) und
  **45** (Drittland, nicht steuerbar).
- **Die drei Rechnungstypen im Vergleich:**

  | Typ | USt | USt-VA | ZM |
  |-----|-----|--------|-----|
  | Inland | 19 / 7 % | Kz 81 / 86 | nein |
  | EU-Geschäftskunde | 0 %, Reverse Charge | Kz 21 | **ja** |
  | Drittland (UK/US/CH …) | 0 %, nicht steuerbar | Kz 45 | nein |

- **ZM (`./bb zm`):** listet die EU-Umsätze eines Quartals je Kunde (USt-IdNr. + Netto).
  **Wichtig:** Die ZM richtet sich nach dem **Rechnungs-/Leistungsdatum** (nicht nach der
  Zahlung wie die Ist-USt-VA) – ein EU-Umsatz kann daher in ZM und USt-VA in verschiedenen
  Quartalen landen. Abgabe über *Mein ELSTER* bis zum 25. des Folgemonats. Das Eintippen
  in ELSTER erledigst du selbst; das Tool liefert die fertige Aufstellung.

---

## Datensicherung & Recht

- **Backup:** Sichere regelmäßig den Profilordner (`profiles/`, Ort siehe oben) —
  darin liegen Datenbank, Rechnungs-PDFs, Belege und die Konfiguration. Er ist
  bewusst nicht im Git.
- **Aufbewahrung:** Rechnungen und Buchungsbelege müssen aufbewahrt werden
  (seit 2025 i. d. R. **8 Jahre**, vorher 10). PDFs unverändert lassen (GoBD).
- **E-Rechnung:** Seit 01.01.2025 müssen Unternehmen E-Rechnungen (XRechnung/ZUGFeRD)
  **empfangen** können; die Pflicht zum **Versand** an Geschäftskunden greift stufenweise
  ab 2027/2028. Dieses Tool erzeugt aktuell PDF-Rechnungen; ein ZUGFeRD-Export
  (PDF mit eingebettetem XML) lässt sich später ergänzen.

> **Hinweis:** Dies ist ein selbstgebautes Hilfswerkzeug, keine zertifizierte Software
> und keine Steuerberatung. Für die Richtigkeit deiner Steuererklärung bist du
> verantwortlich – im Zweifel Steuerberater fragen. Insbesondere die genaue
> Rechtsgrundlage beim Reverse Charge (§13b UStG vs. innergem. Leistung nach §3a UStG)
> solltest du für deinen Fall bestätigen lassen.

---

## Aufbau

```
core.py              gemeinsame Logik (Datenbank, PDF, Steuerberechnung)
app.py               Web-App (Flask)
desktop.py           Fenster-Hülle (pywebview), Einstiegspunkt der App
bookkeeping.py       CLI      · bb        Wrapper dafür
assistant.py         LLM-Werkzeuge für den Chat
mcp_server.py        MCP-Server · bb-mcp  Wrapper dafür
wirex.py             Anbindung an das Wirex-Konto
templates/           HTML-Seiten
static/              CSS und Schriften der Oberfläche
fonts/               eingebettete Schrift für die PDFs
packaging/           Icons, PyInstaller-Spec, Bauskript
tests/               Testsuite (läuft auf macOS, Windows und Linux)
profiles/            deine Buchhaltungen (nicht im Git)

site/                Landing-Page klarbuchhaltung.de (statisch, eigenständig)
worker.js            Cloudflare Worker: liefert site/ aus, leitet Nebendomains um
wrangler.jsonc       Deploy-Konfiguration dafür
```

---

## Landing-Page deployen

Die Seite unter [klarbuchhaltung.de](https://klarbuchhaltung.de) liegt in `site/`
und hat mit der App nichts zu tun — reines HTML/CSS, kein Build-Schritt, keine
externen Fonts oder CDNs. `site/tokens.css` und `site/fonts/` sind Kopien aus
`static/`, damit der Ordner allein lauffähig ist. Änderst du die Design-Tokens
der App, musst du sie hier von Hand nachziehen.

Lokal ansehen:

```bash
cd site && python3 -m http.server 8899     # http://127.0.0.1:8899
```

Veröffentlichen — **kein CI, das läuft von Hand**:

```bash
npx wrangler@4 deploy          # aus dem Repo-Root
npx wrangler@4 deploy --dry-run  # nur Konfiguration prüfen
```

Die Anmeldung kommt aus dem lokalen wrangler-OAuth-Login (`wrangler login`),
nicht aus einem Token im Repo. Ein `git push` deployt nichts.

### Domains

| Adresse | Verhalten |
|---|---|
| `klarbuchhaltung.de` | die Seite |
| `www.klarbuchhaltung.de` | 301 auf die Hauptdomain |
| `klar.levertz.com` | 301 auf die Hauptdomain, Pfad und Query bleiben |
| `*.workers.dev` | die Seite, bewusst ohne Weiterleitung (zum Testen) |

Die Weiterleitungen macht `worker.js`. Er läuft dank `assets.run_worker_first`
vor der Asset-Auslieferung — sonst wäre `index.html` schneller und die
Weiterleitung würde nie greifen. Neue Weiterleitung: Hostname in die Liste
`WEITERLEITEN` und als `custom_domain` in die `routes` von `wrangler.jsonc`.
Die Zone muss dafür in Cloudflare liegen, sonst bricht der Deploy ab.

Redirect-Logik ohne Deploy prüfen (`wrangler dev` taugt nicht dafür, dort sieht
der Worker immer `localhost`):

```bash
node --input-type=module -e '
import w from "./worker.js";
const env = { ASSETS: { fetch: () => new Response("SEITE") } };
for (const u of ["https://klar.levertz.com/x?a=1", "https://klarbuchhaltung.de/"]) {
  const r = await w.fetch(new Request(u), env);
  console.log(r.status, u, "->", r.headers.get("location") ?? "Seite");
}'
```

Legt Cloudflare beim Hinzufügen einer Zone A- oder CNAME-Records auf dem Apex
oder `www` an (passiert beim Import von einem anderen Anbieter), müssen die weg
— sonst kollidieren sie mit den Custom Domains und `wrangler deploy` bricht ab.

## Mitmachen

```bash
.venv/bin/python -m pytest        # rund 610 Tests, wenige Sekunden
```

Die Testsuite läuft bei jedem Push auf allen drei Systemen. Kein Test fasst
echte Daten an: sämtliche Pfade zeigen währenddessen in ein Wegwerf-Verzeichnis.

---

## Haftung

Selbstgebautes Hilfswerkzeug, keine zertifizierte Software und keine
Steuerberatung. Für die Richtigkeit deiner Steuererklärung bist du
verantwortlich — im Zweifel Steuerberater fragen. Das gilt besonders für die
Rechtsgrundlage beim Reverse Charge (§ 13b UStG vs. innergemeinschaftliche
Leistung nach § 3a UStG).

## Lizenz

MIT — siehe [LICENSE](LICENSE).
