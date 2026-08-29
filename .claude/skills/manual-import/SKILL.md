---
name: manual-import
description: Débloque les téléchargements terminés que Sonarr/Radarr refusent d'importer tout seuls (état importBlocked/importPending dans la file), en distinguant ce qui s'importe sans risque, ce qui demande de choisir une cible, et ce qu'il ne faut pas importer du tout. À utiliser quand l'utilisateur signale un téléchargement fini qui n'apparaît pas dans la bibliothèque, un titre « manquant » alors qu'il a été grabé, une entrée bloquée dans la file d'attente, ou demande de traiter les imports manuels en attente.
---

# manual-import — débloquer les imports en attente

## Contexte

Un téléchargement à 100 % n'est pas un titre importé. Sonarr et Radarr
rangent dans leur file, en `importBlocked` ou `importPending`, tout ce
qu'ils ont refusé d'importer automatiquement — et **rien ne les en sort
tout seul** : ni le temps, ni un redémarrage, ni la recherche périodique.
L'item continue de compter comme manquant dans `wanted/missing`, et aucune
carte du dashboard ne le signale.

C'est le piège de diagnostic principal sur cette infra. Le 2026-08-29, sur
20 titres comptés manquants, **5 étaient en réalité téléchargés à 100 % et
coincés là** — dont trois épisodes de *Tomb Raider King* depuis quatre
jours et *One Piece* S23E17 depuis presque un mois. Avant de conclure
« aucune release trouvée », toujours croiser avec la file.

Deux vues portent des informations différentes, et il faut les deux :

- `GET /api/v3/queue` donne le **motif** du blocage (`statusMessages`) ;
- `GET /api/v3/manualimport?downloadId=…` donne les **candidats** fichier
  par fichier, avec la cible que l'arr a su résoudre et ses `rejections`.

Un candidat sans aucun rejet peut très bien être bloqué côté file : c'est
le cas le plus fréquent ici.

## Les quatre familles de blocage

`scripts/manual-import.py list` les sépare, parce qu'elles n'appellent pas
la même décision. **Ne jamais les traiter en bloc.**

1. **IMPORTABLE** — aucun rejet, cible résolue. Le blocage vient de la
   file : typiquement *« matched to series/movie by ID, automatic import is
   not possible »*, c'est-à-dire une release dont le titre ne se parse pas
   et que l'arr n'a rattachée qu'en relisant l'historique du grab. Import
   direct, sans risque. C'était le cas des trois *Tomb Raider King* et de
   *La vie est un miracle*.

2. **À RATTACHER** — le candidat revient avec `episodes: []` /
   `movie: null` : l'arr ne sait pas à quoi le fichier correspond. Deux
   sous-cas très différents, à distinguer AVANT d'agir :
   - la **numérotation absolue des anime**, que Sonarr ne remappe pas
     (*« Invalid season or episode »* sur `One Piece S01E1172`, qui est en
     fait S23E17). Le titre est au catalogue, seule la cible manque →
     `assign`.
   - le **titre absent du catalogue** (*« Unknown Movie »*, *« Movie title
     mismatch »*). Là il n'y a rien à rattacher tant que le film/la série
     n'a pas été ajouté à l'arr. Vérifier d'abord :
     `GET /api/v3/movie` ou `/series`, chercher par titre. Si le titre est
     bien absent, **le dire à l'utilisateur au lieu de l'ajouter d'office** :
     un téléchargement peut très bien avoir été fait à la main, hors arr, et
     ne rien avoir à faire dans la bibliothèque gérée.

3. **REFUSÉ** — cible résolue, mais de vrais rejets sur le candidat
   (*« Not a Custom Format upgrade for existing episode file(s) »*,
   *« was not found in the grabbed release »*). Ce sont des releases dont
   on ne veut pas : un pack dont les épisodes sont déjà là en mieux, un
   doublon. `apply` ne les touche jamais. La bonne suite est en général de
   les purger (voir plus bas) — mais c'est une suppression, donc à
   confirmer avec l'utilisateur.

4. **ERREUR** — l'appel `manualimport` lui-même a échoué. À rapporter tel
   quel, ne pas retenter en boucle.

## Comment l'utiliser

Depuis la racine du repo (`server/`) :

```bash
python3 scripts/manual-import.py list          # diagnostic (défaut)
python3 scripts/manual-import.py list --json   # même chose, sortie machine
python3 scripts/manual-import.py apply --dry-run
python3 scripts/manual-import.py apply         # importe la famille 1 seulement
```

Pour la famille 2, une fois la cible identifiée :

```bash
# Sonarr — rattacher un fichier à un ou plusieurs épisodes
python3 scripts/manual-import.py assign sonarr \
  --download-id <hash> --episode-ids 1596

# Radarr — rattacher à un film, en corrigeant une langue mal détectée
python3 scripts/manual-import.py assign radarr \
  --download-id <hash> --movie-id 41 --language French
```

`--path <nom de fichier>` si le téléchargement en contient plusieurs,
`--dry-run` sur les deux sous-commandes.

## Étapes

1. **Diagnostiquer** — `list`. S'il n'y a rien, le dire et s'arrêter : le
   problème de l'utilisateur est ailleurs (chercher plutôt du côté de
   `wanted/missing` et de `scripts/search-missing.py`).

2. **Traiter la famille 1** — `apply`. C'est la seule action sans décision
   à prendre. Vérifier au passage l'avertissement *« langue douteuse »* (voir
   plus bas) : s'il apparaît, préférer `assign … --language French` fichier
   par fichier plutôt qu'`apply` en masse.

3. **Traiter la famille 2, une par une.** Résoudre la cible d'abord, ne
   jamais deviner : un `episodeIds` faux **écrase le fichier d'un autre
   épisode**.
   - Anime en numérotation absolue : `GET /api/v3/episode?seriesId=<id>&
     seasonNumber=<n>` et matcher sur `absoluteEpisodeNumber`, pas sur le
     numéro affiché dans le nom de la release.
   - Titre absent du catalogue : remonter à l'utilisateur, cf. famille 2
     ci-dessus.

4. **Signaler la famille 3** sans rien supprimer : lister ce que c'est et
   pourquoi ça ne s'importera jamais, puis demander s'il faut purger.

5. **Vérifier** que l'import a bien eu lieu — l'API répond `queued`/`started`,
   ce n'est pas une confirmation. Relire `GET /api/v3/episode?seriesId=…`
   (`hasFile`) ou `GET /api/v3/movie/<id>` (`hasFile`), et confirmer que la
   file s'est vidée. Un `apply` qui « réussit » sans que `hasFile` passe à
   `true` veut dire que l'import a été refusé après coup — relancer `list`,
   le candidat aura cette fois des `rejections`.

## Purger une entrée qui ne s'importera jamais

Pour la famille 3, après accord de l'utilisateur :

```bash
docker exec arr-sonarr-1 curl -s -X DELETE -H "X-Api-Key: $SONARR_KEY" \
  "http://localhost:8989/api/v3/queue/<queueId>?removeFromClient=true&blocklist=true&skipRedownload=true"
```

`blocklist=true` n'est pas optionnel ici : sans elle, le flux RSS peut
reproposer exactement la même release et on rejoue le même import bloqué —
c'est le schéma de regrab en boucle documenté dans `CLAUDE.md`.
`skipRedownload=true` évite de déclencher une recherche de remplacement,
qui n'a pas lieu d'être quand le fichier est déjà présent en mieux.
Vérifier ensuite que les fichiers déjà en place n'ont pas bougé
(`hasFile`) : `removeFromClient` supprime les données du torrent, mais un
fichier importé en hardlink survit — le confirmer plutôt que le supposer.

## Langue mal détectée

`list` marque **⚠ langue douteuse** quand le nom du fichier porte un tag
`MULTI`/`VFF`/`VF2`/`TRUEFRENCH`/`VOSTFR`… alors que la langue détectée
n'est pas le français. Sur `La.Vie.est.un.Miracle.2002.MULTI…`, Radarr
proposait *Vietnamese*. Ce n'est pas cosmétique : la langue part dans le
nom du fichier renommé et dans le `.nfo` lu par Jellyfin. Corriger avec
`assign … --language French`.

## Limites connues

- `apply` traite tous les arr d'un coup. Pour n'en viser qu'un, passer
  `--download-id`.
- Le script ne sait pas **ajouter** un film ou une série au catalogue : la
  famille 2 « titre absent » se termine toujours par une question à
  l'utilisateur.
- `importMode: auto` laisse l'arr choisir entre hardlink et copie selon sa
  config `mediamanagement` — c'est voulu, mais ça veut dire qu'un import
  peut consommer de l'espace disque si le hardlink échoue (voir le piège
  des deux bind-mounts séparés dans `CLAUDE.md`).
- Rien ici ne détecte un téléchargement bloqué **avant** la fin (seeding
  arrêté, torrent mort) : ce script ne regarde que ce qui est téléchargé à
  100 % et refusé à l'import. Un torrent qui ne finit pas relève de
  `make clearr`.
