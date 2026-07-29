# Serveur maison — infra as code

Docker Compose + Traefik pour faire tourner un petit home server : reverse
proxy HTTPS unique, media server (Jellyfin), cloud personnel (Nextcloud),
client torrent derrière VPN (Transmission), automatisation séries/films
(Arr), recherche/requête unifiée (Seerr), et une politique de sauvegarde
restic. Conçu pour tourner sur une seule machine, mais pensé pour être
repris ailleurs.

Ce fichier se concentre sur l'**installation et l'exploitation courante**.
Pour le détail des services et le pourquoi des choix techniques, voir
[`ARCHITECTURE.md`](ARCHITECTURE.md) ; pour les problèmes déjà rencontrés
et leurs solutions, voir [`ISSUES.md`](ISSUES.md).

> Pour un futur agent/IA qui retravaille sur ce repo : `CLAUDE.md` ne garde
> que ce qui est utile pour intervenir sur le repo (décisions à respecter,
> pièges à ne pas répéter) — les trois fichiers ci-dessus (README,
> ARCHITECTURE, ISSUES) sont le quoi/pourquoi/comment destiné aux humains.

## Services

| Service | Rôle | Sous-domaine | Accès |
|---|---|---|---|
| Traefik (`traefik/`) | reverse proxy HTTPS + dashboard de liens | `DOMAIN`, `www.DOMAIN` (dashboard) | public |
| Jellyfin (`jellyfin/`) | media server | `jellyfin.DOMAIN` | public |
| Nextcloud (`nextcloud/`) | cloud personnel | `nextcloud.DOMAIN` | public |
| Transmission (`vpn/`) | client torrent derrière VPN | `transmission.DOMAIN` | LAN uniquement |
| Arr (`arr/`) : Prowlarr, Sonarr, Radarr, cross-seed, recyclarr | automatisation récupération séries/films | `prowlarr/sonarr/radarr.DOMAIN` | LAN uniquement |
| Seerr (`seerr/`) | recherche/requête unifiée (grand public) | `seerr.DOMAIN` | public |

Détail de chaque service, schémas et rationale des choix : voir
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Installation

### Prérequis

- Un hôte Linux natif (pas de support Windows/WSL2, voir
  [`ISSUES.md`](ISSUES.md#windows--wsl2) pour le détail des raisons).
- Docker Engine + Docker Compose v2 (`docker compose`, pas
  `docker-compose`), `make`, `git`.
- Un nom de domaine dont vous contrôlez le DNS (pour les sous-domaines et
  les certificats Let's Encrypt), et la capacité de rediriger les ports
  80/443 de votre routeur vers la machine.
- (optionnel, Jellyfin) un GPU exposant `/dev/dri/renderD128` pour le
  transcodage matériel — sinon retirez le bloc `devices`/`group_add` de
  `jellyfin/docker-compose.yml`.
- (optionnel, VPN) un abonnement fournissant une config OpenVPN (`.ovpn`)
  — testé avec un provider custom (AirVPN).
- (VPN) module kernel `ip_tables` chargé sur l'hôte — les Ubuntu récents
  ne le chargent plus par défaut, or `haugene/transmission-openvpn` en a
  besoin pour ses règles de routing/kill-switch. Si le container échoue à
  démarrer ou que le tunnel ne route rien, vérifier `lsmod | grep
  ip_tables` et sinon créer `/etc/modules-load.d/ip-tables.conf` contenant
  `ip_tables` (puis `modprobe ip_tables` ou reboot).
- [`restic`](https://restic.net/) pour les sauvegardes.

### Étapes

1. **Cloner le repo**
   ```sh
   git clone <url-du-repo> server && cd server
   ```

2. **Créer et adapter les valeurs partagées** — copier l'exemple et le
   remplir :
   ```sh
   cp .env.shared.example .env.shared
   ```
   `PUID`/`PGID` (uid/gid de l'utilisateur qui doit posséder les fichiers
   créés), `RENDER_GID` (`getent group render` si vous avez un GPU),
   `DOMAIN` (le vôtre), `DATA_ROOT` (où stocker les données applicatives —
   idéalement un disque avec de la place), `LAN_CIDR` (plage de votre
   réseau local — restreint l'accès à Transmission/Arr via le middleware
   Traefik `ipallowlist`), `DNS_PRIMARY`/`DNS_SECONDARY` (résolveurs DNS
   forcés sur Jellyfin/arr/vpn — Cloudflare par défaut, pas besoin d'y
   toucher sauf préférence).

3. **Créer le réseau Docker partagé**
   ```sh
   make network
   ```

4. **Configurer les secrets de chaque stack** — copier l'exemple et
   remplir :
   ```sh
   cp traefik/.env.example    traefik/.env      # ACME_EMAIL
   cp nextcloud/.env.example  nextcloud/.env    # POSTGRES_USER/PASSWORD, NEXTCLOUD_ADMIN_USER/PASSWORD
   cp vpn/.env.example        vpn/.env          # OPENVPN_USERNAME/PASSWORD
   cp arr/.env.example        arr/.env          # PROWLARR/SONARR/RADARR/CROSSSEED_API_KEY (à remplir après le 1er démarrage, étape 12)
   ```

5. **Config OpenVPN** (si vous utilisez la stack `vpn/`) : déposez le
   fichier `.ovpn` fourni par votre provider dans `vpn/custom/` (voir la
   doc de [haugene/docker-transmission-openvpn](https://haugene.github.io/docker-transmission-openvpn/)
   pour `OPENVPN_PROVIDER=CUSTOM`).

6. **Montages personnels** (Jellyfin, Nextcloud) — copier les exemples et
   pointer vers vos propres dossiers :
   ```sh
   cp jellyfin/docker-compose.override.yml.example  jellyfin/docker-compose.override.yml
   cp nextcloud/docker-compose.override.yml.example nextcloud/docker-compose.override.yml
   ```
   `arr/docker-compose.override.yml` n'est **pas** à copier par défaut —
   Sonarr/Radarr utilisent déjà `${DATA_ROOT}/library` par défaut, l'override
   ne sert que si vous voulez cette bibliothèque ailleurs.

7. **DNS** : créez un enregistrement (A ou AAAA) pour chaque sous-domaine
   utilisé vers l'IP publique de la machine — au minimum `<DOMAIN>` et
   `www.<DOMAIN>` (dashboard), `nextcloud.<DOMAIN>` et `jellyfin.<DOMAIN>`,
   plus `transmission.<DOMAIN>` si vous déployez la stack VPN,
   `prowlarr.<DOMAIN>`/`sonarr.<DOMAIN>`/`radarr.<DOMAIN>` si vous déployez
   la stack `arr`, et `seerr.<DOMAIN>` si vous déployez `seerr`.

8. **Démarrer Traefik en premier**
   ```sh
   make up STACK=traefik
   make logs STACK=traefik   # vérifier l'obtention des certificats
   ```

9. **Démarrer Jellyfin**
   ```sh
   make up STACK=jellyfin
   ```
   Dans l'UI Jellyfin, ajoutez deux bibliothèques : type Films →
   `/library/film`, type Séries → `/library/series` (chemins montés en
   lecture seule depuis `${DATA_ROOT}/library`, alimentés par Sonarr/Radarr,
   voir étape 12). Sans ça, [Seerr](#seerr) ne peut pas savoir qu'un
   contenu est déjà téléchargé et le proposera à tort en requête.

10. **Démarrer Nextcloud**
    ```sh
    make up STACK=nextcloud
    ```
    Le compte admin est créé automatiquement depuis
    `NEXTCLOUD_ADMIN_USER`/`PASSWORD` (`nextcloud/.env`).

11. **Démarrer le VPN / Transmission**
    ```sh
    make up STACK=vpn
    ```
    Le client torrent doit pointer vers
    `https://transmission.<DOMAIN>/transmission/rpc` (pas de port publié
    sur l'hôte).

12. **Démarrer et configurer Arr** (nécessite `vpn` déjà démarré, la stack
    rejoint son réseau `vpn-internal`) :
    ```sh
    make up STACK=arr
    ```
    Les clés API sont générées au premier démarrage de chaque app,
    impossible de les connaître avant :
    1. Récupérez `<ApiKey>` dans
       `${DATA_ROOT}/.arr/{prowlarr,sonarr,radarr}/config/config.xml` (ou
       Settings → General dans chaque UI), reportez-les dans `arr/.env`.
       Récupérez aussi la clé cross-seed (`docker compose ... exec
       cross-seed cross-seed api-key`) pour `CROSSSEED_API_KEY`.
    2. `make up STACK=arr` à nouveau pour que `cross-seed`/`recyclarr`
       redémarrent avec les vraies clés.
    3. Configuration manuelle via les UI (normal pour cet écosystème, pas
       automatisable en compose) :
       - ajouter des indexeurs dans Prowlarr, le connecter à Sonarr/Radarr
         (Settings → Apps) ;
       - configurer Transmission comme client de téléchargement dans
         Sonarr/Radarr (host `transmission-vpn`, port `9091`) ;
       - ajouter `/library/series` et `/library/movies` comme Root Folders ;
       - ajouter le script `/config/custom-cross-seed-notify.sh` comme
         Connection **Custom Script** (déclenché sur Import/Upgrade) dans
         Sonarr/Radarr — pas le type "Webhook" générique, voir
         [`ISSUES.md`](ISSUES.md#arr--sonarr--radarr--cross-seed) ;
       - mettre `useClientTorrents: true` dans `arr/cross-seed/config.js`
         (faux par défaut), sinon les webhooks échouent systématiquement,
         voir [`ISSUES.md`](ISSUES.md#arr--sonarr--radarr--cross-seed) ;
       - renseigner `CROSS_SEED_INDEXER_IDS` dans `arr/.env` (voir
         `.env.example`) avec les IDs des indexeurs Prowlarr que cross-seed
         doit chercher (page Indexers, l'ID apparaît dans l'URL de chaque
         indexeur) — propre à votre instance, pas dans `config.js` ; `make
         up STACK=arr` pour que cross-seed reparte avec ces IDs ;
       - (optionnel) ajouter une connexion **Emby/Jellyfin**
         (`implementation: MediaBrowser`) dans Sonarr/Radarr ciblant
         `jellyfin:8096`, avec `mapFrom=/data_root/library` /
         `mapTo=/library`, pour un refresh Jellyfin ciblé sur
         import/upgrade/renommage (sinon Jellyfin ne détecte les nouveaux
         fichiers que via sa surveillance temps réel).

    Si votre bibliothèque (`${DATA_ROOT}/library`) n'est pas sur le même
    disque que le reste de `DATA_ROOT` (vérifiable avec `df` sur les deux
    chemins), les imports Sonarr/Radarr basculeront automatiquement en
    copie au lieu du hardlink — fonctionnel mais plus lent et
    transitoirement plus gourmand en espace disque.

13. **Démarrer et configurer Seerr** (nécessite `jellyfin` et `arr` déjà
    démarrés, la stack rejoint le réseau de `arr/`) :
    ```sh
    mkdir -p ${DATA_ROOT}/.seerr/config && sudo chown -R ${PUID}:${PGID} ${DATA_ROOT}/.seerr
    make up STACK=seerr
    ```
    Le `chown` est nécessaire avant le tout premier démarrage : contrairement
    aux images `arr/`, Seerr tourne nativement en UID 1000 sans étape
    root-puis-drop et ne chown pas lui-même son volume (voir
    [`ISSUES.md`](ISSUES.md)).

    Tout se configure ensuite via l'assistant de première connexion
    (`https://seerr.<DOMAIN>`) : connexion à Jellyfin
    (`http://jellyfin:8096`) puis à Sonarr/Radarr
    (`http://sonarr:8989`/`http://radarr:7878`, clés API dans `arr/.env`).
    Une fois les bibliothèques Jellyfin ajoutées (étape 9), lancez
    manuellement le job **"Jellyfin Full Library Scan"** (Settings → Jobs
    & Cache) plutôt que d'attendre le cron périodique, pour que Seerr
    reconnaisse immédiatement le contenu déjà téléchargé.

14. **Vérifier** : `https://www.<DOMAIN>` (Nextcloud), `https://jellyfin.<DOMAIN>`,
    `https://transmission.<DOMAIN>` depuis le LAN, et `https://seerr.<DOMAIN>`.

15. **Sauvegardes** :
    ```sh
    make backup          # première exécution : crée le dépôt restic et
                          # génère sauvegarde/restic-password — copiez-le
                          # ailleurs immédiatement (gestionnaire de mdp)
    make cron-install     # programme la sauvegarde hebdo + le cron Nextcloud
    ```

### Maintenance courante

| Commande | Effet |
|---|---|
| `make up STACK=<nom>` | démarre/recrée une stack |
| `make down STACK=<nom>` | arrête une stack |
| `make config STACK=<nom>` | affiche la config résolue (debug des `${VAR}`) |
| `make logs STACK=<nom>` | logs en direct |
| `make update STACK=<nom>` | pull + rebuild + recrée (+ maintenance `occ` si `nextcloud`) |
| `make update-all` | `update` sur nextcloud, vpn, jellyfin, arr |
| `make backup` | sauvegarde restic (aussi via cron) |
| `make restore SNAPSHOT=<id\|latest>` | restauration guidée d'un snapshot |
| `make cron-install` | (ré)installe `scripts/crontab` comme crontab de l'hôte |
| `make cleanup` | TUI de nettoyage torrents/bibliothèque, voir ci-dessous |
| `make dashboard-refresh` | régénère le dashboard immédiatement (aussi via cron 5 min) |

#### Supprimer un torrent + sa place dans la bibliothèque (`make cleanup`)

Sonarr/Radarr et Transmission ne se parlent pas à la suppression : effacer
un film/une série dans Sonarr/Radarr ne touche pas le torrent dans le
client, et inversement. C'est un manque connu et non résolu de
l'écosystème *arr (aucun outil communautaire — Decluttarr, Removarr... —
ne couvre ce cas précis). `scripts/torrent-cleanup.py` comble ce trou pour
ce déploiement précis : un TUI qui liste les torrents Transmission (âge,
taille, ratio, tracker — résolu via Prowlarr quand possible) et, à la
suppression d'un torrent, retrouve et supprime aussi les fichiers
`library/` correspondants (matching par inode — ne fonctionne que parce
que Sonarr/Radarr importent en hardlink, voir
[`ARCHITECTURE.md`](ARCHITECTURE.md)).

Contrôles : `↑`/`↓`/`j`/`k` naviguer, `/` filtrer par nom, `s`/`S` changer
le critère de tri ou son sens, `Entrée` supprimer avec confirmation
(détail des fichiers touchés), `D` (majuscule) supprimer directement sans
confirmation, `q` quitter. Le marqueur `L` (vert) signale les torrents
ayant une correspondance dans `library/`. Un compteur en bas d'écran
totalise l'espace libéré pendant la session.

**Évite le re-téléchargement automatique** : en plus des fichiers, l'outil
retrouve (par correspondance de chemin) si le fichier supprimé correspond
à un film Radarr ou un épisode Sonarr, et agit en conséquence — sans quoi
Sonarr/Radarr, toujours monitored, redemanderait le même contenu à la
prochaine recherche RSS/manuelle :
- **Film** : retiré complètement de Radarr (+ exclusion de liste, pour
  éviter un re-ajout automatique via une liste Trakt/Seerr).
- **Épisode/saison** : si la saison est *terminée* (aucun épisode à venir)
  et que tous ses fichiers connus sont supprimés, son monitoring est
  désactivé — sinon, seuls les épisodes concernés le sont, saison et
  série restant suivies pour que les prochains épisodes d'une saison en
  cours continuent d'être recherchés.

Best-effort : si Sonarr/Radarr est injoignable ou que le fichier ne leur
est pas connu (téléchargement jamais importé), ce volet est simplement
sauté — la suppression des fichiers n'est jamais bloquée pour autant.
Le plan d'action est affiché dans l'écran de confirmation (`Entrée`) avant
exécution.

Journal détaillé de chaque suppression (fichiers touchés côté Transmission,
bibliothèque et actions Sonarr/Radarr, erreurs éventuelles) dans
`${DATA_ROOT}/.torrent-cleanup.log`.
