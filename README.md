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
- `python3` (stdlib seule, aucun paquet pip) — utilisé par
  `make dashboard-refresh`, `make arr-overrides` et les stats Transmission du
  dashboard (`scripts/*.py`).
- [`restic`](https://restic.net/) pour les sauvegardes.
- `logrotate` — le cron de l'étape 21 s'en sert pour tourner les journaux du
  dépôt et l'access log Traefik. Lancé en tant qu'utilisateur avec son propre
  fichier d'état, donc **sans root** et sans rien déposer dans
  `/etc/logrotate.d`.
- (optionnel, recommandé) `fail2ban` sur l'hôte. Il ne fait pas partie de la
  stack mais complète l'access log Traefik (`accessLog` dans
  `traefik/traefik.yml`), qui est le seul endroit où les requêtes WAN — y
  compris les 403 des middlewares LAN-only — laissent une trace. Sans jail
  lisant ce fichier, fail2ban ne surveille que SSH.
- (optionnel, Jellyfin) les **plugins** ne sont pas provisionnés : à installer
  une fois dans l'UI. `Kodi Sync Queue` est nécessaire si vous utilisez
  l'addon de l'étape 22 (c'est lui qui propage une suppression jusqu'à Kodi).
  Le **transcodage matériel** (VAAPI) ne l'est pas non plus, il dépend du GPU
  de la machine — voir Tableau de bord → Lecture.
- (optionnel, client Kodi) Kodi 19+ avec l'addon `jellyfin-kodi` en mode sync,
  si vous voulez l'entrée de menu contextuel « Supprimer avec clearr »
  (étape 22).

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
   cp arr/.env.example        arr/.env          # PROWLARR/SONARR/RADARR/CROSSSEED/JELLYFIN_API_KEY + CROSS_SEED_INDEXER_IDS (renseignés aux étapes 14 et 18)
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
    Créez le compte administrateur dans l'assistant de première connexion
    (`https://jellyfin.<DOMAIN>`), puis reportez ses identifiants dans
    `jellyfin/.env` :
    ```sh
    cp jellyfin/.env.example jellyfin/.env   # JELLYFIN_ADMIN_USER/PASSWORD
    ```
    Ils ne servent qu'à deux opérations que Jellyfin réserve à une session admin
    et refuse à une clé API : créer la première clé API (étape 14) et créer le
    compte propriétaire de Seerr (étape 17). Les bibliothèques, elles, sont
    créées automatiquement à l'étape 17 — rien à ajouter à la main ici.

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

12. **Démarrer Arr** (nécessite `vpn` déjà démarré, la stack rejoint son
    réseau `vpn-internal`) :
    ```sh
    make up STACK=arr
    ```

13. **Démarrer Seerr** (nécessite `jellyfin` et `arr` démarrés, la stack
    rejoint le réseau de `arr/`) :
    ```sh
    make up STACK=seerr
    ```
    Il démarre non configuré : c'est l'étape 17 qui le renseigne. `make up`
    crée `${DATA_ROOT}/.seerr/config` au préalable — sans ça Docker le
    créerait en `root:root` et Seerr crasherait en boucle sur `EACCES`, ne
    tournant pas en root et ne chownant pas son volume lui-même (voir
    [`ISSUES.md`](ISSUES.md)).

14. **Collecter les clés API** — générées au premier démarrage, donc
    impossibles à connaître avant :
    ```sh
    make api-keys
    ```
    Lit les clés Prowlarr/Sonarr/Radarr dans leur `config.xml`, demande la
    sienne à cross-seed, crée la clé API Jellyfin, et écrit les cinq dans
    `arr/.env` sans écraser une valeur déjà renseignée (voir
    `scripts/provision.py`).

15. **Redémarrer Arr** pour que `cross-seed` reparte avec les vraies clés :
    ```sh
    make up STACK=arr
    ```
    (`recyclarr` n'est pas un service continu : il est en `profiles: [manual]`
    et ne s'exécute qu'à la demande, étape 16.)

16. **Provisionner les profils qualité arr** :
    ```sh
    make recyclarr-sync   # custom formats + profils issus des guides TRaSH
    make arr-overrides    # réglages hors périmètre recyclarr, config anime
                          # versionnée (arr/profiles/), connexions Jellyfin
    ```
    Dans cet ordre : `arr-overrides` référence par nom des custom formats que
    `recyclarr-sync` vient de créer, et échoue explicitement s'ils manquent.
    Ces deux commandes sont ensuite enchaînées chaque nuit par le cron
    (étape 21).

17. **Provisionner le reste de la configuration** :
    ```sh
    make provision
    ```
    Crée ce qui se faisait auparavant à la main dans les UI : les trois
    bibliothèques Jellyfin (`/library/film`, `/library/series`,
    `/library/anime`), les applications Sonarr/Radarr côté Prowlarr,
    Transmission comme client de téléchargement, les Root Folders
    (`/data_root/library/...` — le préfixe `/data_root` est essentiel, c'est le
    montage unique qui rend le hardlink possible à l'import, voir
    [`ARCHITECTURE.md`](ARCHITECTURE.md)), le **remote path mapping** qui
    traduit les chemins annoncés par Transmission (`/data/completed/`) vers
    ceux que voient les arr (`/data_root/.transmission/data/completed/`) —
    sans lui les téléchargements aboutissent mais ne sont **jamais importés**,
    sans erreur nulle part —, la Connection **Custom Script**
    cross-seed, puis toute la configuration de Seerr (compte propriétaire
    importé depuis Jellyfin, bibliothèques, Sonarr/Radarr avec les profils de
    l'étape 16, et le scan complet de bibliothèque).

    À lancer **après** l'étape 16 : la configuration Seerr désigne les profils
    qualité par nom. Strictement additif — il ne réécrit jamais un objet
    existant — donc relançable sans risque, et chaque objet est indépendant :
    un service arrêté ne fait échouer que ce qui le concerne.

18. **Déclarer vos indexeurs Prowlarr**. Ils sont créés par `make provision`
    (étape 17) à partir de `arr/profiles/prowlarr-indexers.json`, à copier
    depuis le `.example` à côté et à adapter :

    ```sh
    cp arr/profiles/prowlarr-indexers.json.example arr/profiles/prowlarr-indexers.json
    ```

    Ce fichier est **gitignoré** — il nomme les trackers que vous utilisez,
    ce qui n'a pas sa place dans un dépôt public — mais il est inclus dans la
    sauvegarde restic. Il ne contient **aucun secret** : les clés de compte
    (`apikey`, `passkey`) y sont désignées par nom de variable, à renseigner
    dans `arr/.env` (voir son `.example`). Sans la variable attendue,
    l'indexeur n'est pas créé et le script le signale — un indexeur sans son
    secret ne répondrait à aucune recherche, ce qui est plus difficile à
    diagnostiquer qu'un objet absent.

    Pour connaître le `definitionName` d'un tracker, cherchez-le dans la liste
    des définitions Cardigann que Prowlarr expose :

    ```sh
    docker exec arr-prowlarr-1 curl -s -H "X-Api-Key: <clé>" \
      http://localhost:9696/api/v1/indexer/schema \
      | python3 -c 'import json,sys; [print(i["definitionName"], "—", i["name"]) for i in json.load(sys.stdin)]' \
      | grep -i <nom-du-tracker>
    ```

    Ne listez dans `fields` que ce qui **diffère du défaut** de la définition :
    tout le reste est repris du schéma, donc suit les mises à jour amont au
    lieu d'être figé. Rien n'empêche par ailleurs d'ajouter un indexeur
    directement dans l'UI (`https://prowlarr.<DOMAIN>`) — il ne sera
    simplement pas recréé sur une installation neuve.

    Reportez ensuite les IDs dans `CROSS_SEED_INDEXER_IDS` (`arr/.env`, voir
    `.env.example` — l'ID apparaît dans l'URL de chaque indexeur) et relancez
    `make up STACK=arr` pour que cross-seed les prenne en compte. Ces IDs sont
    attribués par Prowlarr et **changent si un indexeur est recréé** : un ID
    obsolète ne produit aucune erreur, seulement des recherches sans résultat
    (voir [`ISSUES.md`](ISSUES.md)). Prowlarr synchronise de lui-même les
    indexeurs vers Sonarr/Radarr, les applications de l'étape 17 étant en
    `fullSync`.

    Si votre bibliothèque (`${DATA_ROOT}/library`) n'est pas sur le même
    disque que le reste de `DATA_ROOT` (vérifiable avec `df` sur les deux
    chemins), les imports Sonarr/Radarr basculeront automatiquement en
    copie au lieu du hardlink — fonctionnel mais plus lent et
    transitoirement plus gourmand en espace disque.

19. **Générer le dashboard** (page de liens servie par la stack `traefik`
    sur `<DOMAIN>`/`www.<DOMAIN>`) :
    ```sh
    make dashboard-refresh
    ```
    `dashboard/html/` est généré, pas versionné — sans cette commande le
    domaine nu renvoie une page vide jusqu'au premier passage du cron
    (étape 21).

20. **Vérifier** : `https://<DOMAIN>` (dashboard),
    `https://nextcloud.<DOMAIN>`, `https://jellyfin.<DOMAIN>`,
    `https://seerr.<DOMAIN>`, et `https://transmission.<DOMAIN>` depuis le
    LAN.

21. **Sauvegardes et tâches planifiées** :
    ```sh
    make backup          # première exécution : crée le dépôt restic et
                          # génère sauvegarde/restic-password — copiez-le
                          # ailleurs immédiatement (gestionnaire de mdp)
    make cron-install     # installe scripts/crontab comme crontab de l'hôte
    ```
    `make cron-install` programme six tâches : le cron interne Nextcloud
    (`cron.php`, 5 min), la sauvegarde restic (hebdomadaire, dimanche 3h),
    la régénération du dashboard (5 min), `recyclarr-sync` +
    `apply-arr-overrides.py` (quotidien, minuit), la refermeture du
    middleware LAN-only (5 min, ne fait rien sauf après un
    `make switch-lan-only-middleware` — voir plus bas) et la rotation des
    journaux (quotidien, 4h30). Les tâches liées à une
    stack sont protégées par `scripts/require-running.sh` : elles ne font
    rien si la stack concernée est arrêtée.

    La commande **fusionne** ces tâches dans un bloc délimité par deux
    commentaires marqueurs et recopie verbatim tout ce qui est en dehors :
    vos propres jobs cron survivent à une réinstallation. Elle refuse
    d'installer plutôt que de deviner si `crontab -l` échoue pour une raison
    autre que « pas encore de crontab », ou si les marqueurs du bloc sont
    déséquilibrés. Elle rend aussi la configuration logrotate vers
    `${DATA_ROOT}/.logrotate.conf` — `logrotate` n'interprétant aucune
    variable, ses chemins doivent être littéraux.

22. **(optionnel) Addon Kodi « Supprimer avec clearr »** — sur la machine où
    tourne Kodi, pas forcément le serveur :
    ```sh
    make kodi-install     # ou KODI_HOME=/autre/.kodi
    ```
    Installe l'addon dans `~/.kodi/addons/` et pré-remplit l'URL de clearr
    depuis `.env.shared`. **Redémarrer Kodi** ensuite ; si l'entrée n'apparaît
    pas dans le menu contextuel d'un film/série, l'activer dans
    `Paramètres → Extensions → Mes extensions → Menus contextuels`. Suppose
    `jellyfin-kodi` en mode sync — détails et délais mesurés dans
    [`kodi/README.md`](kodi/README.md).

### Maintenance courante

`make help` (la cible par défaut : `make` tout court suffit) liste les commandes
et leurs arguments directement depuis le `Makefile`.

| Commande | Effet |
|---|---|
| `make help` | liste les cibles et leurs arguments |
| `make up STACK=<nom>` | démarre/recrée une stack |
| `make down STACK=<nom>` | arrête une stack |
| `make config STACK=<nom>` | affiche la config résolue (debug des `${VAR}`) |
| `make logs STACK=<nom>` | logs en direct |
| `make update STACK=<nom>` | pull + rebuild + recrée (+ maintenance `occ` si `nextcloud`) |
| `make update-all` | `update` sur nextcloud, vpn, jellyfin, arr, seerr (continue même si un stack échoue, résumé + prune images + refresh dashboard à la fin) |
| `make backup` | sauvegarde restic (aussi via cron) |
| `make restore SNAPSHOT=<id\|latest>` | restauration guidée d'un snapshot |
| `make cron-install` | (ré)installe `scripts/crontab` comme crontab de l'hôte |
| `make api-keys` | collecte les clés API générées au 1er démarrage vers `arr/.env` (voir étape 14) |
| `make provision` | crée la config d'installation restante (bibliothèques Jellyfin, objets arr, Seerr) — additif et relançable, voir étape 17 |
| `make clearr` | TUI de nettoyage torrents/bibliothèque (service `clearr`), voir ci-dessous |
| `make dashboard-refresh` | régénère le dashboard immédiatement (aussi via cron 5 min) |
| `make recyclarr-sync` | applique les guides TRaSH aux profils qualité arr (aussi via cron quotidien) |
| `make arr-overrides` | réapplique les réglages hors périmètre recyclarr + provisionne `arr/profiles/` + maintient les déclencheurs de suppression des connexions Jellyfin (à lancer après `recyclarr-sync`) |
| `make kodi-install` | installe l'addon Kodi « Supprimer avec clearr » dans le profil Kodi de l'utilisateur courant, voir [`kodi/README.md`](kodi/README.md) |
| `make switch-lan-only-middleware` | ouvre (ou referme) au WAN les services normalement restreints au LAN, voir ci-dessous |

#### Ouvrir temporairement les services LAN-only (`switch-lan-only-middleware`)

Transmission, Prowlarr, Sonarr, Radarr et clearr ne sont joignables que depuis
`LAN_CIDR` (middleware Traefik `ipAllowList`). Pour dépanner depuis l'extérieur
alors qu'on n'a qu'un accès SSH :

```sh
make switch-lan-only-middleware   # ouvre au WAN, referme seul au bout d'1 h
make switch-lan-only-middleware   # referme tout de suite
scripts/lan-only-middleware.sh status   # où on en est
```

- **Aucun conteneur n'est redémarré** : le middleware est défini dans
  `traefik/dynamic/lan-only.yml`, rechargé à chaud par Traefik. Seule sa plage
  d'adresses change (`LAN_CIDR` → tout le monde).
- **La refermeture est automatique une heure après l'ouverture**, assurée par une
  tâche cron installée par `make cron-install` (donc elle survit à la fin de la
  session SSH et à un redémarrage). Elle tombe dans les 5 minutes suivant
  l'échéance, pas à la seconde.
- Tant que c'est ouvert, **le dashboard affiche un bandeau rouge** rappelant
  l'heure de refermeture — c'est aussi pourquoi la commande régénère le dashboard.
- Ces services n'ont pas d'authentification forte : à n'ouvrir que le temps
  nécessaire.

#### Supprimer un torrent + sa place dans la bibliothèque (`clearr`)

Sonarr/Radarr et Transmission ne se parlent pas à la suppression : effacer
un film/une série dans Sonarr/Radarr ne touche pas le torrent dans le
client, et inversement. C'est un manque connu et non résolu de
l'écosystème *arr (aucun outil communautaire — Decluttarr, Removarr... —
ne couvre ce cas précis). `clearr` (`arr/clearr/`, service de la stack
`arr`) comble ce trou pour ce déploiement précis, sous deux formes qui
partagent la même logique :
- **web**, à `https://clearr.${DOMAIN}` (LAN uniquement, démarré en continu
  par `make up STACK=arr`) ;
- **TUI**, via `make clearr` (ponctuel, même conteneur/image) ;
- **depuis le menu contextuel de Kodi**, pour un film ou une série entière —
  addon à installer côté client avec `make kodi-install`, voir
  [`kodi/README.md`](kodi/README.md).

Liste les torrents Transmission (âge, taille, ratio, tracker — résolu via
Prowlarr quand possible), avec un onglet Séries et un onglet Films en plus
de l'onglet Torrents. Les torrents cross-seedés (même fichier injecté sur
plusieurs indexeurs par `cross-seed`) sont regroupés sous un seul
téléchargement d'origine, repliable. À la suppression d'un torrent, retrouve
et supprime aussi les fichiers `library/` correspondants (matching par
inode — ne fonctionne que parce que Sonarr/Radarr importent en hardlink,
voir [`ARCHITECTURE.md`](ARCHITECTURE.md)). Depuis les onglets Séries/Films,
un titre entier se supprime d'un coup (tous ses torrents + fichiers
résiduels). Une purge groupée retire en une fois tous les torrents dont le
fichier a disparu du disque (marqués ABS).

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
  cours continuent d'être recherchés. Depuis les onglets Séries/Films, une
  série ou un film entier est retiré complètement, quel que soit l'état de
  diffusion.

Best-effort : si Sonarr/Radarr est injoignable ou que le fichier ne leur
est pas connu (téléchargement jamais importé), ce volet est simplement
sauté — la suppression des fichiers n'est jamais bloquée pour autant.
Le plan d'action est affiché dans l'écran de confirmation avant exécution.

Journal de chaque suppression (fichiers touchés côté Transmission,
bibliothèque et actions Sonarr/Radarr, erreurs éventuelles) dans
`${DATA_ROOT}/.clearr.log`, tourné chaque semaine par le cron de l'étape 21.
Niveau `INFO` par défaut ; passer `CLEARR_LOG_LEVEL=DEBUG` dans
`arr/docker-compose.yml` pour le détail des appels RPC et des recherches de
correspondance, utile seulement en diagnostic (~95 Ko/jour).
