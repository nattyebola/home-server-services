// Config versionnée : aucun secret en dur, les clés API viennent de
// arr/.env (PROWLARR_API_KEY/SONARR_API_KEY/RADARR_API_KEY) passées au
// conteneur via env_file, lues ici par process.env.*.
//
// À vérifier/adapter contre la doc cross-seed courante au moment du premier
// déploiement (noms de champs stables depuis plusieurs versions majeures,
// mais à confirmer) : https://www.cross-seed.org/docs/basics/options

module.exports = {
  // Un indexeur Prowlarr = une URL Torznab dédiée par ID (pas d'endpoint
  // agrégé côté Prowlarr) — lister explicitement chaque ID actif plutôt que
  // le seul "1" d'origine ("Torr9", supprimé depuis) : bug repéré le
  // 2026-07-28, cross-seed cherchait sur un indexeur mort depuis le
  // déploiement (410 Gone), donc 0 résultat/0 injection à chaque webhook.
  // IDs à vérifier dans Prowlarr (Indexers) si un indexeur est
  // ajouté/supprimé/recréé — ils ne sont pas stables dans le temps.
  torznab: [2, 3, 4, 5].map(
    (id) => `http://prowlarr:9696/${id}/api?apikey=${process.env.PROWLARR_API_KEY}`
  ),
  sonarr: [`http://sonarr:8989/?apikey=${process.env.SONARR_API_KEY}`],
  radarr: [`http://radarr:7878/?apikey=${process.env.RADARR_API_KEY}`],
  torrentClients: ["transmission:http://transmission-vpn:9091/transmission/rpc"],
  // false par défaut chez cross-seed — sans ça, le webhook déclenché par
  // arr/scripts/cross-seed-notify.sh ne consulte jamais le client réel pour
  // matcher l'infoHash reçu et échoue systématiquement ("Torrent client does
  // not have any torrent with criteria") même quand le torrent y est bien.
  useClientTorrents: true,
  // dataDirs et linkDirs sont deux sous-chemins du même montage /data
  // (docker-compose.yml) — nécessaire pour que cross-seed puisse hardlink
  // de l'un vers l'autre (voir CLAUDE.md). dataDirs garde le chemin exact
  // renvoyé par Transmission (pas de remapping possible côté cross-seed).
  dataDirs: ["/data/completed"],
  linkDirs: ["/data/.cross-seed-links"],
  action: "inject",
  duplicateCategories: true,
  // Sans cadence, le daemon ne recherche que sur événement webhook (nouvel
  // import Sonarr/Radarr) — jamais l'historique déjà présent avant le fix
  // des IDs Torznab ci-dessus (2026-07-28). Ajouté pour que les torrents
  // récents soient re-vérifiés périodiquement (ex. tracker qui vient
  // d'indexer un titre plus lentement que Sonarr/Radarr ne l'a importé),
  // pas juste au moment de l'import. Le rattrapage de l'historique complet
  // (torrents plus anciens que excludeOlder ci-dessous, jamais couverts par
  // cette cadence récurrente) se fait manuellement via `cross-seed search`
  // — voir CLAUDE.md, fait une fois le 2026-07-28 suite au fix des IDs.
  searchCadence: "3 days",
  // Requis dès que searchCadence est défini, sous deux contraintes de
  // cross-seed (config invalide sinon, boucle de crash constatée au
  // premier restart avant ce fix) : excludeRecentSearch >= 3x searchCadence,
  // et excludeOlder doit être 2-5x excludeRecentSearch. Avec un cadence de
  // 3 jours : ne resert un torrent que s'il n'a pas déjà été cherché dans
  // les 9 derniers jours, et seulement s'il a été vu il y a moins de 30
  // jours — la cadence récurrente cible les ajouts récents, pas tout
  // l'historique (voir rattrapage manuel ci-dessus).
  excludeOlder: "30 days",
  excludeRecentSearch: "9 days",
};
