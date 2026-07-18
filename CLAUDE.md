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
- **Secrets** : `.env` par stack (gitignoré) + `.env.example` versionné.
  Valeurs non secrètes partagées (`PUID`/`PGID`/`RENDER_GID`/`DOMAIN`/
  `DATA_ROOT`) dans `.env.shared` à la racine. Toujours passer par le
  `Makefile` (`make <target> STACK=<nom>`), jamais `docker compose` en
  direct dans un dossier de stack (il ne chargerait pas `.env.shared`).
- **Montages host-specific** (bibliothèques Jellyfin, external storage
  Nextcloud) : dans `docker-compose.override.yml` par stack (gitignoré,
  chargé automatiquement par le Makefile s'il existe) + `.example`
  versionné à côté. Jamais dans le compose file de base — objectif :
  qu'un autre déploiement puisse reprendre la stack sans dépendre des
  chemins de cette machine.
- **Nextcloud** : image communautaire (pas AIO — incompatible avec
  rootless/infra-as-code, cf. README).
- **Repo public** sur GitHub (`nattyebola/home-server-services`, remote
  `origin` via deploy key dédiée `~/.ssh/id_ed25519_server_backup` / alias
  SSH `github-server-backup`, pas la clé perso de l'utilisateur). Ne
  jamais committer un secret ou une info identifiante en dur (email,
  chemin perso...) — toujours via `.env`/`.env.shared`, jamais dans un
  fichier versionné. `DOMAIN=example.com` et le username Unix `ebola`
  dans les valeurs restent volontairement en clair (déjà publics/peu
  sensibles, décision explicite de l'utilisateur) — ça ne couvre que les
  *nouveaux* ajouts.

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
- Avant de modifier un des trois `docker-compose.override.yml` réels
  (gitignorés, contiennent les vrais chemins `/home/ebola/...`), se rappeler
  qu'ils ne sont pas versionnés : toute évolution structurelle doit aussi
  se refléter dans le `.example` correspondant.

## Repo

```
server/
├── .env.shared              # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT — versionné, pas de secret
├── Makefile                  # network/up/down/config/logs/update/update-all/backup/restore/cron-install STACK=<nom>
├── README.md                  # doc humaine : services, choix, problèmes rencontrés, install
├── scripts/
│   ├── crontab                    # source de vérité du crontab hôte — `make cron-install`
│   ├── backup.sh                   # sauvegarde restic hebdomadaire
│   └── restore.sh                  # restauration guidée d'un snapshot restic
├── sauvegarde/                # non versionné — dépôt restic + mot de passe + staging
├── traefik/                  # socket-proxy + traefik ; .env(ACME_EMAIL)/.example
├── jellyfin/                 # docker-compose.yml + override.yml(.example) pour les bibliothèques
├── nextcloud/                 # db-next/app/web/news-updater ; .env/.example ; override.yml(.example)
└── vpn/                       # transmission-vpn (réseau isolé) + sidecar transmission-proxy ; .env/.example
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
- Résilience visée : perte de `/data` → restauration depuis
  `sauvegarde/` (sur `/home`, disque différent). Perte de `/home` → seul
  l'infra-as-code est récupérable depuis GitHub, la sauvegarde restic est
  perdue avec (accepté pour l'instant, pas d'offsite).

Détails, rationale et guide d'installation complet : `README.md`.
