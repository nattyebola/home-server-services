# server/ — instructions pour Claude

Infra as code de services home server (Docker Compose + Traefik + Makefile).
Guide d'installation : **voir `README.md`**. Description des services et
choix d'architecture expliqués : **voir `ARCHITECTURE.md`**. Problèmes
rencontrés : **voir `ISSUES.md`**. Ces trois fichiers sont destinés aux
humains. Ce fichier ne garde que ce qui sert à retravailler sur ce repo
sans relitiger des décisions déjà prises ou répéter des pièges déjà
rencontrés — pas l'historique des itérations, ni le détail relisible dans
le code lui-même.

**Ce fichier ne garde que le transverse.** Le détail par domaine vit dans
`.claude/docs/*.md`, chargé à la demande : ces fichiers portent des décisions
et des pièges qui ont coûté cher, les ignorer revient à les redécouvrir.

## Détail par domaine — lire AVANT d'y toucher

- **`.claude/docs/clearr.md`** — `arr/clearr/` (web, TUI, CLI
  `delete-by-inode`) et l'addon Kodi `kodi/context.clearr`.
- **`.claude/docs/arr-config.md`** — `scripts/provision.py`,
  `scripts/apply-arr-overrides.py`, `scripts/search-missing.py`,
  `arr/profiles/`, `arr/recyclarr/`, et la chaîne arr → Jellyfin → Kodi
  (metadata writer, renommage à l'import, tags, ratio des indexeurs publics).
- **`.claude/docs/arr-pieges.md`** — diagnostiquer un grab, un import bloqué,
  un regrab en boucle, un custom format, un score de profil, cross-seed,
  Seerr.
- **`.claude/docs/traefik-dashboard.md`** — `traefik/`, `dashboard/`,
  `scripts/generate-dashboard.py`, `scripts/lan-only-middleware.sh`, ou tout
  label / middleware Traefik.
- **`.claude/docs/transmission.md`** — `scripts/transmission-stats.py`, la
  résolution des trackers, ou lancer un `transmission-remote`.
- **`.claude/docs/backup.md`** — `scripts/backup.sh` / `scripts/restore.sh`,
  ou lancer une sauvegarde / restauration.

Règle : **un seul de ces fichiers suffit en général** — les lire tous
reviendrait à l'ancien CLAUDE.md monolithique. En cas de doute sur
l'existence d'une décision passée dans un domaine, `grep` d'abord
`.claude/docs/`.

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
  Kodi (`.claude/docs/clearr.md`).
- **Images toujours en `:latest`** (jamais de tag figé) — voulu
  explicitement, l'utilisateur accepte le risque de casse pour avoir
  toujours les dernières versions. La reproductibilité d'une restauration
  passe par le manifeste de digests capturé à chaque `make backup`
  (`scripts/backup.sh`), pas par des tags fixes dans les compose files.
  **Une seule exception, subie et non choisie : `recyclarr`**, qui ne publie
  plus de tag `latest` (« Use a major version tag (e.g. `8`) », README
  upstream ; le seul tag non numérique du registre est `edge`, une build de
  développement au digest distinct). D'où `:8`, qui suit la dernière 8.x.
  **Ne pas proposer d'y remettre `:latest`** — le tag n'existe pas, le pull
  échouerait. Corollaire à surveiller : aucun automatisme ne franchira la
  majeure suivante, le bump vers `:9` est manuel et rien ne le signalera.
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
  `apply-arr-overrides.py` (`.claude/docs/arr-config.md`).
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


## Repo

```
server/
├── .env.shared(.example)     # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT/LAN_CIDR/DNS_* — réel gitignoré
├── Makefile                  # `make help` (cible par défaut) liste tout — voir plus bas
├── README.md                  # doc humaine : services, install
├── ARCHITECTURE.md            # doc humaine : architecture, choix structurants
├── ISSUES.md                  # doc humaine : problèmes rencontrés
├── .claude/docs/             # détail par domaine, chargé à la demande (voir l'index en tête de ce fichier)
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
│   ├── search-missing.py           # recherche hebdo des manquants déjà sortis, plafonnée + rotation — `make search-missing`
│   ├── manual-import.py            # débloque les imports en attente (importBlocked/importPending) — skill manual-import
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

Détails, rationale et guide d'installation complet : `README.md` /
`ARCHITECTURE.md` / `ISSUES.md`.
