// Config versionnée : aucun secret en dur, les clés API viennent de
// arr/.env (PROWLARR_API_KEY/SONARR_API_KEY/RADARR_API_KEY) passées au
// conteneur via env_file, lues ici par process.env.*.
//
// À vérifier/adapter contre la doc cross-seed courante au moment du premier
// déploiement (noms de champs stables depuis plusieurs versions majeures,
// mais à confirmer) : https://www.cross-seed.org/docs/basics/options

module.exports = {
  torznab: [`http://prowlarr:9696/1/api?apikey=${process.env.PROWLARR_API_KEY}`],
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
};
