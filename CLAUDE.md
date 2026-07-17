# Serveur maison — ebola-salon

Ce dossier (`server`) est le projet de migration/refonte des services home server actuellement décrits dans `~/docker` (dossier en lecture seule, référence uniquement — ne jamais y écrire).

## Contexte

- Cette machine est un PC de salon 3-en-1 : home server, PC de jeu et media center, branché sur la TV du salon.
- Quels services garder/jeter/remplacer parmi ceux listés plus bas sera décidé au fur et à mesure.

## Décisions d'architecture cible

- **Runtime** : Docker (pas Podman — choix assumé par familiarité, malgré l'intérêt de Podman pour le rootless natif et Quadlet/systemd).
- **Containers rootless** : chaque container doit exécuter son process avec un utilisateur non-root à l'intérieur (pas de daemon Docker rootless complet — le démon reste classique/root, seul le process dans le container est non-root). Exception assumée et documentée : **Portainer**, qui a besoin d'un accès direct au socket Docker pour fonctionner (équivalent à root sur l'hôte de toute façon).
- **Durcissement systématique des containers** : tous les services ont `security_opt: no-new-privileges:true` et `cap_drop: ALL`. Seul `db-next` (Postgres) a un `cap_add` ciblé (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`), nécessaire car son entrypoint démarre encore en root avant de descendre en privilège via `gosu` — aucun autre service n'a besoin de capacité ajoutée. Les montages de volumes doivent aussi rester au plus près du besoin réel (ex : ne jamais monter un `$HOME` entier si un seul sous-dossier est utilisé).
- **Reverse proxy** : Traefik, avec découverte automatique des containers via labels — un seul point d'entrée HTTPS (443) pour faire cohabiter tous les services sur le même nom de domaine (`example.com`, un sous-domaine par service). Traefik ne lit jamais le socket Docker en direct : il passe par un `docker-socket-proxy` (accès lecture seule, restreint) pour rester lui aussi non-root.
- **Supervision** : Portainer, avec son intégration native "Docker provider" pour piloter/visualiser Traefik et les stacks.
- **Infra as code** : toute la configuration (compose files, labels Traefik, config Portainer) doit être versionnable en fichiers texte dans ce dépôt — pas de configuration faite uniquement via une UI qui ne serait pas reflétée dans le repo. `server/` est un dépôt git initialisé.
- **Secrets** : fichiers `.env` par stack, non versionnés (exclus via `.gitignore`), avec un `.env.example` versionné à côté pour documenter les clés attendues.
- **Valeurs partagées non secrètes** (PUID/PGID, GID du groupe `render`, domaine, racine des données) : source unique dans `server/.env.shared` (versionné), référencées dans chaque compose file via `${PUID}`, `${DOMAIN}`, `${DATA_ROOT}`, etc. `docker compose` ne charge pas ce fichier tout seul (il ne cherche un `.env` que dans le dossier de la stack) : on passe donc toujours par le `Makefile` (`make up STACK=<nom>`, `make down STACK=<nom>`, `make config STACK=<nom>` pour valider le rendu, `make logs STACK=<nom>`) plutôt que par `docker compose` en direct dans un dossier de stack.
- **Nextcloud** : image communautaire classique (pas Nextcloud AIO) — AIO pilote ses propres containers via le socket Docker et sa config vit dans son UI, incompatible avec les exigences rootless/infra-as-code ci-dessus.

### Structure du dépôt

```
server/
├── .env.shared         # PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT — source unique, versionné, pas de secret
├── Makefile             # up/down/config/logs STACK=<nom> — charge .env.shared + le .env local de la stack
├── traefik/            # reverse proxy + TLS (Let's Encrypt), remplace proxy + letsencrypt-companion
│   ├── docker-compose.yml   # services: socket-proxy, traefik
│   ├── traefik.yml          # config statique (entrypoints, provider docker, resolver ACME)
│   └── letsencrypt/         # acme.json (non versionné)
├── portainer/
│   └── docker-compose.yml
├── jellyfin/
│   └── docker-compose.yml   # accès GPU (/dev/dri/renderD128), bibliothèque sur /data ; montages ciblés (Musique, Images/photos) plutôt que tout /home/ebola
└── nextcloud/
    ├── docker-compose.yml   # db-next, app, web, news-updater (proxy/letsencrypt-companion supprimés)
    ├── .env / .env.example
    ├── app/Dockerfile       # nextcloud:fpm-alpine + ffmpeg
    └── web/Dockerfile, nginx.conf  # nginxinc/nginx-unprivileged, écoute 8080 (au lieu de 80) ; plus de proxy_pass cms_pico (plugin retiré)
```

Réseau externe partagé requis avant tout déploiement : `make network` (crée `traefik-public` s'il n'existe pas déjà — Traefik + tout service exposé via labels doivent le rejoindre).

**État** : Traefik, Portainer, Jellyfin et Nextcloud sont déployés et tournent depuis `server/` (bascule depuis `~/docker` effectuée — les anciens containers `proxy`, `letsencrypt-companion` et l'ancien `jellyfin` en `network_mode: host` ne sont plus lancés). Seule la stack `vpn/` (Transmission) n'est pas encore migrée et tourne toujours depuis `~/docker/vpn`.

**Passe de durcissement du 2026-07-17** (suite à une revue de sécurité des stacks déployées) :
- `security_opt: no-new-privileges:true` + `cap_drop: ALL` ajoutés à tous les services (cf. bullet dédié plus haut).
- Mount Jellyfin `/home/ebola:/hosthome` (home entier exposé) restreint aux deux sous-dossiers réellement utilisés par ses bibliothèques (`Musique`, `Images/photos`), en lecture seule.
- Règle `location /sites/` (proxy_pass hairpin vers `https://www.example.com/` pour le plugin cms_pico, plus utilisé) retirée de `nextcloud/web/nginx.conf`.
- Incident résolu : `jellyfin.example.com` et `portainer.example.com` servaient le certificat auto-signé par défaut de Traefik car leur DNS était en NXDOMAIN au moment des tentatives ACME précédentes. Le DNS a été corrigé côté OVH puis Traefik redémarré pour relancer l'obtention des certificats Let's Encrypt — à surveiller si le problème revient (Traefik ne retente pas seul un certificat en échec, un restart est nécessaire après correction DNS).

## Matériel (relevé le 2026-07-14)

- **Modèle** : ASRock B550 Phantom Gaming-ITX/ax
- **CPU** : AMD Ryzen 9 5900X, 12 cœurs / 24 threads, jusqu'à ~4,95 GHz
- **RAM** : 30 Gi au total (swap 8 Gi)
- **GPU** : AMD Radeon RX 9070 XT (Navi 48) — pas de GPU Nvidia, pas de `nvidia-smi`. Le GPU est utilisé par Jellyfin pour le transcodage (`/dev/dri/renderD128`) et par la session de jeu/TV.
- **Stockage** :
  - `nvme0n1` (NVMe, 1,8T) : partitionné en `/` (194G), `/home` (1,6T), `/boot/efi`
  - `sda` (1,8T) monté sur `/data` (ext4) — sert de stockage de données pour transmission et nextcloud
- **OS** : Ubuntu 26.04 LTS (Resolute Raccoon), noyau 7.0.0-27-generic
- Hostname : `ebola-salon`

Cette machine a largement les ressources pour du transcodage vidéo, plusieurs conteneurs simultanés et du stockage de fichiers — la contrainte n'est pas la puissance mais plutôt le fait qu'elle sert aussi de PC de jeu/media (attention à ne pas saturer le GPU/CPU quand elle est utilisée en salon).

## Services actuellement décrits dans `~/docker` (à migrer)

Le dossier `~/docker` contient 3 sous-dossiers, chacun avec son `docker-compose.yml` :

### 1. `vpn/` — Transmission via VPN
- **Image** : `haugene/transmission-openvpn`
- Client torrent Transmission dont tout le trafic passe par un tunnel OpenVPN custom (config dans `vpn/custom/default.ovpn`)
- Port UI : 9091
- Données sur `/data/.transmission/{config,data}`
- Contient des identifiants VPN en clair dans le compose (à traiter comme secret lors de la migration)

### 2. `jellyfin/` — Serveur média
- **Image** : `jellyfin/jellyfin`
- Mode réseau `host`, accès direct au GPU (`/dev/dri/renderD128`) pour le transcodage matériel
- Bibliothèque pointée sur `/data/.transmission/data/completed` (les fichiers téléchargés par Transmission)
- Config/cache stockés localement dans `jellyfin/{config,cache}`
- Contient aussi un dossier `caddy_data` (reverse proxy Caddy, présent sur le disque mais pas dans le compose actuel — à vérifier)

### 3. `nextcloud/` — Nextcloud + reverse proxy
Stack à plusieurs conteneurs :
- `db-next` : PostgreSQL 15 (données sur `/data/.nextcloud/db-next`)
- `app` : Nextcloud (build custom dans `nextcloud/app/`), monte les photos, vidéos et téléchargements de l'utilisateur en plus de sa propre donnée
- `web` : frontend nginx (build custom), exposé en tant que `www.example.com`
- `proxy` + `letsencrypt-companion` : reverse proxy nginx + génération auto de certificats Let's Encrypt
- `news-updater` : rafraîchit périodiquement l'app Nextcloud News
- Contient des secrets en clair dans le compose / `db.env` (mots de passe DB et admin Nextcloud) — à sécuriser lors de la migration

## Notes de sécurité pour la migration

Les fichiers _compose_ actuels contiennent des mots de passe et identifiants en clair (VPN, PostgreSQL, admin Nextcloud). Lors de la migration vers `server/`, ces secrets doivent être extraits vers un mécanisme dédié (fichiers `.env` hors dépôt, secrets manager, etc.) plutôt que recopiés tels quels.
