# server/ — instructions pour Claude

Infra as code de services home server (Docker Compose + Traefik + Makefile).
Description des services, choix d'architecture expliqués, problèmes
rencontrés et guide d'installation : **voir `README.md`**, destiné aux
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
  `DATA_ROOT`) dans `.env.shared`/`.env.shared.example` à la racine
  (`.env.shared` gitignoré depuis le 2026-07-18, il identifiait ce
  déploiement — domaine, chemins). Toujours passer par le `Makefile`
  (`make <target> STACK=<nom>`), jamais `docker compose` en direct dans
  un dossier de stack (il ne chargerait pas `.env.shared`).
- **Montages host-specific** (bibliothèques Jellyfin, external storage
  Nextcloud) : dans `docker-compose.override.yml` par stack (gitignoré,
  chargé automatiquement par le Makefile s'il existe) + `.example`
  versionné à côté. Jamais dans le compose file de base — objectif :
  qu'un autre déploiement puisse reprendre la stack sans dépendre des
  chemins de cette machine.
- **Nextcloud** : image communautaire (pas AIO — incompatible avec
  rootless/infra-as-code, cf. README).
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

## Pièges à ne pas répéter

- **`vpn/transmission-vpn` ne doit jamais rejoindre un second réseau
  Docker** (ex. `traefik-public`) et sa variable `LOCAL_NETWORK` ne doit
  jamais contenir son propre sous-réseau — les deux cassent le routing
  sortant du tunnel (route `redirect-gateway def1` qui couvre
  `172.16.0.0/12`, la plage par défaut des réseaux Docker). Toujours
  passer par le sidecar `transmission-proxy` pour exposer le RPC ; pour
  autoriser un pair du même réseau Docker sans casser le routage, utiliser
  `UFW_ALLOW_GW_NET=true`, pas `LOCAL_NETWORK`. Détails complets : README.
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
  ajoutant un indexeur. Fix : forcer `dns: 1.1.1.1/1.0.0.1` sur le service
  concerné (déjà le cas sur Jellyfin par défaut ; ajouté aussi sur
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
  de génération (`scripts/generate-dashboard.sh`) copie les logos dans le
  dossier généré plutôt que de les monter séparément.

## Repo

```
server/
├── .env.shared(.example)     # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT — réel gitignoré, .example versionné
├── Makefile                  # network/up/down/config/logs/update/update-all/backup/restore/cron-install STACK=<nom> ; dashboard-refresh (sans STACK)
├── README.md                  # doc humaine : services, choix, problèmes rencontrés, install
├── scripts/
│   ├── crontab                    # source de vérité du crontab hôte — `make cron-install`
│   ├── backup.sh                   # sauvegarde restic hebdomadaire
│   ├── restore.sh                  # restauration guidée d'un snapshot restic
│   └── generate-dashboard.sh       # régénère dashboard/html/ — `make dashboard-refresh`
├── sauvegarde/                # non versionné — dépôt restic + mot de passe + staging
├── traefik/                  # socket-proxy + traefik + dashboard (page statique de liens) ; .env(ACME_EMAIL)/.example
├── jellyfin/                 # docker-compose.yml + override.yml(.example) pour les bibliothèques
├── nextcloud/                 # db-next/app/web/news-updater ; .env/.example ; override.yml(.example)
├── vpn/                       # transmission-vpn (réseau isolé) + sidecar transmission-proxy ; .env/.example
├── arr/                       # prowlarr/sonarr/radarr/cross-seed/recyclarr ; .env/.example ; override.yml.example (optionnel)
├── seerr/                     # recherche/requête unifiée (successeur Jellyseerr/Overseerr) ; pas de .env (config via son assistant web)
└── dashboard/                 # assets (logos) + html/ généré — pas de compose file, servi par traefik/ (voir ci-dessus)
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

Détails, rationale et guide d'installation complet : `README.md`.
