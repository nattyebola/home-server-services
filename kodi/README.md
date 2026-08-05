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
   puis récupère ses identifiants externes (IMDb / TMDB / TVDB) par JSON-RPC
   (`VideoLibrary.GetMovieDetails`/`GetTVShowDetails`, propriété `uniqueid`).
   Ce sont les `ProviderIds` que jellyfin-kodi a recopiés depuis Jellyfin.
2. Confirmation obligatoire (boîte de dialogue « Supprimer / Annuler »).
3. `POST https://clearr.${DOMAIN}/api/delete/{film,series}` avec ces ids.
   clearr les résout en id Sonarr/Radarr puis supprime le titre.
4. Une notification Kodi affiche le message renvoyé par clearr (titre supprimé et
   espace libéré, ou la raison de l'échec), et l'addon s'arrête là.

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

- **Films et séries entières uniquement.** Pas d'épisode ni de saison : clearr
  n'expose la suppression que d'un titre complet (une série supprimée l'est avec
  toutes ses saisons, y compris celles encore en diffusion). Le `<visible>` de
  `addon.xml` masque l'entrée sur les autres types.
- **Un titre sans identifiant externe** dans la base Kodi ne peut pas être
  résolu : l'addon le dit et n'envoie rien.
- **Un identifiant qui correspond à plusieurs titres** côté Sonarr/Radarr fait
  refuser la suppression (voir `_find_by_external_ids` dans
  `arr/clearr/app/core.py`) — à traiter dans l'interface web de clearr.
- **Aucune authentification**, comme le reste de clearr : le service est
  LAN-only (middleware Traefik `arr-lan-only`) et son interface web expose déjà
  les mêmes suppressions sans jeton.
