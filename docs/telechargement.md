[← Accueil](../README.md) · **Téléchargement**

# 🧲 Téléchargement : VPN, Transmission et arr

La chaîne qui transforme une demande (« je veux ce film ») en fichier rangé
dans la bibliothèque, sans jamais sortir hors du tunnel VPN.

| Service | Stack | Rôle |
|---|---|---|
| `transmission-vpn` | `vpn/` | client torrent ; tout son trafic passe par OpenVPN (AirVPN) |
| `transmission-proxy` | `vpn/` | nginx, seul pont entre Traefik et le RPC de Transmission |
| `webproxy` | `vpn/` | relais TCP vers le Privoxy intégré (navigateur du LAN → IP du tunnel) |
| Prowlarr | `arr/` | gère les indexeurs (trackers) et les partage aux autres |
| Sonarr / Radarr | `arr/` | suivent séries / films, grabent, importent dans `library/` |
| cross-seed | `arr/` | re-partage un fichier déjà téléchargé sur d'autres trackers |
| recyclarr | `arr/` | applique les profils qualité des guides TRaSH (à la demande) |
| clearr | `arr/` | suppression propre torrent + bibliothèque → [page dédiée](clearr.md) |

Toutes les interfaces sont **LAN uniquement** (`transmission.`, `prowlarr.`,
`sonarr.`, `radarr.`, `clearr.<DOMAIN>`).

## Le parcours d'un téléchargement

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant S as Seerr
    participant A as Sonarr / Radarr
    participant P as Prowlarr
    participant T as Transmission (VPN)
    participant J as Jellyfin
    participant K as Kodi
    participant C as cross-seed

    U->>S: Demande un film / une série
    S->>A: Ajoute le titre (profil qualité choisi)
    A->>P: Recherche (RSS ou recherche active)
    P-->>A: Releases des indexeurs
    A->>T: Grab de la meilleure release
    Note over T: Télécharge dans<br>completed/{sonarr,radarr}
    T-->>A: Téléchargement terminé
    A->>A: Import : hardlink vers library/, renommage, .nfo
    A->>J: « Rafraîchis ce dossier »
    Note over J: ~60 s (LibraryMonitorDelay)
    J->>K: Synchronisation (jellyfin-kodi)
    A-)C: Notification d'import (Custom Script)
```

## VPN / Transmission

```mermaid
flowchart LR
    LAN(("🏠 LAN")) --> Traefik
    Traefik -->|"traefik-restricted<br>+ filtre LAN"| Proxy["transmission-proxy"]
    Proxy -->|"vpn-internal"| TVPN["transmission-vpn"]
    Arr["Prowlarr / Sonarr /<br>Radarr / cross-seed"] -->|"vpn-internal<br>transmission-vpn:9091"| TVPN
    Browser["Navigateur du LAN"] -->|"IP_LAN:8118"| WebProxy["webproxy"] --> TVPN
    TVPN ==>|"tunnel OpenVPN"| Internet(("🌍"))
```

- Le client torrent se configure sur
  `https://transmission.<DOMAIN>/transmission/rpc`. Aucun port n'est publié sur
  l'hôte.
- Les arr joignent `transmission-vpn:9091` **directement** par
  `vpn-internal`. `transmission-proxy` ne sert qu'à l'accès humain.
- **`webproxy`** expose le Privoxy de l'image pour qu'un navigateur du LAN
  sorte par l'IP du tunnel (contourner un blocage géographique).
- **10 téléchargements simultanés** (`TRANSMISSION_DOWNLOAD_QUEUE_SIZE`, défaut
  5). Un magnet qui ne trouve pas ses métadonnées garde sa place sans rien
  télécharger : à 5, trois magnets bloqués suffisaient à geler toute la file.

> [!CAUTION]
> Privoxy **n'a aucune authentification**. Le port est lié à l'IP LAN de
> l'hôte (défaut `127.0.0.1`), jamais `0.0.0.0` : un port publié par Docker
> contourne le pare-feu. Ne jamais le rediriger sur la box. Côté navigateur,
> désactiver WebRTC (il révèle l'IP réelle) et n'y connecter aucun compte
> personnel : l'IP de sortie est celle qui seede.

### Pièges connus

- **Ne jamais attacher `transmission-vpn` à un second réseau Docker.** L'image
  pousse une route `redirect-gateway def1`, qui couvre aussi `172.16.0.0/12`,
  la plage des réseaux Docker. Un second réseau casse tout le routage sortant
  (`rtnl: generic error (-101)`, plus de DNS ni de ping), de façon
  reproductible. D'où le sidecar `transmission-proxy`.
- **Même piège avec `LOCAL_NETWORK`** : n'y mettez jamais le sous-réseau du
  conteneur (chaque entrée fait un `ip route replace`). Pour laisser un pair
  du même réseau joindre le RPC, utiliser `UFW_ALLOW_GW_NET=true`.
- **Module `ip_tables` requis sur l'hôte** (absent par défaut sur les Ubuntu
  récents, passés à nftables) : `/etc/modules-load.d/ip-tables.conf` contenant
  `ip_tables`. Sans lui, le kill-switch ne se met pas en place.
- **`transmission-remote -w <dir>` avant `-a` change le dossier par défaut de
  la session**, pas celui du torrent ajouté. Toujours écrire
  `-a <magnet> -w <dir>`. Sonarr/Radarr collant leur catégorie au dossier par
  défaut courant, l'erreur se propage à tous les grabs suivants.
- **`settings.json` n'est réécrit qu'à l'arrêt du daemon** : lire l'état réel
  par `session-get`, jamais dans le fichier ; une édition à chaud est perdue.
- **Déplacer des données** : `torrent-set-location … move=true`, jamais `mv`.
  Sur le même disque, l'inode est conservé (les hardlinks survivent), mais pas
  les symlinks de cross-seed.

## Prowlarr, Sonarr, Radarr

Images `lscr.io/linuxserver/*`. Elles démarrent normalement en root pour
appliquer `PUID`/`PGID` ; ici elles tournent dans le mode non-root documenté
par LinuxServer (`user: PUID:PGID` + `tmpfs /run`, sans `cap_add`). On perd
seulement les Docker Mods, qui ne sont pas utilisés.

### Hardlinks : un fichier, deux chemins

Un fichier téléchargé est **à la fois** seedé par Transmission et rangé dans la
bibliothèque, sans occuper deux fois la place :

```mermaid
flowchart LR
    subgraph disk["Un seul disque, un seul montage ${DATA_ROOT}:/data_root"]
        D[".transmission/data/completed/sonarr/<br>Release.Name.S01E01.mkv"]
        L["library/series/Titre/Season 01/<br>Titre - S01E01.mkv"]
        I[("inode 123456<br>les octets")]
        D --- I
        L --- I
    end
    T["Transmission<br>(seed)"] --> D
    J["Jellyfin / Kodi<br>(lecture)"] --> L
```

Trois conditions, toutes posées par `make provision` :

1. **Un seul montage** `${DATA_ROOT}:/data_root` pour Sonarr et Radarr. Deux
   bind-mounts séparés, même d'un seul et même disque, font échouer `link()`
   en `Cross-device link`, et l'import retombe **silencieusement** en copie
   (~185 Go récupérés en corrigeant).
2. **Remote path mapping** : Transmission annonce `/data/completed/…`, les arr
   voient `/data_root/.transmission/data/completed/…`. Sans lui les
   téléchargements aboutissent mais ne sont **jamais importés**, sans erreur.
3. Root folders sur `/data_root/library/{film,series,anime}`.

Si `library/` est sur un autre disque que le reste de `DATA_ROOT`
(vérifiable avec `df`), les imports deviennent des copies : ça marche, en plus
lent et en plus gros.

### Ce que le dépôt configure

| Réglage | Porté par | Effet |
|---|---|---|
| Custom formats + profils des guides TRaSH | `recyclarr` (`arr/recyclarr/`) | profils `WEB-2160p (Combined)` (Sonarr) et `[SQP] SQP-1 WEB (2160p)` (Radarr) |
| Profils anime `Anime (Fansub) VF` / `VOSTFR` | `arr/profiles/sonarr-anime.json` | choix de la langue **par série** (profil à changer à la main dans Sonarr) |
| Ce que recyclarr écrase ou ne couvre pas | `apply-arr-overrides.py` | tailles de palier, langue, connexions Jellyfin, `.nfo`, renommage, ratio des indexeurs publics… réappliqué chaque nuit |
| Délai de 3 h sur l'anime VOSTFR | tag `anime-vostfr-delai` | laisse arriver une release VOSTFR plutôt que grabber la première puis la remplacer 5 fois ; une release déjà ≥ 50 part tout de suite |
| Ratio 1.5 sur les indexeurs publics | `PUBLIC_INDEXER_SEED_RATIO` | un tracker public ne compte pas le ratio ; les privés ne sont pas touchés |
| Repacks départagés par le score | `downloadPropersAndRepacks: doNotPrefer` | un REPACK n'est plus grabé s'il score moins que le fichier en place (le CF `Repack/Proper` garde la préférence) |
| Rejet des archives/exécutables | `failDownloads` | une « release » `.exe`/`.zipx` est marquée en échec et remplacée automatiquement |
| Recherche des manquants | `search-missing.py`, lundi 5 h | Sonarr/Radarr **ne re-cherchent jamais** seuls un manquant raté au RSS ; plafonné à 12 recherches, rotation par ancienneté ; tâche en rouge si l'arr fait échouer la commande de recherche |
| Marqueurs de fin de saison | `mark-finale.sh`, hook + cron 2 h | `†` fin de saison, `‡` fin de série, `½` mi-saison devant le titre d'épisode (visible dans Kodi) |

> [!IMPORTANT]
> **L'ordre compte** : `make recyclarr-sync` **puis** `make arr-overrides`.
> Les overrides désignent par leur nom des custom formats que recyclarr crée.
> Le cron de minuit les enchaîne sur une seule ligne.

**Déclaratif ou additif ?** `apply-arr-overrides.py` **fait autorité** : une
modification faite à la main dans l'UI sur son périmètre est annulée la nuit
suivante. `provision.py` fait l'inverse : il **crée ce qui manque et ne
réécrit jamais**, donc ce qu'il a créé (client de téléchargement, root
folders, tags…) peut être ajusté librement dans les UI.

### Pièges connus

- **Un titre « manquant » est souvent déjà téléchargé**, bloqué dans la file
  (`importBlocked`/`importPending`) : aucune erreur visible hors de la file.
  Le dashboard compte ces téléchargements (un pack compte pour un, même si
  Sonarr crée une entrée de file par épisode) ; `scripts/manual-import.py`
  (skill `manual-import`) les débloque.
- **Un REPACK grabé puis refusé à l'import** (« Not a Custom Format upgrade »),
  sur un fichier déjà en place en mieux : symptôme du défaut
  `preferAndUpgrade`, qui fait passer le repack avant le score. Réglé à
  `doNotPrefer` par `make arr-overrides` (2026-10-01). L'entrée restée en file
  se purge via le skill `manual-import`.
- **Fichiers posés en vrac** à la racine d'un dossier scanné : ignorés sans
  log. Utiliser *Manual Import*.
- **Un import manuel par l'API sans `downloadId` déplace le fichier** au lieu
  de le hardlinker : le torrent perd ses données et passe ABS dans clearr.
  Hors file d'attente, passer `"importMode": "hardlink"`. Pour réparer :
  `ln` du fichier de `library/` vers son ancien chemin sous `completed/`, puis
  vérifier le torrent dans Transmission.
- **Écritures asynchrones** : certaines écritures de config Servarr répondent
  `202 Accepted` et s'appliquent plus tard (observé de 0,5 s à 53 s). Un
  « lire → comparer » juste derrière voit encore l'ancienne valeur.
  `apply-arr-overrides.py` relit donc jusqu'à stabilisation (`settle()`) :
  il faut **60 s sans rien à corriger**, relu toutes les 10 s. Un
  `make arr-overrides` dure donc au moins 2 minutes (Sonarr puis Radarr).
- **Rapport de `make arr-overrides` en cas d'erreur** : les lignes
  `corrigé:` listent tout ce qui a été écrit, même si l'étape a échoué
  ensuite ; un indexeur en échec n'empêche pas de traiter les suivants. Exit 1
  dès qu'une ligne `erreur:` apparaît.
- **`make provision` / `make api-keys` : `ignoré:` ≠ `erreur:`**. `ignoré:`
  (exit 0) = prérequis pas encore en place (clé absente, conteneur arrêté,
  fichier à copier) : relancer une fois corrigé. `erreur:` (exit 1) = service
  injoignable ou réponse HTTP en erreur. Un élément ignoré n'empêche plus de
  traiter les autres (indexeurs, serveurs Seerr).
- **Un custom format géré par recyclarr est réécrit au sync suivant** : ne pas
  le modifier par l'API, créer un CF distinct (c'est l'origine de
  `VOSTFR (hors suffixe)`).
- **Boucle de regrab infinie** : un CF qui matche un suffixe présent dans le
  titre du post mais **absent du nom du fichier** fait scorer la release plus
  haut que le fichier importé. La même release a alors l'air d'un upgrade
  d'elle-même, et le même magnet a été grabé 3 à 5 fois. Tout
  `cutoffFormatScore` doit être atteignable par un *fichier*.
- **Les arr demandent un login, même depuis le LAN** (`authenticationRequired:
  enabled`, posé par `make arr-overrides`). Avec `disabledForLocalAddresses`,
  toute IP Docker comptait comme locale : jellyfin, seerr ou nextcloud-web,
  exposés au WAN, lisaient la clé API sur `/initialize.json`. Les appels par
  clé API (Seerr, cross-seed, clearr, scripts) ne sont pas concernés.
  Réglage relu au démarrage seulement : redémarrer l'arr après un changement.
- **`trustedNetworks` = le réseau Docker du proxy**, pas le LAN : il désigne
  les proxies dont on croit le `X-Forwarded-For` (l'IP réelle du client dans
  les journaux).

## cross-seed

Re-partage sur d'autres trackers un fichier déjà téléchargé (même contenu,
autre `.torrent`), ce qui améliore le ratio sans télécharger une deuxième
fois. Déclenché à chaque import par un **Custom Script** Sonarr/Radarr
(`arr/scripts/cross-seed-notify.sh`), plus une recherche périodique.

- Le montage reste `/data` (cross-seed n'a pas de remote path mapping) ; ses
  liens vont dans `/data/.cross-seed-links`, **sous le même montage**.
- La recherche périodique ne couvre que les torrents de moins de 30 jours
  (cadence 3 j / exclusion 9 j / 30 j, contraintes de validation de
  cross-seed). Un rattrapage complet se lance à la main, jamais en automatique.

### Pièges connus

- **Custom Script, pas Webhook** : le Webhook générique envoie un test sans
  vrai hash, cross-seed le rejette et **la connexion ne s'enregistre pas**.
- **Scripts montés en dossier** : `arr/scripts/` est vu en
  `/custom-scripts` (lecture seule) par Sonarr et Radarr. Un script modifié
  par `git pull` est pris en compte **tout de suite**, sans restart. Avant le
  2026-09-29, chaque script était monté seul sous `/config/custom-*.sh`, et
  le conteneur gardait l'ancienne version. `make provision` repointe une
  connexion restée sur l'ancien chemin.
- **`useClientTorrents: true`** requis dans `arr/cross-seed/config.js`, sinon
  chaque notification échoue (`Torrent client does not have any torrent…`).
- **Les ID d'indexeur Prowlarr changent** quand un indexeur est recréé. Un ID
  obsolète dans `CROSS_SEED_INDEXER_IDS` ne produit **aucune erreur**,
  seulement `Found 0 torrents` à chaque fois. Après toute recréation
  d'indexeur, mettre à jour `arr/.env` puis `make up STACK=arr`.

## Voir aussi

- [Médias](medias.md) — ce qui se passe après l'import (Jellyfin, `.nfo`,
  Kodi, Seerr).
- [clearr](clearr.md) — supprimer un titre sans qu'il revienne.
- [Komga](komga.md) — les BD passent par Prowlarr mais **sans** arr.
