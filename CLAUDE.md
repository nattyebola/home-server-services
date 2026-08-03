# server/ — instructions pour Claude

Infra as code de services home server (Docker Compose + Traefik + Makefile).
Guide d'installation : **voir `README.md`**. Description des services et
choix d'architecture expliqués : **voir `ARCHITECTURE.md`**. Problèmes
rencontrés : **voir `ISSUES.md`**. Ces trois fichiers sont destinés aux
humains. Ce fichier ne garde que ce qui sert à retravailler sur ce repo
sans relitiger des décisions déjà prises ou répéter des pièges déjà
rencontrés.

## Décisions à respecter

Ne pas proposer de revenir dessus sans que l'utilisateur le redemande
explicitement :

- **`scripts/torrent-cleanup.py` (ancien `make cleanup`) remplacé par le
  service `clearr`** (`arr/clearr/`, ajouté le 2026-07-31) : nettoyage
  manuel torrents/bibliothèque accessible en web LAN-only
  (`clearr.${DOMAIN}`, middleware `arr-lan-only`) en plus de la TUI
  d'origine (`make clearr`), toutes deux appuyées sur le même
  `arr/clearr/app/core.py` — plus aucune duplication de la logique de
  `webapp.py` (FastAPI + Jinja2 + Bootstrap 5, voir plus bas) et
  `tui.py`/`cli.py` (curses/`delete-by-inode`, comportement inchangé)
  importent tous `core.py`, jamais l'inverse.
  Toute référence plus bas à `scripts/torrent-cleanup.py`/`make cleanup`
  (nombreuses, décisions/pièges documentés avant ce refactor) reste valable
  sur le fond — juste relire "torrent-cleanup.py" comme
  "`arr/clearr/app/core.py`" et "`make cleanup`" comme "`clearr`" (web ou
  TUI selon le contexte). Pas de réécriture de ces entrées une par une :
  elles documentent des pièges/décisions déjà pris, le déplacement de
  fichier ne les invalide pas.
  Changement de transport, pas seulement d'emplacement : la TUI tournait
  sur l'hôte et atteignait Transmission/Sonarr/Radarr/Prowlarr via `docker
  exec <container> curl ...` (seule option depuis l'hôte, `vpn-internal`
  étant un réseau Docker isolé) — `clearr` tourne maintenant lui-même en
  conteneur, rejoint directement `vpn-internal` (externe,
  `vpn_vpn-internal`) et le réseau `default` d'arr, et parle en HTTP direct
  aux noms de service (`transmission-vpn:9091`, `sonarr:8989`,
  `radarr:7878`, `prowlarr:9696`) — jamais de socket Docker monté dans le
  conteneur (aurait cassé le modèle rootless/`cap_drop: ALL`, un accès au
  socket Docker est root-équivalent sur l'hôte, pire que n'importe quel
  `cap_add` déjà accepté dans ce repo). `host_to_arr_path()`/
  `arr_path_to_host()` de l'ancien script ont disparu : `clearr` monte
  `${DATA_ROOT}:/data_root` en lecture-écriture, exactement comme
  sonarr/radarr (même mount unique, même raison hardlink — voir plus bas),
  donc core.py et arr partagent déjà le même référentiel de chemins, plus
  besoin de traduire.
  1er service du repo avec des dépendances Python tierces (`fastapi`,
  `uvicorn`, `jinja2`, `python-multipart` — `arr/clearr/requirements.txt`)
  et 2e avec un build custom après `nextcloud/app`/`nextcloud/web` (`build:
  ./clearr`, pas d'`image:` fixée, `python:3-slim` — tag flottant, cohérent
  avec la philosophie "toujours la dernière version"). `make update
  STACK=arr` doit donc aussi rebuilder (conditionnel étendu à `arr` en plus
  de `nextcloud` dans le target `update`).
  Frontend FastAPI + Jinja2, pas de HTMX vendoré malgré la discussion
  initiale avec l'utilisateur (qui avait validé "FastAPI + HTMX") :
  `static/clearr.js` reste un petit JS maison (data-get/data-post/
  data-live pour la navigation par fragments) plutôt qu'une lib tierce
  récupérée telle quelle. Même modèle de fond que prévu : chaque
  action recharge et rerend un fragment HTML calculé from scratch côté
  serveur (`core.load_full_state()` à chaque requête, jamais de cache ni
  d'état en mémoire entre deux requêtes — contrairement à la TUI qui ne
  recharge qu'au démarrage) ; mesuré acceptable (repris du diagnostic fait
  avant implémentation) à l'échelle de cette bibliothèque, à revoir si ça
  dérive. Erreur réseau (ex. `vpn/transmission-vpn` arrêté) rendue comme un
  bandeau lisible (`@app.exception_handler(RuntimeError)`) plutôt qu'un 500
  brut — la TUI avait déjà ce filet au niveau de `run()`, le web ne l'avait
  pas nativement (chaque route peut lever indépendamment), piège rencontré
  en testant l'image sans réseau réel derrière.
  **Bootstrap 5.3.3 choisi comme framework CSS** (2026-07-31, après un
  comparatif Bootstrap/Bulma/Pico.css présenté en artifact à l'utilisateur —
  Bootstrap retenu car l'écran le plus dense de clearr, une table avec
  badges/actions par ligne + modal, est justement le terrain où ses
  composants prêts à l'emploi rapportent le plus). `bootstrap.min.css`/
  `bootstrap.min.js` (pas le bundle — Popper n'est utile qu'aux dropdowns/
  tooltips/popovers, aucun utilisé ici) vendorés dans `static/`, récupérés
  via `curl` sur `cdn.jsdelivr.net` (pas `WebFetch`, qui peut reformater du
  contenu non-HTML — `curl` donne les octets exacts, intégrité vérifiée par
  un second téléchargement + comparaison sha256 avant de committer). Table
  resserrée (`table-sm` + padding réduit dans `clearr.css`) — compacité
  demandée explicitement. Tri (`core.sort_items`, appelé sur les torrents
  bruts avant mise en forme dans `torrent_view()`) vérifié explicitement
  par un test sur des données qui feraient échouer un tri fait sur la
  chaîne affichée plutôt que la valeur brute (tailles 900Mo/1.5Go/4.0Go,
  âges 30j/3mois/5j — un tri alphabétique sur ces chaînes donne un ordre
  différent de l'ordre numérique réel).
  Web : suppression toujours confirmée par une modale — le composant Modal
  natif de Bootstrap (`bootstrap.Modal.getOrCreateInstance()`/`.show()`/
  `.hide()` dans `clearr.js`, bouton Annuler en `data-bs-dismiss="modal"`)
  plutôt que le `<dialog>` natif fait main de la première version : focus
  trap/Échap/clic-sur-le-fond déjà corrects dans son JS, les réimplémenter
  aurait été strictement moins bien. Pas d'équivalent au raccourci `D` de
  la TUI, jugé pas nécessaire pour une UI à la souris.
  Clic sur un en-tête de colonne trie dessus (ascendant), reclique inverse
  le sens — remplace les raccourcis séparés `s`/`S` de la TUI, plus direct
  au clic. Groupes cross-seed : plus de `<details>`/`<summary>` (incompatible
  avec de vraies lignes de `<table>` Bootstrap, un `<details>` ne peut pas
  entourer des `<tr>`) — remplacés par le composant Collapse de Bootstrap
  sur des `<tr class="collapse">`, un bouton `data-bs-toggle="collapse"
  data-bs-target=".xseed-<id>"` ciblant une classe (partagée par toutes les
  lignes enfants du groupe) plutôt qu'un id unique. Piège : l'animation de
  hauteur de Bootstrap est pensée pour des blocs, pas des `tr`
  (`display:table-row`) — sans `tr.collapsing { transition: none }`
  (`clearr.css`) l'ouverture/fermeture d'un groupe clignote au lieu d'un
  show/hide net ; comportement documenté de Bootstrap sur les tableaux, pas
  un bug de leur côté.
  Bootstrap 5.3 ne suit pas `prefers-color-scheme` tout seul — un script en
  tête de `page.html` pose `data-bs-theme` selon `matchMedia("(prefers-
  color-scheme: dark)")` avant le rendu du `<body>` pour éviter un flash
  clair→sombre.

- **Jaquette au survol + liens IMDb/TVDB/TMDB/Sonarr/Radarr dans les 3 vues de
  `clearr`, sans aucun appel WAN côté serveur** (ajouté le 2026-08-03, contrainte
  demandée explicitement) : les ids externes (`imdbId`/`tvdbId`/`tmdbId`/
  `titleSlug`) sont déjà dans les objets `/api/v3/series`|`/movie` (vérifié :
  0 manquant sur 21 séries / 24 films), et les jaquettes sont déjà en cache
  disque sous `${DATA_ROOT}/.arr/{sonarr,radarr}/config/MediaCover/<id>/
  poster-250.jpg` (~20 Ko), donc lisibles directement par `clearr` via son mount
  `${DATA_ROOT}:/data_root` — servies par une route `/poster/{kind}/{arr_id}`
  (`core.poster_file`, `arr_id` passé par `int()` = la garantie anti-traversal)
  plutôt que proxifiées vers l'API arr, encore moins re-téléchargées chez
  thetvdb/tmdb. Seuls les liens sortent, et c'est le navigateur qui les suit au
  clic. TVDB adressé par `thetvdb.com/dereferrer/series/<tvdbId>` (Sonarr expose
  l'id, pas le slug du site) ; Radarr par `<tmdbId>` (son `titleSlug` EST le
  tmdbId).
  Vue Torrents : un torrent n'est rattaché à un titre arr que via
  `core.build_arr_meta_index()`/`torrent_meta()` (films par chemin exact de
  `movieFile.path`, séries par préfixe de `series.path`, mêmes critères que
  `plan_radarr_deletion`/`plan_sonarr_unmonitor`), en repartant des inodes déjà
  calculés par `analyze_torrent_files()` — donc zéro `stat` supplémentaire et
  symlinks cross-seed déjà résolus. Coût mesuré : +2 appels arr par rendu,
  onglet Torrents toujours à ~50 ms sur 223 torrents. Best-effort : un torrent
  jamais importé n'a ni jaquette ni lien (39 sur 168 parents au moment de
  l'ajout), un arr injoignable dégrade la vue sans la casser.
  Jaquette chargée seulement au premier survol (`data-poster` porte l'URL, pas
  un `<img>` dans le HTML — sinon 45 images à chaque rendu de page alors qu'on
  en regarde une), affichée dans un unique conteneur flottant attaché au
  `<body>` : les lignes vivent dans `.table-responsive`, dont l'`overflow`
  découperait une vignette positionnée dans le tableau. Ancrée sur la cellule
  survolée et pas sur le curseur (pas de vignette qui suit la souris), `z-index`
  sous celui de la modale Bootstrap. Pas de tooltip Bootstrap ici non plus
  (exigerait Popper, non vendoré — voir plus haut).
  Refacto au passage : `render_series_tab`/`render_films_tab` (squelettes
  identiques) fusionnées en `render_arr_tab(tab, ...)` + un `ARR_TABS` de 3
  valeurs par onglet, et la cellule titre des 3 vues passe par un seul macro
  Jinja `templates/_meta.html` — sans quoi le même bloc jaquette/liens aurait
  été écrit trois fois. `core.find_series_by_id`/`find_movie_by_id` remplacent
  4 `next((... for ... if id == ...))` recopiés dans les routes.
  Bouton de suppression réduit à **une croix ✕ (U+2715) dans les 3 vues**
  (demandé le 2026-08-03, le sens passant par `title`/`aria-label`) — un emoji
  (🗑, premier essai le même jour) a été rejeté par l'utilisateur : rendu par la
  police couleur du système, donc ni monochrome ni cohérent d'un appareil à
  l'autre, là où un glyphe texte hérite du rouge de `.btn-outline-danger`.
  Et **colonne d'actions
  figée à droite dans la vue Torrents** (`.table-sticky-actions`, `position:
  sticky` sur le `:last-child`) : c'est la seule vue assez large pour que
  `.table-responsive` défile horizontalement dès qu'on zoome, le bouton sortait
  alors de l'écran. `background-color` explicite obligatoire sur la cellule
  figée (`--bs-table-bg` est transparent, les cellules qui défilent dessous
  resteraient visibles au travers) — le surlignage de survol de Bootstrap passe
  par un `box-shadow` inset, donc peint par-dessus ce fond et continue de
  marcher. Aucune bordure/ombre sur le bord gauche de cette colonne (essayée
  puis retirée le même jour, demandé explicitement : visible en permanence, y
  compris quand rien ne défile).
  `DOMAIN` (nécessaire aux liens `sonarr.${DOMAIN}`/`radarr.${DOMAIN}`) vient de
  `.env.shared`, donc hors du `env_file: .env` du service : injecté par un bloc
  `environment:` dans `arr/docker-compose.yml`. Absent = pas de lien arr, le
  reste fonctionne.

- **Rootless par container**, pas de daemon Docker rootless. `cap_drop:
  ALL` + `security_opt: no-new-privileges:true` partout ; `cap_add` ciblé
  seulement sur `db-next` et `vpn/transmission-vpn` (démarrent root puis
  descendent en privilège), justifié en commentaire dans leur compose
  file — ne pas en ajouter ailleurs sans le même genre de nécessité.
- **Images toujours en `:latest`** (jamais de tag figé) — voulu
  explicitement, l'utilisateur accepte le risque de casse pour avoir
  toujours les dernières versions. La reproductibilité d'une restauration
  passe par le manifeste de digests capturé à chaque `make backup`
  (`scripts/backup.sh`), pas par des tags fixes dans les compose files.
- **Secrets et valeurs propres au déploiement** : `.env` par stack
  (gitignoré) + `.env.example` versionné — même chose pour les valeurs
  partagées entre stacks (`PUID`/`PGID`/`RENDER_GID`/`DOMAIN`/
  `DATA_ROOT`/`LAN_CIDR`/`DNS_PRIMARY`/`DNS_SECONDARY`) dans
  `.env.shared`/`.env.shared.example` à la racine (`.env.shared` gitignoré
  depuis le 2026-07-18, il identifiait ce déploiement — domaine, chemins).
  `LAN_CIDR` (ajouté le 2026-07-23) alimente le middleware Traefik
  `ipallowlist.sourcerange` dans `vpn/` et `arr/` (plus dans
  `traefik/docker-compose.yml` depuis le 2026-07-24, voir dashboard
  ci-dessous) — ne jamais remettre ce CIDR en dur dans un compose file,
  toujours `${LAN_CIDR}`. `DNS_PRIMARY`/`DNS_SECONDARY`
  (ajoutés le 2026-07-23, mêmes valeurs Cloudflare par défaut qu'avant —
  pas un secret, juste une valeur dupliquée à ne pas refaire diverger)
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
- **Nextcloud** : image communautaire (pas AIO — incompatible avec
  rootless/infra-as-code, cf. ARCHITECTURE.md).
- **Hôte Linux natif requis, pas de support Windows/WSL2** (évalué le
  2026-07-23, cf. ISSUES.md section "Windows / WSL2") : noyau WSL2 sans
  chargement de module (casse le fix `ip_tables`), NAT cassant la
  joignabilité 80/443 pour Let's Encrypt, VM non persistante (casse le
  cron de backup), hardlinks Sonarr/Radarr cassés sur un disque Windows
  monté en drvfs, pas de passthrough VAAPI pour Jellyfin. Ne pas proposer
  WSL2 comme option de déploiement sans que ces points soient résolus.
- **Seerr (`seerr/`, image `ghcr.io/seerr-team/seerr`)** pour la recherche/
  requête unifiée, pas Jellyseerr/Overseerr — les deux projets ont fusionné
  dans Seerr et sont dépréciés depuis (voir docs.seerr.dev). Ne pas proposer
  de revenir sur l'ancienne image.
- **Timezone de tous les containers alignée sur l'hôte** via bind-mount
  `/etc/localtime:/etc/localtime:ro` (déjà en place sur `vpn/transmission-vpn`
  depuis le début, généralisé à tous les services le 2026-07-23) — préféré à
  une variable d'env `TZ` : ne dépend pas d'un paquet `tzdata` présent dans
  chaque image et suit automatiquement les changements d'heure d'été/hiver
  de l'hôte. Ajouter ce montage à tout nouveau service plutôt que `TZ=...`.
- **Repo public** sur GitHub (`nattyebola/home-server-services`, remote
  `origin` via deploy key dédiée `~/.ssh/id_ed25519_server_backup` / alias
  SSH `github-server-backup`, pas la clé perso de l'utilisateur). Ne
  jamais committer un secret ou une info identifiante en dur (email,
  domaine, chemin perso...) dans un fichier versionné — toujours via
  `.env`/`.env.shared` (gitignorés) + leur `.example` (placeholders
  génériques). Le vrai username Unix de la machine reste en clair dans les
  `docker-compose.override.yml` gitignorés (pas versionnés, donc pas
  concernés) — décision explicite de l'utilisateur. Ne pas l'écrire en
  clair ici pour autant (ce fichier-ci est versionné) : voir `whoami`/`$USER`
  sur la machine si besoin de le retrouver.
- **`scripts/torrent-cleanup.py` (`make cleanup`)** : TUI maison pour
  supprimer un torrent + ses fichiers Transmission + les fichiers
  `library/` correspondants en une seule action. Écrit sur mesure plutôt
  que d'ajouter un service tiers (Decluttarr, Removarr...) : aucun ne
  couvre ce cas précis (suppression Sonarr/Radarr → nettoyage automatique
  du client torrent), c'est un trou connu et non résolu de l'écosystème
  *arr (vérifié le 2026-07-23, cf. issue GitHub ManiMatter/decluttarr#292).
  Le matching bibliothèque se fait par inode (device+inode), donc ne
  fonctionne que grâce au fix hardlink ci-dessus — sans lui, `library/` et
  `.transmission/data/` étaient des copies distinctes, pas des liens.
  Synchronise aussi Sonarr/Radarr (ajouté le 2026-07-23) pour éviter qu'un
  titre encore monitored soit re-téléchargé à la prochaine recherche : film
  Radarr → retiré complètement (+ import exclusion) ; épisode/saison
  Sonarr → saison désactivée seulement si elle est *terminée* (aucun
  épisode à venir, `totalEpisodeCount == episodeCount` côté API) et
  entièrement supprimée, sinon seuls les épisodes concernés sont
  désactivés — pour ne jamais couper le monitoring d'une saison en cours
  de diffusion. Matching par chemin (`movieFile.path`/`episodefile.path`
  vus par les conteneurs arr, donc traduits via le même montage
  `/data_root` que le fix hardlink), pas par nom — fiable même si le titre
  affiché diffère (VO/VF, ponctuation...). Best-effort : une instance arr
  injoignable ou un fichier jamais importé ne bloque jamais la suppression
  des fichiers eux-mêmes.
  Marqueur `'M'` dans le listing (ajouté le 2026-07-28, suite à 11 torrents
  antérieurs à la stack arr repérés avec un fichier disparu — cas
  Transmission `"No data found!"`, jamais nettoyé tout seul : `make cleanup`
  synchronise Sonarr/Radarr mais ne supprime rien de son propre chef) +
  raccourci `Maj+P` pour les purger tous de Transmission en une fois après
  confirmation. Détection via `resolved_stat()` plutôt qu'un `os.stat()` nu
  — piège rencontré en l'écrivant, voir ci-dessous. Footer devenu trop long
  une fois ce raccourci ajouté : liste des touches déplacée dans un écran
  d'aide dédié (touche `?`) plutôt que condensée sur une ou deux lignes.
  Mode non-interactif `delete-by-inode <dev> <ino> [--dry-run]` (ajouté le
  2026-07-28) réutilisé par le skill `anime-vf` (voir plus bas) : retrouve
  et supprime un torrent Transmission par `(dev, ino)` déjà connu — capturé
  par l'appelant *avant* qu'un import Sonarr/Radarr ne remplace le fichier
  `library/` correspondant, un stat a posteriori échouerait sinon.
  Réutilise `resolved_stat()`/`torrent_host_files()`/`TransmissionClient`
  tels quels plutôt que de redupliquer la logique de matching par inode
  dans le skill — mêmes pièges (symlinks cross-seed) que le reste du
  script. Contrairement à une suppression dans la TUI, ne déclenche
  délibérément PAS `plan_sonarr_unmonitor`/`plan_radarr_deletion` :
  l'épisode reste monitored, il vient d'être remplacé par une meilleure
  release, pas retiré. Sortie JSON sur stdout (`{"found", "deleted",
  "torrent"}`) pour être consommée par un appelant scripté plutôt que lue
  dans le log.
  Vues **Séries**/**Films** (ajoutées le 2026-07-31, touche `Tab` pour
  cycler Torrents → Séries → Films) : pour un titre qui n'intéresse plus
  du tout, plutôt que de supprimer torrent par torrent puis gérer
  manuellement Sonarr/Radarr. `Entrée` sur une série retrouve tous ses
  torrents (préfixe du dossier de la série, comme `plan_sonarr_unmonitor`
  mais sans sa condition "saison terminée" — ici on supprime tout, peu
  importe l'état de diffusion), les supprime, nettoie aussi tout fichier
  résiduel du dossier sans torrent correspondant
  (`cleanup_orphan_series_files`), puis retire la série de Sonarr
  (`DELETE /api/v3/series/{id}`, `addImportListExclusion=true`) — retrait
  complet, pas juste `monitored=false`, symétrique avec le comportement
  film existant (`plan_radarr_deletion`). `Entrée` sur un film réutilise
  directement le flux de suppression normal d'un torrent quand un
  correspond (le `radarr_delete` de `plan_arr_actions` s'en charge déjà) ;
  sans torrent correspondant (jamais téléchargé, ou fichier orphelin hors
  suivi), Radarr supprime lui-même son fichier (`deleteFiles=true`).
  Piège rencontré en testant en lecture seule avant d'activer quoi que ce
  soit côté destructif : `series["path"]` (API Sonarr) est vu par le
  **conteneur** (`/data_root/library/...`), pas par l'hôte — sans passer
  par `arr_path_to_host()` (symétrique de `host_to_arr_path()`) avant de
  comparer aux chemins de `library_index`/`find_library_matches` (en
  espace hôte), aucun torrent n'aurait jamais matché une série et
  `cleanup_orphan_series_files` aurait cherché un dossier inexistant sur
  le disque. Pas de jaquette dans ces vues (demandé explicitement) :
  `curses` ne sait afficher que du texte, une vraie image nécessiterait
  un protocole terminal (Kitty/iTerm2/Sixel, marche seulement si le
  terminal SSH le supporte) ou une dépendance externe (`chafa`, rendu
  approximatif) — reporté à plus tard.
- **`dashboard` (`traefik/docker-compose.yml`) accessible en WAN comme en
  LAN** (changé le 2026-07-24, avant restreint par `ipallowlist`) — les
  sous-domaines qu'elle liste sont de toute façon publics via Certificate
  Transparency dès qu'un certificat Let's Encrypt leur a été émis, donc
  restreindre l'accès à la page n'apportait pas de confidentialité réelle.
  Les cartes des services LAN-only (Transmission/Prowlarr/Sonarr/Radarr)
  restent dans le HTML généré et cliquables, mais un script côté client
  (dans `scripts/generate-dashboard.py`) les grise dynamiquement pour un
  visiteur WAN : il sonde une image réelle de chaque service via `<img>`
  (`onload`/`onerror`, pas `fetch`/`XHR` — ceux-ci échouent pareil, bloqués
  ou non par CORS, donc ne distinguent pas un 403 ipallowlist d'un succès).
  Chemin de la sonde dans `PROBE_PATH` — ne pas supposer `/favicon.ico`
  générique : `transmission-proxy` le redirige vers du HTML, chemin
  overridé vers `/transmission/web/images/favicon.ico`. Ne pas proposer de
  revenir à un dashboard LAN-only sans redemande explicite.
- **Le dashboard vit sur le domaine nu et `www.${DOMAIN}`, pas
  `dashboard.${DOMAIN}`** (changé le 2026-07-24, en même temps que le
  passage de Nextcloud sur `nextcloud.${DOMAIN}` — avant sur
  `www.${DOMAIN}`, ce qui bloquait ces deux domaines pour le dashboard).
  Routeur Traefik : `Host(\`${DOMAIN}\`) || Host(\`www.${DOMAIN}\`)` dans
  `traefik/docker-compose.yml`. Le changement de domaine de Nextcloud
  n'est pas qu'un label Traefik : `trusted_domains`/`overwrite.cli.url`
  sont aussi dans `config/config.php` (persisté, hors compose) — à mettre
  à jour via `occ config:system:set` dans le container `app` (pas en
  éditant le fichier à la main), sinon Nextcloud rejette le nouveau nom
  d'hôte avec une erreur "domaine non fiable".
- **`hsts` et `security-headers` : deux middlewares Traefik partagés,
  définis une seule fois sur le container `traefik` lui-même**
  (`traefik/docker-compose.yml`, labels sans routeur associé — Traefik ne
  se reverse-proxy pas, mais un container avec `traefik.enable=true` peut
  déclarer un middleware sans router pour que d'autres stacks le
  référencent via `<nom>@docker`, `@docker` = provider, nécessaire pour
  référencer un middleware déclaré sur un autre container/stack). Chaque
  stack les ajoute à son propre routeur — ne jamais redéfinir
  `stsSeconds`/`customResponseHeaders`/etc. en dur dans un compose file,
  toujours `hsts@docker`/`security-headers@docker`. Un routeur avec déjà
  un autre middleware (ex. `arr-lan-only`) les combine en liste séparée
  par virgules : `arr-lan-only,security-headers@docker,hsts@docker`.
  - `hsts` (ajouté le 2026-07-24, généralisé à tous les services y compris
    LAN-only) : `stsSeconds=15552000`, `stsIncludeSubdomains=true`,
    `forceSTSHeader=true`. `nextcloud-hsts` (middleware local dupliquant
    les mêmes valeurs) supprimé au passage.
  - `security-headers` (ajouté le 2026-07-24 pour le dashboard seul sous
    le nom `dashboard-headers`, généralisé à tous les services le
    2026-07-24) : `X-Robots-Tag: noindex, nofollow, noarchive` (serveur
    perso, aucun service ne doit être indexé par un moteur de recherche
    ou appris par un crawler d'entraînement IA), `X-Frame-Options: DENY`,
    `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
    Sur le dashboard, redondant avec sa balise `<meta name="robots">`
    (voir plus bas) et son `dashboard/assets/robots.txt` — volontaire,
    couvre les crawlers qui ne parsent pas le HTML.
  - `rate-limit` (ajouté le 2026-07-24, appliqué à `jellyfin` et `seerr`
    seulement — Nextcloud a son propre anti-bruteforce intégré, les
    services `arr`/`transmission` sont déjà LAN-only via `ipallowlist`) :
    `average=50`, `burst=100` par IP source. Limite tout le routeur, pas
    que l'endpoint de login (Traefik seul ne sait pas cibler par code de
    réponse/chemin pour faire du vrai anti-bruteforce à la fail2ban) —
    valeurs volontairement généreuses pour ne jamais gêner un usage normal
    (Jellyfin charge plusieurs images de bibliothèque en parallèle),
    testé le 2026-07-24 : 200 requêtes concurrentes → ~110 passent
    (302), le reste 429 ; 20 requêtes séquentielles → aucune impactée.
- **Dashboard exclu des moteurs de recherche/crawlers IA** (ajouté le
  2026-07-24, conséquence de son exposition WAN) : balise `<meta
  name="robots">` dans le HTML généré + `dashboard/assets/robots.txt`
  (versionné, `Disallow: /`), en plus de l'en-tête `X-Robots-Tag` du
  middleware partagé `security-headers` ci-dessus. Si un nouveau fichier
  statique est ajouté à la racine servie par `dashboard`, garder
  `robots.txt` à jour dans `dashboard/assets/` (source versionnée), pas
  directement dans `dashboard/html/` (généré, gitignoré).
- **Healthcheck sur tous les services** (ajouté le 2026-07-24, avant la
  moitié en étaient dépourvus : `nextcloud-app`/`web`, `seerr`, `traefik`,
  `dashboard`, `arr/*`, `vpn/transmission-proxy` — sans ça un process
  bloqué sans crasher restait `Up` indéfiniment, `restart: unless-stopped`
  ne se déclenchant que sur un exit du process, jamais sur un statut
  unhealthy). Un check HTTP réel quand un endpoint non authentifié existe
  (`/ping` Servarr pour `prowlarr`/`sonarr`/`radarr`, `/status.php` pour
  `web`, `/api/v1/status` pour `seerr`) ; simple connect TCP sinon
  (`nc -z`, pour `app` sur le port fastcgi 9000 sans HTTP propre,
  `cross-seed` sans endpoint non authentifié connu, `dashboard`/
  `transmission-proxy` sur nginx) ; `pgrep supercronic` pour `recyclarr`
  qui n'a aucun serveur (juste un scheduler interne — ne détecte pas un
  job individuel bloqué, seulement le scheduler mort). `traefik` a un
  entrypoint statique dédié `healthcheck` lié à `127.0.0.1:8082`
  (`traefik.yml`, jamais publié dans `ports:`) + `ping: {}`, plutôt que de
  réutiliser `web`/`websecure` — sinon la redirection http→https de
  l'entrypoint `web` s'appliquerait aussi à la sonde interne. Version
  volontairement simple : aucune auto-remédiation (pas de watcher type
  `autoheal` sur le socket Docker), juste de la visibilité (`docker ps`,
  dashboard ci-dessous).
- **Le dashboard reflète l'état `unhealthy` d'un service** (ajouté le
  2026-07-24, conséquence directe du point ci-dessus) :
  `scripts/generate-dashboard.py` lit `docker ps --filter
  health=unhealthy` (en plus de `docker ps --filter status=running`
  déjà utilisé pour public/local/arrêté) et ajoute un contour rouge
  (`.logo-unhealthy`) autour du logo + un texte d'avertissement sous la
  carte. Régénéré automatiquement toutes les 5 min par cron
  (`scripts/crontab`, `make cron-install`) plutôt que seulement à la
  main (`make dashboard-refresh`) — sinon un service qui devient
  unhealthy entre deux régénérations manuelles resterait affiché comme
  sain arbitrairement longtemps.
- **Le dashboard affiche des stats Transmission (ratio session/total,
  débits, ratio par tracker), visibles WAN et LAN** (ajouté le 2026-07-28) :
  `scripts/transmission-stats.py` interroge le RPC Transmission via le même
  mécanisme docker-exec-curl que `scripts/torrent-cleanup.py` (RPC non
  authentifié, joignable seulement depuis l'intérieur du conteneur — réseau
  `vpn-internal` isolé) et sort du JSON consommé par
  `scripts/generate-dashboard.py`. Ratio session = `current-stats` de
  `session-stats` (compteurs remis à zéro à chaque redémarrage du daemon,
  donc "depuis l'uptime" demandé) ; ratio total = `cumulative-stats` (jamais
  remis à zéro). Un ratio glissant sur 24h avait été ajouté le même jour
  (delta calculé contre l'échantillon le plus proche d'"il y a 24h" dans un
  historique persisté sous `${DATA_ROOT}/.transmission-stats-history.jsonl`)
  puis retiré quelques heures plus tard, jugé pas utile par l'utilisateur —
  l'historique persisté (même fichier, même convention dotfile sous
  DATA_ROOT que `.torrent-cleanup.log`) ne sert plus qu'à l'échelle des
  mètres de débit (`record_speed_sample()`/`historical_max_speed()`,
  fenêtre de rétention ~25h conservée telle quelle, toujours pertinente pour
  ça). Ratio par tracker = somme de `uploadedEver`/`downloadedEver`
  par host d'annonce (`torrent-get.trackerStats`), noms résolus via Prowlarr
  (même logique que `tracker_host`/`resolve_tracker_name` dans
  `torrent-cleanup.py`, dupliquée plutôt que factorisée — `torrent-cleanup.py`
  est un script curses autonome, pas une lib). Un torrent multi-tracker
  compte dans chaque tracker auquel il annonce (impossible de départager
  l'upload par tracker côté RPC Transmission) : le ratio par tracker pris
  individuellement reste correct, mais la somme des trackers peut dépasser
  le volume réel total. Contrairement aux cartes de service (grisées côté
  WAN via `data-probe`, cf. ci-dessus), cette section n'a aucun gating LAN :
  ce sont des chiffres agrégés en snapshot (recalculés à chaque régénération
  cron, 5 min), pas un accès de contrôle au client — voulu explicitement.
  Ratio calculé côté Transmission, peut différer du ratio réel compté par
  chaque tracker (leur propre comptage d'annonce fait foi, pas les
  compteurs locaux du client) — pas encore de lecture directe des ratios
  de compte sur les trackers privés eux-mêmes (tr4cker/c411/nya.si évoqués
  par l'utilisateur), reporté à plus tard : Prowlarr n'expose aucune notion
  de compte/ratio (juste un agrégateur de recherche), il faudrait un
  scraper dédié par tracker (API si le tracker tourne sur UNIT3D/Gazelle,
  sinon parsing HTML de la page de profil).
- **La section Transmission du dashboard affiche des cartes (débits, ratios
  total/session, ratio par tracker dans la table dépliée), pas du texte
  brut** (ajouté le 2026-07-28, demandé par l'utilisateur — un premier essai
  en mètres/barres horizontales le même jour a été remplacé par des cartes
  après retour direct de l'utilisateur : il voulait retrouver le style carte
  du reste du dashboard). Chaque carte de débit et chaque carte de ratio
  affiche une **jauge** en arc de cercle (`gauge_svg()`/`zone_gauge_svg()`
  dans `scripts/generate-dashboard.py`, rendues via `dashboard/templates/
  gauge.html` et `gauge-zones.html` : pur SVG, pas de lib JS de graphiques —
  cohérent avec le reste du dashboard, aucune dépendance externe). Jauge de
  débit : arc de fond + arc coloré proportionnel + aiguille. Jauge de ratio :
  pas d'arc de remplissage, le fond est directement divisé en 3 zones de
  sévérité fixes (rouge/jaune/vert) sur l'échelle 0-4 de `ratio_pct()`,
  l'aiguille pointe juste sa position dessus (`zone_gauge_svg()` généralisée
  le même jour pour être réutilisée par l'espace disque, voir plus bas) —
  remplace une **balance à
  bascule** (`balance_svg()`, fléau tournant selon le log2(ratio)) essayée le
  même jour et écartée après retour direct de l'utilisateur : peu pratique à
  l'usage (sens de bascule pas immédiat). Jauge de ratio préférée à un
  anneau de progression pour la cohérence avec la jauge de débit (un seul
  type d'icône sur tout le dashboard). La table détaillée par tracker
  (repliée par défaut) garde une mini-barre horizontale, pas une mini-jauge :
  trop peu de place dans une cellule de tableau. Zone de sévérité (couleurs
  de la jauge de ratio / de la mini-barre) par seuil générique torrenting
  (rouge `<1` en dessous de l'équilibre, jaune `1–2`, vert `≥2` — pas le
  seuil `seedRatio=2` propre à Nyaa.si côté Sonarr, voir plus haut, qui ne
  s'applique qu'à cet indexeur). Jauge de débit cappée sur `speed_scale`
  (maximum observé sur l'historique ~25h persisté par `transmission-stats.py`,
  calculé par `historical_max_speed()`, avec un
  plancher de 1 Mo/s tant que rien n'a encore été échantillonné à pleine
  vitesse) plutôt qu'une capacité de ligne figée en config : le débit VPN
  réel dépend du pair/tracker distant et de l'overhead du tunnel, pas
  seulement de la capacité FAI — une valeur figée aurait été fausse dès le
  premier changement de serveur VPN ou de tracker. Conséquence : `.transmission-stats-history.jsonl`
  gagne deux champs par échantillon (`download_speed`/`upload_speed`,
  absents des échantillons antérieurs à ce changement — lus avec
  `.get(..., 0)`, ne faussent pas le calcul, juste sous-estiment le max tant
  que l'historique pré-existant n'a pas été purgé après ~25h).
- **Les cartes de la section `Transmission &amp; système` sont toutes à
  plat dans un seul flux (`.stats-flow`, flexbox — pas de grille CSS, pas de
  sous-section/titre de groupe)** (état stabilisé le 2026-07-28 après
  plusieurs itérations — voir ci-dessous). Chaque carte porte son propre
  libellé, y compris ce qui vivait avant dans un titre de groupe désormais
  disparu : débits ("Débit descendant"/"Débit montant"), ratios ("Ratio
  total"/"Ratio session (…)"), torrents (titre "Torrents" replié en haut de
  la carte multi-lignes), indexeurs (titre "Indexeurs" replié en haut de la
  liste), disque ("Disque" — raccourci depuis "Disque libre" le 2026-07-28,
  demandé par l'utilisateur ; carte placée après indexeurs, dernière du
  flux, également demandé). `.stats-flow` utilise flexbox
  (`flex-wrap`) plutôt qu'une grille CSS — demandé explicitement — avec une
  largeur de carte fixe par `flex: 0 0 190px` sur `.stat` (pas
  `grid-template-columns`).
  `align-items` du conteneur est revenu sur `stretch` (2026-07-30, demandé
  explicitement) après un premier temps sur `flex-start` (chaque carte
  gardait sa propre hauteur, pas d'étirement à celle de la plus grande
  carte de sa "rangée") : les cartes d'une même ligne (flex-wrap crée des
  "flex lines" indépendantes, l'étirement ne dépasse jamais la ligne)
  s'étirent maintenant à la hauteur de la plus haute d'entre elles. Le
  contenu interne de chaque carte absorbe cet espace en plus sans qu'il
  ait fallu toucher aux templates : `.stat` centre son contenu
  verticalement (`justify-content: center`), `.stat-list` (indexeurs,
  tâches planifiées) l'ancre en haut (`justify-content: flex-start`).
  Itérations précédentes (pour référence, pas l'état actuel) : Débits/Ratio
  d'abord groupés côte à côte avec un titre `h3` par groupe, puis une
  deuxième ligne "Système" (un seul titre) éclatée en trois sous-sections
  titrées (Torrents/Disque/Indexeurs), le tout construit en CSS grid
  (`.stats-grid`, `grid-template-columns: repeat(auto-fill, …)`) — abandonné
  au profit du flux plat ci-dessus, toujours à la demande de l'utilisateur.
  La carte torrents elle-même a changé de forme plusieurs fois avant de se
  stabiliser en liste empilée label/valeur (`render_stat_item()`/
  `stat-multi-item.html`, une ligne par métrique, valeur alignée à droite) :
  Actifs, Surveillés, En téléchargement, En erreur. Téléchargement = torrent
  avec `status == 4` (spec RPC) ; erreur = champ `error != 0` (tracker
  injoignable, fichier introuvable sur disque, etc. — `errorString` donne le
  détail par torrent, pas exposé au dashboard, juste le compte), valeur
  colorée en rouge si `>0` sinon verte (`stat-value-critical`/
  `stat-value-good`) — seule la valeur "erreur" est colorée, les autres
  restent en encre neutre (0 en téléchargement n'est pas anormal,
  contrairement à une erreur). « Torrents actifs » = tout torrent avec
  `status != 0` (spec RPC, 0 = stopped/paused) ; « torrents surveillés » =
  tous les torrents présents dans le client, actifs ou non — hypothèse de
  lecture de la demande utilisateur ("nombre de torrents actifs" vs
  "surveillés"), vérifiée en interrogeant le RPC en direct le 2026-07-28
  (257 actifs / 278 au total à ce moment), à corriger si l'intention était
  différente.
  Indexeurs Prowlarr : `/api/v1/indexer` (liste + `enable`) croisé avec
  `/api/v1/indexerstatus` (liste des indexeurs actuellement désactivés après
  échecs répétés, vide si tout va bien — un indexeur y figurant compte comme
  en échec, par `indexerId`), via `docker exec arr-prowlarr-1 curl` (même
  mécanisme que `build_prowlarr_tracker_map()` dans `transmission-stats.py`,
  dupliqué plutôt que partagé, voir plus haut) ; liste chaque indexeur avec
  un point coloré par état plutôt qu'un compte agrégé, pour voir directement
  LEQUEL est en échec sans changer d'écran.
  Chaque carte a sa propre disponibilité (best-effort indépendant, voir
  `build_stats_section()`) : espace disque et indexeurs Prowlarr s'affichent
  même si `vpn/transmission-vpn` est arrêté (contrairement à Débits/Ratio/
  torrents, qui dépendent tous de `transmission-stats.py` donc du VPN) ;
  toute la section (`Transmission &amp; système`, renommée depuis
  "Transmission — ratios & débits" en cours de route) n'est omise que si
  aucune carte n'est disponible. Les paragraphes `<p class="note">` sous la
  section (légende des seuils de couleur ratio/débit/disque) retirés le même
  jour à la demande de l'utilisateur — les zones de couleur restent
  identiques sur les jauges elles-mêmes (rouge/jaune/vert), seul le texte
  d'explication en bas de section a disparu.
- **`scripts/generate-dashboard.py` (réécrit en Python le 2026-07-28,
  remplace l'ancien `generate-dashboard.sh`)** : venait d'un script bash+jq
  qui construisait le HTML par concaténation de chaînes, devenu illisible
  avec l'ajout des mètres ci-dessus — demandé explicitement par
  l'utilisateur pour séparer vues et code avant d'étendre le dashboard
  (future page monitoring évoquée). Vues dans `dashboard/templates/*.html`
  (un fichier par composant : `page`, `section-grid`, `card-clickable`,
  `card-down`, `stat-card`, `gauge`, `gauge-zones`, `stats-flow`,
  `multi-stat-card`, `stat-multi-item`, `indexers-card`, `mini-meter`,
  `tracker-row`, `tracker-details`, `section-transmission` — `gauge`/
  `gauge-zones` ajoutés le 2026-07-28 en remplaçant un premier essai en
  mètres puis en balance ; `stats-flow` le même jour, remplace un détour par
  `stats-row`/`stats-group` (groupes titrés en CSS grid, voir plus haut) par
  un flux plat flexbox sans sous-section), rendues via `string.Template` de
  la stdlib (substitution `$variable` uniquement, zéro logique dans un
  template — boucles/conditions et calculs géométriques (angles/points
  d'arc des jauges) restent en Python, ex.
  `build_cards()`/`build_stats_section()` décident quelles cartes
  existent et les joignent avant de les passer au template parent). CSS/JS déplacés en
  fichiers statiques (`dashboard/assets/dashboard.css`/`dashboard.js`, copiés
  vers `dashboard/html/assets/` comme les logos) plutôt qu'embarqués dans le
  heredoc. Zéro nouvelle dépendance (`python3` déjà requis par
  `transmission-stats.py`/`torrent-cleanup.py`, `string.Template` est
  stdlib) — et ça fait disparaître `jq` du chemin de génération du
  dashboard (extraction des labels Traefik via `docker compose config
  --format json` + `re`/`json` du stdlib plutôt qu'un programme `jq`
  embarqué). Alternative envisagée et écartée : rester en bash avec des
  templates substitués par `envsubst` — diff plus petit mais les boucles
  (cartes/mètres/lignes de tracker) restent aussi pénibles qu'avant, ce qui
  était précisément le problème à résoudre. `make cron-install` doit être
  relancé après ce changement (`scripts/crontab` invoque désormais `python3
  .../generate-dashboard.py`, pas plus le `.sh`).
- **Les sections du dashboard (Public/Local/Stack non lancée/Transmission)
  ne s'affichent que si elles ont du contenu** (ajouté le 2026-07-28) —
  plus de placeholder `"aucun"`/`"aucune"`/`"indisponible"` pour une section
  vide, la section entière est omise. La section Transmission spécifiquement
  ne s'affiche que si `vpn/transmission-vpn` tourne (vérifié via le même
  `RUNNING` construit par `docker ps` que les cartes de service), pas
  seulement si `transmission-stats.py` a réussi à sortir un JSON — évite
  d'afficher un message d'erreur générique le temps que le conteneur
  redémarre. Le tableau détaillé "ratio par tracker" est en plus replié par
  défaut dans un `<details>`/`<summary>` natif (pas de JS) — verbeux avec
  plusieurs trackers privés, pas l'information qu'on veut voir en premier.
- **`max-file: "3"` sur tous les blocs `logging` json-file** (ajouté le
  2026-07-24) — `max-file` n'était jamais fixé alors que `max-size` l'est
  partout, donc un seul fichier de logs par service : dès qu'il atteignait
  `max-size`, tout l'historique précédent disparaissait plutôt que d'être
  conservé dans un fichier tourné. Toujours ajouter les deux ensemble sur
  tout nouveau service, jamais `max-size` seul.
- **Ratio-limite 2 sur les torrents Nyaa.si via `seedCriteria.seedRatio`
  côté Sonarr, pas un script maison** (demandé le 2026-07-28) — Prowlarr n'a
  aucun champ ratio/seed sur son propre objet indexeur (vérifié via l'API,
  `/api/v1/indexer/<id>`), seul Sonarr/Radarr en expose un (`seedCriteria.
  seedRatio`/`seedTime`, poussé au client de téléchargement au moment du
  grab). Réglé à `2` sur l'indexeur synchronisé `Nyaa.si (Prowlarr)` côté
  Sonarr (`/api/v3/indexer/4`, via l'API — pas l'UI, pas dans un fichier
  versionné, comme les autres réglages arr faits par API). Un premier
  script (`scripts/apply-nyaa-ratio-limit.py`, cron 5 min) avait été écrit
  puis retiré (2026-07-28, même jour) : il couvrait plus de cas (aussi les
  torrents déjà présents et une future injection cross-seed sur Nyaa.si,
  qui contournerait ce réglage Sonarr — cross-seed injecte directement
  dans Transmission sans repasser par le grab Sonarr), mais l'utilisateur a
  préféré la simplicité du réglage natif à cette couverture plus large.
  Les torrents déjà présents au moment de la demande gardent le
  `seedRatioLimit=2` posé une fois manuellement (pas rétroactif, mais pas
  besoin de le refaire — déjà fait). Trou connu et accepté : un futur
  torrent Nyaa.si injecté par cross-seed n'aura pas cette limite.
- **Deux profils qualité Sonarr distincts pour préférer l'audio français sur
  un anime donné, pas un réglage global** (`Anime (Fansub)` inchangé /
  `Anime (Fansub) VF` nouveau, ajoutés le 2026-07-28 via l'API — comme le
  point ci-dessus, pas dans un fichier versionné, pas gérés par
  `arr/recyclarr/recyclarr.yml` qui ne gère que "WEB-2160p (Combined)" côté
  Sonarr : aucun risque qu'un `recyclarr sync` écrase ces deux profils).
  Custom Format `FRENCH` créé (regex `\b(TRUEFRENCH|FRENCH|VFF|VFQ)\b`,
  résoudre son id par nom, ne pas le supposer fixe) : scoré à 0 (neutre) sur
  `Anime (Fansub)`, à 200 sur `Anime (Fansub) VF` (au-dessus de `MULTi`=100
  et `VOSTFR`=50 déjà présents par défaut sur ce profil — `VOSTFR` ici veut
  dire japonais + sous-titres français, pas de l'audio français, à ne pas
  confondre). Bascule voulue par série (via le skill `anime-vf`, voir
  ci-dessous), pas un scoring partagé par tous les animes — un premier essai
  avait modifié `Anime (Fansub)` en place, corrigé le même jour après retour
  direct de l'utilisateur : il voulait deux profils séparés, la bascule
  faisant elle-même office de sélection explicite par série plutôt qu'une
  préférence globale imposée à tous les animes.
  Skill `.claude/skills/anime-vf/SKILL.md` : bascule la série demandée sur
  `Anime (Fansub) VF`, relance une recherche (`SeriesSearch`), rapporte ce
  qui a été grabé — le remplacement du fichier existant reste ensuite
  automatique côté Sonarr (`upgradeAllowed: true` sur les deux profils),
  aucune action manuelle nécessaire une fois la bonne release grabée. Le
  skill capture le `(dev, ino)` de chaque fichier déjà présent avant de
  lancer la recherche, attend l'import de la nouvelle release, puis
  supprime l'ancien torrent via `scripts/torrent-cleanup.py
  delete-by-inode` (ajouté le même jour, voir plus haut) — demandé
  explicitement par l'utilisateur plutôt que de laisser ce nettoyage
  manuel. Fonctionne seulement si une release FRENCH/VFF/VFQ/TRUEFRENCH
  existe réellement chez les indexeurs Prowlarr configurés au moment de la
  recherche — pas de garantie de disponibilité.
- **`scripts/vpn-bench.py` (skill `vpn-bench`)** compare latence/débit entre
  le serveur AirVPN actuellement configuré (`vpn/custom/default.ovpn`) et
  d'autres pays, ajouté le 2026-07-29 suite à un bench manuel
  Belgique/Pays-Bas/Allemagne/Suisse (Belgique gagnante sur les trois
  métriques à la fois — pas de changement de serveur suite à ce test).
  Fonctionne parce que le certificat client AirVPN (`<cert>`/`<key>` dans le
  `.ovpn`) est lié au compte, pas au serveur : seule la ligne `remote
  [pays].vpn.airdns.org <port>` change entre pays, donc pas besoin de
  regénérer quoi que ce soit depuis le site AirVPN pour tester un autre
  pays. Restaure systématiquement la config d'origine à la fin (backup sur
  disque en plus de la copie en mémoire, pour survivre à un kill dur du
  script) — jamais de changement permanent sans le redemander
  explicitement. Latence mesurée vers un tracker tiré au hasard parmi les
  torrents actifs à chaque run (pas de tracker fixe en config) — accepté
  explicitement par l'utilisateur, moins reproductible d'un run à l'autre
  mais plus simple.
- **`scripts/apply-arr-overrides.py` (`make arr-overrides`, aussi enchaîné
  par cron quotidien juste après `make recyclarr-sync`, voir
  `scripts/crontab`)** réapplique les réglages des
  deux profils qualité principaux (Sonarr `WEB-2160p (Combined)`, Radarr
  `[SQP] SQP-1 WEB (2160p)`) qu'aucune propriété recyclarr n'expose : tailles
  de palier "Quality Definition" (voir commentaires dans
  `arr/recyclarr/recyclarr.yml`) et champ `language` du profil Radarr forcé
  à "Original" par le guide. Ajouté le 2026-07-29 en creusant la question de
  la reproductibilité de ces réglages sur un autre déploiement — vérifié en
  direct ce même jour que ces valeurs avaient déjà dérivé sur cette instance
  (retombées aux défauts du guide TRaSH), le pense-bête en commentaire
  ("repasser ces valeurs après coup") n'ayant jamais été relancé depuis le
  dernier `recyclarr sync` @daily qui les écrase. Résout le profil Radarr
  par nom (pas par id, contrairement à l'ancien commentaire qui supposait
  l'id 7 figé) pour rester valide si le profil est un jour recréé avec un
  autre id, ou répliqué sur un autre déploiement où l'id serait différent.
  Idempotent (compare avant d'écrire, `déjà à jour, rien à faire` si rien à
  changer) et best-effort par arr (une erreur sur Sonarr n'empêche pas
  d'essayer Radarr).
  **Élargi le 2026-08-02 à la config anime** (`arr/profiles/sonarr-anime.json`,
  versionné) : les 2 custom formats qui nous appartiennent (`FRENCH`,
  `VOSTFR (hors suffixe)`) et les 3 profils `Anime (Fansub)*`. Décision prise
  après avoir remis à plat l'utilité de recyclarr — l'utilisateur le garde
  (sa vraie valeur : les MAJ communautaires des ~120 regex, listes LQ /
  groupes de release / tags de plateformes) mais veut que **toute la config
  custom vive dans le repo**. Ces objets n'étaient couverts par aucun
  `trash_id`, donc rien ne les recréait sur une installation neuve et rien ne
  rattrapait leur dérive : ils n'existaient que dans la base Sonarr,
  récupérables par la sauvegarde restic mais pas reproductibles depuis git.
  Le JSON est **déclaratif et fait autorité** : tout custom format absent de
  `scores` est remis à 0 sur le profil concerné.
  Qualités et custom formats désignés **par nom**, jamais par id (propres à
  chaque instance — c'est précisément pourquoi un dump d'API brut ne serait
  pas reproductible) ; un profil absent est créé à partir de
  `/api/v3/qualityprofile/schema` plutôt qu'en versionnant tout l'arbre des
  paliers de qualité (2,7 Ko de JSON au total, relisible dans un diff). Ordre
  imposé : custom formats d'abord, profils ensuite (qui les référencent par
  nom) — et surtout `make recyclarr-sync` AVANT tout le script, sinon les CF
  du guide scorés par les profils anime (`MULTi`, `LQ`, `Upscaled`...)
  n'existent pas encore ; ce cas lève une erreur explicite plutôt que de
  créer un profil silencieusement dépourvu de la moitié de ses scores.
  `api_put` passe par un `api_write` commun qui vérifie désormais la réponse
  (`curl -s` sort 0 même sur un 400 — sans ça une écriture refusée par la
  validation Sonarr était comptée comme réussie).
  Chemin de **création** testé explicitement le 2026-08-02 en dupliquant la
  config sous des noms jetables (`… ZZTEST`) : les 3 profils créés de zéro
  ressortent identiques aux vrais (cutoff, qualités autorisées, scores), 2e
  exécution idempotente, objets supprimés après coup — sans quoi ce chemin,
  celui qui justifie tout l'exercice, n'aurait jamais été exercé avant le
  jour d'une vraie réinstallation (même leçon que le bug de `pg_dump` plus
  haut). Ne couvre QUE ces deux profils : les profils
  `Anime (Fansub)*` (et le custom format `FRENCH` qui n'y est scoré que sur
  `Anime (Fansub) VF`) restent volontairement hors périmètre — usage
  personnel de l'utilisateur, pas géré pour la reproductibilité multi-PC,
  décidé explicitement en écartant l'option d'un script plus large. Ce trou
  connu (custom formats/profils Anime encore API-only, non reproductibles
  sans repasser à la main par l'API) reste documenté par les entrées
  existantes plus haut (profils Anime, `cutoffFormatScore`) mais n'a pas de
  script de provisioning dédié.
  Cron initialement réglé à 00h15, un délai de sécurité estimé après le
  sync interne `@daily` (00h00) du conteneur recyclarr — repéré le
  2026-07-30 (utilisateur) comme une fenêtre de 15 min pendant laquelle ces
  réglages sont dans l'état par défaut du guide TRaSH, pas les valeurs
  voulues. Fix : le scheduler interne de recyclarr (`CRON_SCHEDULE`, cron
  `@daily` par défaut de l'image) est désactivé — aucune valeur ne le
  désactive proprement (seule une expression cron qui ne matche jamais,
  ex. `31 février`, fonctionne comme contournement, écarté comme pas assez
  propre) ; le service `recyclarr` passe donc en mode manuel pur
  (`arr/docker-compose.yml` : plus de `restart:`/healthcheck, `profiles:
  [manual]` pour rester absent de `make up STACK=arr`), déclenché
  uniquement par `make recyclarr-sync` (nouveau target Makefile,
  `docker compose run --rm recyclarr sync` — passer un argument à
  l'entrypoint de l'image bascule du mode cron au mode CLI one-shot).
  `scripts/crontab` enchaîne désormais `recyclarr-sync` puis
  `apply-arr-overrides.py` sur une seule ligne cron (`&&`, plus deux
  horaires séparés) : la fenêtre où ces réglages sont faux se limite à la
  durée réelle du sync (quelques secondes, mesuré), pas à un délai de
  sécurité estimé.
- **Carte dashboard "Tâches planifiées"** (ajoutée le 2026-07-30, remplace
  l'ancienne carte solo "Dernière sauvegarde") : liste chaque tâche de
  `scripts/crontab` avec un point coloré — vert si elle a tourné avec
  succès il y a moins de temps que l'écart normal entre deux occurrences de
  son cron, rouge sinon (demandé explicitement ainsi par l'utilisateur).
  Même gabarit carte-liste que les indexeurs Prowlarr (`indexers-card.html`)
  — classes CSS renommées `.indexer-list`/`.indexer-dot-*` →
  `.status-list`/`.status-dot-*` (génériques, réutilisées par les deux
  cartes) à cette occasion. Deux mécanismes de détection selon la tâche
  (voir `render_scheduled_tasks_card()`/`cron_marker_age_seconds()` dans
  `scripts/generate-dashboard.py`) :
  - **Sauvegarde restic** : garde son check existant (âge réel du dernier
    snapshot via `restic snapshots --latest 1`, voir `BACKUP_MAX_AGE_DAYS`
    ci-dessus) — plus fiable qu'un marqueur de fin de script, qui ne prouve
    que "le script est allé jusqu'au bout", pas que le snapshot produit est
    valide.
  - **Nextcloud (cron.php), rafraîchissement dashboard, recyclarr +
    overrides arr** : chaque ligne de `scripts/crontab` écrit désormais
    `date +\%s > __DATA_ROOT__/.cron-status/<nom>` à la fin de sa chaîne de
    commandes (`&&`, donc jamais atteint si une étape précédente échoue) ;
    `cron_marker_age_seconds()` compare l'âge du marqueur à l'intervalle
    attendu (`SCHEDULED_TASKS`, codé en dur par tâche plutôt qu'un
    parseur générique d'expression cron — seulement 3 tâches concernées,
    une abstraction générique n'aurait rien simplifié). `__DATA_ROOT__` est
    un 3e placeholder substitué par `make cron-install` (comme
    `__REPO_ROOT__`/`__PUID__`), qui crée aussi `.cron-status/` s'il
    n'existe pas encore.
  La composition de la liste ne dépend JAMAIS de la disponibilité d'un
  marqueur/snapshot (demandé explicitement le 2026-07-30 après un premier
  essai qui omettait une tâche tant que son marqueur n'existait pas
  encore) : une tâche sans marqueur/snapshot est affichée en rouge, pas
  absente. Liste volontairement minimale (nom + point coloré, pas d'âge
  affiché) pour rester cohérente avec le gabarit indexeurs déjà en place.

  En revanche, une tâche liée à une stack arrêtée EST absente de la liste
  (raffiné le 2026-07-30, même jour, suite à une remarque de l'utilisateur :
  "si la stack n'est pas lancée, le cron lui-même ne devrait pas être
  lancé" — pas seulement caché côté dashboard). `SCHEDULED_TASKS` porte
  donc un 4e élément, la liste des services `<project>/<service>` requis
  (mêmes clés que le `running` de `docker_ps_set()`) : `["nextcloud/app"]`
  pour Nextcloud (cron.php), `["arr/sonarr", "arr/radarr"]` pour Recyclarr
  + overrides arr, liste vide pour Sauvegarde restic et Rafraîchissement
  dashboard (pas de stack associée — ce dernier est justement ce qui doit
  tourner pour refléter qu'une autre stack est arrêtée). `render_scheduled_tasks_card()`
  saute entièrement la ligne si un des services requis n'est pas dans
  `running` — rouge serait un faux signal d'échec pour un arrêt volontaire.
  Ce filtrage dashboard est un affichage, pas une garantie : le vrai fix
  est côté cron (voir `scripts/require-running.sh` ci-dessous), sans quoi
  le job continuerait de tourner (et d'échouer, ou de réussir inutilement)
  toutes les 5 min/tous les jours contre une stack arrêtée, pour rien.

- **`scripts/require-running.sh <project>/<service> [...]`** (ajouté le
  2026-07-30) : exit 0 seulement si chaque service listé a un conteneur
  `running` au moment de l'appel (`docker ps --filter label=com.docker.compose.project=...
  --filter label=com.docker.compose.service=...`, mêmes labels que
  `docker_ps_set()` dans `scripts/generate-dashboard.py`). Deux usages :
  - En tête de chaîne `&&` dans `scripts/crontab` pour les 2 tâches liées à
    une stack précise (Nextcloud cron.php → `nextcloud/app` ; Recyclarr +
    overrides arr → `arr/sonarr` ET `arr/radarr`) : si le guard échoue,
    toute la chaîne s'arrête avant le premier `docker exec`/
    `docker compose run` — silencieux plutôt qu'un échec répété.
  - Dans `scripts/backup.sh`, pour rendre le dump de la base Nextcloud
    best-effort plutôt que fatal : `pg_dump` sur `nextcloud/db-next`
    tournait auparavant sans vérifier qu'il était démarré, sous `set -e` —
    si `nextcloud` était arrêté au moment du cron hebdo, TOUT le script
    échouait (aucun manifeste d'images, aucun `restic backup`), pas
    seulement le dump DB. Le fichier `nextcloud-db.sql` d'un run précédent
    est supprimé du staging plutôt que laissé tel quel si le dump est
    sauté ce run-là — un vieux dump silencieusement re-sauvegardé comme
    s'il était frais serait pire qu'une sauvegarde manquante.
  Pas de gating équivalent sur la sauvegarde restic elle-même côté
  dashboard/cron : c'est une sauvegarde globale (manifeste de toutes les
  stacks), pas le cron d'un seul service — seule sa dépendance interne au
  dump Nextcloud est rendue best-effort, pas la tâche entière.
- **Marge de 20 % (`CRON_MARKER_SLACK = 1.2` dans `scripts/generate-dashboard.py`)
  sur l'intervalle attendu de chaque tâche planifiée** (ajouté le 2026-07-30,
  suite à un faux rouge constaté sur "Rafraîchissement dashboard" lui-même) —
  sans marge, un marqueur comparé pile à l'intervalle du cron (300s pour un
  `*/5`) passe rouge dès que la génération du dashboard tombe dans les
  dernières secondes avant le tick suivant (jitter du scheduler, ou un `make
  dashboard-refresh` manuel qui ne tombe pas pile sur le cycle) alors que la
  tâche tourne normalement (confirmé via `refresh.log` : succès à chaque
  tick). S'applique aussi à `BACKUP_MAX_AGE_DAYS` (7j → 8.4j effectifs) pour
  la même raison, la sauvegarde restic étant elle aussi hebdomadaire pile.
- **Pied de page `Généré le … — make dashboard-refresh` déplacé en `<footer>`
  en bas de page (ajouté le 2026-07-30)**, et surlignage rouge côté client du
  timestamp si le cron de régénération semblait arrêté (`data-generated`/
  `.updated-stale` dans `dashboard.js`/`dashboard.css`) retiré le même jour —
  devenu redondant avec la carte "Tâches planifiées" (ci-dessus), qui
  surveille déjà ce cron précis via son propre marqueur.
- **Section "Transmission &amp; système" renommée "Monitoring"** (2026-07-30)
  et **masquée par défaut, dépliable via un switch** (même jour, demandé par
  l'utilisateur) : le titre et le switch (`.section-header-row` dans
  `section-transmission.html`) restent toujours visibles et alignés sur une
  même ligne à gauche — seul le contenu (`#monitoring-content`,
  `.monitoring-hidden`) est masqué, jamais le titre lui-même (corrigé après
  un premier essai qui masquait toute la `<section>`, titre inclus). État du
  switch retenu en `localStorage` (`dashboard.js`) pour survivre à la
  régénération cron toutes les 5 min — sans ça l'utilisateur aurait dû
  redéplier la section à chaque rechargement de page.
- **Débits (descendant/montant) et Ratios (total/session) fusionnés chacun en
  une seule carte à 2 jauges** (`render_dual_gauge_card()`/
  `dual-gauge-card.html`/`gauge-column.html`, 2026-07-30) — la première ligne
  de la section comptait 4 cartes séparées, réduites à 2 cartes `.stat-span-2`
  de 2 colonnes chacune, même principe que la carte Torrents
  (`render_torrents_files_card()`). Chaque carte porte un titre ("Débits"/
  "Ratios") ; les libellés de colonne ont perdu leur préfixe redondant avec ce
  titre ("Débit descendant" → "Descendant", "Ratio total" → "Total").
  `.stat-dual-gauge` doit explicitement passer `align-items: stretch` (comme
  `.stat-torrents-files`) pour que `.stat-columns-row` occupe toute la largeur
  de la carte — sans ça le `.stat` de base (`align-items: center`) laisse la
  ligne se resserrer à la largeur de son contenu et `justify-content` n'a plus
  d'espace à répartir, un piège rencontré en l'écrivant. `justify-content:
  space-evenly` (pas `space-between`) sur `.stat-columns-row` de cette carte
  spécifiquement : demandé explicitement, l'espace entre les 2 jauges doit
  être identique à l'espace entre chaque jauge et le bord de la carte, pas
  concentré au milieu avec les jauges collées aux bords.
- **Carte Torrents (`stat-torrents-files`) : contenu ancré en haut, pas centré
  verticalement** (`justify-content: flex-start`, 2026-07-30, demandé
  explicitement) — cohérent avec `.stat-list` (indexeurs, tâches planifiées),
  qui suit le même principe pour un contenu de hauteur variable.
- **Accordéon "Ratio par tracker" remplacé par une simple carte `.stat-span-3`
  à plat dans le flux** (`tracker-card.html`, 2026-07-30, remplace
  `tracker-details.html`/`<details>`) — placée juste avant "Tâches
  planifiées" dans `build_stats_section()` : comme les rangées précédentes
  remplissent exactement 4 slots chacune, les deux tombent naturellement sur
  la même rangée (tracker en 1re position, tâches planifiées en 2e) sans
  logique de positionnement CSS dédiée.
- **Cartes dépendant d'une stack arrêtée : placeholder "Arrêté" plutôt que
  disparition silencieuse** (`render_stat_placeholder()`/
  `stat-placeholder.html`, 2026-07-30, demandé explicitement) — Débits/
  Ratios/Torrents/Ratio par tracker (dépendent de `vpn/transmission-vpn` via
  `fetch_transmission_stats()`) et Indexeurs (dépend de `arr/prowlarr`, via
  `render_indexers_card(running)`) affichent désormais un `.stat`/`.stat-span-N`
  grisé (`opacity: .45`, même esprit que `.card-down` pour les cartes de
  service Public/Local) avec le titre de la carte + "Arrêté", dans le même
  gabarit de span que la carte réelle (pour ne pas décaler la mise en page
  des cartes suivantes) — au lieu d'être simplement omises comme avant.
  Cible précisément le cas "stack arrêtée" : si la stack tourne mais que la
  donnée reste indisponible pour une autre raison (transmission-stats.py en
  échec, clé API Prowlarr absente), comportement best-effort inchangé (carte
  omise, pas de placeholder) — `render_indexers_card()` a donc changé de
  signature (prend `running`) pour court-circuiter avant tout `docker exec`
  si `arr/prowlarr` n'est pas dans `running`, plutôt que de tenter l'appel
  pour rien. Disques et Tâches planifiées n'ont pas de dépendance "carte
  entière" à une stack unique (Disques lit directement le filesystem hôte ;
  Tâches planifiées gère déjà chaque ligne individuellement, voir
  `require-running.sh` ci-dessus) — pas concernées par ce placeholder.

## Pièges à ne pas répéter

- **Un `%` non échappé dans la partie commande d'une ligne crontab est
  interprété par cron comme un saut de ligne** — tout ce qui suit devient
  l'entrée standard fournie à la commande, pas la suite de la ligne de
  commande. Rencontré le 2026-07-30 en ajoutant `date +%s > marqueur` dans
  `scripts/crontab` (voir carte "Tâches planifiées" ci-dessus) : la
  commande fonctionnait parfaitement testée à la main (`sh -c '...'`, ou
  même `env -i ... /bin/sh -c '...'` pour reproduire l'environnement
  minimal de cron), mais silencieusement `date +` sans argument sous le
  vrai daemon cron — `%s > __DATA_ROOT__/.cron-status/<nom>` était avalé
  comme stdin, jamais exécuté comme redirection. Aucune erreur visible
  nulle part (`MAILTO=""` supprime le mail que cron aurait envoyé sur un
  échec, et cron ne logue dans syslog que le lancement de la commande, pas
  sa sortie) — repéré uniquement en comparant l'âge du marqueur après
  plusieurs vrais ticks à ce qu'un test manuel produisait. Fix : échapper
  en `date +\%s`. Tout futur ajout dans `scripts/crontab` utilisant `%`
  (format `date`, ou toute autre commande) doit faire pareil — et se
  méfier qu'un test manuel réussi ne prouve rien sur le comportement réel
  sous cron pour cette classe de bug précise.
- **`vpn/transmission-vpn` ne doit jamais rejoindre un second réseau
  Docker** (ex. `traefik-public`) et sa variable `LOCAL_NETWORK` ne doit
  jamais contenir son propre sous-réseau — les deux cassent le routing
  sortant du tunnel (route `redirect-gateway def1` qui couvre
  `172.16.0.0/12`, la plage par défaut des réseaux Docker). Toujours
  passer par le sidecar `transmission-proxy` pour exposer le RPC ; pour
  autoriser un pair du même réseau Docker sans casser le routage, utiliser
  `UFW_ALLOW_GW_NET=true`, pas `LOCAL_NETWORK`. Détails complets : ISSUES.md.
- **`vpn/transmission-vpn` a besoin du module kernel `ip_tables` chargé sur
  l'hôte** — absent par défaut sur les Ubuntu récents (remplacé par
  `nftables`), nécessaire aux règles de routing/kill-switch de
  `haugene/transmission-openvpn`. Fix : `/etc/modules-load.d/ip-tables.conf`
  contenant `ip_tables` (déjà en place sur ce déploiement). Prérequis host,
  pas dans le compose file — à vérifier sur toute nouvelle machine.
- **Traefik ne retente pas seul un certificat ACME resté en échec** (ex.
  après un DNS temporairement en NXDOMAIN) — un restart du container est
  nécessaire une fois le problème sous-jacent corrigé.
- **`jellyfin.db` corrompue (`SQLite Error 11: database disk image is
  malformed`)** : ne pas juste supprimer le fichier pour forcer une
  régénération — `config/config/system.xml` garde
  `IsStartupWizardCompleted=true`, donc Jellyfin se croit en mise à
  niveau et plante en boucle sur d'anciennes migrations qui supposent un
  schéma déjà existant. Avant de reset : essayer une réparation
  (`sqlite3 <copie>.db ".recover" > dump.sql`, réimporter dans un fichier
  neuf, `PRAGMA integrity_check`/`foreign_key_check`, `REINDEX; VACUUM;`)
  — a fonctionné sans perte de données le 2026-07-20 malgré la
  corruption. Si reset complet malgré tout : repasser aussi
  `IsStartupWizardCompleted` à `false` dans `system.xml` pour repartir
  sur le vrai chemin "nouvelle installation". Le dossier
  `data/SQLiteBackups/` (backups auto intégrés à Jellyfin) était vide au
  moment de l'incident — vérifier de temps en temps qu'il se remplit
  réellement.
- Avant de modifier un des `docker-compose.override.yml` réels (gitignorés,
  contiennent les vrais chemins de la machine, sous le home de l'utilisateur),
  se rappeler qu'ils ne
  sont pas versionnés : toute évolution structurelle doit aussi se refléter
  dans le `.example` correspondant. Ne jamais copier un `.example` tel quel
  sans remplir ses placeholders (`/path/to/...`) — Docker crée sinon
  silencieusement l'arborescence bidon correspondante en root sur l'hôte
  (arrivé avec `arr/docker-compose.override.yml.example`, nettoyé le
  2026-07-22).
- **Rejoindre le réseau `vpn-internal` d'une autre stack** (ex. `arr/` pour
  atteindre `transmission-vpn:9091`) : son vrai nom Docker est
  `vpn_vpn-internal` (préfixé par le dossier du projet compose, `vpn/`
  ne fixe pas de `name:` de réseau) — le déclarer `external: true` avec
  juste `vpn-internal` échoue (`network ... declared as external, but
  could not be found`). Toujours ajouter `name: vpn_vpn-internal` sur la
  déclaration externe (voir `arr/docker-compose.yml`). Rejoindre ce réseau
  depuis un autre container ne pose aucun problème en soi — seul
  `transmission-vpn` lui-même ne doit jamais toucher un second réseau (cf.
  piège ci-dessus).
- **DNS du FAI qui renvoie `127.0.0.1` pour certains domaines** (blocage
  anti-piratage côté FAI, ex. domaines de trackers/indexeurs) — se présente
  comme une panne réseau (`Connection refused`) alors que le domaine répond
  normalement via un résolveur public. Rencontré sur `arr/prowlarr` en
  ajoutant un indexeur. Fix : forcer `dns: ${DNS_PRIMARY}/${DNS_SECONDARY}`
  (Cloudflare par défaut, `.env.shared`) sur le service concerné (déjà le
  cas sur Jellyfin par défaut ; ajouté aussi sur
  `prowlarr`/`sonarr`/`radarr`/`cross-seed`).
- **Sonarr/Radarr n'importent pas les fichiers vidéo posés en vrac à la
  racine d'un dossier scanné** (scan "dossiers non mappés"/Library Import)
  — ils ne reconnaissent que la convention un-film/une-série par
  sous-dossier, sans erreur ni log pour les fichiers ignorés. Pour un
  fichier existant hors de cette convention, utiliser **Manual Import**
  (liste aussi les fichiers en vrac, matching manuel), pas le scan
  automatique.
- **cross-seed + Sonarr/Radarr : Connect "Custom Script", pas "Webhook"**
  — le type Webhook générique de Sonarr/Radarr envoie un payload de test
  factice (pas de vrai hash de torrent) au moment d'enregistrer la
  connexion ; cross-seed le rejette (`A valid infoHash or an accessible
  path must be provided`), ce qui empêche l'enregistrement de la connexion
  côté Sonarr/Radarr (échec bloquant, pas juste un warning ignorable). La
  méthode documentée par cross-seed est un Custom Script
  (`arr/scripts/cross-seed-notify.sh`) qui lit `$sonarr_download_id`/
  `$radarr_download_id` et appelle l'API cross-seed lui-même.
- **`useClientTorrents: true` requis dans `arr/cross-seed/config.js`**
  (faux par défaut chez cross-seed) — sans ça, le webhook déclenché par
  `arr/scripts/cross-seed-notify.sh` ne consulte jamais le client réel pour
  matcher l'infoHash reçu et échoue systématiquement (`Torrent client does
  not have any torrent with criteria`), même quand le torrent y est bien
  présent (vérifié le 2026-07-22 en interrogeant directement l'API RPC de
  `transmission-vpn`). Le job périodique "inject" ne rattrape pas ces
  échecs non plus tant que ce réglage manque.
- **`arr/cross-seed/config.js` : les URLs Torznab Prowlarr sont par ID
  d'indexeur (`http://prowlarr:9696/<id>/api`), pas un endpoint agrégé** —
  repéré le 2026-07-28 : la config pointait en dur sur l'ID 1 ("Torr9"),
  supprimé depuis dans Prowlarr et remplacé par 4 indexeurs différents
  (IDs 2-5). Résultat silencieux pendant 4 jours depuis le déploiement :
  chaque webhook `cross-seed-notify.sh` déclenchait bien une recherche,
  mais toujours contre un indexeur mort (`410 Gone` dans les logs), donc
  0 résultat/0 injection à chaque fois — jamais d'erreur bloquante côté
  Sonarr/Radarr, juste un `[webhook] Found 0 torrents` systématique.
  Confirmé en comparant les IDs actifs à ceux synchronisés côté Sonarr
  (`GET /api/v3/indexer`, champ `baseUrl` de chaque indexeur `(Prowlarr)`).
  Si un indexeur est ajouté/supprimé/recréé dans Prowlarr, penser à
  vérifier ses ID(s) actuel(s) (page Indexers, ou l'URL de chaque
  indexeur `(Prowlarr)` côté Sonarr/Radarr) et à les refléter dans
  `CROSS_SEED_INDEXER_IDS` (`arr/.env`, voir ci-dessous) — ces IDs ne sont
  pas stables dans le temps et rien ne prévient d'un ID devenu obsolète
  autrement qu'en lisant les logs cross-seed.
  Ces IDs vivaient à l'origine en dur dans le tableau `torznab` de
  `config.js` — déplacés dans `CROSS_SEED_INDEXER_IDS` (`arr/.env`) le
  2026-07-29, repéré par l'utilisateur comme une valeur propre à ce
  déploiement (quels indexeurs Prowlarr existent, dans quel ordre) qui
  n'avait pas sa place dans un fichier versionné sur un repo public — même
  principe que `TRACKER_ALIASES` (voir plus haut). `config.js` lit
  désormais `process.env.CROSS_SEED_INDEXER_IDS.split(",")` au lieu d'un
  tableau littéral. Nyaa.si (id 4, public, pas de ratio) exclu de cette
  liste le même jour : cross-seeder un torrent déjà obtenu d'un tracker à
  ratio vers un tracker public n'apporte aucun bénéfice de ratio (seul
  l'inverse — un torrent Nyaa cross-seedé vers un tracker à ratio — a un
  intérêt), et le retrait ne coupe que cette direction : un fichier
  d'origine Nyaa.si continue d'être cross-seedé normalement vers les
  indexeurs à ratio restants (`torznab` ne filtre que les cibles
  cherchées, pas la source du fichier).
- **`searchCadence` dans `arr/cross-seed/config.js` impose deux contraintes
  de validation non documentées ailleurs que dans l'erreur elle-même**
  (repéré le 2026-07-28 en l'ajoutant, boucle de crash immédiate sinon —
  `restart: unless-stopped` redémarre en boucle sur une config invalide,
  pas de dégradation silencieuse) : `excludeRecentSearch` doit être défini
  et valoir au moins 3x `searchCadence` ; `excludeOlder` doit lui aussi
  être défini et valoir 2 à 5x `excludeRecentSearch`. Valeurs retenues :
  cadence 3 jours, `excludeRecentSearch` 9 jours, `excludeOlder` 30 jours —
  la recherche périodique automatique ne couvre donc que les torrents vus
  il y a moins de 30 jours (ajouts récents), jamais tout l'historique. Le
  rattrapage de l'historique complet (au-delà de 30 jours) se fait à la
  demande via `docker exec <container> cross-seed search --exclude-older
  999999999 --exclude-recent-search 0` (bypass explicite des deux filtres),
  fait une première fois le 2026-07-28 juste après le fix des IDs Torznab
  ci-dessus — jamais automatisé (potentiellement des centaines de requêtes
  aux indexeurs en une fois, à ne lancer qu'en connaissance de cause).
- **`seerr` (image `ghcr.io/seerr-team/seerr`) ne chown pas lui-même son
  volume `/app/config`** — contrairement aux images linuxserver.io (`arr/`),
  il tourne nativement en UID 1000 sans étape root-puis-drop, donc si
  `${DATA_ROOT}/.seerr/config` n'existe pas encore, Docker le crée en
  `root:root` et le container crash en boucle (`EACCES` sur
  `/app/config/logs`). Avant le premier `make up STACK=seerr` : `sudo chown
  -R 1000:1000 ${DATA_ROOT}/.seerr` (1000 = PUID/PGID par défaut).
- **Seerr ne détecte les films/séries déjà téléchargés par Sonarr/Radarr
  comme "disponibles" qu'en scannant les bibliothèques Jellyfin** — pas en
  interrogeant Sonarr/Radarr directement pour l'existant. Sans bibliothèque
  Jellyfin pointant sur `${DATA_ROOT}/library` (montage ajouté le
  2026-07-22 à `jellyfin/docker-compose.yml`, absent par défaut), tout
  apparaît comme non disponible et Seerr propose de re-demander du contenu
  déjà présent. Fix : ajouter les bibliothèques Jellyfin (`/library/film` en
  type Films, `/library/series` en type Séries — noms de dossiers réels,
  pas de traduction anglaise), puis lancer manuellement le job "Jellyfin
  Full Library Scan" côté Seerr (Settings → Jobs & Cache) au lieu d'attendre
  le cron périodique.
- **Un bind-mount ne peut pas être monté sous un point de montage déjà
  `:ro`** — Docker ne peut pas créer le mountpoint interne dans un parent
  en lecture seule (`mkdirat ... read-only file system`). Déjà rencontré
  sur `arr/cross-seed` (`config.js` vs volume `/links`, voir son compose
  file) et à nouveau le 2026-07-23 sur `dashboard/` (voulait monter
  `./assets` sous `./html:...:ro`) — solution : un seul mount, le script
  de génération (`scripts/generate-dashboard.py`) copie les logos dans le
  dossier généré plutôt que de les monter séparément.
- **Deux bind-mounts Docker séparés du même disque physique n'autorisent
  pas les hardlinks entre eux**, même si `stat` rapporte le même `st_dev`
  des deux côtés — `link()` refuse avec `Cross-device link` dès que
  source et destination sont sur deux montages distincts, peu importe
  que ce soit littéralement la même partition sous-jacente (confirmé le
  2026-07-23 : `sonarr`/`radarr` montaient `${DATA_ROOT}/.transmission/data:/data`
  et `${DATA_ROOT}/library:/library` séparément — `copyUsingHardlinks:
  true` était bien actif, mais chaque import retombait silencieusement
  sur une copie complète, doublant l'espace disque de tout ce qui était
  déjà importé, ~185 Go récupérés en corrigeant). Fix : un seul mount
  `${DATA_ROOT}:/data_root` dans `arr/docker-compose.yml` pour sonarr et
  radarr (au lieu de deux mounts séparés), avec remote path mapping
  (host `transmission-vpn`, `/data/completed/` → `/data_root/.transmission/data/completed/`)
  et root folders/chemins des séries et films existants repointés sur
  `/data_root/library/...` via l'API. Même piège sur `arr/cross-seed`
  (`dataDirs`/`linkDirs` montés séparément dans l'ancien
  `docker-compose.yml`) corrigé le 2026-07-23 : contrairement à
  Sonarr/Radarr, cross-seed n'a pas de "remote path mapping" — il compare
  tel quel le chemin renvoyé par le client torrent (`/data/completed/...`)
  à son propre `dataDirs`, donc impossible de renommer ce mount comme pour
  sonarr/radarr sans casser le matching. Fix : garder le même montage
  `${DATA_ROOT}/.transmission/data:/data` (comme avant, mais sans `:ro`)
  et faire pointer `linkDirs` vers un sous-dossier de ce même mount
  (`/data/.cross-seed-links`, host
  `${DATA_ROOT}/.transmission/data/.cross-seed-links`) au lieu de l'ancien
  volume séparé `.arr/cross-seed/links` (supprimé, dossier vide au moment
  du fix).
- **Connexion Sonarr/Radarr → Jellyfin ("Emby/Jellyfin" notification,
  ajoutée le 2026-07-24 via l'API, pas les UI — pas dans un fichier
  versionné) nécessite `mapFrom`/`mapTo`**, sinon le refresh ciblé déclenché
  sur import/upgrade ne trouve pas le bon dossier côté Jellyfin : Sonarr/
  Radarr voient la bibliothèque sous `/data_root/library/...` (même mount
  unique que le fix hardlink ci-dessus), Jellyfin la voit sous
  `/library/...` (son propre mount, `jellyfin/docker-compose.yml`).
  `mapFrom=/data_root/library` / `mapTo=/library` dans les deux connexions.
  Cible `jellyfin:8096` en direct (réseau `traefik-public` partagé, pas de
  passage par Traefik). Clé API réutilisée depuis celle déjà générée pour
  Seerr plutôt qu'une clé dédiée à Sonarr/Radarr — voir ARCHITECTURE.md.
- **`scripts/backup.sh` dumpait silencieusement la mauvaise base Postgres
  depuis le début** (repéré le 2026-07-24 en testant `make restore` pour de
  vrai — jusque-là jamais exercé) : `pg_dump -U "$POSTGRES_USER"
  "${POSTGRES_DB:-$POSTGRES_USER}"` retombe sur `$POSTGRES_USER` ("postgres",
  le superuser d'amorçage de l'image officielle) quand `POSTGRES_DB` n'est
  pas définie — et `nextcloud/.env` ne définit que `POSTGRES_USER`/
  `PASSWORD`, jamais `POSTGRES_DB`. Toutes les sauvegardes précédentes
  avaient donc un `nextcloud-db.sql` de 26 lignes (juste l'en-tête pg_dump,
  0 `COPY`) au lieu du vrai dump (~1,57M lignes, 279 tables) — la base
  réelle s'appelle `nextcloud` (codée en dur via `POSTGRES_DB=nextcloud`
  sur le service `app`, `nextcloud/docker-compose.yml`), pas
  `$POSTGRES_USER`. Fix dans `backup.sh` et dans les instructions
  affichées par `restore.sh` : fallback `${POSTGRES_DB:-nextcloud}`.
  Validé par un restore réel + import du dump dans une base Postgres
  temporaire (`restore_test`, supprimée après coup) : 279/279 tables,
  compte de lignes identique à la base live sur une table témoin
  (`oc_users`). Retenir la leçon plus généralement : un chemin de
  restauration jamais exercé peut cacher ce genre de bug arbitrairement
  longtemps — voir la limite `${VAR:-default}` d'un fallback silencieux
  quand `VAR` n'est jamais définie nulle part.
- **Un `os.stat()` nu sur un fichier `library/`/`.transmission/data/` ne
  suit pas correctement un symlink cross-seed** (`torrent-cleanup.py`,
  repéré le 2026-07-28 en ajoutant le marqueur `'M'` ci-dessus) — cross-seed
  (`linkType` symlink par défaut, `arr/cross-seed/config.js`) crée ses liens
  dans `.cross-seed-links/<tracker>/...` en pointant vers le chemin **tel
  que vu par le conteneur** (`/data/completed/...`), pas le chemin hôte où
  tourne ce script Python. `os.stat()` suit le lien avec la racine de
  l'hôte, qui n'a pas de `/data` : le fichier semble absent alors qu'il
  existe très bien à l'intérieur des conteneurs — 91 faux positifs constatés
  sur ~230 torrents (tous les cross-seeds fraîchement injectés) avant le
  fix. Solution : `resolved_stat()` détecte le symlink via
  `os.path.islink()`, lit sa cible via `os.readlink()`, et si elle commence
  par `/data` la traduit en chemin hôte avec la même fonction
  `container_path_to_host()` que le reste du script avant de faire le vrai
  `os.stat()`. Le même bug affectait aussi silencieusement le marqueur `'L'`
  et `find_library_matches()` (sous-évaluaient les correspondances
  library/ pour tout torrent cross-seed) — corrigés avec le même helper.
- **La résolution nom-de-tracker (`resolve_tracker_name`, dupliquée dans
  `torrent-cleanup.py` et `scripts/transmission-stats.py`, voir ci-dessus)
  échouait sur YggReborn** (repéré le 2026-07-28) — le domaine d'annonce
  BitTorrent réel (`tracker.yggreborn.org`) et celui listé par Prowlarr
  (`indexerUrls: www.yggreborn.org`) sont deux sous-domaines **frères** du
  même domaine de base (`yggreborn.org`), pas l'un suffixe de l'autre : un
  `hostname.endswith("." + domain)` nu ne matche jamais. Fix : `base_domain()`
  (2 derniers labels du hostname, ex. `www.yggreborn.org` → `yggreborn.org`)
  appliqué des deux côtés avant de comparer, dans les deux scripts.
  Nyaa.si n'a lui aucun tracker qui lui soit propre — seulement des
  trackers publics génériques (`nyaa.tracker.wf`, `open.stealth.si`,
  `tracker.opentrackr.org`, `exodus.desync.com`, `tracker.torrent.eu.org`),
  aucune info Prowlarr ne permet de les rattacher à Nyaa spécifiquement.
  Confirmé par l'utilisateur le 2026-07-28 que ce bundle exact de 5 domaines
  est bien celui de Nyaa.si (vérifié : toujours les 5 ensemble, jamais
  mélangés à un torrent d'un autre indexeur dans cette bibliothèque) — ajouté
  en alias, matché en exact et non via `base_domain()` : `tracker.torrent.
  eu.org` réduirait à `eu.org`, un vrai domaine public partagé par
  d'innombrables sites sans rapport, un faux positif bien pire que
  l'inverse. Si un futur torrent d'un autre indexeur ajoute l'un de ces
  trackers publics en complément du sien (pratique courante pour la
  redondance), il serait aussi étiqueté "Nyaa.si" à tort — accepté en
  connaissance de cause, à revoir si ça arrive. Ces alias vivent dans
  `TRACKER_ALIASES` (`arr/.env`, voir `.env.example`), pas en dur dans le
  code des scripts (premier essai le 2026-07-28, corrigé la même journée
  suite à une remarque de l'utilisateur) : quels trackers publics un
  indexeur embarque est une donnée propre à ce déploiement, pas une
  décision d'architecture qui a sa place dans un script versionné sur un
  repo public — même principe que les autres valeurs par-déploiement déjà
  en `.env`/`.env.shared` (voir plus haut).
  Piège annexe repéré en ajoutant cet alias : `transmission-stats.py`
  sommait `uploadedEver`/`downloadedEver` une fois par **host** brut, pas
  par nom résolu — un torrent Nyaa comptait donc son volume 5 fois (une
  par tracker du bundle) une fois les 5 hosts collapsés sous le même nom,
  gonflant `uploaded`/`downloaded` d'un facteur 5 dans la section
  Transmission du dashboard (le ratio affiché n'était pas faussé, par
  coïncidence : numérateur et dénominateur gonflés du même facteur). Fixé
  en dédupliquant par nom résolu avant d'accumuler. Même dédup appliquée à
  `tracker_display()` dans `torrent-cleanup.py` (affichait sinon
  `"Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si"` dans la colonne TRACKER).
  **Un tracker non rattachable à un indexeur Prowlarr est désormais traité à
  part des indexeurs configurés** (2026-08-02, dans les deux consommateurs) :
  `resolve_tracker_name()` renvoie `(nom, officiel)` au lieu du seul nom,
  `officiel=False` signifiant "retombé sur le hostname brut" (tracker public
  embarqué dans le `.torrent`, pas un indexeur qu'on interroge nous-mêmes).
  Déclencheur : une vieille release YggReborn (post de janvier, grabbée le
  2026-08-02) embarquait **24 hosts d'annonce** — son tracker plus une
  vingtaine de trackers publics de secours, dont des IPs nues. Conséquences
  côté affichage, différentes par frontend :
  - Dashboard (`tracker-card.html`) : les lignes non officielles restent dans
    le HTML généré mais sont masquées par CSS (`.tracker-row-other`), un
    switch "Tous les trackers" les révèle (classe `show-all-trackers`, état
    en `localStorage` — même mécanisme et même raison que le switch
    Monitoring : la page est régénérée par cron toutes les 5 min). Filtrage
    côté client, pas côté Python : la donnée reste dans le JSON de
    `transmission-stats.py` (champ `official` par entrée).
  - `clearr` (colonne TRACKER) : tous les non-officiels d'un même torrent
    sont repliés sous un seul libellé `"Autre"` (`OTHER_TRACKER_LABEL`), les
    hostnames bruts partant dans un tooltip `title=` natif — **pas** un
    tooltip Bootstrap, qui aurait exigé Popper (non vendoré, voir plus haut).
    `tracker_display()` renvoie donc `(libellé, hostnames_non_officiels)`.
    Bénéficie aussi à la TUI, qui hérite du libellé court (sa colonne
    tronquait déjà à 19 caractères, donc jamais cassée — c'était bien le web
    le cas à corriger).
- **Supprimer un episodefile via l'API Sonarr (`DELETE
  /api/v3/episodefile/{id}`) déclenche quasi instantanément la recherche
  automatique interne de Sonarr pour l'épisode redevenu "manquant"** —
  rencontré le 2026-07-29 sur `THE GHOST IN THE SHELL` (id série 21) :
  l'intention était de supprimer les fichiers `[Reza] ... v2/v3` (bloqués en
  upgrade par la comparaison de révision, voir plus haut) puis grabber
  manuellement une release VOSTFR précise (audio japonais + sous-titres FR,
  sans doublage) via `POST /api/v3/release`. Sonarr a grabé de lui-même une
  release MULTi (Tsundere-Raws, doublage français inclus, déjà scorée plus
  haut que VOSTFR sur le profil par défaut — voir plus haut) dans la fenêtre
  de quelques secondes entre la suppression et la tentative de grab manuel,
  avant même qu'une recherche puisse être relancée pour cibler spécifiquement
  la release voulue. Sur ce cas précis, l'issue (doublage FR) a finalement
  été acceptée par l'utilisateur après coup, mais retenir pour la prochaine
  fois : supprimer un fichier existant pour forcer un remplacement laisse une
  fenêtre de course avec la recherche automatique de Sonarr, qui peut
  grabber autre chose que prévu si le profil de la série préfère déjà un
  autre format. Pas de parade fiable identifiée encore (voir aussi le piège
  ci-dessus sur le cache de release qui expire trop vite pour enchaîner
  recherche fraîche + grab manuel de façon fiable).
- **Le cache de résultats de `GET /api/v3/release?episodeId=...` expire vite
  côté Sonarr** — rencontré le 2026-07-29, même incident que ci-dessus : un
  `guid` récupéré par un appel `GET /api/v3/release` précédent peut déjà être
  invalide au moment du `POST /api/v3/release` (grab), avec l'erreur
  `"Couldn't find requested release in cache, try searching again"` — a
  fortiori si une recherche automatique de Sonarr (voir piège ci-dessus)
  s'est intercalée entre les deux et a invalidé/remplacé le cache. Le grab
  manuel doit suivre la recherche GET dans la foulée, sans appel
  intermédiaire qui laisse le temps à autre chose de rafraîchir le cache.
- **Le paramètre `seriesId` de `GET /api/v3/history` n'est pas fiable** —
  vérifié le 2026-07-29 en testant le skill `anime-vf` sur One Punch Man :
  la réponse contenait des entrées d'autres séries (Jujutsu Kaisen) malgré
  le filtre. Toujours filtrer côté client sur `record["seriesId"]`, jamais
  faire confiance au filtrage serveur de cet endpoint. Voir aussi
  `eventType=1` (entier) plutôt que la chaîne `"grabbed"`, qui renvoie une
  réponse vide dans nos tests.
- **`cutoffFormatScore: 10000` (valeur par défaut des guides TRaSH, "upgrade
  jusqu'à la meilleure release possible") peut provoquer des re-téléchargements
  en boucle d'une release déjà possédée** — diagnostiqué le 2026-07-29 sur
  `THE GHOST IN THE SHELL` S01E04 (profil Sonarr `Anime (Fansub)`, id 9) :
  grabbé 3 fois en une journée, confirmé via `GET /api/v3/history` que c'était
  littéralement le même magnet/infoHash (`nyaa.si/view/2138643`) à chaque
  fois, pas 3 releases concurrentes. Mécanisme : les custom formats
  `VOSTFR`/`SUBFRENCH` (`ReleaseTitleSpecification`) matchent sur le suffixe
  entre parenthèses que certains groupes (ex. Tsundere-Raws) ajoutent au titre
  du post Nyaa.si (`(VF, FRENCH, SUBFRENCH, VOSTFR, ...)`) — un suffixe absent
  du nom réel du fichier `.mkv` dans le torrent. Au grab, la release score 150
  (VOSTFR détecté dans le titre complet) ; après import, le fichier
  ré-évalué ne score plus que 100 (pas de "VOSTFR" dans le nom de fichier) →
  la même release déjà possédée a l'air d'être un upgrade de +50 à la
  prochaine recherche → regrab → réimport → score à nouveau tronqué → boucle.
  Avec `cutoffFormatScore=10000` (max réellement atteignable sur ce profil :
  155) l'épisode ne sort jamais de la liste "cutoff unmet" et reste
  éligible à chaque RSS sync (~15 min) / recherche périodique — cause
  probable aussi du quota C411 tapé le 2026-07-28 ([[project_c411_quota_2026-07-28]]),
  la plupart des profils étant dans le même état (recherches perpétuelles qui
  ne trouvent en général rien de mieux, mais consomment du quota indexeur).
  Fix appliqué le 2026-07-29 : `cutoffFormatScore` abaissé à un plafond
  réaliste (somme des custom formats à score positif réellement cumulables
  sur une même release, pas la somme brute de `formatItems` qui compte aussi
  des alternatives mutuellement exclusives comme les plateformes de
  streaming) sur les 5 profils qui l'avaient à 10000 : `WEB-1080p`→1782,
  `WEB-2160p (Combined)`→4000, `Anime (Fansub)`→155, `Anime (Fansub) VF`→335,
  `Anime (Fansub) VOSTFR`→105. Fait via API directe pour les 4 profils non
  gérés par recyclarr (mêmes conventions que les profils Anime, voir plus
  haut) ; pour `WEB-2160p (Combined)` (géré par `arr/recyclarr/recyclarr.yml`,
  resynchronisé par le cron interne `@daily` du conteneur recyclarr, cf.
  commentaire dans son `docker-compose.yml`), un simple appel API aurait été
  écrasé au sync suivant — passé plutôt par la clé `upgrade.until_score` du
  schéma de config recyclarr (`quality_profiles:`, confirmé via
  `https://schemas.recyclarr.dev/latest/config/quality-profiles.json`),
  validé avec `recyclarr sync --preview` avant application réelle.
  **Ce fix seul ne suffisait pas** — constaté le 2026-08-02 sur `BLACK TORCH`
  S01E05 (5 grabs, 4 imports en 2h le 2026-08-01), même signature sur
  Chainsmoker Cat / Sparks of Tomorrow / Daemons of the Shadow Realm / Ghost
  in the Shell (2 grabs à ~15 min d'écart, l'intervalle du RSS sync ; 8 grabs
  strictement redondants sur 280 dans l'historique). Abaisser
  `cutoffFormatScore` ne touche que le *plafond* ; le moteur de la boucle est
  l'écart entre le score annoncé au grab et le score réévalué après import, et
  155 restait au-dessus du maximum qu'un *fichier* peut atteindre sur ce
  profil (100 pour une release MULTi, `VOSTFR` ne matchant jamais le nom du
  `.mkv`) — la porte de l'upgrade ne se refermait donc jamais. Correctif de
  fond appliqué le 2026-08-02, en deux volets :
  - **Lookahead « pas entre parenthèses » `(?![^()]*\))`** ajouté aux regex
    qui matchaient le suffixe `(VF, FRENCH, SUBFRENCH, VOSTFR)` des posts
    Tsundere-Raws. Validé avant application sur les 193 titres réels de
    `GET /api/v3/history` : 31 faux positifs supprimés, 81 vrais
    VOSTFR/SUBFRENCH conservés ; CF `MULTi` vérifié non concerné (0 titre où
    il ne matche qu'entre parenthèses), donc laissé tel quel.
  - **`Anime (Fansub)` : `cutoffFormatScore` 155 → 100**, le vrai maximum
    atteignable par un fichier importé sur ce profil — filet pour tout futur
    suffixe exotique que le lookahead ne couvrirait pas.

  Piège structurant rencontré en l'appliquant : **le CF `VOSTFR` est géré par
  recyclarr** (`trash_id 07a32f77690263bb9fda1842db7e273f`, référencé deux
  fois dans `recyclarr.yml` — `custom_formats` pour son score sur
  `WEB-2160p (Combined)` et `custom_format_groups` via `[Optional] French
  Audio Version`), et recyclarr resynchronise la **définition** du CF, pas
  seulement son score : un `PUT /api/v3/customformat/49` est bien appliqué
  mais serait réécrit au sync quotidien suivant. Repéré uniquement parce que
  `recyclarr sync --preview` a été relancé *après* le PUT et affichait
  `Update │ VOSTFR` (réflexe à garder pour tout changement API sur un CF :
  vérifier qu'il n'est pas dans un `trash_ids` avant de supposer qu'il tient).
  Solution retenue plutôt que de sortir `VOSTFR` de `recyclarr.yml` (qui
  aurait aussi fait perdre sa création automatique sur un déploiement neuf,
  donc un recul de reproductibilité) : un **CF distinct que nous possédons**,
  `VOSTFR (hors suffixe)` (id 51), portant les regex corrigées — même schéma
  que le CF `FRENCH` (id 50, créé à la main le 2026-07-28, confirmé absent du
  diff recyclarr donc modifiable directement, ce qui a été fait). Le CF
  `VOSTFR` du guide reste intact et scoré **0** sur les profils concernés, le
  nouveau CF reprenant son score : `Anime (Fansub)` (9) et
  `Anime (Fansub) VOSTFR` (11), les deux seuls profils anime où `VOSTFR` est
  positif donc où `grab > fichier` est possible. Vérifié après coup :
  `recyclarr sync --preview` ressort `No changes` sur Custom Format ET Quality
  Profile, et `GET /api/v3/parse` score le titre à suffixe 100 (= le score du
  fichier) au lieu de 150.
  Deux trous connus laissés en l'état, délibérément : `Anime (Fansub) VF` (10)
  a `VOSTFR` à **-100**, donc le suffixe fait scorer le grab *plus bas* que le
  fichier — asymétrie inverse, incapable de boucler (son vrai risque venait du
  CF `FRENCH`, corrigé) ; `WEB-2160p (Combined)` (8) a `VOSTFR` à +100 et est
  géré par recyclarr, mais le suffixe est une pratique Tsundere-Raws (anime
  1080p) qui ne croise pas ce profil 2160p — à revoir si un regrab en boucle y
  apparaît.

  **Tous les `cutoffFormatScore` repris le 2026-08-02** en généralisant le
  raisonnement : un cutoff n'a de sens que s'il est (a) atteignable et (b)
  posé là où l'objectif du profil est rempli. Le calcul de 2026-07-29
  additionnait naïvement les CF à score positif, sans voir que beaucoup sont
  **mutuellement exclusifs** (un seul tier, une seule plateforme de streaming,
  un seul codec vidéo, un seul format audio, une seule résolution, un seul
  niveau de repack — les specs `negate` du guide les enchaînent) : plusieurs
  valeurs "réalistes" étaient donc encore inatteignables. Maximums réels
  recalculés en tenant compte de ces groupes :
  - `Anime (Fansub) VF` (10) : 335 → **200** (= score `FRENCH`, son CF
    déterminant ; max réel 325, l'ancien 335 comptait AV1 *et* x265).
  - `Anime (Fansub) VOSTFR` (11) : 105 → **20** (max réel 95). Pas 50
    (= score `VOSTFR`) comme le raisonnement "CF déterminant" le voudrait :
    20 = score `MULTi` sur ce profil, choisi pour que les 14 séries migrées
    depuis `Anime (Fansub)` (voir ci-dessous) ne repartent pas en masse en
    recherche. Contrepartie assumée : un fichier MULTi déjà en place bloque
    l'upgrade vers un VOSTFR, la préférence VOSTFR ne joue donc plus qu'au
    moment du grab, quand les deux sont proposés dans la même recherche.
  - `WEB-2160p (Combined)` (8) : 4000 → **2000** (max réel 3907).
  - Radarr `[SQP] SQP-1 WEB (2160p)` : 10000 → **1800** (max réel 4562) —
    jamais couvert par le correctif de 2026-07-29, qui n'avait touché que
    Sonarr. Ajouté dans `recyclarr.yml` (le champ n'y était pas du tout,
    d'où le défaut du guide).
    Piège rencontré en l'ajoutant : dès qu'un profil fournit une liste
    `qualities:` explicite (le cas depuis l'activation HDTV/DVD du
    2026-08-01), recyclarr **exige** `until_quality` en plus de
    `until_score`, sinon le sync échoue en validation. Repris tel quel du
    cutoff qualité déjà en place (`Bluray|WEB-2160p`) pour ne rien changer
    d'autre que le score.
  Pour les deux profils généralistes (8 et Radarr 7), pas de CF déterminant :
  la valeur est calée sur le haut de la distribution réellement observée
  (Sonarr 8 : 43 fichiers, médiane 250, max 3405 ; Radarr : 19 fichiers,
  médiane 255, max 2105). Ces distributions sont **bimodales** selon qu'une
  release porte ou non un tag de groupe "Tier" (+1600 à 1700), largement
  absent des trackers FR — même constat qui avait fait remettre
  `min_format_score` à 0. Viser le maximum aurait maintenu des recherches
  perpétuelles (donc du quota indexeur) pour la majorité des titres, pour
  lesquels aucune release Tier n'existera jamais.

- **Profil Sonarr `WEB-1080p` supprimé** (2026-08-02, demandé) — assigné à
  aucune série, absent de `recyclarr.yml` comme de `arr/profiles/`, donc
  jamais recréé (vérifié par un cycle `recyclarr-sync` +
  `apply-arr-overrides.py` complet après suppression). Ses 37 scores de
  custom format et son `cutoffFormatScore=1782` n'avaient jamais été
  reproductibles depuis le repo — c'était le seul trou identifié en
  inventoriant les profils ce jour-là, refermé en supprimant le profil
  plutôt qu'en le versionnant. La mention `WEB-1080p`→1782 plus haut est un
  reliquat historique du correctif de 2026-07-29, le profil n'existe plus.
  Profils Sonarr restants : les 6 par défaut (aucun utilisé),
  `WEB-2160p (Combined)`, `Anime (Fansub) VF`, `Anime (Fansub) VOSTFR`.

- **Profil Sonarr `Anime (Fansub)` supprimé** (2026-08-02, demandé) : ses 14
  séries (dont One Piece, 23 saisons — 133 épisodes au total) déplacées vers
  `Anime (Fansub) VOSTFR` via `PUT /api/v3/series/editor`, puis
  `DELETE /api/v3/qualityprofile/9`. Ordre imposé : Sonarr refuse de
  supprimer un profil encore assigné, et il fallait d'abord le retirer de
  `arr/profiles/sonarr-anime.json` sous peine de le voir recréé au cron
  suivant par `apply-arr-overrides.py`. Impact chiffré avant d'agir (c'est
  ce qui a fixé le cutoff à 20 plutôt que 50) : à 50, 70 des 133 fichiers
  repassaient sous le cutoff ; à 20, seulement 17 — ceux qui n'ont ni
  `MULTi` ni `VOSTFR` (One Piece ×10, Dorohedoro ×4, Smoking Behind… ×2,
  BLACK TORCH ×1), donc légitimement à améliorer.
  **Piège de méthode** : `GET /api/v3/wanted/cutoff` ne reflète **que** le
  cutoff *qualité*, pas `cutoffFormatScore` — un épisode dont le fichier est
  à la bonne qualité mais sous le score n'y apparaît pas, alors qu'il reste
  bel et bien éligible à l'upgrade. Ne pas s'en servir pour estimer l'ampleur
  d'une vague de re-recherche : recalculer les scores fichier par fichier
  (`GET /api/v3/episodefile`, champ `customFormats`, croisé avec les
  `formatItems` du profil cible), ou vérifier au cas par cas avec
  `GET /api/v3/release` dont les `rejections` mentionnent explicitement
  `Existing file meets cutoff`.
- **Un bind-mount Docker d'un fichier unique (pas un dossier) peut rester
  figé sur l'ancien inode si le fichier hôte est remplacé plutôt que modifié
  en place** — repéré le 2026-07-29 en éditant `arr/recyclarr/recyclarr.yml`
  (monté `:ro` dans `recyclarr`) : le conteneur continuait de lire l'ancien
  contenu après l'édition (`docker exec ... cat` ne montrait pas le
  changement), alors que `docker inspect` confirmait le bon chemin hôte monté.
  Cause : l'inode du fichier avait changé sur l'hôte (`stat -c '%i'` différent
  côté hôte et côté conteneur) — un bind-mount de fichier suit l'inode capturé
  au démarrage du conteneur, pas le chemin. Un simple `docker restart` du
  conteneur suffit à faire relire le fichier à jour ; ne pas conclure trop
  vite qu'un changement de config n'a "pas pris" sans vérifier ça d'abord.

## Repo

```
server/
├── .env.shared(.example)     # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT — réel gitignoré, .example versionné
├── Makefile                  # network/up/down/config/logs/update/update-all/backup/restore/cron-install STACK=<nom> ; dashboard-refresh/clearr/arr-overrides (sans STACK)
├── README.md                  # doc humaine : services, install
├── ARCHITECTURE.md            # doc humaine : architecture, choix structurants
├── ISSUES.md                  # doc humaine : problèmes rencontrés
├── scripts/
│   ├── crontab                    # source de vérité du crontab hôte — `make cron-install`
│   ├── backup.sh                   # sauvegarde restic hebdomadaire
│   ├── restore.sh                  # restauration guidée d'un snapshot restic
│   ├── generate-dashboard.py       # régénère dashboard/html/ — `make dashboard-refresh`
│   ├── transmission-stats.py       # JSON ratios/débits pour generate-dashboard.py
│   ├── apply-arr-overrides.py      # réapplique tailles/language des 2 profils qualité principaux + provisionne la config anime — `make arr-overrides`
│   └── require-running.sh          # exit 0 si les services <project>/<service> donnés tournent — guard cron + backup.sh
├── sauvegarde/                # non versionné — dépôt restic + mot de passe + staging
├── traefik/                  # socket-proxy + traefik + dashboard (page statique de liens) ; .env(ACME_EMAIL)/.example
├── jellyfin/                 # docker-compose.yml + override.yml(.example) pour les bibliothèques
├── nextcloud/                 # db-next/app/web/news-updater ; .env/.example ; override.yml(.example)
├── vpn/                       # transmission-vpn (réseau isolé) + sidecar transmission-proxy ; .env/.example
├── arr/                       # prowlarr/sonarr/radarr/cross-seed/recyclarr/clearr ; .env/.example ; override.yml.example (optionnel)
│   ├── clearr/                 # web (FastAPI/Jinja2/Bootstrap) + TUI + CLI delete-by-inode, un seul core.py partagé — voir plus haut
│   └── profiles/               # config arr custom versionnée (sonarr-anime.json) — appliquée par scripts/apply-arr-overrides.py
├── seerr/                     # recherche/requête unifiée (successeur Jellyseerr/Overseerr) ; pas de .env (config via son assistant web)
└── dashboard/                 # templates/ (vues, string.Template) + assets (logos, css, js) + html/ généré — pas de compose file, servi par traefik/ (voir ci-dessus)
```

`make network` (crée `traefik-public` si absent) avant tout `make up`.

## Sauvegarde — repères rapides

- `make backup` (aussi via cron dimanche 3h, `scripts/crontab`) : dump
  `pg_dump` Nextcloud + manifeste des digests d'images en cours
  d'exécution + `restic backup` + `restic forget --keep-weekly 8
  --prune` + tag git `backup-YYYY-MM-DD` si l'infra a changé depuis le
  dernier tag de ce type, poussé sur `origin`.
- `make restore SNAPSHOT=<id|latest>` : restaure dans un dossier à part et
  affiche les étapes manuelles — ne touche jamais le live automatiquement.
- Mot de passe restic dans `sauvegarde/restic-password` (gitignoré,
  généré au premier `make backup`) — pas de copie ailleurs = dépôt
  illisible en cas de perte.
- Résilience visée : perte du disque `DATA_ROOT` → restauration depuis
  `sauvegarde/` (sur un disque différent). Perte de celui-ci → seul
  l'infra-as-code est récupérable depuis GitHub, la sauvegarde restic est
  perdue avec (accepté pour l'instant, pas d'offsite).

Détails, rationale et guide d'installation complet : `README.md` /
`ARCHITECTURE.md` / `ISSUES.md`.
