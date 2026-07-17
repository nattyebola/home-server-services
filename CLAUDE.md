# Serveur maison — ebola-salon

Ce dossier (`server`) contient l'infra as code des services home server tournant sur `ebola-salon`.

## Contexte

- Cette machine est un PC de salon 3-en-1 : home server, PC de jeu et media center, branché sur la TV du salon.

## Décisions d'architecture cible

- **Runtime** : Docker (pas Podman — choix assumé par familiarité, malgré l'intérêt de Podman pour le rootless natif et Quadlet/systemd).
- **Containers rootless** : chaque container doit exécuter son process avec un utilisateur non-root à l'intérieur (pas de daemon Docker rootless complet — le démon reste classique/root, seul le process dans le container est non-root).
- **Durcissement systématique des containers** : tous les services ont `security_opt: no-new-privileges:true` et `cap_drop: ALL`. Seul `db-next` (Postgres) a un `cap_add` ciblé (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`), nécessaire car son entrypoint démarre encore en root avant de descendre en privilège via `gosu` — aucun autre service n'a besoin de capacité ajoutée (exception : `vpn/transmission-vpn`, cf. plus bas, qui a des besoins réseau spécifiques). Les montages de volumes doivent aussi rester au plus près du besoin réel (ex : ne jamais monter un `$HOME` entier si un seul sous-dossier est utilisé).
- **Reverse proxy** : Traefik, avec découverte automatique des containers via labels — un seul point d'entrée HTTPS (443) pour faire cohabiter tous les services sur le même nom de domaine (`example.com`, un sous-domaine par service). Traefik ne lit jamais le socket Docker en direct : il passe par un `docker-socket-proxy` (accès lecture seule, restreint) pour rester lui aussi non-root.
- **Infra as code** : toute la configuration (compose files, labels Traefik) doit être versionnable en fichiers texte dans ce dépôt — pas de configuration faite uniquement via une UI qui ne serait pas reflétée dans le repo. `server/` est un dépôt git initialisé.
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
├── jellyfin/
│   └── docker-compose.yml   # accès GPU (/dev/dri/renderD128), bibliothèque sur /data ; montages ciblés (Musique, Images/photos) plutôt que tout /home/ebola
├── nextcloud/
│   ├── docker-compose.yml   # db-next, app, web, news-updater (proxy/letsencrypt-companion supprimés)
│   ├── .env / .env.example
│   ├── app/Dockerfile       # nextcloud:fpm-alpine + ffmpeg
│   └── web/Dockerfile, nginx.conf  # nginxinc/nginx-unprivileged, écoute 8080 (au lieu de 80) ; plus de proxy_pass cms_pico (plugin retiré)
└── vpn/
    ├── docker-compose.yml   # transmission-vpn (haugene/transmission-openvpn) + sidecar transmission-proxy (nginx)
    ├── .env / .env.example  # OPENVPN_USERNAME/PASSWORD
    ├── custom/default.ovpn  # config AirVPN custom
    └── proxy/nginx.conf     # sidecar : proxy_pass vers transmission-vpn:9091, seul pont vers traefik-public
```

Réseau externe partagé requis avant tout déploiement : `make network` (crée `traefik-public` s'il n'existe pas déjà — Traefik + tout service exposé via labels doivent le rejoindre).

**État** : Traefik, Jellyfin, Nextcloud et vpn/Transmission sont déployés et tournent depuis `server/`.

**Portainer retiré le 2026-07-17** : stack supprimée (pas de besoin identifié). Le container et son bind mount `portainer/data/` (contenant des fichiers root-owned, écrits via l'accès direct au socket Docker) ont été supprimés. Si un besoin de supervision réapparaît, le compose file reste consultable dans l'historique git (commits antérieurs à sa suppression).

**Passe de durcissement du 2026-07-17** (suite à une revue de sécurité des stacks déployées) :
- `security_opt: no-new-privileges:true` + `cap_drop: ALL` ajoutés à tous les services (cf. bullet dédié plus haut).
- Mount Jellyfin `/home/ebola:/hosthome` (home entier exposé) restreint aux deux sous-dossiers réellement utilisés par ses bibliothèques (`Musique`, `Images/photos`), en lecture seule.
- Règle `location /sites/` (proxy_pass hairpin vers `https://www.example.com/` pour le plugin cms_pico, plus utilisé) retirée de `nextcloud/web/nginx.conf`.
- Incident résolu : `jellyfin.example.com` et `portainer.example.com` servaient le certificat auto-signé par défaut de Traefik car leur DNS était en NXDOMAIN au moment des tentatives ACME précédentes. Le DNS a été corrigé côté OVH puis Traefik redémarré pour relancer l'obtention des certificats Let's Encrypt — à surveiller si le problème revient (Traefik ne retente pas seul un certificat en échec, un restart est nécessaire après correction DNS).

**Stack `vpn/` (Transmission)** — piège rencontré à la mise en place, à connaître avant de retoucher cette stack :
- `haugene/transmission-openvpn` pousse une route `redirect-gateway def1` qui scinde `0.0.0.0/0` en deux routes `/1` (`0.0.0.0/1` + `128.0.0.0/1`) couvrant la quasi-totalité de l'espace IPv4 — y compris les plages `172.16.0.0/12` que Docker utilise par défaut pour ses réseaux (donc `traefik-public` et tout réseau créé sans IPAM explicite).
- **Attacher ce container à un second réseau docker (ex. `traefik-public`) casse le routing sortant du tunnel** (DNS/ping ne sortent plus, `sitnl_send: rtnl: generic error (-101)` dans les logs) — reproduit de façon fiable, que ce soit en hot-attach (`docker network connect`, qui échoue explicitement avec "cannot program address ... conflicts with existing route") ou en déclarant les deux réseaux dès la création du container. **Solution : le garder sur un unique réseau dédié** (`vpn-internal`, subnet pinné) et faire pont vers Traefik via un **sidecar** (`transmission-proxy`, nginx sur `vpn-internal` + `traefik-public`) qui ne touche jamais au tunnel.
- Autre piège lié : la variable `LOCAL_NETWORK` fait un `ip route replace <subnet> via <gw>` pour chaque entrée — si on y met le **propre sous-réseau du container** (celui sur lequel il est déjà connecté), ça casse aussi le routing sortant (même symptôme). Pour autoriser un pair du même réseau docker (ici le sidecar) à atteindre le port RPC sans toucher au routage, utiliser `UFW_ALLOW_GW_NET=true` à la place (ne fait qu'une lecture de route + une règle ufw, pas de `route replace`).
- Contrôle d'accès : whitelist RPC de Transmission (host + IP) désactivée (`TRANSMISSION_RPC_HOST_WHITELIST_ENABLED=false`, `TRANSMISSION_RPC_WHITELIST_ENABLED=false`) — l'accès est filtré en amont par le middleware LAN-only de Traefik (`ipallowlist.sourcerange=192.168.0.0/24`).
- Durcissement (`cap_drop: ALL` etc.) appliqué et validé une fois le problème réseau isolé — `db-next`-like : le container démarre en root (configure iptables/ufw/tun avant de descendre en PUID/PGID), `cap_add` nécessaire : `NET_ADMIN`, `NET_RAW` (sinon `iptables-restore` échoue), `MKNOD` (création du device tun), `CHOWN`/`DAC_OVERRIDE`/`FOWNER`/`SETGID`/`SETUID` (écriture fichiers + drop de privilège), `KILL`/`SETPCAP`/`SETFCAP`/`SYS_CHROOT`/`AUDIT_WRITE`/`FSETID` (scripts internes de l'image, sans quoi `kill`/ufw échouent par endroits).
- **Accès RPC client torrent** : `localhost:9091` ne fonctionne plus depuis la migration (le container n'a plus de port publié sur l'hôte, cf. isolation réseau ci-dessus). Le client torrent doit pointer vers `https://transmission.${DOMAIN}/transmission/rpc` (donc `https://transmission.example.com/transmission/rpc`), joignable uniquement depuis le LAN (`192.168.0.0/24`, middleware Traefik). Validé fonctionnel le 2026-07-17.

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

