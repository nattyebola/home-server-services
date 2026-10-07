# server/ — instructions pour Claude

Infra as code de services home server (Docker Compose + Traefik + Makefile).

**Doc humaine : `docs/`, une page par thème**, indexée dans `README.md`
(installation, exploitation, architecture, traefik, telechargement, medias,
clearr, komga, nextcloud, sauvegarde, depannage). C'est là que vit le
*pourquoi* des choix. Ce fichier ne garde que les règles transverses, en une
ligne chacune, pour ne pas relitiger une décision prise ni répéter un piège.

**Détail par domaine dans `.claude/docs/*.md`, à lire AVANT d'y toucher**
(décisions et pièges qui ont coûté cher) :

- **`clearr.md`** — `arr/clearr/` (web, TUI, CLI `delete-by-inode`) et l'addon
  Kodi `kodi/context.clearr`.
- **`arr-config.md`** — `scripts/provision.py`, `apply-arr-overrides.py`,
  `search-missing.py`, `arr/profiles/` (profils de qualité), chaîne arr →
  Jellyfin → Kodi (`.nfo`, renommage, tags, ratio des indexeurs publics).
- **`arr-pieges.md`** — diagnostiquer un grab, un import bloqué, un regrab en
  boucle, un custom format, un score de profil, cross-seed, Seerr.
- **`traefik-dashboard.md`** — `traefik/`, `dashboard/`,
  `generate-dashboard.py`, `lan-only-middleware.sh`, tout label/middleware.
- **`transmission.md`** — `transmission-stats.py`, résolution des trackers,
  tout `transmission-remote`.
- **`backup.md`** — `backup.sh` / `restore.sh`, lancer une sauvegarde ou une
  restauration.
- **`komga.md`** — `komga/`, `completed/bd`, catégorie `bd` de Prowlarr, vue
  BD de clearr.

Un seul de ces fichiers suffit en général. En cas de doute sur une décision
passée : `grep` d'abord `.claude/docs/` et `docs/`. Les commentaires de code
« voir CLAUDE.md » désignent cet ensemble (ce fichier + `.claude/docs/`).

**Tenir la doc humaine à jour** : tout changement visible par un humain (une
commande, une tâche cron, un réglage, un comportement) se reflète dans la page
`docs/` du thème ; un nouveau piège va dans sa section « Pièges connus », et
dans le tableau par symptôme de `docs/depannage.md` s'il a un symptôme
reconnaissable. Style : français, phrases courtes, tableaux et Mermaid plutôt
que des paragraphes, encadrés GitHub (`> [!WARNING]`) pour ce qui casse.

## Décisions à respecter

Ne pas proposer d'y revenir sans demande explicite de l'utilisateur.

### Socle

- **Non-root par conteneur**, pas de daemon Docker rootless : `cap_drop: ALL`
  + `no-new-privileges:true` partout. `cap_add` ciblé **seulement** sur
  `db-next` et `vpn/transmission-vpn`, justifié en commentaire. N'en ajouter
  ailleurs qu'avec la même nécessité.
- **Jamais de socket Docker monté dans un conteneur** (root-équivalent sur
  l'hôte). C'est ce qui a décidé le transport HTTP de clearr et de l'addon Kodi.
- **Images en `:latest`**, jamais de tag figé ; la reproductibilité passe par
  le manifeste de digests de `make backup`. **Majeur tenu, par choix
  (2026-09-29) : `traefik:v3`** (un majeur casse la config) **et
  `postgres:15-alpine`** (un majeur ne relit pas le répertoire de données :
  dump + réimport à la main, fin de vie 11/2027).
- **Arr : `authenticationRequired: enabled`** (2026-09-29), login même depuis
  le LAN. `disabledForLocalAddresses` laissait les conteneurs WAN de
  `traefik-public` lire la clé API (`/initialize.json`). Sonde du dashboard
  sur `/Content/…`, servi sans auth.
- **Secrets et valeurs propres au déploiement** : `.env` par stack +
  `.env.shared` (gitignorés) avec leur `.example`. `LAN_CIDR` alimente les
  `ipAllowList` — jamais en dur dans un compose file. Toujours `make <cible>
  STACK=<nom>`, jamais `docker compose` en direct (`.env.shared` non chargé).
- **Montages propres à la machine** : `docker-compose.override.yml` gitignoré
  + `.example` versionné, jamais dans le compose de base. Toute évolution
  structurelle d'un override se reporte dans son `.example`.
- **Timezone** : bind-mount `/etc/localtime:/etc/localtime:ro` sur tout
  nouveau service, pas `TZ=`.
- **Logs** : `max-size` **et** `max-file: "3"` ensemble, jamais `max-size` seul.
- **Healthcheck sur tout service** : HTTP réel si un endpoint non authentifié
  existe, sinon connexion TCP. `traefik` a un entrypoint dédié `healthcheck`
  sur `127.0.0.1:8082` (jamais publié), pour échapper à la redirection
  http→https. **Aucune auto-remédiation** (pas d'`autoheal`, il exigerait le
  socket).
- **`dns: ${DNS_PRIMARY}/${DNS_SECONDARY}`** sur `jellyfin`, `arr/*`, `vpn/*`
  (DNS du FAI qui ment sur les trackers).
- **Hôte Linux natif requis, pas de WSL2** (évalué le 2026-07-23, détail dans
  `docs/depannage.md#windows--wsl2`).
- **Nextcloud : image communautaire**, pas AIO (socket Docker, UI propre).
- **Seerr (`ghcr.io/seerr-team/seerr`)**, pas Jellyseerr/Overseerr, fusionnés
  et dépréciés.
- **Profils de qualité arr : page blanche du 2026-10-07**, écrits à partir des
  règles de `docs/telechargement.md` (« Règles de sélection ») et d'une étude
  des releases réelles. **Plus de recyclarr** (les CF TRaSH ne changeaient le
  choix que dans 3 % des cas, toujours en pire sur nos trackers FR). **Jamais
  d'upgrade** (`upgradeAllowed: false`, regrab minimum) + **délai unique de
  24 h**. Langue > résolution > codec > HDR (un seul groupe de qualités).
  **Anime plafonné à 1080p** (2 % des épisodes ont une 2160p FR). Plafond
  2160p tenu à 100 Mo/min (disque). Détail : `.claude/docs/arr-config.md`.
- **Komga lit directement `completed/bd`, en `:ro`** : pas d'arr, pas de
  hardlink, pas d'import (arbitré le 2026-09-22 : « pouvoir lire, pas une
  bibliothèque bien rangée »). Le `:ro` n'est pas négociable (données
  seedées). Komf écarté ; s'il revient, son mode `COMIC_INFO` est interdit.

### Repo public et historique git

- **Repo public** (`nattyebola/home-server-services`, remote `origin` par
  deploy key dédiée, alias SSH `github-server-backup`, pas la clé perso).
  Jamais de secret ni d'info identifiante (email, domaine, chemin perso) dans
  un fichier versionné. Le vrai username Unix vit dans les overrides
  gitignorés, pas ici (`whoami` si besoin).
- **Historique réécrit le 2026-08-09** (force-push, `git-filter-repo`) pour
  retirer une adresse e-mail présente dans `traefik/traefik.yml` **et** comme
  auteur de tous les commits. `user.email` local posé sur l'adresse `noreply`.
  Objets orphelins locaux purgés. **Ne pas relancer ce nettoyage** (il
  changerait tous les SHA). Conséquence durable : les SHA des snapshots
  restic antérieurs n'existent plus — sans importance, on restaure avec la
  version courante du dépôt. **Plus de tags `backup-*`** (supprimés le
  2026-09-29, `backup.sh` n'en crée plus) : ne pas les réintroduire.

## Pièges à ne pas répéter

Explications et symptômes : `docs/depannage.md` et la section « Pièges
connus » de chaque page.

### Un test manuel réussi ne prouve rien

Trois bugs silencieux, invisibles à la main. Se méfier dès qu'un comportement
dépend d'un ordonnanceur, d'une file d'attente ou d'un échappement.

- **Écritures de config Servarr asynchrones** : un `202 Accepted` (ex. `PUT`
  d'une taille de palier) s'applique *après* la réponse (0,5 s à 53 s observés). Un « lire → comparer → écrire »
  enchaîné derrière voit l'ancienne valeur et se déclare satisfait. Fix :
  `settle()` dans `apply-arr-overrides.py`. **Ne pas « corriger » par un
  `sleep` dans `scripts/crontab`** (latence inconnue).
- **`%` non échappé dans une ligne crontab = saut de ligne** pour cron :
  `date +%s` devient `date +`, sans aucune erreur visible. Toujours `\%`.
- **`awk -v` interprète les échappements** et transforme `\%` en `%` :
  `install-crontab.sh` passe son bloc à awk par fichier, jamais `-v`.

### Cron et crontab

- **`make cron-install` ne remplace jamais le crontab entier** : il fusionne
  `scripts/crontab` dans un bloc entre deux marqueurs et recopie le reste.
  Le bloc va **en dernier** (son `MAILTO=""` ne vaut que pour les lignes qui
  suivent). Un job perso vit hors du bloc. La migration depuis les versions
  sans marqueur retire les lignes identiques au bloc, puis celles qui
  mentionnent le chemin du checkout (et les affiche).
- **Jamais de chemin, d'uid ou de `DATA_ROOT` en littéral dans
  `scripts/crontab`**, même en commentaire : `__REPO_ROOT__`, `__DATA_ROOT__`,
  `__PUID__` sont substitués sur tout le fichier.

### Réseau et Docker

- **`vpn/transmission-vpn` : jamais de second réseau Docker**, jamais son
  propre sous-réseau dans `LOCAL_NETWORK` (la route `redirect-gateway def1`
  couvre `172.16.0.0/12`). Exposer le RPC via `transmission-proxy` ; autoriser
  un pair du même réseau par `UFW_ALLOW_GW_NET=true`.
- **Module `ip_tables` requis sur l'hôte** pour le VPN
  (`/etc/modules-load.d/ip-tables.conf`) — à vérifier sur toute machine neuve.
- **Réseau externe d'une autre stack = nom préfixé** : `name: vpn_vpn-internal`,
  `name: arr_default`.
- **Hardlinks impossibles entre deux bind-mounts**, même disque et même
  `st_dev` : un seul mount `${DATA_ROOT}:/data_root` pour sonarr/radarr +
  remote path mapping. cross-seed n'a pas de remote path mapping : il garde
  `/data` et ses `linkDirs` sont sous le même mount (`/data/.cross-seed-links`).
- **Pas de bind-mount sous un point de montage `:ro`** (`mkdirat … read-only`).
- **Un bind-mount de fichier suit l'inode**, pas le chemin : fichier remplacé
  = conteneur figé sur l'ancien contenu. `make restart`, et monter le dossier
  quand le fichier doit changer à chaud. Vérifier ça avant de conclure qu'une
  config « n'a pas pris ».
- **`.env` modifié = `make up`, pas `make restart`** (qui ne recrée pas) :
  sinon la commande compose suivante recrée le service, à un moment inattendu.
- **`cap_drop: ALL` retire `CAP_DAC_OVERRIDE`** : secret `600` illisible
  (« introuvable »). Fix : `user: "${PUID}:${PGID}"`, jamais élargir les
  permissions ni le `cap_drop`.
- **Jamais un `.example` d'override copié sans remplir ses placeholders** :
  Docker crée l'arborescence bidon en root.
- **Traefik ne retente pas seul un certificat ACME en échec** : restart.
- Bruit connu : ~15 `middleware "hsts@docker" does not exist` pendant ~5 s à
  chaque redémarrage de Traefik.

## Repo

```
server/
├── .env.shared(.example)   PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT/LAN_CIDR/DNS_*
├── Makefile                `make help` (défaut), généré depuis les annotations des cibles
├── README.md, docs/        doc humaine (une page par thème)
├── CLAUDE.md, .claude/     docs/ par domaine + skills/ (changelogs, indexer-quota,
│                           manual-import, multi-groupes, server-report, vpn-bench)
├── scripts/                crontab + install-crontab.sh, logrotate.conf,
│                           provision.py, apply-arr-overrides.py, search-missing.py,
│                           manual-import.py, backup.sh/restore.sh, generate-dashboard.py,
│                           transmission-stats.py, lan-only-middleware.sh,
│                           require-running.sh, vpn-bench.py, image-versions.py
├── traefik/ jellyfin/ nextcloud/ vpn/ arr/ seerr/ komga/   une stack par dossier
│   └── arr/{clearr,profiles,cross-seed,scripts}/
├── dashboard/              templates/ + assets/ ; html/ généré
├── kodi/, gnome/           postes clients (addon Kodi, extension GNOME Govee)
└── sauvegarde/             non versionné — dépôt restic
```

- `make network` avant tout `make up`.
- **Nouvelle cible make** : annotation `cible: ## ARG=<valeur> — description`
  sur la ligne de définition (l'aide est un `grep` du fichier brut). Pour une
  cible précédée de `cible: STACK := arr`, l'annotation va sur la ligne
  `cible: network` (un `#` dans une affectation serait avalé). Pas
  d'échappement make (`$${DOMAIN}`) dans l'annotation.
- **logrotate** tourne sans root, avec son état sous `DATA_ROOT`, sur la
  config **rendue** par `make cron-install` (logrotate n'expand aucune
  variable). `.gitignore` couvre `*.log.*`.
