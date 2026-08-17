# Paketquellen

Vier Kanäle, drei davon einreichfertig in diesem Ordner. Sie bringen zweierlei:
Installation mit einem Befehl — und Rückverweise auf klarbuchhaltung.de aus
Repositories, die selbst gut ranken und von Antwortmaschinen gelesen werden.

Alle Prüfsummen beziehen sich auf **v1.0.1**. Bei jedem Release müssen
`version`/`pkgver` und die SHA256-Werte mitwandern; die Werte stehen im
Release (`gh release view v1.0.1 --json assets`).

## winget (Windows) — `winget/`

Vier Manifest-Dateien, `InstallerType: zip` mit `NestedInstallerType: portable`,
weil das Release keinen Installer enthält, sondern den PyInstaller-Ordner.

```powershell
winget validate --manifest packaging\winget
winget install --manifest packaging\winget      # lokaler Testlauf
```

Einreichen: Fork von `microsoft/winget-pkgs`, Dateien nach
`manifests/l/LennardEvertz/Klar/1.0.1/`, PR. Die Pipeline prüft automatisch,
Rückmeldung meist innerhalb weniger Tage. Keine Bekanntheitsschwelle.

**Grenze:** Eine Portable-Installation legt einen Shim in den PATH, aber keinen
Startmenü-Eintrag. Wer den will, braucht später einen echten Installer
(Inno Setup oder NSIS) und ein Manifest mit `InstallerType: inno`.

## Homebrew (macOS) — `homebrew/Casks/klar.rb`

Der Haupttap `homebrew/cask` verlangt Bekanntheit — 75 Sterne oder 30 Forks
bzw. Watcher. Das Repo hat aktuell 1 Stern, ein PR dorthin würde geschlossen.
Deshalb zunächst ein eigener Tap:

```bash
gh repo create lennardevertz/homebrew-klar --public \
  --description "Homebrew-Tap für Klar"
# Casks/klar.rb aus diesem Ordner hineinlegen, committen, pushen
brew install --cask lennardevertz/klar/klar
```

Die Installationszeile gehört danach in README und auf die Landing-Page.
Sobald die Schwelle erreicht ist, wandert dieselbe Datei per PR nach
`Homebrew/homebrew-cask`; der Tap kann parallel bestehen bleiben.

**Grenze:** Die App ist weder signiert noch notarisiert — der Cask erklärt das
in `caveats` samt `xattr`-Befehl. Ein Apple-Developer-Konto (99 $/Jahr) würde
das lösen und wäre auch für den Haupttap der sauberere Weg.

## AUR (Arch Linux) — `aur/`

`PKGBUILD` und `klarbuchhaltung.desktop`, gebaut aus dem Linux-Tarball des
Releases. Kein Review-Prozess: Wer ein AUR-Konto mit hinterlegtem SSH-Schlüssel
hat, veröffentlicht sofort.

```bash
makepkg -si                       # lokal prüfen
namcap *.pkg.tar.zst
git clone ssh://aur@aur.archlinux.org/klarbuchhaltung-bin.git
cp PKGBUILD klarbuchhaltung.desktop klarbuchhaltung-bin/
cd klarbuchhaltung-bin
makepkg --printsrcinfo > .SRCINFO
git add -A && git commit -m "Erstveröffentlichung 1.0.1" && git push
```

Voraussetzung: Konto auf aur.archlinux.org, öffentlicher SSH-Schlüssel im
Profil hinterlegt. Getestet werden sollte das PKGBUILD auf einem Arch-System —
die Abhängigkeiten `gtk3` und `webkit2gtk-4.1` sind die Annahme, mit der
pywebview sein Fenster zeichnet.

## Flathub — offen

Bewusst nicht vorbereitet. Flathub baut aus den Quellen in einer Sandbox ohne
Netzzugang: Jede Python-Abhängigkeit müsste als eigenes Modul mit Prüfsumme im
Manifest stehen (`flatpak-pip-generator`), dazu kommen AppStream-Metadaten und
eine App-ID unterhalb einer nachweislich eigenen Domain
(`de.klarbuchhaltung.Klar`). Das ist ein eigener Arbeitstag plus Review, und es
lässt sich nur auf einem Linux-System mit `flatpak-builder` ehrlich testen.

Reihenfolge-Empfehlung: erst winget und AUR (schnell, kein Gatekeeping), dann
den Homebrew-Tap, Flathub zuletzt.
