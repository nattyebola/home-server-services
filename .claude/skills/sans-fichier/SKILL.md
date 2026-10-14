---
name: sans-fichier
description: Fait l'inventaire des séries, animés et films qui n'ont aucun fichier en bibliothèque, en écartant ce qui n'est pas encore sorti (ou seulement en salle) et ce qui est sorti depuis moins de 2 semaines, puis sépare ce que Sonarr/Radarr cherchent sans rien trouver de ce qu'ils ne chercheront jamais (rien de suivi). À utiliser quand l'utilisateur demande ce qui manque dans la bibliothèque, les titres sans fichier, pourquoi une série ou un film n'a jamais été téléchargé, ou lance /sans-fichier.
---

# sans-fichier — titres sortis et toujours absents

## Contexte

Le périmètre est le **titre entier** : une série ou un anime sans aucun
épisode en bibliothèque (`episodeFileCount == 0`), un film sans fichier
(`hasFile == false`). Un épisode isolé manquant dans une série entamée
n'en fait pas partie. Ces épisodes-là sont traités par `search-missing.py`
(lundi, 5 h).

Un titre sans fichier n'est pas forcément anormal. Le catalogue en compte
toujours une bonne moitié qui n'est pas encore sortie. L'intérêt du skill
est de ne garder que **le reste**, puis de dire pourquoi il manque.

## Critères (fixés par l'utilisateur le 2026-10-14)

| Écarté | Règle |
|---|---|
| Pas encore sorti (série) | aucun épisode voulu déjà diffusé, et une saison suivie attend sa diffusion |
| Pas encore sorti (film) | pas de sortie numérique ou physique passée. **Une sortie en salle seule ne compte pas** (il n'en existe que des CAM, refusées par les profils), même si Radarr dit `isAvailable` (`minimumAvailability: inCinemas`) |
| Trop récent | sorti depuis moins de **14 jours** (`GRACE_DAYS`, `--grace N`) : délai de grab de 24 h + attente d'une VF |
| Dans la file | listé à part, renvoyer au skill `manual-import` |

Dates de référence :

- **série suivie** : le plus ancien épisode voulu (suivi, diffusé) ;
- **série sans rien de suivi** : le premier épisode diffusé ;
- **film** : la première sortie numérique ou physique. Sans aucune date,
  on se fie au statut Radarr `released` (vieux documentaires).

Les spéciaux (saison 0) sont ignorés.

## Étape 1 — collecter

```bash
python3 .claude/skills/sans-fichier/sans_fichier.py          # rapport
python3 .claude/skills/sans-fichier/sans_fichier.py --json   # brut, toutes catégories
```

Lecture seule, ~10 s : aucune requête aux indexeurs, aucun suivi modifié.
La « dernière recherche auto » vient de l'état de `search-missing.py`
(`$DATA_ROOT/.search-missing-state.json`).

## Étape 2 — présenter

Deux tableaux, par type (série / anime / film) :

1. **Suivis, sortis, aucun fichier trouvé** : Sonarr/Radarr cherchent,
   rien n'est passé le profil. Indiquer la dernière recherche auto.
2. **Sortis, mais rien de suivi** : aucune saison ni aucun épisode suivi
   (ajout en « épisodes futurs uniquement », ou film non suivi).
   Sonarr/Radarr **ne les chercheront jamais**. Souvent voulu (on attend
   la saison suivante) : poser la question, ne pas présumer.

Les écartés tiennent en une ligne chacun (titre, date), sans détail.

## Étape 3 — proposer (rien sans accord)

- **Catégorie 1** : une recherche ciblée (`MoviesSearch` /
  `SeriesSearch`) consomme du quota d'indexeur, une requête par indexeur
  activé. Proposer titre par titre, en petit nombre (voir skill
  `indexer-quota`). Si un titre ressort vide recherche après recherche,
  c'est en général qu'aucune release ne passe le profil (pas de FR,
  par exemple). Le dire plutôt que relancer : `search-missing` le
  repasse déjà toutes les 2 semaines.
- **Catégorie 2** : suivre les saisons voulues (`PUT /series` avec
  `seasons[].monitored`, ou `PUT /movie` pour un film), puis laisser
  `search-missing` les prendre. Choix de l'utilisateur, saison par saison.
- Commit seulement si l'utilisateur le demande.

## Pièges

- **Une saison suivie ne veut pas dire des épisodes suivis.** Sonarr suit
  par épisode : une saison cochée dont les épisodes déjà diffusés sont
  décochés ne cherche rien (cas *Frieren* S2 le 2026-10-14). Le script
  regarde le suivi de chaque épisode, pas celui de la saison.
- **`isAvailable` de Radarr n'est pas « sorti »** : avec
  `minimumAvailability: inCinemas`, un film en salle est « disponible ».
  `search-missing.py` le cherche donc, en vain, jusqu'à sa sortie
  numérique. Le skill, lui, l'écarte.
- Un titre absent du catalogue n'apparaît évidemment pas ici. Pour un
  téléchargement fini mais non importé, voir `manual-import`.
