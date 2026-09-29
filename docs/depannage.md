[← Accueil](../README.md) · **Dépannage**

# 🩺 Dépannage

Chaque page thématique a sa section « Pièges connus ». Celle-ci part du
**symptôme** et renvoie à la bonne page, puis regroupe les pièges Docker qui
touchent toutes les stacks.

## Par symptôme

| Symptôme | Cause probable | Voir |
|---|---|---|
| Téléchargement fini, **jamais importé**, aucune erreur | remote path mapping absent, ou entrée `importBlocked`/`importPending` dans la file | [Téléchargement](telechargement.md#pièges-connus-1) |
| Un titre « manquant » depuis des semaines | Sonarr/Radarr ne re-cherchent jamais seuls ; ou il est bloqué dans la file | [Téléchargement](telechargement.md#ce-que-le-dépôt-configure) |
| Imports **en copie** au lieu de hardlinks, disque qui se remplit | deux bind-mounts séparés, ou `library/` sur un autre disque | [Téléchargement](telechargement.md#hardlinks--un-fichier-deux-chemins) |
| Le **même torrent regrabé** en boucle | custom format qui matche le titre du post mais pas le nom de fichier | [Téléchargement](telechargement.md#pièges-connus-1) |
| Recherches cross-seed toujours à `Found 0 torrents` | ID d'indexeur obsolète dans `CROSS_SEED_INDEXER_IDS` | [Téléchargement](telechargement.md#cross-seed) |
| Indexeur en `429` / « API Request Limit reached » | souvent le backoff d'échec de Prowlarr, pas un quota | skill `indexer-quota` |
| Plus de DNS ni de ping dans `transmission-vpn`, `rtnl: generic error (-101)` | conteneur attaché à un 2e réseau, ou `LOCAL_NETWORK` mal réglé | [Téléchargement](telechargement.md#vpn--transmission) |
| `transmission-vpn` ne démarre pas / ne route rien | module `ip_tables` absent sur l'hôte | [Installation](installation.md#prérequis) |
| `Connection refused` vers un tracker qui répond ailleurs | DNS du FAI qui renvoie `127.0.0.1` | [plus bas](#dns-du-fai-menteur) |
| Dashboard surligné **« périmé »** | régénération en échec (`docker ps` ou `docker compose config`), voir `dashboard/refresh.log` | [Traefik](traefik.md#pièges-connus) |
| Carte **grisée** sur le dashboard alors que le service est `healthy` | chemin de sonde qui redirige vers `/login` (arr : sonder `/Content/…`) | [Traefik](traefik.md#le-dashboard) |
| Les 5 services LAN répondent **404** | `traefik/dynamic/lan-only.yml` absent | [Traefik](traefik.md#pièges-connus) |
| Certificat resté en échec | Traefik ne retente pas seul | [Traefik](traefik.md#pièges-connus) |
| Titre mal identifié dans Jellyfin/Kodi, ou insupprimable depuis Kodi | `.nfo` absent ou ancien : rescan côté arr, puis « Identifier » dans Jellyfin | [Médias](medias.md#des-titres-bien-identifiés--les-nfo) |
| Épisode rangé dans la **mauvaise saison** | Jellyfin lit la saison dans le nom de fichier | [Médias](medias.md#des-titres-bien-identifiés--les-nfo) |
| Jellyfin : toutes les connexions notées depuis `172.18.0.x` | `KnownProxies` résolu sur une ancienne IP de Traefik | [Médias](medias.md#pièges-connus-jellyfin) |
| Jellyfin mal rafraîchi après un import, `401` dans les logs arr | forme d'authentification refusée par Jellyfin 12 | [Médias](medias.md#pièges-connus-jellyfin) |
| Seerr en crash `EACCES` en boucle | dossier de config créé en root | [Médias](medias.md#pièges-connus) |
| Seerr propose de redemander un titre déjà là | pas de bibliothèque Jellyfin sur `library/` | [Médias](medias.md#seerr) |
| clearr : bandeau rouge après une suppression, ou « Purge refusée » | arr injoignable au moment de la suppression ; données Transmission non montées | [clearr](clearr.md#pièges-connus) |
| Un port paraît ouvert au WAN quand on teste depuis le serveur | `/etc/hosts` fait résoudre le domaine vers l'IP LAN | [Installation](installation.md#hôte--etchosts) |
| BD illisible dans Komga | `.cbr` en RAR5 ou archive « solid » | [Komga](komga.md#pièges-connus) |
| Avertissements de sécurité dans l'admin Nextcloud | `security-headers` remis sur son routeur | [Nextcloud](nextcloud.md#pièges-connus) |
| Une tâche cron « réussit » à la main mais pas sous cron | `%` non échappé | [Exploitation](exploitation.md#tâches-planifiées) |
| Un réglage arr corrigé « déjà à jour » alors qu'il a dérivé | écriture Servarr asynchrone (`202`) | [Téléchargement](telechargement.md#pièges-connus-1) |

## Pièges Docker transverses

### Bind-mount de fichier figé sur l'ancien contenu

Un bind-mount de **fichier** suit l'inode capturé au démarrage, pas le chemin.
Si le fichier hôte est *remplacé* (éditeur qui réécrit, `mv`, génération par
script), le conteneur continue de lire l'ancien, alors que `docker inspect`
montre le bon chemin. **`make restart STACK=…`** suffit. Quand un fichier
doit pouvoir être remplacé à chaud, monter son **dossier** (c'est le cas de
`traefik/dynamic/`).

### Pas de montage sous un parent en lecture seule

Docker ne peut pas créer un point de montage à l'intérieur d'un montage `:ro`
(`mkdirat … read-only file system`). Solution : un seul montage, ou deux
montages côte à côte. Rencontré sur cross-seed et sur le dashboard.

### Hardlink refusé entre deux montages du même disque

`link()` échoue en `Cross-device link` dès que source et destination sont sur
deux bind-mounts distincts, **même s'il s'agit de la même partition** (et même
si `stat` affiche le même `st_dev`). D'où le montage unique
`${DATA_ROOT}:/data_root`, voir [Téléchargement](telechargement.md#hardlinks--un-fichier-deux-chemins).

### Secret « introuvable » avec `cap_drop: ALL`

Sans `CAP_DAC_OVERRIDE`, root dans le conteneur ne lit plus un fichier `600`
appartenant à l'utilisateur de l'hôte. Beaucoup d'outils rapportent alors
« fichier absent ». Solution : `user: "${PUID}:${PGID}"`. Ne jamais élargir
les permissions du secret, ni assouplir le `cap_drop`.

### Arborescences fantômes en root

Un chemin d'exemple non remplacé dans un override, ou un dossier monté qui
n'existe pas encore, est créé par Docker **en root**. Les services non-root
crashent alors en `EACCES`. `make up` crée à l'avance les dossiers connus
(Seerr, Komga, `traefik/dynamic/`) ; pour le reste, remplir les `.example`
avant de les utiliser.

### DNS du FAI menteur

Certains FAI répondent `127.0.0.1` pour des domaines de trackers (blocage
anti-piratage). Cela ressemble à une panne réseau (`Connection refused`) alors
que le domaine répond via un résolveur public. D'où `dns:
${DNS_PRIMARY}/${DNS_SECONDARY}` sur `jellyfin`, `arr/*` et `vpn/*`. À ajouter
sur tout nouveau service qui interroge des trackers.

### Réseau d'une autre stack introuvable

Un réseau créé par une stack s'appelle `<dossier>_<nom>` : `vpn_vpn-internal`,
`arr_default`. La déclaration `external: true` doit porter ce `name:`.

## Windows / WSL2

**Non supporté.** Ce dépôt suppose un vrai hôte Linux, et WSL2 casse plusieurs
briques centrales :

| Brique | Ce qui casse sous WSL2 |
|---|---|
| Kill-switch VPN | noyau Microsoft figé, pas de chargement de module : le correctif `ip_tables` ne s'applique pas |
| Let's Encrypt, accès public | NAT routeur → Windows → VM, `netsh portproxy` à maintenir, IP de VM qui change à chaque redémarrage |
| Sauvegarde, disponibilité | Windows arrête la VM au repos : le cron du dimanche ne tourne pas |
| Hardlinks, clearr | cassés si `DATA_ROOT` est sur un disque Windows (`/mnt/c`, drvfs) : retour aux copies complètes |
| Transcodage Jellyfin | le passthrough GPU vise CUDA/DirectML, pas VAAPI (`/dev/dri/renderD128`) |

Le modèle non-root, lui, n'y pose aucun problème. Nextcloud ou Jellyfin sans
transcodage pourraient tourner en bricolant, mais pas l'ensemble.

## Historique

- Un ancien plugin Nextcloud (`cms_pico`) laissait une règle de proxy
  `location /sites/` dans la config nginx ; retirée avec le plugin.
