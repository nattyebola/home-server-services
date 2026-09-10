# Traefik, dashboard, exposition LAN/WAN

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à
`traefik/`, `dashboard/`, `scripts/generate-dashboard.py`,
`scripts/lan-only-middleware.sh` ou à un label/middleware Traefik.

## Traefik, dashboard, exposition

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
    domaine public (voir `.claude/docs/arr-config.md`) : y ajouter un
    service qui parle à
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
  l'inode qu'il avait au démarrage (voir « Réseau et Docker » dans
  `CLAUDE.md`). Le script
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
  - Seules les valeurs qui signalent une **anomalie** sont colorées dans la
    carte Torrents (rouge si `>0`, sinon vert) : « En erreur », « Absents »,
    et depuis le 2026-08-31 les deux compteurs d'imports. 0 en téléchargement
    n'est pas anormal, contrairement à une erreur. « Actifs » = `status != 0`
    (spec RPC), « surveillés » = tous les torrents présents.
  - **3e colonne « Imports » (Bloqués / En attente)** dans cette même carte,
    demandée le 2026-08-31 : nombre d'entrées de file Sonarr+Radarr en
    `importBlocked`/`importPending` (`arr_stuck_imports()`), les mêmes états
    que `stuck_queue_records()` de `scripts/manual-import.py` — **à garder
    alignés**, ce qui est compté doit être ce que `manual-import.py list` sait
    traiter.
    **`GET /api/v3/queue` masque par défaut les entrées orphelines**, et le
    paramètre qui les réintègre n'a pas le même nom d'un arr à l'autre :
    `includeUnknownSeriesItems=true` (Sonarr) / `includeUnknownMovieItems=true`
    (Radarr). Une entrée devient orpheline dès que son titre quitte le
    catalogue — un download que l'arr voit toujours chez le client alors que la
    série a été retirée — donc précisément un import qui ne se débloquera
    jamais seul. Les deux scripts l'ont ignoré : `arr_stuck_imports()` ne
    passait aucun des deux, `stuck_queue_records()` seulement le volet films.
    5 entrées `importBlocked` sont ainsi restées invisibles des deux, la carte
    affichant un **0 vert** — le faux négatif silencieux que son propre
    docstring dit vouloir éviter. Corrigé le 2026-09-09 (`unknown_param` dans
    `ARR_QUEUE_APPS` et dans `ARRS`). Ne jamais conclure « 0 import bloqué »
    d'une lecture de file sans ces paramètres. Comble un trou de visibilité : un téléchargement fini que l'arr
    refuse d'importer ne se débloque jamais tout seul et ne ressort nulle part
    ailleurs (le 2026-08-29, 5 des 20 titres comptés manquants étaient là).
    **Seule fonction du fichier en tout-ou-rien et pas en best-effort** : un
    arr arrêté ou injoignable affiche « — », jamais le compte de l'autre seul
    — un `0` partiel annoncerait l'absence de l'anomalie même qu'on cherche à
    voir. Colonnes à ~137px une fois à 3 dans une carte restée `stat-span-2`
    (un span-3 aurait décalé la carte tracker et les tâches planifiées, cf. le
    calage en rangées de 4 slots), d'où des libellés courts et un `flex-wrap`
    ciblé dans `.stat-torrents-files`. **Les 8 valeurs de la carte portent un
    `title=` natif** donnant leur définition exacte (généralisé le 2026-08-31
    depuis la seule colonne Imports) : les libellés tiennent en deux mots mais
    aucun ne dit ce qui est compté — « Actifs » est un `status` RPC, « Absents »
    et « En bibliothèque » les marqueurs ABS/BIB de clearr. Ces définitions
    vivent dans `transmission-stats.py` : les reprendre de là, pas les
    réinventer.
    Corollaire assumé du rattachement à cette carte : `vpn/transmission-vpn`
    arrêté remplace tout le bloc par un placeholder, compteurs d'imports
    compris, alors qu'ils ne dépendent que des arr.
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

