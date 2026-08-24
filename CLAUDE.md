# server/ — instructions pour Claude

Infra as code de services home server (Docker Compose + Traefik + Makefile).
Guide d'installation : **voir `README.md`**. Description des services et
choix d'architecture expliqués : **voir `ARCHITECTURE.md`**. Problèmes
rencontrés : **voir `ISSUES.md`**. Ces trois fichiers sont destinés aux
humains. Ce fichier ne garde que ce qui sert à retravailler sur ce repo
sans relitiger des décisions déjà prises ou répéter des pièges déjà
rencontrés — pas l'historique des itérations, ni le détail relisible dans
le code lui-même.

## Décisions à respecter

Ne pas proposer de revenir dessus sans que l'utilisateur le redemande
explicitement :

### Socle

- **Rootless par container**, pas de daemon Docker rootless. `cap_drop:
  ALL` + `security_opt: no-new-privileges:true` partout ; `cap_add` ciblé
  seulement sur `db-next` et `vpn/transmission-vpn` (démarrent root puis
  descendent en privilège), justifié en commentaire dans leur compose
  file — ne pas en ajouter ailleurs sans le même genre de nécessité.
  **Jamais de socket Docker monté dans un conteneur** : un accès au socket
  est root-équivalent sur l'hôte, pire que n'importe quel `cap_add` déjà
  accepté ici. C'est ce qui a décidé le transport de `clearr` et de l'addon
  Kodi (voir plus bas).
- **Images toujours en `:latest`** (jamais de tag figé) — voulu
  explicitement, l'utilisateur accepte le risque de casse pour avoir
  toujours les dernières versions. La reproductibilité d'une restauration
  passe par le manifeste de digests capturé à chaque `make backup`
  (`scripts/backup.sh`), pas par des tags fixes dans les compose files.
- **Secrets et valeurs propres au déploiement** : `.env` par stack
  (gitignoré) + `.env.example` versionné — même chose pour les valeurs
  partagées entre stacks (`PUID`/`PGID`/`RENDER_GID`/`DOMAIN`/
  `DATA_ROOT`/`LAN_CIDR`/`DNS_PRIMARY`/`DNS_SECONDARY`) dans
  `.env.shared`/`.env.shared.example` à la racine. `LAN_CIDR` alimente les
  middlewares `ipAllowList` — ne jamais remettre ce CIDR en dur dans un
  compose file. `DNS_PRIMARY`/`DNS_SECONDARY` (Cloudflare par défaut)
  alimentent les blocs `dns:` de `arr/`, `vpn/` et `jellyfin/`. Toujours
  passer par le `Makefile` (`make <target> STACK=<nom>`), jamais `docker
  compose` en direct dans un dossier de stack (il ne chargerait pas
  `.env.shared`).
- **Montages host-specific** (bibliothèques Jellyfin, external storage
  Nextcloud) : dans `docker-compose.override.yml` par stack (gitignoré,
  chargé automatiquement par le Makefile s'il existe) + `.example`
  versionné à côté. Jamais dans le compose file de base — objectif :
  qu'un autre déploiement puisse reprendre la stack sans dépendre des
  chemins de cette machine.
- **Timezone de tous les containers alignée sur l'hôte** via bind-mount
  `/etc/localtime:/etc/localtime:ro`, préféré à une variable d'env `TZ` :
  ne dépend pas d'un paquet `tzdata` présent dans chaque image et suit
  automatiquement les changements d'heure. Ajouter ce montage à tout
  nouveau service plutôt que `TZ=...`.
- **`max-file: "3"` sur tous les blocs `logging` json-file** — sans lui, un
  seul fichier de logs par service : dès qu'il atteint `max-size`, tout
  l'historique disparaît au lieu d'être conservé dans un fichier tourné.
  Toujours ajouter les deux ensemble sur tout nouveau service, jamais
  `max-size` seul.
- **Healthcheck sur tous les services** : check HTTP réel quand un endpoint
  non authentifié existe (`/ping` Servarr, `/status.php` pour `web`,
  `/api/v1/status` pour `seerr`) ; simple connect TCP sinon (`nc -z` pour
  `app` sur le fastcgi 9000, `cross-seed`, `dashboard`/
  `transmission-proxy`) ; `pgrep supercronic` pour `recyclarr`. `traefik` a
  un entrypoint statique dédié `healthcheck` sur `127.0.0.1:8082`
  (`traefik.yml`, jamais publié dans `ports:`) plutôt que de réutiliser
  `web`/`websecure` — sinon la redirection http→https s'appliquerait aussi
  à la sonde. Aucune auto-remédiation volontairement (pas de watcher type
  `autoheal` sur le socket Docker), juste de la visibilité.
- **Hôte Linux natif requis, pas de support Windows/WSL2** (évalué le
  2026-07-23, cf. ISSUES.md) : noyau WSL2 sans chargement de module (casse
  le fix `ip_tables`), NAT cassant la joignabilité 80/443 pour Let's
  Encrypt, VM non persistante (casse le cron de backup), hardlinks
  Sonarr/Radarr cassés sur un disque Windows en drvfs, pas de passthrough
  VAAPI pour Jellyfin. Ne pas proposer WSL2 sans que ces points soient
  résolus.
- **Nextcloud** : image communautaire (pas AIO — incompatible avec
  rootless/infra-as-code, cf. ARCHITECTURE.md).
- **Seerr (`seerr/`, image `ghcr.io/seerr-team/seerr`)** pour la recherche/
  requête unifiée, pas Jellyseerr/Overseerr — les deux projets ont fusionné
  dans Seerr et sont dépréciés depuis. Ne pas proposer l'ancienne image.

### Repo public et historique git

- **Repo public** sur GitHub (`nattyebola/home-server-services`, remote
  `origin` via deploy key dédiée `~/.ssh/id_ed25519_server_backup` / alias
  SSH `github-server-backup`, pas la clé perso de l'utilisateur). Ne
  jamais committer un secret ou une info identifiante en dur (email,
  domaine, chemin perso...) dans un fichier versionné — toujours via
  `.env`/`.env.shared` (gitignorés) + leur `.example` (placeholders
  génériques). Le vrai username Unix reste en clair dans les
  `docker-compose.override.yml` gitignorés (décision explicite) — ne pas
  l'écrire ici pour autant, ce fichier est versionné : `whoami` sur la
  machine si besoin.
- **Historique git réécrit le 2026-08-09 (force-push)** pour retirer une
  adresse e-mail personnelle, présente à la fois dans `traefik/traefik.yml`
  et **comme auteur/committer de tous les commits**. Les deux volets sont
  indissociables : réécrire le seul fichier laisse l'adresse dans les
  métadonnées. Fait avec `git-filter-repo` (`--mailmap` +
  `--replace-text`), et `git config --local user.email` posé sur l'adresse
  `noreply` — sans ça le commit suivant réintroduit l'ancienne.
  Conséquences vivantes : les tags `commit-<sha>` des snapshots restic
  antérieurs pointent vers des commits disparus (idem les SHA dans
  `sauvegarde/backup.log` et `infra-commit.txt`) — lors d'une restauration,
  reprendre le tag `backup-*` correspondant plutôt que le SHA.
  **Après toute réécriture, purger aussi les objets orphelins locaux** :
  le force-push ne nettoie que le distant, et git garde deux semaines les
  objets non référencés — l'ancienne adresse reste donc lisible dans la
  base d'objets du checkout. `git reflog expire --expire=now
  --expire-unreachable=now --all` puis `git gc --prune=now`.
  Ne pas relancer ce nettoyage : il est fait, le refaire ne changerait que
  tous les SHA une nouvelle fois.

### clearr (`arr/clearr/`) — nettoyage torrents/bibliothèque

- **Remplace l'ancien `scripts/torrent-cleanup.py` / `make cleanup`**
  (2026-07-31) : web LAN-only (`clearr.${DOMAIN}`, middleware
  `arr-lan-only`) **et** TUI d'origine (`make clearr`), toutes deux
  appuyées sur le même `arr/clearr/app/core.py`. `webapp.py` (FastAPI +
  Jinja2 + Bootstrap) et `tui.py`/`cli.py` (curses / `delete-by-inode`)
  importent `core.py`, **jamais l'inverse**.
  Tourne en conteneur et rejoint `vpn-internal` (externe,
  `vpn_vpn-internal`) + le réseau `default` d'arr : HTTP direct vers
  `transmission-vpn:9091`, `sonarr:8989`, `radarr:7878`, `prowlarr:9696`.
  L'ancienne TUI passait par `docker exec <container> curl` depuis l'hôte —
  plus besoin, et surtout pas de socket Docker (voir Socle).
  Monte `${DATA_ROOT}:/data_root` en lecture-écriture, exactement comme
  sonarr/radarr (même mount unique, même raison hardlink) : core.py et arr
  partagent donc le même référentiel de chemins, **il n'y a plus de
  traduction de chemin à faire** (les `host_to_arr_path()`/
  `arr_path_to_host()` de l'ancien script ont disparu avec).
  1er service du repo avec des dépendances Python tierces
  (`arr/clearr/requirements.txt`) et 2e avec un build custom après
  `nextcloud/app`/`web` (`build: ./clearr`, `python:3-slim`, tag flottant) —
  `make update STACK=arr` doit donc aussi rebuilder.
  Chaque requête recalcule tout (`core.load_full_state()`), **jamais de
  cache ni d'état en mémoire entre deux requêtes** — contrairement à la TUI
  qui ne recharge qu'au démarrage. Mesuré acceptable à l'échelle de cette
  bibliothèque, à revoir si ça dérive.
- **Pas de HTMX ni de Popper**, malgré la validation initiale de
  « FastAPI + HTMX » : `static/clearr.js` est un petit JS maison
  (`data-get`/`data-post`/`data-live`). Bootstrap est vendoré en
  `bootstrap.min.css`/`.min.js` **sans le bundle** — donc pas de Popper,
  donc **aucun tooltip / dropdown / popover Bootstrap n'est disponible**
  (les infobulles passent par un `title=` natif).
  Pour vendorer un asset : `curl`, **jamais `WebFetch`** (qui peut reformater
  du contenu non-HTML — `curl` donne les octets exacts), et vérifier
  l'intégrité par un second téléchargement + comparaison sha256 avant de
  committer.
- **Zéro appel WAN côté serveur**, contrainte demandée explicitement. Les
  jaquettes viennent du cache disque des arr
  (`${DATA_ROOT}/.arr/{sonarr,radarr}/config/MediaCover/<id>/poster-250.jpg`)
  servi par `/poster/{kind}/{arr_id}` — `arr_id` passé par `int()`, c'est
  **la** garantie anti-traversal. Les ids externes
  (`imdbId`/`tvdbId`/`tmdbId`) sont déjà dans les objets arr. Seuls les
  liens sortent, suivis par le navigateur. TVDB adressé par
  `thetvdb.com/dereferrer/series/<tvdbId>` (Sonarr n'expose pas le slug) ;
  Radarr par `<tmdbId>` (son `titleSlug` EST le tmdbId).
  Seule exception au « zéro WAN » : `core.quality_profile_names()`, sur le
  réseau interne.
  `DOMAIN` (liens `sonarr.${DOMAIN}`/`radarr.${DOMAIN}`) vient de
  `.env.shared`, hors du `env_file: .env` du service : injecté par un bloc
  `environment:` dans `arr/docker-compose.yml`. Absent = pas de lien arr,
  le reste fonctionne.
- **Rattachement torrent → titre arr** (`core.build_arr_meta_index()`/
  `torrent_meta()`) : films par chemin exact de `movieFile.path`, séries
  par préfixe de `series.path` — mêmes critères que
  `plan_radarr_deletion`/`plan_sonarr_unmonitor`, en repartant des inodes
  déjà calculés par `analyze_torrent_files()` (donc zéro `stat`
  supplémentaire, symlinks cross-seed déjà résolus). Best-effort : un
  torrent jamais importé n'a ni jaquette ni lien, un arr injoignable dégrade
  la vue sans la casser.
- **Un seul gabarit par comportement partagé entre les 3 vues** :
  `templates/_meta.html` (cellule titre : jaquette + liens),
  `templates/details.html` (fiche « toutes les informations connues », en
  modale, purement descriptive), `render_arr_tab(tab, ...)` + `ARR_TABS`
  pour Séries/Films. Sans ça le même bloc serait écrit deux ou trois fois.
  `core.find_series_by_id`/`find_movie_by_id` plutôt que des `next((...))`
  recopiés dans les routes.
- **Erreur réseau rendue en bandeau lisible**
  (`@app.exception_handler(RuntimeError)`) plutôt qu'un 500 brut — la TUI
  avait déjà ce filet dans `run()`, le web non (chaque route peut lever
  indépendamment). Le handler teste le préfixe `/api/` et répond en **JSON**
  dans ce cas : sinon l'addon Kodi recevait le fragment Bootstrap destiné au
  navigateur.
- **Mise en page réglée au détail près, demandée ainsi — ne pas
  « nettoyer » le CSS de `clearr.css` sans redemande** : table resserrée
  (`table-sm`), suppression via une **croix ✕ (U+2715)** et pas un emoji
  (rendu par la police couleur du système, donc ni monochrome ni cohérent
  d'un appareil à l'autre, là où un glyphe texte hérite du rouge de
  `.btn-outline-danger`), colonne d'actions figée à droite dans la vue
  Torrents (`.table-sticky-actions`) sans bordure ni ombre sur son bord
  gauche, tri au clic sur l'en-tête de colonne (reclic = sens inverse).
  Trois pièges CSS à ne pas re-découvrir :
  - `tr.collapsing { transition: none }` est **obligatoire** : les groupes
    cross-seed sont des `<tr class="collapse">` (un `<details>` ne peut pas
    entourer des `<tr>`), et l'animation de hauteur de Bootstrap est pensée
    pour des blocs — sans ça l'ouverture clignote. Comportement documenté
    de Bootstrap sur les tableaux, pas un bug.
  - `background-color` explicite sur la cellule figée : `--bs-table-bg` est
    transparent, les cellules qui défilent dessous resteraient visibles au
    travers. Le surlignage de survol de Bootstrap passe par un `box-shadow`
    inset, donc peint par-dessus et continue de marcher.
  - `.title-link.has-poster` redéclaré explicitement : `.title-link` étant
    déclaré après `.has-poster`, son `text-decoration: none` effaçait le
    souligné pointillé qui signale une jaquette au survol.
  Jaquette chargée seulement au premier survol dans les tableaux
  (`data-poster` porte l'URL, pas un `<img>` — sinon 45 images par rendu
  alors qu'on en regarde une), dans un conteneur flottant attaché au
  `<body>` (les lignes vivent dans `.table-responsive`, dont l'`overflow`
  découperait une vignette). Dans la fiche détail c'est un vrai `<img>` :
  une seule fiche ouverte à la fois, rien à économiser.
  Bootstrap 5.3 ne suit pas `prefers-color-scheme` tout seul — un script en
  tête de `page.html` pose `data-bs-theme` avant le rendu du `<body>` pour
  éviter un flash clair→sombre.
- **Suppression toujours confirmée par une modale**, composant Modal natif
  de Bootstrap plutôt qu'un `<dialog>` fait main : focus trap / Échap /
  clic-sur-le-fond déjà corrects, les réimplémenter aurait été strictement
  moins bien.
- **Les écrans de confirmation annoncent aussi les fichiers sans torrent**
  et marquent les torrents encore en seed (🌱) ou arrêtés (⏸,
  `SEEDING_STATUSES`) : `execute_delete_series` supprime les torrents
  **puis** balaie le dossier (`cleanup_orphan_files`), la modale promettait
  donc moins que ce qu'elle faisait. Déclencheur : un fichier `library/`
  dont Sonarr avait retiré le torrent du client après le ratio atteint — le
  hardlink Transmission disparu, le fichier restant.
  `core.orphan_files_under()` factorise le calcul, `series_orphan_files()`
  l'applique au dossier d'une série. **Piège : les deux appelants ne
  raisonnent pas dans le même espace de chemins** — un titre hors arr a ses
  *données* sous le dossier visé (les couverts sont les `host_files` du
  torrent), une série n'y a que des *hardlinks* (les couverts sont les
  `lib_matches`, les données vivant sous `.transmission/data`). Comparer les
  mauvais chemins ferait passer tous les fichiers pour orphelins.
  Les films n'ont volontairement pas d'orphelins : `_delete_movie` ne balaie
  pas le dossier du film (soit un torrent le couvre, soit Radarr supprime son
  propre fichier), donc en annoncer serait promettre plus que ce qui est fait.
- **Bouton « Orphelins library/ »**, pas une 4e vue (demandé) :
  `core.library_orphan_files()`/`delete_library_orphans()`. Comble un trou
  structurel — les 3 vues partent des torrents ou des objets arr, donc un
  fichier ni lié à un torrent ni connu d'un arr n'apparaissait nulle part.
  Le calcul coûte `2 + N` appels arr (~170 ms contre ~50 ms pour l'onglet
  Torrents) : à la demande, jamais au rendu d'un onglet — d'où l'absence de
  compteur ou de bouton grisé.
  Couverture d'un fichier : un **torrent** le couvre s'il partage son inode ;
  un **arr** le couvre si c'est un de ses `episodefile`/`movieFile` **ou** un
  sidecar (`SIDECAR_EXTENSIONS`) sous le dossier d'un titre qu'il connaît
  encore. Ce 2e volet est indispensable depuis le metadata writer (241
  `.nfo`), sans lui ils ressortaient tous orphelins. Corollaire voulu : une
  vidéo posée dans le dossier d'une série suivie mais jamais importée **est**
  un orphelin (c'est le cas qui a motivé la demande), là où un `.nfo` au même
  endroit ne l'est pas. Couverture par **chemin de fichier exact**, pas par
  préfixe de dossier de titre — un préfixe aurait rendu invisible exactement
  le cas cherché.
  **`_arr_covered_paths()` n'est PAS best-effort**, seule exception du
  module : elle lève `RuntimeError` si un appel arr échoue au lieu de
  dégrader. Sans la liste des fichiers d'un arr, tout ce qu'il gère passerait
  pour orphelin — proposer de supprimer la moitié de `library/` sur un
  timeout serait le pire échec possible de cette fonction.
  Le POST **recalcule** la liste au lieu de reprendre les chemins de la
  modale : aucun chemin à supprimer ne vient du client.
- **Fiche détail : agrégé pour une série, complet pour un film.** Série →
  `core.fetch_episode_files()` puis valeurs *distinctes* de qualité /
  groupe / langue / codec / résolution (empiler le `mediaInfo` de 12
  épisodes noierait la fiche ; ce qu'on veut savoir c'est s'il y a un
  mélange). `_distinct()` préserve l'ordre de première apparition, pas
  l'ordre alphabétique. Fichiers triés par `relativePath` (= ordre
  saison/épisode) et non par l'ordre de l'API, qui est celui des ids donc
  des imports. Film → un seul fichier, détail complet, **sans custom
  formats** : le `movieFile` imbriqué dans l'objet film de Radarr n'en porte
  pas (contrairement à l'`episodefile` de Sonarr), les afficher n'aurait
  donné que des tirets.
  `seedRatioLimit` **et** `seedRatioMode` demandés ensemble à `torrent-get`
  (`_seed_limit_label()`) : afficher la limite sans le mode induit en
  erreur, un torrent en mode 0 traînant souvent un `seedRatioLimit` résiduel
  qui ne s'applique pas.
  Une section sans lignes n'est pas rendue — un film sans fichier n'affiche
  simplement ni Fichier(s) ni Média, sans cas particulier dans le code.
- **Mode CLI `delete-by-inode <dev> <ino> [--dry-run]`**, réutilisé par le
  skill `anime-vf` : retrouve et supprime un torrent par `(dev, ino)` déjà
  connu — capturé par l'appelant *avant* qu'un import ne remplace le fichier
  `library/`, un stat a posteriori échouerait. Ne déclenche délibérément
  **pas** `plan_sonarr_unmonitor`/`plan_radarr_deletion` : l'épisode reste
  monitored, il vient d'être remplacé par une meilleure release, pas retiré.
  Sortie JSON sur stdout pour un appelant scripté.
- **Synchronisation arr à la suppression**, pour éviter qu'un titre encore
  monitored soit re-téléchargé : film Radarr → retiré complètement (+ import
  exclusion) ; épisode/saison Sonarr → saison désactivée seulement si elle
  est *terminée* (`totalEpisodeCount == episodeCount`) et entièrement
  supprimée, sinon seuls les épisodes concernés — pour ne jamais couper le
  monitoring d'une saison en cours de diffusion. Depuis les vues
  Séries/Films, retrait complet du titre (`DELETE /api/v3/series/{id}`,
  `addImportListExclusion=true`), sans la condition « saison terminée » :
  ici on supprime tout. Matching par chemin, pas par nom — fiable même si le
  titre affiché diffère (VO/VF, ponctuation).
  Best-effort : un arr injoignable ou un fichier jamais importé ne bloque
  jamais la suppression des fichiers eux-mêmes.
- Écrit sur mesure plutôt que d'ajouter un service tiers (Decluttarr,
  Removarr...) : aucun ne couvre « suppression Sonarr/Radarr → nettoyage
  automatique du client torrent », trou connu et non résolu de l'écosystème
  *arr (cf. issue ManiMatter/decluttarr#292).
- TUI seulement : marqueur `'M'` pour un torrent dont le fichier a disparu
  (cas Transmission « No data found! », jamais nettoyé tout seul) +
  `Maj+P` pour les purger en masse, et un écran d'aide (`?`) plutôt qu'un
  footer surchargé. Pas de jaquette (curses ne fait que du texte ; une vraie
  image demanderait un protocole terminal ou `chafa`). Les ajouts récents
  sont **web seulement**.

### Addon Kodi « Supprimer avec clearr » (`kodi/context.clearr`)

- `make kodi-install`, ajouté le 2026-08-05 — détail humain dans
  `kodi/README.md`. Envoie les ids externes + le chemin du titre sélectionné
  à `POST /api/{preview,delete}/{film,series}` de clearr, qui réutilisent les
  mêmes helpers `_delete_series`/`_delete_movie` que les routes web.
- **Transport HTTP, pas la CLI** : `python -m app delete-…` aurait été aussi
  court, mais clearr tourne en conteneur — l'atteindre depuis un client media
  supposerait SSH ou l'utilisateur Kodi dans le groupe `docker`
  (root-équivalent, cf. Socle). Le conteneur écoute déjà et est joignable en
  LAN, l'addon n'a besoin que d'`urllib`.
- **Résolution par id externe puis par chemin** (`_resolve_target`) : Kodi ne
  connaît pas les ids Sonarr/Radarr, seulement les `ProviderIds` que
  jellyfin-kodi recopie depuis Jellyfin. IMDb d'abord, puis TVDB/TMDB. **Un
  id qui matche plusieurs titres renvoie None (404) plutôt que d'en deviner
  un** — c'est une suppression de fichiers.
  Le repli par chemin sert les titres hors arr (Jellyfin sert aussi
  `completed/` comme bibliothèque), et il est **plus discriminant** que les
  ids : deux dossiers peuvent porter le même `tvdbId` (2 saisons téléchargées
  séparément = 2 séries pour Jellyfin) là où la résolution par id refuse — à
  raison — de trancher.
  `core.resolve_media_path()` ne suppose **aucun préfixe commun** (Kodi voit
  `/grosDur/...`, clearr `/data_root/...`, un client distant verrait un
  partage réseau) : il cherche le plus long suffixe de composants qui existe
  réellement sous `completed/` ou `library/`, **2 composants minimum** — sans
  ce plancher, un chemin finissant par `film` résoudrait sur toute la
  catégorie. Introuvable ou ambigu = rien de supprimé.
- **Le repli par chemin s'interdit `library/`** (`is_arr_managed_path`) : un
  titre qui y vit est presque toujours suivi par un arr, supprimer ses
  fichiers sans retirer son entrée le ferait re-télécharger — et ça referme
  du même coup le cas « id ambigu », dont les fichiers sont justement là.
  Message renvoyant vers l'UI web dans ce cas.
- Supprime les torrents **et** les fichiers du dossier qu'aucun torrent ne
  couvre (choix explicite) : sans ça un titre récupéré à la main, sans
  torrent, serait insupprimable depuis Kodi. Aucun appel arr dans ce mode,
  par construction.
- **Routes `/api/preview/...` appelées avant la confirmation** : la boîte
  annonce « 3 torrents — 15,6 Go » et pas seulement le titre, et un titre non
  résolu est signalé *avant* la confirmation. Elles couvrent les deux modes,
  pas seulement le repli — deux comportements selon l'origine du titre
  auraient été plus de code pour moins de cohérence. `orphan_lines()` plafonne
  à 5 noms : la boîte `yesno` de Kodi ne défile pas, au-delà le texte passe
  sous les boutons.
- **Dépend de jellyfin-kodi en chemins directs** (`useDirectPaths=1`) : en
  mode addon, Kodi ne connaît qu'une URL `plugin://`, inexploitable — seuls
  les titres suivis par arr resteraient supprimables.
- **Ids lus par JSON-RPC** (`VideoLibrary.Get*Details`, propriété `uniqueid`)
  et non par les InfoLabels `ListItem.UniqueID(imdb)`/`IMDBNumber`, qui ne
  remontent que l'id désigné par défaut alors qu'on veut pouvoir retomber sur
  les autres.
- **Le retrait de la ligne dans Kodi n'est pas fait par l'addon** (option
  choisie par l'utilisateur après comparatif) : Kodi ne réplique pas le disque
  mais la bibliothèque Jellyfin, donc la chaîne est `clearr supprime` →
  `Jellyfin rafraîchit` → `KodiSyncQueue` → `jellyfin-kodi retire`. Un
  `VideoLibrary.RemoveMovie` local aurait fait disparaître la ligne
  instantanément mais désynchronisé la table de correspondance de
  jellyfin-kodi.
  **Aucun `Container.Refresh` à la fin** : la propagation prend **1 min 38 s**
  (chronométré — <1 s côté serveur, puis 65 s de `LibraryMonitorDelay`, 5 s de
  KodiSyncQueue, 25 s avant `LibraryChanged`, 3 s de retrait). Un refresh
  différé de 6 s rerendait strictement la même liste.
- Durées de notification dans `NOTIFICATION_MS`/`NOTIFICATION_ERROR_MS`
  (2,5 s / 5 s) : la suppression est déjà terminée quand elles s'affichent,
  ce n'est qu'un accusé de réception ; l'erreur reste plus longue, elle porte
  une information à lire. À ne pas confondre avec les toasts de jellyfin-kodi
  (`newvideotime`), qui ne viennent pas de cet addon.
- **L'URL de clearr est un réglage d'addon** (`resources/settings.xml`), pas
  une constante : elle contient `${DOMAIN}`, hors de question dans un fichier
  versionné. `make kodi-install` la pré-remplit depuis `.env.shared` et
  **n'écrase jamais un `settings.xml` existant**, Kodi réécrivant lui-même ce
  fichier. Addon **copié** et non symlinké (Kodi refuse un addon dont le
  dossier est un lien sortant de son `addons/`).
- **Pas de jeton d'authentification sur ces routes**, décidé explicitement :
  le service est LAN-only et son UI web expose déjà les mêmes suppressions en
  POST sans jeton — à revoir pour les deux ensemble, jamais pour l'API seule.

### Chaîne arr → Jellyfin → Kodi

- **Metadata writer « Kodi (XBMC) / Emby » activé sur Sonarr et Radarr**,
  porté par `scripts/apply-arr-overrides.py` (`XBMC_METADATA_*`, 2026-08-06) :
  **Jellyfin n'apprend jamais les ids externes des arr** — la connexion
  Emby/Jellyfin ne signale qu'un dossier à rescanner, aucun identifiant ne
  circule, donc Jellyfin réidentifie chaque titre par une recherche TMDB sur
  le nom de dossier + année. Deux erreurs réelles constatées, toutes deux dues
  à un homonyme plus populaire (`Dead Man (1995)` pris pour *Dead Man
  Walking*, `One Piece` sans année pris pour la série live-action de 2023).
  Les `.nfo` écrits à côté des fichiers portent les `uniqueid`
  imdb/tmdb/tvdb, et les bibliothèques Jellyfin ont `Nfo` en tête de leur
  `LocalMetadataReaderOrder` : l'identification devient déterministe.
  **Ce n'est pas qu'un problème d'affichage** : un id faux rendait le titre
  insupprimable depuis Kodi (l'addon envoie les ids vus par Jellyfin, et le
  repli par chemin s'interdit `library/`).
  Images désactivées volontairement (`movieImages`/`seriesImages`/
  `seasonImages`/`episodeImages`/`episodeImageThumb`) : Jellyfin télécharge
  déjà ses jaquettes dans son cache, on ne veut que les identifiants dans
  `library/`.
  **Contrepartie : le `.nfo` fait autorité sur le titre affiché**, Jellyfin
  n'utilise donc plus la traduction TMDB. D'où `movieMetadataLanguage: 2`
  (French) côté Radarr. **Sonarr n'a aucun champ équivalent** (vérifié champ
  par champ ; son `uiLanguage` ne concerne que son interface), donc **les
  titres de séries s'affichent en anglais/original** — écart **accepté
  explicitement** après avoir écarté les deux parades : renommer et
  verrouiller le titre dans Jellyfin (manuel), ou reprendre le titre français
  depuis les `alternateTitles` de Sonarr (qui les connaît, mais sans étiquette
  de langue — deviner lequel est le français est exactement le genre
  d'heuristique qui a produit le bug corrigé ici). Ne pas reproposer sans
  redemande.
  Jellyfin **relit un `.nfo` modifié tout seul** (il en suit la date à son
  scan) ; un `/Items/{id}/Refresh` explicite ne sert qu'à ne pas attendre.
  Les `.nfo` ne sont écrits qu'à l'import ou sur rescan — rattraper une
  bibliothèque existante avec `RescanMovie`/`RescanSeries` sans argument.
  Pour **réidentifier** un titre déjà faux : `POST
  /Items/RemoteSearch/{Movie,Series}` puis `POST /Items/RemoteSearch/Apply/{id}`
  (équivalent API du bouton « Identifier ») — un rescan arr seul ne suffit
  pas, Jellyfin ne relit un `.nfo` qu'à un rafraîchissement de métadonnées.
  Audit à refaire de la même façon en cas de doute : comparer
  `imdbId`/`tmdbId`/`tvdbId` des objets arr aux `ProviderIds` des items
  Jellyfin **appariés par chemin** (`/library/...` côté Jellyfin ↔
  `/data_root/library/...` côté arr).
- **Connexion Sonarr/Radarr → Jellyfin entièrement provisionnée**
  (`JELLYFIN_FIELDS`/`JELLYFIN_TRIGGERS` dans
  `scripts/apply-arr-overrides.py`), création incluse.
  `mapFrom=/data_root/library` / `mapTo=/library` est **obligatoire** : les
  arr voient la bibliothèque sous `/data_root/library/...` (mount unique du
  fix hardlink), Jellyfin sous `/library/...` — sans ça le refresh ciblé ne
  trouve pas le bon dossier. Cible `jellyfin:8096` en direct (réseau
  `traefik-public` partagé, pas de passage par Traefik).
  Déclencheurs voulus : `onSeriesDelete`+`onEpisodeFileDelete` côté Sonarr,
  `onMovieDelete`+`onMovieFileDelete` côté Radarr. Ils étaient **tous à
  `False`** depuis la création des connexions : Jellyfin ne découvrait une
  suppression que par son propre watcher. Les `*ForUpgrade` restent
  volontairement à `False` — le remplacement est déjà annoncé par `onUpgrade`,
  les activer ajouterait un « retiré puis rajouté » à chaque upgrade.
  **Déclaratif et faisant autorité** : tout déclencheur `on*` hors de la liste
  voulue est remis à `False`.
  `JELLYFIN_API_KEY` dans `arr/.env` — même clé que celle de Seerr, réutilisée
  plutôt qu'une clé dédiée. Le reste (`JELLYFIN_FIELDS`, triggers) est déclaré
  **dans le script** et non en `.env` : `jellyfin:8096` est un nom de service
  Docker et `mapFrom`/`mapTo` découlent des montages du repo, rien là-dedans
  n'identifie ce déploiement.
  **`MissingIntegration` → `note:` + exit 0**, pas une erreur : `README.md`
  donne cette connexion pour optionnelle, un déploiement sans Jellyfin verrait
  sinon le cron quotidien sortir en échec. D'où un 3e canal de sortie dans
  `main()` (`notes`, à côté de `changed`/`errors`) — à réutiliser pour toute
  future intégration optionnelle plutôt que de choisir entre « silencieux » et
  « échec ». Sans clé mais avec une connexion existante, les champs non
  secrets et les déclencheurs sont quand même maintenus.
  **Piège central : l'API Servarr renvoie les champs secrets masqués en
  `********`** et les préserve quand on les repasse tels quels. C'est ce qui
  rend possible le mode « sans clé », mais ça veut aussi dire qu'une clé qui
  aurait dérivé est **indétectable** par comparaison (elle est donc exclue de
  `jellyfin_signature()`, sinon chaque passage réécrirait pour rien).
  `POST /api/v3/notification/testall` est le seul moyen de vérifier qu'une clé
  stockée fonctionne encore.
  Ce n'est pas recyclarr qui fait dériver ces champs (il ne touche pas aux
  notifications) : le réalignement quotidien ne sert qu'à rattraper une
  modification par mégarde dans l'UI, la reproductibilité sur une installation
  neuve étant l'objectif principal.
  **Ces déclencheurs ne raccourcissent PAS le délai de Jellyfin** (affirmé à
  tort puis démenti par la mesure) : `POST /Library/Media/Updated` émis à la
  main déclenche le rafraîchissement **60 s plus tard** — Jellyfin fait passer
  les mises à jour *signalées* par le même temporisateur `LibraryMonitorDelay`
  (60 s, `${DATA_ROOT}/.jellyfin/config/config/system.xml`) que les événements
  inotify. Ce qu'ils apportent : le bon dossier est signalé explicitement,
  sans dépendre d'inotify. Réduire le délai demanderait de baisser
  `LibraryMonitorDelay` lui-même — **non fait**, réglage global qui protège
  aussi les imports d'un scan lancé sur un fichier encore en écriture.
  Trou connu : une suppression qui ne retire pas le titre côté arr (vue
  Torrents, où Sonarr se contente d'un `unmonitor`) ne notifie rien et reste
  tributaire du watcher.
  Bruit connu : Sonarr logue deux `[Warn] MediaBrowserProxy: Unable to send
  notification to Emby` par suppression alors que `notify` est à `false` —
  c'est la notification *à l'écran* des clients, pas le rafraîchissement de
  bibliothèque. Sans effet, non diagnostiqué plus loin.
- **Tag `pour-les-enfants` sur les deux arr**, créé par `scripts/provision.py`
  (`ARR_TAGS`) : posé depuis Seerr au moment de la requête (override `tags`
  par requête), il ressort dans le `<tag>` du `.nfo`, donc dans les `Tags` de
  l'item Jellyfin, donc dans la table `tag` de Kodi, où il sert de filtre.
  Dans `provision.py` et pas `apply-arr-overrides.py` : un tag est un objet
  que l'utilisateur peut légitimement renommer, donc créé-si-absent et jamais
  réécrit. Rattachement **par libellé**, les ids diffèrent d'un arr à l'autre.
  **Libellé en tirets, pas en underscores** : Radarr valide `^[a-z0-9-]+$` et
  refuse `pour_les_enfants`, là où Sonarr l'accepte — divergence de validation
  entre les deux, à ne pas re-découvrir. Le même libellé des deux côtés est ce
  qui permet de n'écrire qu'un seul filtre en aval.
  Trou connu, non traité : le tag **`fr-priority`** (Sonarr) est la cible d'un
  **delay profile** qui ne vit que dans la base Sonarr — ni le tag ni le profil
  ne sont reproductibles depuis le repo.
- **Seerr parle à Jellyfin en direct (`jellyfin:8096`)**, pas par le domaine
  public (2026-08-24) : il était réglé sur `https://jellyfin.${DOMAIN}:443`,
  donc chaque requête traversait Traefik et son middleware `rate-limit` — que
  son propre `AvailabilitySync` fait sauter (13 × `429` pendant le sync de
  03:00, dont 12 titres non retrouvés). Comme ce job retire la disponibilité
  d'un média introuvable, **un 429 y est indiscernable d'une suppression
  réelle**, et le job se déclare quand même « complete ».
  **Piège : renseigner `externalHostname` en même temps.** Les liens « Lire
  sur Jellyfin » sont construits dans `server/entity/Media.ts`, qui retombe
  sur `getHostname()` (= l'adresse *interne*) quand il est vide — tous les
  liens montrés aux utilisateurs deviendraient `http://jellyfin:8096/...`. Il
  porte donc l'URL publique complète, schéma inclus et **sans slash final**
  (`Media.ts` concatène `${jellyfinHost}/web/...`). `mediaUrl` est recalculé
  en `@AfterLoad()`, les entrées déjà en base reprennent la bonne URL seules.
  **Éditer `settings.json` conteneur arrêté** (`docker stop`/`start`, pas de
  recréation) : Seerr le réécrit lui-même, une édition à chaud est perdue.
  `provision.py` écrit déjà l'adresse interne **et** `externalHostname` dérivé
  de `DOMAIN` — la dérive venait de l'assistant web, pas du script.

### Scripts de provisioning arr

- **`scripts/provision.py` (`make api-keys` / `make provision`)** automatise
  la configuration d'installation qui se faisait à la main dans les UI :
  bibliothèques Jellyfin, clés API arr + cross-seed + Jellyfin, applications
  Prowlarr, client de téléchargement, root folders, remote path mapping,
  Connection cross-seed, tags, config Seerr complète.
  **Deux targets et pas un** parce que l'ordre est contraint dans les deux
  sens : `api-keys` doit précéder `recyclarr-sync`/`arr-overrides` (qui ont
  besoin des clés), et la config Seerr de `provision` doit les suivre (elle
  désigne les profils qualité **par nom**).
  **Créé-si-absent, jamais réécrit** — l'inverse d'`apply-arr-overrides.py`.
  Volontaire : ce sont des objets d'infrastructure que l'utilisateur peut
  légitimement ajuster ensuite dans les UI, et ce script n'est donc **pas**
  dans `scripts/crontab`. Piège évité de peu : un premier jet POSTait
  `/settings/jellyfin` de Seerr sans garde, ce qui aurait remplacé l'adresse
  réglée à la main sur une installation en service.
  **Best-effort par objet (`run_step`)**, imposé par des validations
  synchrones côté serveur : Sonarr valide le client de téléchargement en s'y
  connectant au moment du POST (un `transmission-vpn` arrêté faisait échouer
  tout le provisioning) ; la Connection Custom Script exige que le fichier
  existe côté conteneur ; Prowlarr vérifie que Sonarr/Radarr sait le joindre
  en retour sur `PROWLARR_INTERNAL_URL`.
  `jellyfin/.env` porte `JELLYFIN_ADMIN_USER`/`PASSWORD`, nécessaires aux deux
  seules opérations que Jellyfin refuse à une clé API : créer la première clé
  (bootstrap) et créer le compte propriétaire de Seerr. **Une clé API suffit
  en revanche pour créer une bibliothèque** — ne pas réintroduire de
  dépendance aux identifiants dans `make provision`.
  Détail sans conséquence : après suppression d'une bibliothèque Jellyfin,
  celle-ci reste un moment listée par l'endpoint que Seerr interroge (cache
  côté Jellyfin) — elle arrive désactivée côté Seerr. Corollaire voulu : une
  bibliothèque personnelle (ex. « Kids ») n'est pas activée dans Seerr par le
  script, `JELLYFIN_LIBRARIES` seul l'est.
- **`scripts/apply-arr-overrides.py` (`make arr-overrides`)**, enchaîné par
  cron quotidien juste après `make recyclarr-sync` : **déclaratif et faisant
  autorité**, il réapplique tout ce que recyclarr écrase ou ne couvre pas.
  Périmètre : tailles de palier « Quality Definition » et champ `language` des
  deux profils principaux (Sonarr `WEB-2160p (Combined)`, Radarr `[SQP] SQP-1
  WEB (2160p)`), config anime (`arr/profiles/sonarr-anime.json`), connexions
  Jellyfin, metadata writer, ratio des indexeurs publics.
  Résout les profils **par nom, jamais par id** (propres à chaque instance —
  c'est précisément pourquoi un dump d'API brut ne serait pas reproductible).
  Idempotent et best-effort par arr.
  `arr/profiles/sonarr-anime.json` couvre les 2 custom formats qui nous
  appartiennent (`FRENCH`, `VOSTFR (hors suffixe)`) et les profils
  `Anime (Fansub)*` : aucun `trash_id` ne les couvrait, donc rien ne les
  recréait sur une installation neuve et rien ne rattrapait leur dérive.
  **Le JSON fait autorité** : tout custom format absent de `scores` est remis
  à 0 sur le profil concerné. Un profil absent est créé depuis
  `/api/v3/qualityprofile/schema` plutôt qu'en versionnant tout l'arbre des
  paliers.
  **Ordre imposé** : custom formats d'abord, profils ensuite (qui les
  référencent par nom) — et surtout `make recyclarr-sync` AVANT tout le
  script, sinon les CF du guide scorés par les profils anime (`MULTi`, `LQ`,
  `Upscaled`...) n'existent pas encore ; ce cas lève une erreur explicite
  plutôt que de créer un profil silencieusement dépourvu de la moitié de ses
  scores.
  `api_put` passe par un `api_write` commun qui **vérifie la réponse** :
  `curl -s` sort 0 même sur un 400, sans ça une écriture refusée par la
  validation Sonarr était comptée comme réussie.
  **`settle()` : relecture jusqu'à 2 passes consécutives sans rien à
  corriger** (bornées à 6 tentatives × 5 s), appliqué aux seules étapes que
  recyclarr fait dériver. Nécessaire à cause des écritures Servarr asynchrones
  — voir le piège dédié plus bas. Une passe qui corrige remet le compteur à
  zéro, donc une écriture en deux temps ne conclut pas sur la première
  accalmie.
- **Le scheduler interne de recyclarr est désactivé**, le service passe en
  mode manuel pur (`arr/docker-compose.yml` : plus de `restart:`/healthcheck,
  `profiles: [manual]` pour rester absent de `make up STACK=arr`), déclenché
  uniquement par `make recyclarr-sync` (`docker compose run --rm recyclarr
  sync` — passer un argument à l'entrypoint bascule du mode cron au mode CLI
  one-shot). Aucune valeur de `CRON_SCHEDULE` ne le désactive proprement, et
  son `@daily` créait une fenêtre pendant laquelle les overrides étaient dans
  l'état par défaut du guide TRaSH. `scripts/crontab` enchaîne donc
  `recyclarr-sync && apply-arr-overrides.py` sur une seule ligne.
  Recyclarr est gardé pour sa vraie valeur — les MAJ communautaires des ~120
  regex, listes LQ, groupes de release, tags de plateformes — mais **toute la
  config custom doit vivre dans le repo**.
- **Limite de ratio 1.5 sur les indexeurs marqués publics**
  (`PUBLIC_INDEXER_SEED_RATIO`) : `seedCriteria.seedRatio` posé sur tout
  indexeur dont
  l'indexeur Prowlarr correspondant a `privacy: public`, jamais sur les
  autres — un tracker privé compte le ratio comme une monnaie, un public n'en
  tient aucun compte, donc y seeder au-delà du nécessaire n'immobilise que la
  copie Transmission. Rattachement par le dernier segment du `baseUrl` de
  l'indexeur synchronisé (`http://prowlarr:9696/<id>/`) : seul Prowlarr porte
  l'information `privacy`, les arr n'en gardent que l'URL.
  **La valeur fait autorité sur l'indexeur PROWLARR**
  (`torrentBaseSettings.seedRatio`), pas seulement côté arr : les deux
  applications Prowlarr sont en `syncLevel: fullSync` et sa tâche
  `ApplicationIndexerSync` tourne **toutes les 6 h**, réécrivant l'indexeur de
  chaque arr depuis la définition de Prowlarr — ce qui efface tout
  `seedCriteria` que l'arr portait. Une valeur posée seulement côté arr ne
  peut donc pas tenir plus de 6 h. La passe côté arr est **conservée** comme
  filet : effet immédiat sans attendre un sync, et seul recours si `syncLevel`
  passait à `addOnly`/`disabled`.
  Prowlarr expose bien `torrentBaseSettings.seedRatio`, `seedTime`,
  `packSeedTime` et `appMinimumSeeders` sur son propre objet indexeur (une
  version antérieure de ce fichier affirmait le contraire, à tort).
  `forceSave=true` sur le PUT côté arr : Sonarr/Radarr testent la connexion à
  l'indexeur au moment de l'écriture, et ces trackers répondent régulièrement
  520/530 — sans ça une panne passagère ferait échouer un réalignement
  purement local.
  Ne pas confondre avec la limite globale de Transmission
  (`ratio-limit-enabled: false`, laissée telle quelle) : c'est bien une limite
  **par torrent** (`seedRatioMode=1`) que les arr poussent au grab.
  **Non rétroactif** : `seedRatio` ne s'applique qu'au moment du grab. Les
  torrents déjà présents ont été repris une fois à la main (`torrent-set` en
  masse). Refaire ce rattrapage si un indexeur public est ajouté avec des
  torrents déjà en place. Un torrent annonçant **aussi** à un tracker privé
  est exclu : lui couper le seed coûterait du ratio là où il compte.
- **Deux profils anime plutôt qu'un scoring global** : `Anime (Fansub) VF`
  (audio français préféré) et `Anime (Fansub) VOSTFR` (japonais + sous-titres).
  La bascule par série fait elle-même office de sélection explicite — un
  premier essai avait modifié un profil partagé en place, corrigé après retour
  de l'utilisateur. Custom Format `FRENCH` (`\b(TRUEFRENCH|FRENCH|VFF|VFQ)\b`,
  résoudre son id **par nom**, ne pas le supposer fixe) scoré 200 sur le profil
  VF. `VOSTFR` veut dire japonais + sous-titres français, **pas** de l'audio
  français — à ne pas confondre.
  Profils Sonarr existants : les 6 par défaut (aucun utilisé),
  `WEB-2160p (Combined)`, `Anime (Fansub) VF`, `Anime (Fansub) VOSTFR`.
  Skill `.claude/skills/anime-vf/SKILL.md` : bascule la série sur le profil
  VF, relance `SeriesSearch`, rapporte ce qui a été grabé — le remplacement
  du fichier reste automatique côté Sonarr (`upgradeAllowed: true`). Le skill
  capture le `(dev, ino)` de chaque fichier **avant** la recherche, attend
  l'import, puis supprime l'ancien torrent via `delete-by-inode`. Ne marche
  que si une release FRENCH/VFF/VFQ/TRUEFRENCH existe réellement chez les
  indexeurs au moment de la recherche.
- **`scripts/vpn-bench.py` (skill `vpn-bench`)** compare latence/débit entre
  le serveur AirVPN configuré (`vpn/custom/default.ovpn`) et d'autres pays.
  Marche parce que le certificat client AirVPN est lié au **compte**, pas au
  serveur : seule la ligne `remote [pays].vpn.airdns.org <port>` change, rien
  à regénérer depuis le site. **Restaure systématiquement la config d'origine
  à la fin** (backup sur disque en plus de la copie en mémoire, pour survivre
  à un kill dur) — jamais de changement permanent sans le redemander.
  Latence mesurée vers un tracker tiré au hasard parmi les torrents actifs
  (accepté explicitement : moins reproductible, plus simple).

### Traefik, dashboard, exposition

- **`hsts` et `security-headers` : middlewares partagés, définis une seule
  fois sur le container `traefik` lui-même** (labels sans routeur associé —
  un container avec `traefik.enable=true` peut déclarer un middleware sans
  router pour que d'autres stacks le référencent via `<nom>@docker`). Ne
  jamais redéfinir `stsSeconds`/`customResponseHeaders` en dur dans un compose
  file, toujours `hsts@docker`/`security-headers@docker`. Un routeur qui a
  déjà un autre middleware les combine en liste :
  `arr-lan-only@file,security-headers@docker,hsts@docker`.
  - `hsts` : `stsSeconds=15552000`, `stsIncludeSubdomains`, `forceSTSHeader`,
    sur tous les services y compris LAN-only.
  - `security-headers` : `X-Robots-Tag: noindex, nofollow, noarchive`
    (serveur perso, aucun service ne doit être indexé ni appris par un
    crawler d'entraînement IA), `X-Frame-Options: DENY`,
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
  - `rate-limit` : `average=50`, `burst=100` par IP, sur `jellyfin` et
    `seerr` seulement (Nextcloud a son propre anti-bruteforce, les arr et
    transmission sont LAN-only). Limite tout le routeur, pas seulement le
    login — Traefik seul ne sait pas cibler par code de réponse ou chemin.
    Valeurs volontairement généreuses pour ne jamais gêner un usage normal.
    **C'est ce middleware qui rate-limitait Seerr** quand il passait par le
    domaine public (voir plus haut) : y ajouter un service qui parle à
    Jellyfin en volume demande d'y penser.
- **Le dashboard vit sur le domaine nu et `www.${DOMAIN}`**, pas
  `dashboard.${DOMAIN}` : `Host(${DOMAIN}) || Host(www.${DOMAIN})` dans
  `traefik/docker-compose.yml`. Nextcloud est sur `nextcloud.${DOMAIN}` — et
  changer le domaine de Nextcloud n'est pas qu'un label Traefik :
  `trusted_domains`/`overwrite.cli.url` sont dans `config/config.php`
  (persisté, hors compose), à mettre à jour via `occ config:system:set` dans
  le container `app` et **pas** en éditant le fichier à la main, sinon
  Nextcloud rejette le nouveau nom d'hôte (« domaine non fiable »).
- **Le dashboard est accessible en WAN comme en LAN** — les sous-domaines
  qu'il liste sont de toute façon publics via Certificate Transparency dès
  qu'un certificat a été émis, donc restreindre la page n'apportait aucune
  confidentialité réelle. Ne pas proposer d'y revenir sans redemande.
  Les cartes des services LAN-only restent dans le HTML et sont grisées côté
  client par une sonde `<img>` (`onload`/`onerror`, **pas** `fetch`/`XHR` —
  ceux-ci échouent pareil bloqués ou non par CORS, donc ne distinguent pas un
  403 d'un succès). Chemin de la sonde dans `PROBE_PATH` : ne pas supposer
  `/favicon.ico` générique, `transmission-proxy` le redirige vers du HTML.
  Exclu des moteurs/crawlers par trois voies redondantes (volontaire, couvre
  les crawlers qui ne parsent pas le HTML) : `<meta name="robots">`,
  `dashboard/assets/robots.txt` et l'en-tête `X-Robots-Tag`. Garder
  `robots.txt` à jour dans `dashboard/assets/` (source versionnée), pas dans
  `dashboard/html/` (généré, gitignoré).
- **Les deux middlewares LAN-only vivent dans le provider `file`, pas dans
  des labels Docker** (`traefik/dynamic/lan-only.yml`, `watch: true`,
  référencés en `arr-lan-only@file`/`transmission-lan-only@file`) — pour
  pouvoir être ouverts au WAN et refermés **sans recréer un seul conteneur**
  (`make switch-lan-only-middleware` : « tester/corriger si je ne suis pas
  chez moi mais que j'ai un accès SSH »). Un label ne peut changer qu'en
  recréant le conteneur qui le porte, ce qui aurait voulu dire redémarrer les
  4 arr + le proxy à chaque aller-retour.
  **Le middleware est rendu inopérant, pas retiré des routeurs** : seule sa
  plage change, `LAN_CIDR` ↔ `0.0.0.0/0` + `::/0`. **`::/0` n'est pas
  décoratif** — sans lui un client IPv6 resterait bloqué et l'ouverture serait
  silencieusement partielle.
  **Un dossier monté, pas un fichier** : un bind-mount de *fichier* garde
  l'inode qu'il avait au démarrage (voir le piège dédié plus bas). Le script
  écrit par `mktemp` + `mv` atomique, pour que Traefik — qui surveille le
  dossier — ne lise jamais un fichier à moitié écrit.
  **Refermeture par un garde cron (`rearm`, toutes les 5 min)**, pas par un
  `sleep 3600` détaché : le cron survit à la déconnexion SSH qui a servi à
  ouvrir et à un redémarrage, ce qu'un processus en arrière-plan ne fait pas —
  et l'oubli est le vrai risque de cette commande. Contrepartie assumée : la
  refermeture tombe entre 60 et 65 min. L'échéance vit dans
  `${DATA_ROOT}/.lan-only-open-until`, son absence = fermé.
  **`ensure` suit le fichier d'état, il ne referme pas aveuglément** : un
  `make up` pendant une fenêtre ouverte recréait le fichier en mode fermé
  alors que le bandeau continuait d'annoncer « ouvert jusqu'à HH:MM » — on se
  croit joignable de l'extérieur alors qu'on ne l'est plus. Il nettoie au
  passage une échéance périmée.
  **Le mode de défaillance est fermé, pas ouvert** : sans
  `traefik/dynamic/lan-only.yml`, Traefik ne résout plus le middleware et les
  5 routeurs répondent **404**. D'où le `lan-only-middleware.sh ensure` appelé
  par `make up`. `traefik/dynamic/` est versionné par un `.gitkeep` — si
  Docker devait créer le dossier lui-même il le ferait en root sur l'hôte ;
  son contenu est gitignoré, il porte `LAN_CIDR`.
  **Pas de marqueur `.cron-status` pour ce garde** : muet en permanence, il ne
  serait jamais que vert. C'est le **bandeau rouge du dashboard**
  (`render_lan_only_banner()`, avec l'heure de refermeture) qui porte la
  visibilité, et c'est pour lui que la bascule régénère le dashboard dans les
  deux sens. Il fallait un signal *serveur* : les cartes sont grisées côté
  client par la sonde `<img>`, qui verrait justement ces services répondre.
  **Un service reste classé « Local (LAN) » même fenêtre ouverte** : faire
  basculer les 5 cartes dans « Public » ferait perdre l'information utile
  (« normalement restreint »).
  **Piège du déplacement, à retenir plus généralement : sortir une
  déclaration des labels casse tout code qui la cherchait là.**
  `extract_traefik_services()` déduisait « LAN » de la présence d'un label
  frère `...ipallowlist.sourcerange` — plus de label, plus de détection, et
  les 5 cartes remontaient dans « Public ». Corrigé par
  `lan_middleware_names()`, qui relève les middlewares porteurs d'un
  `ipAllowList` dans le fichier dynamique, la voie par label restant
  acceptée. Penser à `grep` la clé de label retirée avant de conclure.
  Parseur maison (regex) et **pas PyYAML** : `generate-dashboard.py` n'a
  aucune dépendance hors stdlib, c'est ce qui a motivé sa réécriture depuis
  bash+jq — PyYAML est installé ici mais ne le serait pas forcément sur une
  installation neuve.
- **`scripts/generate-dashboard.py`** : vues dans `dashboard/templates/*.html`
  rendues via `string.Template` de la stdlib (substitution `$variable`
  uniquement, **zéro logique dans un template** — boucles, conditions et
  calculs géométriques des jauges restent en Python). CSS/JS en fichiers
  statiques sous `dashboard/assets/`, copiés vers `dashboard/html/assets/`
  comme les logos. **Zéro dépendance hors stdlib**, et `jq` a disparu du
  chemin de génération. Réécrit depuis un bash+jq qui concaténait des
  chaînes ; rester en bash avec `envsubst` avait été envisagé et écarté — les
  boucles restaient aussi pénibles, ce qui était précisément le problème.
- **Mise en page du dashboard réglée au détail près, demandée ainsi — ne pas
  « nettoyer » `.stats-flow`/`.stat-*` dans `dashboard.css` ni réorganiser
  `build_stats_section()` sans redemande.** Points de fond seuls :
  - Toutes les cartes de « Monitoring » sont **à plat dans un seul flux
    flexbox** (`.stats-flow`, `flex-wrap`, largeur fixe par `flex: 0 0 190px`)
    — pas de grille CSS, pas de sous-section titrée. Chaque carte porte son
    propre libellé.
  - Section **masquée par défaut, dépliable via un switch** ; le titre et le
    switch restent toujours visibles, seul `#monitoring-content` est masqué.
    L'état du switch (comme celui de « Tous les trackers ») vit en
    `localStorage` — indispensable, la page est régénérée par cron toutes les
    5 min et l'utilisateur devrait sinon redéplier à chaque rechargement.
  - Les jauges sont du **SVG pur** (`gauge_svg()`/`zone_gauge_svg()`), aucune
    lib JS de graphiques. Jauge de ratio : pas d'arc de remplissage, le fond
    est divisé en 3 zones de sévérité fixes sur l'échelle 0-4, l'aiguille
    pointe sa position. Seuils générique torrenting (rouge `<1`, jaune `1–2`,
    vert `≥2`), **pas** le seuil `seedRatio` d'un indexeur particulier.
  - Jauge de débit cappée sur `speed_scale` = maximum observé sur
    l'historique ~25 h (`historical_max_speed()`), plancher 1 Mo/s — pas une
    capacité de ligne figée en config : le débit VPN réel dépend du pair
    distant et de l'overhead du tunnel, une valeur figée serait fausse dès le
    premier changement de serveur.
  - Les valeurs « en erreur » de la carte Torrents sont les seules colorées
    (rouge si `>0`, sinon vert) : 0 en téléchargement n'est pas anormal,
    contrairement à une erreur. « Actifs » = `status != 0` (spec RPC),
    « surveillés » = tous les torrents présents.
  - Indexeurs Prowlarr : liste avec un point coloré **par indexeur** plutôt
    qu'un compte agrégé, pour voir directement LEQUEL est en échec sans
    changer d'écran. `/api/v1/indexer` croisé avec `/api/v1/indexerstatus`
    (vide si tout va bien).
- **Une section vide n'est pas affichée** (plus de placeholder « aucun ») ; la
  section Monitoring ne s'affiche que si `vpn/transmission-vpn` tourne, pas
  seulement si `transmission-stats.py` a réussi — évite un message d'erreur
  générique le temps qu'un conteneur redémarre.
  **En revanche une carte dont la stack est arrêtée montre un placeholder
  « Arrêté »** (`render_stat_placeholder()`, grisé, dans le même gabarit de
  span pour ne pas décaler la mise en page) plutôt que de disparaître
  silencieusement. Cible précisément ce cas : si la stack tourne mais que la
  donnée manque pour une autre raison, comportement best-effort inchangé
  (carte omise). `render_indexers_card()` prend donc `running` en argument,
  pour court-circuiter avant tout appel si `arr/prowlarr` est arrêté.
- **Le dashboard reflète l'état `unhealthy`** (`docker ps --filter
  health=unhealthy`, contour rouge `.logo-unhealthy` + avertissement) et est
  régénéré **par cron toutes les 5 min**, pas seulement par `make
  dashboard-refresh` — sinon un service devenu unhealthy resterait affiché
  comme sain arbitrairement longtemps.
- **Stats Transmission visibles WAN et LAN**, sans gating : ce sont des
  chiffres agrégés en snapshot, pas un accès de contrôle au client — voulu
  explicitement. `scripts/transmission-stats.py` sort le JSON consommé par le
  générateur. Ratio session = `current-stats` (remis à zéro à chaque
  redémarrage du daemon, donc « depuis l'uptime ») ; ratio total =
  `cumulative-stats`. `${DATA_ROOT}/.transmission-stats-history.jsonl` ne sert
  plus qu'à l'échelle des jauges de débit (rétention ~25 h).
  Un torrent multi-tracker compte dans **chaque** tracker auquel il annonce
  (impossible de départager l'upload par tracker côté RPC) : le ratio par
  tracker reste correct individuellement, mais la somme peut dépasser le
  volume réel total.
  Ratio calculé côté Transmission, peut différer du ratio compté par chaque
  tracker (leur propre comptage d'annonce fait foi). Pas de lecture directe
  des ratios de compte sur les trackers privés — Prowlarr n'expose aucune
  notion de compte, il faudrait un scraper dédié par tracker. Reporté.
- **Carte « Tâches planifiées »** : une ligne par tâche de `scripts/crontab`,
  point vert si elle a tourné avec succès il y a moins de temps que
  l'intervalle attendu de son cron, rouge sinon. Deux mécanismes de détection :
  - **Sauvegarde restic** : âge réel du dernier snapshot
    (`restic snapshots --latest 1`) — plus fiable qu'un marqueur de fin de
    script, qui ne prouve que « le script est allé au bout », pas que le
    snapshot est valide.
  - **Les autres** : chaque ligne de `scripts/crontab` écrit
    `date +\%s > __DATA_ROOT__/.cron-status/<nom>` en fin de chaîne `&&`
    (donc jamais atteint si une étape échoue) ; `cron_marker_age_seconds()`
    compare à l'intervalle de `SCHEDULED_TASKS`, codé en dur par tâche
    plutôt qu'un parseur générique d'expression cron — trop peu de tâches
    pour qu'une abstraction simplifie quoi que ce soit.
  **Ne jamais écrire un chemin, un uid ou `DATA_ROOT` en littéral dans
  `scripts/crontab`** : `make cron-install` substitue `__REPO_ROOT__`,
  `__PUID__` et `__DATA_ROOT__` depuis ce checkout et `.env.shared` (cron ne
  charge pas `.env.shared` lui-même), et le `sed` réécrit **tout** le fichier,
  commentaires compris — ne pas y épeler ces valeurs même en commentaire.
  **La composition de la liste ne dépend jamais de la disponibilité d'un
  marqueur** : une tâche sans marqueur est affichée **en rouge, pas absente**.
  En revanche une tâche liée à une **stack arrêtée est absente** — rouge
  serait un faux signal d'échec pour un arrêt volontaire. D'où un 4e élément
  dans `SCHEDULED_TASKS` : la liste des services `<project>/<service>`
  requis. Ce filtrage est un affichage, pas une garantie : le vrai fix est
  côté cron (`require-running.sh`).
- **Marge de 20 % (`CRON_MARKER_SLACK = 1.2`)** sur l'intervalle attendu de
  chaque tâche, et sur `BACKUP_MAX_AGE_DAYS` : sans marge, un marqueur comparé
  pile à l'intervalle du cron passe rouge dès que la génération tombe dans les
  dernières secondes avant le tick suivant (jitter du scheduler, ou un
  `dashboard-refresh` manuel hors cycle) alors que la tâche tourne
  normalement.
- **`scripts/require-running.sh <project>/<service> [...]`** : exit 0
  seulement si chaque service listé a un conteneur `running`. Deux usages —
  en tête de chaîne `&&` dans `scripts/crontab` (silencieux plutôt qu'un
  échec répété toutes les 5 min contre une stack arrêtée), et dans
  `scripts/backup.sh` pour rendre le dump Nextcloud **best-effort plutôt que
  fatal** : sous `set -e`, un `nextcloud` arrêté faisait échouer TOUT le
  script (aucun manifeste, aucun `restic backup`). Le `nextcloud-db.sql` d'un
  run précédent est **supprimé** du staging plutôt que laissé si le dump est
  sauté — un vieux dump re-sauvegardé comme s'il était frais serait pire
  qu'une sauvegarde manquante.
  Pas de gating équivalent sur la sauvegarde restic elle-même : c'est une
  sauvegarde globale, pas le cron d'un seul service.

## Pièges à ne pas répéter

### Un test manuel réussi ne prouve rien

Trois bugs de cette classe, tous silencieux, tous invisibles à un test à la
main. Se méfier dès qu'un comportement dépend d'un ordonnanceur, d'une file
d'attente ou d'un échappement.

- **Les écritures de configuration Servarr peuvent être ASYNCHRONES : un
  `200 OK` n'est pas une garantie, un `202 Accepted` en est l'aveu.**
  `PUT /api/v3/qualitydefinition/update` (l'endpoint qu'utilise recyclarr)
  répond **202** : l'arr met la mise à jour en file et l'applique *après*
  avoir répondu.
  ```
  12:26:17.2  recyclarr : PUT .../qualitydefinition/update -> 202 (5 ms)
  12:26:17.4  lecture   : encore les BONNES valeurs
  12:26:17.9  script    : « déjà à jour, rien à faire »
  12:27:10    lecture   : valeurs du guide en place
  ```
  Un script « lire → comparer → écrire » enchaîné juste derrière (`&&`) ne
  voit donc rien à corriger et laisse la dérive s'installer 24 h. Enchaîner
  plus serré rend même la course *plus* facile à perdre. Le piège est vicieux
  parce que le script se déclare explicitement satisfait — faux négatif
  silencieux, pas une erreur. Fix : `settle()` dans
  `apply-arr-overrides.py` (voir plus haut).
  **Ne pas « corriger » ça par un `sleep` dans `scripts/crontab`** : la
  latence de la file n'est pas connue (observée entre 0,5 s et 53 s), un délai
  figé serait soit trop court, soit du temps perdu chaque nuit. Se méfier de
  la même classe de bug pour toute comparaison avant/après sur un endpoint
  Servarr en 202.
- **Un `%` non échappé dans la partie commande d'une ligne crontab est
  interprété par cron comme un saut de ligne** — tout ce qui suit devient
  l'entrée standard de la commande. Rencontré avec `date +%s > marqueur` :
  parfait testé à la main (même avec `env -i ... /bin/sh -c` pour reproduire
  l'environnement minimal), mais silencieusement `date +` sans argument sous
  le vrai daemon. Aucune erreur visible nulle part (`MAILTO=""` supprime le
  mail, et cron ne logue que le lancement de la commande, pas sa sortie).
  Fix : `date +\%s`. Tout futur ajout dans `scripts/crontab` utilisant `%`
  doit faire pareil.
- **Un `-v` d'awk traverse le traitement des séquences d'échappement**, qui
  transforme le `date +\%s` des lignes cron en `date +%s` — elles cessent
  alors de matcher. `install-crontab.sh` passe donc son bloc de comparaison à
  `awk` **via un fichier, pas `-v`**. Même piège `%` que ci-dessus, une couche
  au-dessus.

### Cron et crontab

- **`make cron-install` ne doit jamais remplacer le crontab entier.** Il
  pipait `scripts/crontab` dans `crontab -`, qui écrase toute la table :
  n'importe quel job perso disparaissait silencieusement, sans diff ni
  avertissement. `scripts/install-crontab.sh` fusionne désormais les jobs du
  repo dans un **bloc délimité par deux commentaires marqueurs** et recopie
  verbatim tout ce qui est en dehors. Conséquences :
  - Le bloc est ajouté **en dernier**, délibérément : un `MAILTO=""` ne
    s'applique qu'aux lignes qui le *suivent*, donc le mettre à la fin limite
    ce silence aux jobs du repo au lieu d'avaler aussi le mail des jobs de
    l'utilisateur.
  - Un job perso doit vivre **hors** du bloc ; à l'intérieur il serait
    réécrit à chaque install (comportement voulu, `scripts/crontab` reste la
    source de vérité pour ce qui est managé).
  - Migration depuis les versions pré-marqueurs : les lignes du repo déjà
    présentes sans marqueur sont retirées, sinon elles seraient dupliquées.
    Deux règles dans cet ordre — toute ligne présente **verbatim** dans le
    bloc à installer (attrape aussi `MAILTO` et l'en-tête de commentaires,
    qui resterait sinon orpheline), puis toute ligne restante mentionnant le
    chemin du checkout. Les lignes retirées sont affichées, un job perso qui
    mentionnerait ce chemin étant emporté au passage.

### Réseau et Docker

- **`vpn/transmission-vpn` ne doit jamais rejoindre un second réseau
  Docker** (ex. `traefik-public`) et sa variable `LOCAL_NETWORK` ne doit
  jamais contenir son propre sous-réseau — les deux cassent le routing
  sortant du tunnel (route `redirect-gateway def1`, qui couvre
  `172.16.0.0/12`, la plage par défaut des réseaux Docker). Toujours passer
  par le sidecar `transmission-proxy` pour exposer le RPC ; pour autoriser un
  pair du même réseau Docker sans casser le routage, `UFW_ALLOW_GW_NET=true`,
  **pas** `LOCAL_NETWORK`. Détails : ISSUES.md.
- **`vpn/transmission-vpn` a besoin du module kernel `ip_tables` chargé sur
  l'hôte** — absent par défaut sur les Ubuntu récents (remplacé par
  `nftables`), nécessaire aux règles de routing/kill-switch de
  `haugene/transmission-openvpn`. Fix : `/etc/modules-load.d/ip-tables.conf`
  contenant `ip_tables`. Prérequis host, pas dans le compose file — à
  vérifier sur toute nouvelle machine.
- **Rejoindre le réseau `vpn-internal` d'une autre stack** : son vrai nom
  Docker est `vpn_vpn-internal` (préfixé par le dossier du projet compose) —
  le déclarer `external: true` avec juste `vpn-internal` échoue. Toujours
  ajouter `name: vpn_vpn-internal` sur la déclaration externe. Rejoindre ce
  réseau depuis un autre container ne pose aucun problème en soi ; seul
  `transmission-vpn` lui-même ne doit jamais toucher un second réseau.
- **Deux bind-mounts Docker séparés du même disque physique n'autorisent pas
  les hardlinks entre eux**, même si `stat` rapporte le même `st_dev` des
  deux côtés — `link()` refuse avec `Cross-device link` dès que source et
  destination sont sur deux montages distincts, peu importe que ce soit
  littéralement la même partition. `copyUsingHardlinks: true` était bien
  actif mais chaque import retombait silencieusement sur une copie complète
  (~185 Go récupérés en corrigeant). Fix : **un seul mount
  `${DATA_ROOT}:/data_root`** pour sonarr et radarr, avec remote path mapping
  (`/data/completed/` → `/data_root/.transmission/data/completed/`) et les
  root folders repointés sur `/data_root/library/...`.
  **cross-seed n'a pas de remote path mapping** — il compare tel quel le
  chemin renvoyé par le client (`/data/completed/...`) à son `dataDirs`, donc
  impossible de renommer son mount. Fix : garder `${DATA_ROOT}/.transmission
  /data:/data` et faire pointer `linkDirs` vers un **sous-dossier du même
  mount** (`/data/.cross-seed-links`).
- **Un bind-mount ne peut pas être monté sous un point de montage déjà
  `:ro`** — Docker ne peut pas créer le mountpoint dans un parent en lecture
  seule (`mkdirat ... read-only file system`). Rencontré sur `arr/cross-seed`
  (`config.js` vs volume `/links`) et sur `dashboard/` (`./assets` sous
  `./html:...:ro`) — solution : un seul mount, le script de génération copie
  les assets dans le dossier généré.
- **Un bind-mount de fichier unique reste figé sur l'ancien inode si le
  fichier hôte est remplacé plutôt que modifié en place.** Rencontré sur
  `arr/recyclarr/recyclarr.yml` : le conteneur continuait de lire l'ancien
  contenu (`docker exec ... cat` ne montrait pas le changement) alors que
  `docker inspect` confirmait le bon chemin monté — un bind-mount de fichier
  suit l'inode capturé au démarrage, pas le chemin. Un `docker restart`
  suffit. Ne pas conclure qu'un changement de config « n'a pas pris » sans
  vérifier ça d'abord ; et préférer monter le **dossier** quand le fichier
  doit pouvoir être remplacé à chaud (cf. `traefik/dynamic/`).
- **Ne jamais copier un `.example` de `docker-compose.override.yml` tel quel
  sans remplir ses placeholders** (`/path/to/...`) — Docker crée sinon
  silencieusement l'arborescence bidon correspondante **en root** sur l'hôte.
  Et comme les override réels sont gitignorés, toute évolution structurelle
  doit aussi se refléter dans le `.example`.
- **DNS du FAI qui renvoie `127.0.0.1` pour certains domaines** (blocage
  anti-piratage, ex. domaines de trackers) — se présente comme une panne
  réseau (`Connection refused`) alors que le domaine répond normalement via
  un résolveur public. Fix : forcer `dns: ${DNS_PRIMARY}/${DNS_SECONDARY}`
  sur le service concerné.
- **Traefik ne retente pas seul un certificat ACME resté en échec** (ex.
  après un DNS temporairement en NXDOMAIN) — un restart du container est
  nécessaire une fois le problème corrigé.
- Bruit connu, transitoire : à chaque redémarrage de Traefik, une quinzaine de
  `middleware "hsts@docker" does not exist` sortent pendant ~5 s, le temps que
  le provider docker livre les labels du conteneur traefik lui-même.

### Sauvegarde et restauration

- **`scripts/backup.sh` dumpait silencieusement la mauvaise base Postgres
  depuis le début** (repéré en testant `make restore` pour de vrai — jusque-là
  jamais exercé) : `pg_dump -U "$POSTGRES_USER" "${POSTGRES_DB:-$POSTGRES_USER}"`
  retombe sur `$POSTGRES_USER` (« postgres », le superuser d'amorçage) quand
  `POSTGRES_DB` n'est pas définie — et `nextcloud/.env` ne la définit jamais.
  Toutes les sauvegardes précédentes avaient donc un `nextcloud-db.sql` de 26
  lignes au lieu du vrai dump (279 tables). La base réelle s'appelle
  `nextcloud`, codée en dur sur le service `app`. Fix : fallback
  `${POSTGRES_DB:-nextcloud}`.
  **Deux leçons générales** : un chemin de restauration jamais exercé peut
  cacher ce genre de bug arbitrairement longtemps ; et un `${VAR:-default}`
  est un piège quand `VAR` n'est définie nulle part — le fallback devient le
  cas normal, silencieusement.
- **`jellyfin.db` corrompue (`SQLite Error 11: database disk image is
  malformed`)** : ne pas juste supprimer le fichier pour forcer une
  régénération — `config/config/system.xml` garde
  `IsStartupWizardCompleted=true`, donc Jellyfin se croit en mise à niveau et
  plante en boucle sur d'anciennes migrations qui supposent un schéma
  existant. Avant de reset, essayer une réparation : `sqlite3 <copie>.db
  ".recover" > dump.sql`, réimporter dans un fichier neuf,
  `PRAGMA integrity_check`/`foreign_key_check`, `REINDEX; VACUUM;` — a
  fonctionné sans perte de données malgré la corruption. Si reset complet
  malgré tout : repasser `IsStartupWizardCompleted` à `false`. Le dossier
  `data/SQLiteBackups/` était vide au moment de l'incident — vérifier de temps
  en temps qu'il se remplit réellement.

### Sonarr / Radarr / Prowlarr

- **Sonarr/Radarr n'importent pas les fichiers vidéo posés en vrac à la racine
  d'un dossier scanné** — ils ne reconnaissent que la convention
  un-film/une-série par sous-dossier, sans erreur ni log pour les fichiers
  ignorés. Utiliser **Manual Import**, pas le scan automatique.
- **Supprimer un episodefile via l'API déclenche quasi instantanément la
  recherche automatique interne de Sonarr** pour l'épisode redevenu
  « manquant » : dans la fenêtre de quelques secondes entre la suppression et
  un grab manuel visant une release précise, Sonarr a grabé de lui-même autre
  chose (mieux scorée sur le profil de la série). Supprimer un fichier pour
  forcer un remplacement laisse donc une fenêtre de course. Pas de parade
  fiable identifiée.
- **Le cache de `GET /api/v3/release?episodeId=...` expire vite** : un `guid`
  récupéré par un GET précédent peut déjà être invalide au `POST` (grab), avec
  `"Couldn't find requested release in cache, try searching again"` — a
  fortiori si une recherche automatique s'est intercalée. Le grab manuel doit
  suivre la recherche dans la foulée, sans appel intermédiaire.
- **Le paramètre `seriesId` de `GET /api/v3/history` n'est pas fiable** : la
  réponse contient des entrées d'autres séries malgré le filtre. Toujours
  filtrer côté client sur `record["seriesId"]`. Voir aussi `eventType=1`
  (entier) plutôt que la chaîne `"grabbed"`, qui renvoie une réponse vide.
- **`GET /api/v3/wanted/cutoff` ne reflète que le cutoff *qualité*, pas
  `cutoffFormatScore`** — un épisode à la bonne qualité mais sous le score n'y
  apparaît pas, alors qu'il reste éligible à l'upgrade. Ne pas s'en servir
  pour estimer l'ampleur d'une vague de re-recherche : recalculer les scores
  fichier par fichier (`GET /api/v3/episodefile`, champ `customFormats`,
  croisé avec les `formatItems` du profil cible), ou vérifier au cas par cas
  avec `GET /api/v3/release`, dont les `rejections` mentionnent explicitement
  `Existing file meets cutoff`.
- **Avant tout changement d'un custom format par l'API, vérifier qu'il n'est
  pas géré par recyclarr** : recyclarr resynchronise la **définition** du CF,
  pas seulement son score, donc un `PUT` est appliqué puis réécrit au sync
  suivant. Repéré uniquement parce que `recyclarr sync --preview` a été
  relancé *après* le PUT. Réflexe : `grep` le `trash_id` dans
  `recyclarr.yml`, et relancer `--preview` après coup.
  Solution retenue dans ce cas plutôt que de sortir le CF de `recyclarr.yml`
  (ce qui aurait fait perdre sa création automatique sur un déploiement neuf,
  donc un recul de reproductibilité) : **un CF distinct que nous possédons**
  (`VOSTFR (hors suffixe)`), le CF du guide restant intact et scoré **0** sur
  les profils concernés, le nôtre reprenant son score.

### La boucle de regrab infini (`cutoffFormatScore` + regex)

Le piège le plus coûteux du repo, diagnostiqué en trois passes. À lire en
entier avant de toucher à un score ou à une regex de custom format.

- **Mécanisme** : les CF `VOSTFR`/`SUBFRENCH`/`FRENCH`
  (`ReleaseTitleSpecification`) matchaient le suffixe entre parenthèses que
  certains groupes ajoutent au **titre du post** (`(VF, FRENCH, SUBFRENCH,
  VOSTFR, ...)`) — suffixe **absent du nom réel du fichier `.mkv`**. Au grab
  la release score haut, après import le fichier réévalué score plus bas → la
  release déjà possédée a l'air d'être un upgrade → regrab → réimport → même
  écart → boucle. Confirmé via l'historique : littéralement le même
  magnet/infoHash grabé 3 à 5 fois.
  **Le moteur de la boucle est l'asymétrie grab/fichier**, pas le plafond.
  Abaisser `cutoffFormatScore` seul ne suffit pas : tant que le cutoff reste
  au-dessus du maximum qu'un *fichier* peut atteindre, la porte de l'upgrade
  ne se referme jamais.
- **Un `cutoffFormatScore` n'a de sens que s'il est (a) atteignable et (b)
  posé là où l'objectif du profil est rempli.** Le défaut des guides TRaSH
  (10000) ne l'est jamais, et un calcul naïf par somme des CF positifs ne
  l'est pas non plus : beaucoup sont **mutuellement exclusifs** (un seul tier,
  une seule plateforme de streaming, un seul codec, un seul format audio, une
  seule résolution, un seul niveau de repack — les specs `negate` du guide les
  enchaînent). Pour un profil à CF déterminant, caler sur ce CF ; pour un
  profil généraliste, sur le haut de la distribution **réellement observée**.
  Ces distributions sont **bimodales** selon qu'une release porte ou non un
  tag de groupe « Tier » (+1600 à 1700), largement absent des trackers FR :
  viser le maximum théorique maintiendrait des recherches perpétuelles (donc
  du quota indexeur) pour la majorité des titres.
  Pour un profil géré par recyclarr, passer par `upgrade.until_score` dans
  `recyclarr.yml`, pas par l'API — un appel API serait écrasé au sync.
  **Piège** : dès qu'un profil fournit une liste `qualities:` explicite,
  recyclarr **exige** `until_quality` en plus de `until_score`, sinon le sync
  échoue en validation.
- **Exclure un terme situé dans un groupe parenthésé demande DEUX assertions**,
  et c'est le cœur du correctif dans `arr/profiles/sonarr-anime.json` :
  - lookahead `(?![^()]*\))` — attrape le terme placé **après** un groupe
    imbriqué ;
  - lookbehind `(?<!\([^)]*)` — attrape le terme placé **avant**, le cas
    réellement rencontré.
  Le lookahead seul est contourné par des parenthèses imbriquées : dans
  `(VF, FRENCH, VOSTFR, Koukaku Kidoutai (2026), ...)`, le `(` de `(2026)`
  arrive avant la première `)`, `[^()]*\)` échoue, l'assertion négative
  réussit — et le terme matche alors qu'il est bien dans le suffixe.
  **.NET accepte un lookbehind de longueur variable, Python `re` non** —
  utiliser le module `regex` pour tout test hors Sonarr.
  Méthode de validation à reprendre : calculer la vérité terrain par
  **comptage réel de profondeur de parenthèses**, pas par une autre regex, sur
  les titres réels de `/api/v3/history` + `/api/v3/episodefile` ; puis croiser
  .NET (`/api/v3/parse`, via des CF jetables supprimés après) et Python
  `regex` en exigeant 0 désaccord.
  Deux limites connues laissées en l'état : un terme entouré de groupes fermés
  des **deux** côtés dans les mêmes parenthèses passe encore (aucun schéma de
  nommage réel ne fait ça), et un titre à parenthèses non appariées ne matche
  pas (déjà vrai avant).
- Trou connu, délibéré : `WEB-2160p (Combined)` a `VOSTFR` à +100 et est géré
  par recyclarr, mais le suffixe est une pratique de groupes d'anime 1080p qui
  ne croise pas ce profil 2160p — à revoir si un regrab en boucle y apparaît.

### cross-seed

- **Connect « Custom Script », pas « Webhook »** — le type Webhook générique
  de Sonarr/Radarr envoie un payload de test factice (pas de vrai hash) au
  moment d'enregistrer la connexion ; cross-seed le rejette (`A valid
  infoHash or an accessible path must be provided`), ce qui **empêche
  l'enregistrement** de la connexion (échec bloquant, pas un warning). La
  méthode documentée est un Custom Script
  (`arr/scripts/cross-seed-notify.sh`) qui lit
  `$sonarr_download_id`/`$radarr_download_id` et appelle l'API lui-même.
- **`useClientTorrents: true` requis dans `arr/cross-seed/config.js`** (faux
  par défaut) — sans ça le webhook ne consulte jamais le client réel pour
  matcher l'infoHash reçu et échoue systématiquement (`Torrent client does
  not have any torrent with criteria`), même quand le torrent y est bien
  présent. Le job périodique « inject » ne rattrape pas ces échecs non plus.
- **Les URLs Torznab Prowlarr sont par ID d'indexeur
  (`http://prowlarr:9696/<id>/api`), et ces IDs ne sont pas stables.** La
  config a pointé 4 jours sur un indexeur supprimé : chaque webhook
  déclenchait bien une recherche, mais contre un indexeur mort (`410 Gone`),
  donc 0 injection — jamais d'erreur bloquante, juste un `[webhook] Found 0
  torrents` systématique. Si un indexeur est ajouté/supprimé/recréé dans
  Prowlarr, vérifier les IDs actuels et les refléter dans
  `CROSS_SEED_INDEXER_IDS` (`arr/.env`, pas en dur dans `config.js` : quels
  indexeurs existent et dans quel ordre est propre à ce déploiement). **Rien
  ne prévient d'un ID devenu obsolète autrement qu'en lisant les logs
  cross-seed.**
  Nyaa.si est exclu de cette liste : cross-seeder un torrent déjà obtenu d'un
  tracker à ratio vers un tracker public n'apporte aucun bénéfice (seul
  l'inverse en a un), et le retrait ne coupe que cette direction — un fichier
  d'origine Nyaa.si continue d'être cross-seedé vers les indexeurs à ratio.
- **`searchCadence` impose deux contraintes de validation** non documentées
  ailleurs que dans l'erreur elle-même, et une config invalide fait boucler le
  conteneur en crash (`restart: unless-stopped`) : `excludeRecentSearch` doit
  valoir au moins 3× `searchCadence`, et `excludeOlder` de 2 à 5×
  `excludeRecentSearch`. Valeurs retenues : 3 j / 9 j / 30 j — la recherche
  périodique ne couvre donc **que les torrents vus il y a moins de 30 jours**.
  Le rattrapage complet se fait à la demande via `docker exec <container>
  cross-seed search --exclude-older 999999999 --exclude-recent-search 0`,
  **jamais automatisé** (potentiellement des centaines de requêtes d'un coup).

### Seerr

- **`seerr` ne chown pas lui-même son volume `/app/config`** — contrairement
  aux images linuxserver.io, il tourne nativement en UID 1000 sans étape
  root-puis-drop, donc si `${DATA_ROOT}/.seerr/config` n'existe pas encore,
  Docker le crée en `root:root` et le container crash en boucle (`EACCES`).
  Avant le premier `make up STACK=seerr` :
  `sudo chown -R 1000:1000 ${DATA_ROOT}/.seerr`.
- **Seerr ne détecte les films/séries déjà téléchargés qu'en scannant les
  bibliothèques Jellyfin**, pas en interrogeant Sonarr/Radarr pour l'existant.
  Sans bibliothèque Jellyfin pointant sur `${DATA_ROOT}/library`, tout
  apparaît comme non disponible et Seerr propose de re-demander du contenu
  déjà présent. Après ajout, lancer manuellement « Jellyfin Full Library
  Scan » au lieu d'attendre le cron.

### Résolution des trackers (`core.py` + `transmission-stats.py`)

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
- **Nyaa.si n'a aucun tracker qui lui soit propre**, seulement des trackers
  publics génériques — aucune info Prowlarr ne permet de les rattacher. Ils
  sont donc déclarés en alias dans `TRACKER_ALIASES` (`arr/.env`, pas en dur
  dans le code : quels trackers publics un indexeur embarque est propre à ce
  déploiement) et matchés **en exact, pas via `base_domain()`** —
  `tracker.torrent.eu.org` réduirait à `eu.org`, un domaine public partagé par
  d'innombrables sites sans rapport. Faux positif accepté en connaissance de
  cause : un torrent d'un autre indexeur qui ajouterait l'un de ces trackers
  en secours serait étiqueté « Nyaa.si » à tort.
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

## Repo

```
server/
├── .env.shared(.example)     # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT/LAN_CIDR/DNS_* — réel gitignoré
├── Makefile                  # `make help` (cible par défaut) liste tout — voir plus bas
├── README.md                  # doc humaine : services, install
├── ARCHITECTURE.md            # doc humaine : architecture, choix structurants
├── ISSUES.md                  # doc humaine : problèmes rencontrés
├── scripts/
│   ├── crontab                    # source de vérité des crons DU REPO — `make cron-install`
│   ├── install-crontab.sh          # fusionne scripts/crontab dans un bloc marqué, préserve les jobs perso
│   ├── logrotate.conf              # rendu sous DATA_ROOT par `make cron-install` (logrotate n'expand aucune variable)
│   ├── lan-only-middleware.sh      # ouvre/referme les services LAN-only au WAN — `make switch-lan-only-middleware` + garde cron `rearm`
│   ├── backup.sh                   # sauvegarde restic hebdomadaire
│   ├── restore.sh                  # restauration guidée d'un snapshot restic
│   ├── generate-dashboard.py       # régénère dashboard/html/ — `make dashboard-refresh`
│   ├── transmission-stats.py       # JSON ratios/débits pour generate-dashboard.py
│   ├── provision.py                # config d'installation : clés API, biblios Jellyfin, objets arr, Seerr — `make api-keys` / `make provision`
│   ├── apply-arr-overrides.py      # déclaratif : profils qualité, config anime, connexions Jellyfin, metadata writer, ratio indexeurs publics — `make arr-overrides`
│   └── require-running.sh          # exit 0 si les services <project>/<service> donnés tournent — guard cron + backup.sh
├── sauvegarde/                # non versionné — dépôt restic + staging (le mot de passe vit hors du repo)
├── traefik/                  # socket-proxy + traefik + dashboard ; dynamic/ (middlewares LAN-only) ; .env(ACME_EMAIL)/.example
├── jellyfin/                 # docker-compose.yml + override.yml(.example) pour les bibliothèques ; .env (identifiants admin)
├── nextcloud/                 # db-next/app/web/news-updater ; .env/.example ; override.yml(.example)
├── vpn/                       # transmission-vpn (réseau isolé) + sidecar transmission-proxy ; .env/.example
├── arr/                       # prowlarr/sonarr/radarr/cross-seed/recyclarr/clearr ; .env/.example ; override.yml.example (optionnel)
│   ├── clearr/                 # web (FastAPI/Jinja2/Bootstrap) + TUI + CLI delete-by-inode, un seul core.py partagé
│   └── profiles/               # config arr custom versionnée (sonarr-anime.json) — appliquée par apply-arr-overrides.py
├── kodi/                      # addon de menu contextuel « Supprimer avec clearr » — installé côté client par `make kodi-install`
├── seerr/                     # recherche/requête unifiée ; pas de .env (config via son assistant web + provision.py)
└── dashboard/                 # templates/ (string.Template) + assets (logos, css, js) + html/ généré — servi par traefik/
```

`make network` (crée `traefik-public` si absent) avant tout `make up`.

**`make help` est la cible par défaut**, et la liste est **générée depuis les
annotations `## …` des cibles elles-mêmes** — une liste maintenue à côté
diverge dès qu'on ajoute une cible sans y penser. Convention pour toute
nouvelle cible : `cible: ## ARG=<valeur> — description`, l'annotation sur la
ligne de la *définition*. Pour une cible précédée d'une ligne
`cible: STACK := arr`, elle va sur la ligne `cible: network` — un `#` dans une
affectation de variable make serait avalé comme commentaire et fausserait la
valeur. Ne pas y écrire `$${DOMAIN}` ni d'autre échappement make : l'aide est
produite par un `grep` sur le fichier brut, le texte s'affiche littéralement.

**Rotation des logs** : `scripts/logrotate.conf` couvre les logs de cron du
repo et l'access log Traefik, lancé par cron **sans root** avec son propre
fichier d'état sous `DATA_ROOT` (le logrotate système n'a pas à connaître ce
checkout). Il pointe la config **rendue** sous `DATA_ROOT`, pas le fichier du
repo : logrotate n'expand aucune variable, ses chemins doivent être littéraux,
et c'est `make cron-install` qui fait la substitution. `.gitignore` couvre les
noms tournés (`*.log.*`), pas seulement les noms de base.

## Sauvegarde — repères rapides

- `make backup` (aussi via cron dimanche 3h) : dump `pg_dump` Nextcloud +
  manifeste des digests d'images en cours d'exécution + `restic backup` +
  `restic check --read-data-subset=5%` + `restic forget --group-by host
  --keep-weekly 8 --prune` + tag git `backup-YYYY-MM-DD` si l'infra a changé
  depuis le dernier tag de ce type, poussé sur `origin`.
  Le dump est **supprimé du staging après le backup** : il porte les hachages
  de mots de passe et les jetons d'application de Nextcloud, il n'a pas à
  rester en clair sur le disque une fois dans le dépôt chiffré.
  **`--group-by host` n'est pas cosmétique** : `restic forget` groupe par
  défaut sur (host, paths) et applique la politique à chaque groupe
  *séparément*, donc chaque changement de la liste de chemins crée un nouveau
  groupe et l'ancien cesse d'être élagué — il l'a déjà fait, en épinglant
  ~10 GiB. Pire que l'espace perdu : juste après un tel changement, la
  rétention réelle retombe à un seul snapshot alors que le message annonce
  toujours 8 semaines.
- `make restore SNAPSHOT=<id|latest>` : restaure dans un dossier à part et
  affiche les étapes manuelles — ne touche jamais le live automatiquement.
- **Mot de passe restic dans `~/.config/server-restic-password`** (hors du
  repo, généré au premier `make backup`) — pas de copie ailleurs = dépôt
  illisible en cas de perte. `sauvegarde/restic-password` est l'ancien
  emplacement : `backup.sh`/`restore.sh` s'y replient encore
  (`LEGACY_PASSWORD_FILE`), mais toute commande `restic` lancée à la main doit
  viser le nouveau chemin, sinon `Resolving password failed`.
- Les `.env` de chaque stack sont collectés **par boucle sur `$STACKS`**, pas
  listés à la main : un `.env` ajouté plus tard (c'est arrivé avec
  `jellyfin/.env`, qui porte les identifiants dont `make provision` a besoin)
  n'existerait sinon **nulle part ailleurs que sur ce disque** — gitignoré et
  non sauvegardé. La boucle rend le prochain couvert par construction.
- Délibérément **pas** sauvegardés : `library` (media, re-téléchargeable via
  arr), `.transmission/data`, `.jellyfin/cache` — énormes et jetables.
- Résilience visée : perte du disque `DATA_ROOT` → restauration depuis
  `sauvegarde/` (sur un disque différent). Perte de celui-ci → seul
  l'infra-as-code est récupérable depuis GitHub, la sauvegarde restic est
  perdue avec (accepté pour l'instant, pas d'offsite).

Détails, rationale et guide d'installation complet : `README.md` /
`ARCHITECTURE.md` / `ISSUES.md`.
