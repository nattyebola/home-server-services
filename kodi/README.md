# kodi/ — addon de menu contextuel « Supprimer avec clearr »

Ajoute une entrée au menu contextuel de Kodi sur un film ou une série de la
bibliothèque : elle demande à [clearr](../arr/clearr/) de supprimer le titre —
torrents Transmission, fichiers de `library/` et entrée Sonarr/Radarr, exactement
ce que font les vues Séries/Films de son interface web.

Ce n'est pas un service de la stack : c'est un addon à installer dans le profil
Kodi du **client** (ici Kodi tourne sur la même machine que le serveur, mais rien
n'en dépend — seule l'URL de clearr compte).

## Installation

```sh
make kodi-install                      # profil Kodi de l'utilisateur courant
make kodi-install KODI_HOME=/autre/.kodi   # autre utilisateur / autre profil
```

Le target copie `context.clearr/` dans `~/.kodi/addons/` et pré-remplit l'URL de
clearr (`https://clearr.${DOMAIN}`, lue depuis `.env.shared`) dans les réglages
de l'addon. **Redémarrer Kodi** ensuite pour qu'il charge l'addon ; si l'entrée
n'apparaît pas dans le menu contextuel, l'activer dans
`Paramètres → Extensions → Mes extensions → Menus contextuels`.

L'URL est un réglage d'addon et non une constante du code : elle contient le
domaine du déploiement, qui n'a pas sa place dans un fichier versionné sur un
repo public. Elle est modifiable à tout moment dans les paramètres de
l'extension, et `make kodi-install` ne l'écrase jamais si elle existe déjà.

## Ce qui se passe quand on clique

1. L'addon lit le type (`movie`/`tvshow`) et le `DBID` de l'élément sélectionné,
   puis récupère par JSON-RPC (`VideoLibrary.GetMovieDetails`/
   `GetTVShowDetails`) ses identifiants externes (propriété `uniqueid` : IMDb /
   TMDB / TVDB, les `ProviderIds` que jellyfin-kodi a recopiés depuis Jellyfin)
   **et son chemin** (propriété `file` : le fichier pour un film, le dossier
   pour une série).
2. `POST https://clearr.${DOMAIN}/api/preview/{film,series}` : clearr résout le
   titre et répond ce qui serait supprimé (nombre de torrents, fichiers sans
   torrent, taille totale).
3. Confirmation obligatoire, annonçant ce résumé (« 3 torrents — 15,6 Go »).
4. `POST https://clearr.${DOMAIN}/api/delete/{film,series}`, même corps.
5. Une notification Kodi affiche le message renvoyé par clearr (titre supprimé et
   espace libéré, ou la raison de l'échec), et l'addon s'arrête là.

## Titres hors Sonarr/Radarr

Jellyfin sert aussi le dossier des téléchargements terminés comme bibliothèque
(`.transmission/data/completed`, cf. `jellyfin/docker-compose.override.yml`) :
Kodi affiche donc des films et des séries récupérés à la main, qui n'existent
dans aucune des deux instances arr. L'addon les gère aussi.

clearr essaie d'abord les identifiants externes côté Sonarr/Radarr ; s'ils ne
donnent rien, il retombe sur le **chemin**, et supprime alors les torrents
correspondants (avec leurs données) plus les fichiers du dossier qu'aucun
torrent ne couvre — cas réel : un dossier de 2,6 Go dont les torrents avaient
été retirés de Transmission depuis longtemps, jusqu'ici insupprimable autrement
qu'à la main. Aucun appel Sonarr/Radarr dans ce mode, le titre n'y étant pas.

Le chemin est même plus fiable que les identifiants pour ces titres : deux
dossiers distincts peuvent porter le même `tvdbId` (les deux saisons de Hell's
Paradise, téléchargées séparément, sont deux séries pour Jellyfin), alors que la
résolution par id refuse — à juste titre — de trancher entre deux titres.

Deux garde-fous :

- **Le repli par chemin s'interdit `library/`**, l'arborescence de
  Sonarr/Radarr. Un titre qui y vit est presque toujours suivi par un arr :
  supprimer ses fichiers sans retirer son entrée le ferait simplement
  re-télécharger. L'addon renvoie alors vers l'interface web de clearr.
- **Le préfixe du chemin n'est pas supposé identique** des deux côtés (Kodi voit
  `/grosDur/…`, clearr voit `/data_root/…`, un autre client verrait un partage
  réseau) : clearr cherche le plus long suffixe de composants qui existe
  réellement sous une racine connue, deux composants minimum. Un chemin
  introuvable ou ambigu ne supprime rien.

Ceci suppose que jellyfin-kodi est en **chemins directs** (`useDirectPaths`) :
en mode addon, Kodi ne connaîtrait qu'une URL `plugin://`, que clearr ne saurait
rattacher à aucun fichier. Les titres suivis par Sonarr/Radarr continueraient
d'être supprimables (résolution par id), pas les autres.

L'addon **ne rafraîchit pas la vue** : la ligne ne peut pas disparaître dans la
foulée, la propagation prend plus d'une minute (chiffres plus bas). Elle part
d'elle-même quand jellyfin-kodi retire l'item, au plus tard au prochain
changement de vue. La chaîne est :

```
clearr supprime les fichiers                                    < 1 s
  -> Jellyfin rafraîchit le dossier concerné et retire l'item    +65 s
  -> le plugin KodiSyncQueue enregistre le retrait                +5 s
  -> jellyfin-kodi (Kodi) reçoit LibraryChanged/ItemsRemoved     +25 s
  -> la série disparaît de la base vidéo Kodi                     +3 s
```

Chiffres **mesurés** le 2026-08-05 sur une suppression réelle (série de 12
torrents, 15,6 Go) : 1 min 38 s au total, dont moins d'une seconde côté serveur.
L'essentiel du délai est le `LibraryMonitorDelay` de Jellyfin (60 s), qui
s'applique **aussi** aux mises à jour signalées par Sonarr/Radarr et pas
seulement à sa surveillance inotify — vérifié en appelant `Library/Media/Updated`
à la main : 60,04 s entre l'appel et le rafraîchissement. Les déclencheurs de
suppression des connexions Jellyfin ne raccourcissent donc pas cette attente ;
ils garantissent seulement que le bon dossier est signalé sans dépendre
d'inotify. Pour vraiment réduire le délai il faut baisser `LibraryMonitorDelay`
(`${DATA_ROOT}/.jellyfin/config/config/system.xml`) et l'intervalle du plugin
KodiSyncQueue — pas fait, la suppression étant fiable de toute façon.

Kodi ne réplique pas la bibliothèque du disque mais celle de Jellyfin : c'est
pour ça que le retrait passe par Jellyfin plutôt que d'être fait localement. Un
`VideoLibrary.RemoveMovie` depuis l'addon ferait disparaître la ligne
instantanément, mais désynchroniserait la table de correspondance de
jellyfin-kodi (qui pointerait sur une entrée Kodi disparue) — écarté pour ça.

Tant que l'événement n'est pas arrivé, la ligne reste affichée. Rien n'est perdu :
la suppression côté serveur, elle, est déjà faite — c'est ce que dit la
notification.

## Limites

- **Films, séries et saisons.** Pas d'épisode isolé : clearr supprime un titre
  ou une saison entière, jamais un fichier seul. Le `<visible>` de `addon.xml`
  masque l'entrée sur les autres types.
- **Deux comportements sur une série**, choisis dans la boîte de confirmation :
  « Supprimer » retire les saisons cochées et **laisse la série dans Sonarr**
  (en `monitorNewItems: "all"`), pour qu'une saison future soit quand même
  téléchargée ; « Purger » retire la série complètement, avec exclusion de liste.
  « Purger » n'apparaît que si toutes les saisons sont sélectionnées — une purge
  partielle laisserait les saisons gardées dans `library/` sans plus aucun arr
  pour les revendiquer.
- **Sans purge, rien n'empêche un retour** : une saison supprimée puis
  redemandée depuis Seerr sera re-téléchargée. C'est la différence voulue entre
  les deux boutons, pas un oubli.
- **La liste des saisons vient du serveur**, pas de la base Kodi : celle-ci
  reflète Jellyfin, qui peut connaître des saisons que Sonarr n'a pas. Une
  saison inconnue de Sonarr est refusée plutôt que devinée.
- **Un pack multi-saisons est conservé** si l'une de ses saisons est gardée :
  ses fichiers `library/` de la saison supprimée partent, mais **aucun espace
  n'est libéré** — les données restent seedées. La boîte de confirmation
  distingue alors la taille retirée de la bibliothèque et l'espace réellement
  libéré.
- **Un titre sans identifiant externe ni chemin** dans la base Kodi ne peut pas
  être résolu : l'addon le dit et n'envoie rien.
- **Un identifiant qui correspond à plusieurs titres** côté Sonarr/Radarr fait
  refuser la suppression (voir `_find_by_external_ids` dans
  `arr/clearr/app/core.py`) — à traiter dans l'interface web de clearr. Le repli
  par chemin ne rattrape pas ce cas : il s'interdit `library/` (voir plus haut).
- **Aucune authentification**, comme le reste de clearr : le service est
  LAN-only (middleware Traefik `arr-lan-only`) et son interface web expose déjà
  les mêmes suppressions sans jeton.
