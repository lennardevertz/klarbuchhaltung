// Liefert die Landing-Page aus site/ und leitet die Nebendomains dauerhaft
// auf die Hauptdomain um.
//
// Läuft vor der Asset-Auslieferung (assets.run_worker_first in wrangler.jsonc),
// sonst würde die Weiterleitung nie greifen: index.html wäre schneller.
//
// Bewusst als Liste statt "alles ausser der Hauptdomain": so bleibt die
// workers.dev-Adresse zum Testen erreichbar, auch wenn die Domain noch nicht
// steht.

const ZIEL_DOMAIN = "klarbuchhaltung.de";
const WEITERLEITEN = ["klar.levertz.com", "www.klarbuchhaltung.de"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (WEITERLEITEN.includes(url.hostname)) {
      url.hostname = ZIEL_DOMAIN;
      // 301: Pfad und Query bleiben erhalten, damit auch tiefe Links passen.
      return Response.redirect(url.toString(), 301);
    }

    return env.ASSETS.fetch(request);
  },
};
