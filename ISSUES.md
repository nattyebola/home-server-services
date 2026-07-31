# Problèmes rencontrés

Pièges déjà rencontrés sur ce déploiement et comment ils ont été
contournés — pour éviter de les redécouvrir à chaque fois. Pour
l'installation, voir [`README.md`](README.md) ; pour le rôle de chaque
service et le pourquoi des choix d'architecture, voir
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## VPN / Transmission

- `haugene/transmission-openvpn` pousse une route `redirect-gateway def1`
  qui scinde `0.0.0.0/0` en deux `/1` couvrant la quasi-totalité de
  l'espace IPv4 — y compris `172.16.0.0/12`, la plage que Docker utilise
  par défaut pour ses réseaux. **Attacher ce container à un second réseau
  Docker (ex. le réseau public de Traefik) casse le routing sortant du
  tunnel** (DNS/ping ne sortent plus, `rtnl: generic error (-101)` dans
  les logs) — reproduit de façon fiable, en hot-attach comme en
  déclarant les deux réseaux dès la création. Solution : le garder sur un
  unique réseau dédié (subnet pinné) et faire pont vers Traefik via un
  sidecar (`transmission-proxy`) qui ne touche jamais au tunnel.
- Même piège avec la variable `LOCAL_NETWORK` : elle fait un
  `ip route replace <subnet> via <gw>` par entrée — y mettre le propre
  sous-réseau du container casse pareil le routing. Pour autoriser un pair
  du même réseau Docker (le sidecar) à atteindre le port RPC, utiliser
  `UFW_ALLOW_GW_NET=true` à la place (lecture de route + règle ufw, pas de
  `route replace`).
- `haugene/transmission-openvpn` a besoin du module kernel `ip_tables` sur
  l'hôte pour ses règles de routing/kill-switch — absent par défaut sur
  les Ubuntu récents (remplacé par `nftables`). Sans lui le container ne
  démarre pas correctement. Fix : `/etc/modules-load.d/ip-tables.conf`
  contenant `ip_tables` pour le charger au boot (voir
  [Prérequis](README.md#prérequis)).

## Traefik / Let's Encrypt

- Traefik **ne retente pas de lui-même** un certificat resté en échec
  (par ex. après un DNS en `NXDOMAIN` au moment de la tentative ACME) :
  une fois le DNS corrigé, il faut redémarrer le container pour relancer
  l'obtention du certificat.

## Arr / Sonarr / Radarr / cross-seed

- **DNS du FAI qui renvoie `127.0.0.1` pour certains domaines** (blocage
  anti-piratage côté FAI, ex. domaines de trackers/indexeurs) — se présente
  comme une panne réseau (`Connection refused`) alors que le domaine répond
  normalement via un résolveur public. Rencontré sur `arr/prowlarr` en
  ajoutant un indexeur. Fix : forcer `dns: ${DNS_PRIMARY}/${DNS_SECONDARY}`
  (Cloudflare par défaut, `.env.shared`) sur le service concerné (déjà le
  cas sur Jellyfin par défaut ; ajouté aussi sur
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
  présent (vérifié en interrogeant directement l'API RPC de
  `transmission-vpn`). Le job périodique "inject" ne rattrape pas ces
  échecs non plus tant que ce réglage manque.
- **Bibliothèque sur un disque différent de `DATA_ROOT`** : si
  `${DATA_ROOT}/library` n'est pas sur le même disque physique que le
  reste de `DATA_ROOT` (vérifiable avec `df` sur les deux chemins), les
  imports Sonarr/Radarr basculent automatiquement en copie complète au
  lieu du hardlink — fonctionnel mais plus lent et transitoirement plus
  gourmand en espace disque.

## Divers

- Nextcloud AIO a été volontairement écarté (voir
  [`ARCHITECTURE.md`](ARCHITECTURE.md#nextcloud-nextcloud)) : il pilote
  ses propres containers via le socket Docker, ce qui casse à la fois le
  modèle rootless et infra-as-code de ce repo.
- Un ancien plugin Nextcloud (`cms_pico`) laissait une règle de proxy
  hairpin (`location /sites/` → `https://www.<DOMAIN>/`) dans la config
  nginx ; retirée avec le plugin.

## Windows / WSL2

Ce repo suppose un vrai hôte Linux (bare metal ou VM Linux) ; **pas testé
ni recommandé sous Windows + WSL2**, pour plusieurs raisons qui touchent
au cœur de l'architecture, pas de simples détails :

- Le noyau WSL2 est un noyau Microsoft figé, sans chargement de module à
  la `modprobe`/`systemd-modules-load` — le fix `ip_tables` (voir
  [VPN / Transmission](#vpn--transmission) ci-dessus) risque de ne pas
  s'appliquer, et sans lui le kill-switch iptables de `transmission-vpn`
  ne fonctionne probablement pas.
- Traefik + Let's Encrypt (HTTP-01) exigent une machine joignable
  directement depuis internet sur 80/443 ; sous WSL2 le trafic doit
  traverser routeur → Windows → VM WSL2 (NAT), ce qui demande des règles
  `netsh portproxy` en plus et une IP de VM qui change à chaque
  redémarrage (sauf mode "mirrored networking", récent et pas garanti
  stable).
- WSL2 n'est pas un service persistant : Windows peut arrêter la VM en
  idle, ce qui casse la sauvegarde cron du dimanche (`make cron-install`)
  et la disponibilité continue des services, sauf configuration
  supplémentaire pour empêcher l'arrêt/forcer le démarrage au boot.
- Les hardlinks dont dépendent l'import Sonarr/Radarr et le service `clearr`
  (voir `CLAUDE.md`, section hardlinks) ne
  fonctionnent que si `DATA_ROOT` est sur le filesystem natif WSL2 (ext4
  virtuel) — sur un disque Windows monté en `/mnt/c/...` (drvfs), ils
  cassent et on retombe sur des copies complètes.
- Le passthrough GPU de WSL2 cible CUDA/DirectML, pas les render nodes
  VAAPI Intel/AMD (`/dev/dri/renderD128`) — le transcodage matériel
  Jellyfin est probablement inutilisable tel quel.

Le modèle rootless (`cap_drop`/`no-new-privileges`) n'a lui aucun souci
particulier sous WSL2. Les stacks sans réseau public ni VPN (Nextcloud,
Jellyfin sans transcodage matériel) pourraient tourner en bricolant un
peu, mais l'architecture globale ne s'y prête pas.
