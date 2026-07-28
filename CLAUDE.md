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
  génériques). Le username Unix `ebola` reste en clair dans les
  `docker-compose.override.yml` gitignorés (pas versionnés, donc pas
  concernés) — décision explicite de l'utilisateur.
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
- **Les groupes de cartes Débits/Ratio sont côte à côte sur une même ligne**
  (`.stats-row`, flex + `flex-wrap` pour retomber en colonne sur écran
  étroit — ajouté le 2026-07-28, demandé par l'utilisateur), chaque groupe
  gardant son titre (`h3`) en haut à gauche (`stats-row.html`/`stats-group.html`,
  `stats-row.html` généralisée à N groupes — `$groups`, pas des slots fixes
  `$debits_group`/`$ratio_group` — pour être réutilisée par la deuxième
  ligne ci-dessous). Une deuxième ligne a été ajoutée en dessous (même
  jour, demandé), d'abord sous un unique groupe "Système", puis éclatée en
  trois sous-sections avec chacune son propre titre — **Torrents**,
  **Disque**, **Indexeurs** — demandé explicitement peu après (le titre
  générique "Système" retiré). Torrents : une carte (actifs, surveillés,
  en téléchargement, en erreur — 4 lignes distinctes) ; Disque : jauge
  d'espace disque libre ; Indexeurs : liste des indexeurs Prowlarr (le
  libellé interne "Indexeurs Prowlarr" de la carte, redondant avec le
  nouveau titre de sous-section, retiré au passage).
  La carte torrents a changé de forme plusieurs fois le même jour : d'abord
  deux cartes séparées (actifs/surveillés en `X/Y`, téléchargement/erreur
  côte à côte), fusionnées en une seule à la demande de l'utilisateur ; le
  rendu côte à côte à 3 colonnes dans la largeur d'une carte tronquait les
  libellés ("En télé­chargement" coupé), donc passé à une **liste empilée
  label/valeur** (`render_stat_item()`/`stat-multi-item.html`, une ligne par
  métrique, valeur alignée à droite) plutôt qu'une disposition en colonnes
  (`.stat-multi`, `flex-direction: column` — le nom de la classe date de
  l'essai "plusieurs valeurs côte à côte", conservé tel quel après le
  changement de disposition) ; enfin `X/Y` (actifs/surveillés) éclaté en
  deux lignes séparées ("Actifs" / "Surveillés"), là aussi demandé
  explicitement, pour une présentation homogène avec les deux autres lignes
  de la carte. Téléchargement = torrent avec `status == 4`
  (spec RPC) ; erreur = champ `error != 0` (tracker injoignable, fichier
  introuvable sur disque, etc. — `errorString` donne le détail par torrent,
  pas exposé au dashboard, juste le compte), valeur colorée en rouge si
  `>0` sinon verte (`stat-value-critical`/`stat-value-good` réappliquées à
  `.stat-multi-value` cette fois, pas `.stat-value`) — seule la valeur
  "erreur" est colorée, "téléchargement"/"actifs / surveillés" restent en
  encre neutre (0 en téléchargement n'est pas anormal, contrairement à une
  erreur). Espace disque et indexeurs Prowlarr restent des cartes séparées
  (jauge à zones et liste, voir plus bas — pas concernées par cette fusion).
  « Torrents actifs » = tout torrent Transmission avec `status != 0` (spec
  RPC, 0 = stopped/paused) ; « torrents surveillés » = tous les torrents
  présents dans le client, actifs ou non — hypothèse de lecture de la
  demande utilisateur ("nombre de torrents actifs" vs "surveillés"),
  vérifiée en interrogeant le RPC en direct le 2026-07-28 (257 actifs / 278
  au total à ce moment), à corriger si l'intention était différente.
  Indexeurs Prowlarr : `/api/v1/indexer` (liste + `enable`) croisé avec
  `/api/v1/indexerstatus` (liste des indexeurs actuellement désactivés après
  échecs répétés, vide si tout va bien — un indexeur y figurant compte comme
  en échec, par `indexerId`), via `docker exec arr-prowlarr-1 curl` (même
  mécanisme que `build_prowlarr_tracker_map()` dans `transmission-stats.py`,
  dupliqué plutôt que partagé, voir plus haut). Chaque source de la
  deuxième ligne est indépendante des autres (best-effort par sous-section,
  voir
  `build_stats_section()`) : espace disque et indexeurs Prowlarr s'affichent
  même si `vpn/transmission-vpn` est arrêté (contrairement à Débits/Ratio/
  torrents, qui dépendent tous de `transmission-stats.py` donc du VPN) ;
  toute la section (`Transmission &amp; système`, renommée à cette occasion,
  avant juste "Transmission — ratios & débits") n'est omise que si aucune
  des deux lignes n'a de contenu. `.stat` alignée sur la taille de `.card`
  (mêmes `padding`/`gap`, jauge réduite à 68×40px, valeur à `1rem` — avant
  plus grande que les cartes de service, jugé disproportionné) peu après,
  puis son contenu centré verticalement (`justify-content: center`, demandé
  pour la carte torrents actifs/surveillés dont le texte seul, sans jauge,
  flottait en haut de la carte étirée par le grid à la hauteur des cartes
  voisines) — sauf `.stat-list` (indexeurs), qui reste ancrée en haut.
  `.stats-grid` (Débits/Ratio/Système) passée de `repeat(auto-fill,
  minmax(150px, 1fr))` à `repeat(auto-fill, 190px)` (largeur de colonne
  fixe) le même jour, cartes visiblement de tailles différentes entre
  groupes signalé par l'utilisateur : avec `minmax(…, 1fr)`, chaque grille
  calcule la largeur de ses cartes en fonction de son **propre** nombre de
  cartes et de la largeur de son **propre** conteneur — Débits/Ratio (2
  cartes chacun, conteneur ~460px dans `.stats-row`) et Système (3+ cartes,
  pleine largeur ~960px) obtenaient donc des cartes de tailles différentes
  malgré une règle CSS identique (les colonnes vides générées par
  `auto-fill` dans le conteneur pleine largeur se partagent l'espace `1fr`
  avec les colonnes réellement occupées, rétrécissant ces dernières). Une
  taille de colonne fixe élimine cette dépendance au conteneur — au prix
  d'un espace vide en fin de ligne quand une grille a peu de cartes (ex.
  Système à 3 cartes sur ~960px), accepté comme compromis. Les paragraphes
  `<p class="note">` sous la section (légende des seuils de couleur ratio/
  débit/disque) retirés le même jour à la demande de l'utilisateur — les
  zones de couleur restent identiques sur les jauges elles-mêmes (rouge/
  jaune/vert), seul le texte d'explication en bas de section a disparu ;
  `notes`/`.note` supprimés de `build_stats_section()`/`dashboard.css`
  plutôt que laissés morts.
- **`scripts/generate-dashboard.py` (réécrit en Python le 2026-07-28,
  remplace l'ancien `generate-dashboard.sh`)** : venait d'un script bash+jq
  qui construisait le HTML par concaténation de chaînes, devenu illisible
  avec l'ajout des mètres ci-dessus — demandé explicitement par
  l'utilisateur pour séparer vues et code avant d'étendre le dashboard
  (future page monitoring évoquée). Vues dans `dashboard/templates/*.html`
  (un fichier par composant : `page`, `section-grid`, `card-clickable`,
  `card-down`, `stat-card`, `gauge`, `gauge-zones`, `stats-row`,
  `stats-group`, `mini-meter`, `tracker-row`, `tracker-details`,
  `section-transmission` — `gauge`/`gauge-zones` ajoutés le 2026-07-28 en
  remplaçant un premier essai en mètres puis en balance, `stats-row`/
  `stats-group` le même jour pour le layout Débits/Ratio/Système, voir plus
  haut), rendues via `string.Template` de
  la stdlib (substitution `$variable` uniquement, zéro logique dans un
  template — boucles/conditions et calculs géométriques (angles/points
  d'arc des jauges) restent en Python, ex.
  `build_cards()`/`build_transmission_section()` décident quelles cartes
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

## Pièges à ne pas répéter

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
  contiennent les vrais chemins `/home/ebola/...`), se rappeler qu'ils ne
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
  indexeur `(Prowlarr)` côté Sonarr/Radarr) et à les refléter dans le
  tableau `torznab` de `config.js` — ces IDs ne sont pas stables dans le
  temps et rien ne prévient d'un ID devenu obsolète autrement qu'en
  lisant les logs cross-seed.
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

## Repo

```
server/
├── .env.shared(.example)     # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT — réel gitignoré, .example versionné
├── Makefile                  # network/up/down/config/logs/update/update-all/backup/restore/cron-install STACK=<nom> ; dashboard-refresh/cleanup (sans STACK)
├── README.md                  # doc humaine : services, install
├── ARCHITECTURE.md            # doc humaine : architecture, choix structurants
├── ISSUES.md                  # doc humaine : problèmes rencontrés
├── scripts/
│   ├── crontab                    # source de vérité du crontab hôte — `make cron-install`
│   ├── backup.sh                   # sauvegarde restic hebdomadaire
│   ├── restore.sh                  # restauration guidée d'un snapshot restic
│   ├── generate-dashboard.py       # régénère dashboard/html/ — `make dashboard-refresh`
│   ├── transmission-stats.py       # JSON ratios/débits pour generate-dashboard.py
│   └── torrent-cleanup.py          # TUI de nettoyage manuel torrents+library/ — `make cleanup`
├── sauvegarde/                # non versionné — dépôt restic + mot de passe + staging
├── traefik/                  # socket-proxy + traefik + dashboard (page statique de liens) ; .env(ACME_EMAIL)/.example
├── jellyfin/                 # docker-compose.yml + override.yml(.example) pour les bibliothèques
├── nextcloud/                 # db-next/app/web/news-updater ; .env/.example ; override.yml(.example)
├── vpn/                       # transmission-vpn (réseau isolé) + sidecar transmission-proxy ; .env/.example
├── arr/                       # prowlarr/sonarr/radarr/cross-seed/recyclarr ; .env/.example ; override.yml.example (optionnel)
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
