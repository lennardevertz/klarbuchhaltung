# Cask für den eigenen Tap lennardevertz/homebrew-klar.
#
#   brew install --cask lennardevertz/klar/klar
#
# Bewusst ein eigener Tap statt homebrew/cask: der Haupttap verlangt
# Bekanntheit (75 Sterne bzw. 30 Forks oder Watcher), die das Repo noch nicht
# hat. Sobald die Schwelle erreicht ist, wandert dieselbe Datei per PR nach
# Homebrew/homebrew-cask, und der Tap kann bleiben.
cask "klar" do
  version "1.0.1"
  sha256 "210ec1c22df9b2a72e2ab7760506c2ddf92a0432dd57cca26b593f4846c1791f"

  url "https://github.com/lennardevertz/klarbuchhaltung/releases/download/v#{version}/Buchhaltung-#{version}-macOS.dmg",
      verified: "github.com/lennardevertz/klarbuchhaltung/"
  name "Klar"
  desc "Lokale Buchhaltung für Freiberufler: Rechnungen, EÜR, USt-Voranmeldung"
  homepage "https://klarbuchhaltung.de/"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :big_sur"

  app "Buchhaltung.app"

  # Kein zap auf ~/Library/Application Support/Buchhaltung: dort liegen
  # Buchungen, Rechnungen und Belege, die der Aufbewahrungspflicht
  # unterliegen. Die löscht man von Hand, nicht per Deinstallationsbefehl.
  zap trash: [
    "~/Library/Logs/Buchhaltung.log",
    "~/Library/Saved Application State/de.buchhaltung.app.savedState",
  ]

  caveats <<~EOS
    Die App ist nicht signiert und nicht notarisiert. Beim ersten Start meldet
    macOS deshalb, sie stamme von einem unbekannten Entwickler. Einmalig:

      xattr -dr com.apple.quarantine "/Applications/Buchhaltung.app"

    Deine Buchhaltung liegt danach unter
    ~/Library/Application Support/Buchhaltung — sichere den Ordner selbst.
  EOS
end
