# Transmission + résolution des trackers

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à
`scripts/transmission-stats.py`, à la résolution de trackers de
`arr/clearr/app/core.py`, ou de lancer un `transmission-remote`.

## Transmission

- **`transmission-remote -w <dir>` placé AVANT `-a` change le répertoire de
  téléchargement PAR DÉFAUT de la session, pas la destination du torrent
  ajouté.** Les options sont traitées de gauche à droite : tant que `-a` n'a pas
  été lu, la requête `torrent-add` n'existe pas et `-w` part en `session-set`.
  L'aide (« When used in conjunction with `--add`, set the new torrent's
  download folder. Otherwise, set the default download folder ») ne dit rien de
  l'ordre. **Toujours écrire `-a <magnet> -w <dir>`, jamais l'inverse.**
  Vécu le 2026-09-07 : `transmission-remote -w /data/completed/anime -a "$m"`
  pour 4 magnets. Le contrôle fait dans la foulée (`-t <id> -i` → `Location:
  /data/completed/anime`) était vert — normal, le défaut venait justement de
  basculer — et le dégât est resté invisible 5 h.
  **Deux raisons pour lesquelles ça ne reste pas local à Transmission** :
  - Sonarr/Radarr n'envoient **pas** de chemin absolu quand une catégorie est
    configurée (`tvCategory=sonarr` / `movieCategory=radarr`, cf.
    `scripts/provision.py`) : à chaque grab ils font un `session-get`, lisent le
    `download-dir` courant et y collent la catégorie. Tout ce qu'ils ont grabé
    ensuite a atterri dans `completed/anime/{sonarr,radarr}`.
  - La bibliothèque « Animés » de Jellyfin scanne `/media/anime`
    (= `completed/anime`) en plus de `/library/anime` : les deux dossiers de
    catégorie y sont devenus **deux séries nommées « sonarr » et « radarr »**,
    répliquées jusque dans Kodi.
  Réparation : `session-set` pour remettre `/data/completed`, puis
  `torrent-set-location … move=true` (jamais un `mv` : Transmission perdrait
  les fichiers). Un move sur le même disque est un `rename`, donc **l'inode est
  conservé** et les hardlinks `library/` survivent — vérifié par `stat -c '%i %h'`
  avant/après. **En revanche les symlinks cross-seed ne survivent pas** : ils
  portent le chemin absolu vu par le conteneur, il faut les repointer à la main
  (`ln -sfn`) sous `.cross-seed-links/<tracker>/`.
- **`settings.json` n'est réécrit qu'à l'arrêt du daemon** : un réglage changé
  en RPC vit uniquement en session. Corollaire dans les deux sens — un `docker
  restart` grave la valeur courante (donc une dérive), et une correction faite
  en éditant le fichier à chaud est perdue. Lire l'état réel par `session-get`,
  jamais par `settings.json`.


## Résolution des trackers (`core.py` + `transmission-stats.py`)

La logique est **dupliquée** dans les deux consommateurs plutôt que
factorisée — tout correctif doit être appliqué des deux côtés.

- **Un `os.stat()` nu sur un fichier `library/`/`.transmission/data/` ne suit
  pas correctement un symlink cross-seed.** cross-seed (`linkType` symlink
  par défaut) crée ses liens en pointant vers le chemin **tel que vu par le
  conteneur** (`/data/completed/...`), pas le chemin hôte. `os.stat()` suit le
  lien avec la racine de l'hôte, qui n'a pas de `/data` : le fichier semble
  absent alors qu'il existe très bien dans le conteneur — 91 faux positifs sur
  ~230 torrents avant le fix, et le même bug sous-évaluait silencieusement les
  correspondances `library/`. Fix : `resolved_stat()`, qui détecte le symlink,
  lit sa cible et la traduit en chemin hôte avant le vrai `os.stat()`.
- **Comparer les domaines par leur base, pas par suffixe.** Le domaine
  d'annonce réel (`tracker.yggreborn.org`) et celui listé par Prowlarr
  (`www.yggreborn.org`) sont deux sous-domaines **frères** — un
  `hostname.endswith("." + domain)` nu ne matche jamais. Fix : `base_domain()`
  (2 derniers labels) appliqué des deux côtés.
- **`TRACKER_ALIASES` (`arr/.env`) couvre les deux cas que Prowlarr ne peut
  pas rattacher**, en config et pas en dur dans le code (quels trackers un
  indexeur utilise est propre à ce déploiement). `alias_for()` accepte deux
  formes de clé, l'exacte testée en premier :
  - **host exact** — pour un tracker public mutualisé. **Nyaa.si n'a aucun
    tracker qui lui soit propre**, seulement des trackers publics génériques.
    Y aliaser un domaine de base serait un bien pire faux positif :
    `tracker.torrent.eu.org` réduirait à `eu.org`, partagé par d'innombrables
    sites sans rapport. Faux positif accepté en connaissance de cause : un
    torrent d'un autre indexeur qui ajouterait l'un de ces trackers en secours
    est étiqueté « Nyaa.si » à tort.
  - **domaine de base** — pour un indexeur dont le tracker annonce sur un
    domaine à lui, distinct du site que Prowlarr connaît : C411 annonce sur
    `tk.c411.tw` alors que ses `indexerUrls` sont en `c411.org` (36 torrents
    classés « Autre » avant le fix du 2026-08-29), d'où `c411.tw=C411`, qui
    couvre aussi ses futurs sous-domaines. **Ne l'utiliser que pour un
    domaine appartenant en propre à l'indexeur.**
  Logique dupliquée dans `core.py` et `transmission-stats.py` comme le reste
  de cette section — corriger des deux côtés.
- **Dédupliquer par nom résolu avant d'accumuler.** `transmission-stats.py`
  sommait `uploadedEver`/`downloadedEver` une fois par **host brut** : un
  torrent Nyaa comptait son volume 5 fois une fois les 5 hosts collapsés sous
  le même nom, gonflant les totaux d'un facteur 5 (le ratio n'était pas faussé
  par coïncidence, numérateur et dénominateur gonflés pareil). Même dédup
  nécessaire à l'affichage, qui sortait sinon `"Nyaa.si,Nyaa.si,..."`.
- **Un tracker non rattachable à un indexeur Prowlarr est traité à part** :
  `resolve_tracker_name()` renvoie `(nom, officiel)`, `officiel=False`
  signifiant « retombé sur le hostname brut » (tracker public embarqué dans le
  `.torrent`, pas un indexeur qu'on interroge). Déclencheur : une release avec
  **24 hosts d'annonce**, dont des IPs nues. Côté dashboard les lignes non
  officielles sont masquées par CSS et révélées par un switch (filtrage côté
  client, la donnée reste dans le JSON) ; côté clearr elles sont repliées sous
  un seul libellé « Autre » (`OTHER_TRACKER_LABEL`) avec les hostnames dans un
  `title=` natif.

