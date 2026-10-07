---
name: multi-groupes
description: Propose les groupes de release à ajouter à (ou retirer de) la liste blanche du custom format « Langue : MULTi (groupe vérifié) », qui fait compter un MULTi anime comme de la VF, à partir des pistes audio réellement lues dans la bibliothèque. À utiliser quand l'utilisateur demande quels groupes ajouter à la regex MULTi, pourquoi une MULTi anime est rejetée, si un groupe donné sort bien de la VF, signale un anime MULTi grabé sans audio français, ou lance /multi-groupes.
---

# multi-groupes — tenir la liste blanche des MULTi anime

## Contexte

Sur les profils `Anime` et `Anime VF`, un titre `MULTi` ne vaut VF (3000)
que si son **groupe de release** est dans une liste blanche : custom format
`Langue : MULTi (groupe vérifié)` de `arr/profiles/custom-formats.json`,
spec `ReleaseGroupSpecification`, regex `^(A|B|…)$`. Hors liste, un MULTi
sans autre marqueur FR est **rejeté** (sous le minimum VOSTFR).

Pourquoi une liste blanche (décision du 2026-10-07, détail dans
`.claude/docs/arr-config.md`, entrée « Profils de qualité ») : sur Nyaa, un
`MULTi` veut souvent dire « pistes d'une plateforme » sans français. Les
12 % de MULTi sans FR mesurés venaient **tous de VARYG**, et tous les autres
groupes mesurés avaient l'audio FR. Un groupe inconnu reste donc rejeté
tant qu'on n'a pas vu ses fichiers : ajouter un groupe, c'est accepter ses
releases **sans plus jamais les vérifier** (aucun upgrade ne rattrape un
mauvais grab).

## Règles

- **Seule preuve valable : l'audio des fichiers en bibliothèque**
  (`mediaInfo.audioLanguages`, lu par Sonarr/Radarr à l'import). Seuil
  d'ajout : **≥ 5 fichiers MULTi avec audio FR, sur ≥ 2 œuvres, 0 sans FR**
  (`MIN_FILES`, `MIN_TITLES` dans le script). Plusieurs œuvres parce qu'un
  groupe peut sortir une série avec VF et une autre sans. Les fichiers de
  séries live et de films comptent : on juge la pratique du groupe.
- **La description d'une page Nyaa n'est qu'un indice** (déclarative,
  souvent absente) : elle sert à choisir un candidat à tester, jamais à
  l'ajouter.
- **Un seul MULTi sans FR suffit pour exclure** un groupe, même en liste.
- **Le script ne prouve que ce qui est encore sur le disque.** Un fichier
  sans FR finit remplacé et sa trace disparaît : VARYG ressortait « 4 FR /
  0 sans FR » le soir même de sa mesure. D'où `EXCLUDED_GROUPS` dans le
  script, qui **fait mémoire** : y inscrire le groupe et sa mesure
  **avant** de remplacer le fichier fautif.
- **Rien n'est modifié sans l'accord de l'utilisateur.** Le skill propose,
  l'utilisateur tranche groupe par groupe.

## Étape 1 — collecter

```bash
python3 .claude/skills/multi-groupes/multi_groupes.py --pages
```

Lecture seule, ~10-30 s. Parcourt tous les fichiers Sonarr/Radarr dont le
nom (`sceneName`, sinon nom de fichier) porte `MULTi`, et fait **une**
recherche `MULTi` sur Nyaa via Prowlarr (tracker public, pas de quota en
jeu) pour repérer les groupes actifs. `--pages` lit la description de deux
pages Nyaa par groupe hors liste, en séquentiel (Nyaa répond 429 en
parallèle). `--json` pour la sortie brute.

Le groupe affiché est celui **parsé par Sonarr**, c'est-à-dire exactement ce
que voit la spec (ex. `TSUNDERERAWS` côté AstraTorrent, d'où le `-?` de
`Tsundere-?Raws`).

## Étape 2 — proposer

| Verdict | Signification | Proposition |
|---|---|---|
| `À AJOUTER` | seuil atteint, 0 sans FR | ajouter à la regex |
| `À RETIRER` / `À RETIRER (exclu)` | en liste, mais un MULTi sans FR constaté | retirer de la regex **et** l'inscrire dans `EXCLUDED_GROUPS` |
| `garder (preuve faible)` | en liste sous le seuil | rien ; à surveiller |
| `preuve insuffisante` | FR partout, mais trop peu de fichiers | rien ; attendre |
| `candidat (aucun fichier)` | actif sur Nyaa, jamais grabé | rien, sauf grab de test (ci-dessous) |
| `exclu` | dans `EXCLUDED_GROUPS` | rien |

Présenter à l'utilisateur **uniquement** les `À AJOUTER` / `À RETIRER`, avec
les chiffres (fichiers FR / sans FR, œuvres), puis les candidats à fort
volume Nyaa s'il y en a. Demander groupe par groupe.

**Grab de test d'un candidat** (seul moyen d'obtenir une preuve pour un
groupe que les profils rejettent) : décision de l'utilisateur, jamais
automatique. Dans Sonarr, recherche interactive d'un épisode **manquant**,
grab manuel de la release du groupe malgré le rejet. Un épisode déjà en
bibliothèque ne convient pas : sans upgrade, l'import serait bloqué. Après
import, relancer l'étape 1.

## Étape 3 — appliquer (après accord)

1. Modifier la regex `ReleaseGroupSpecification` du CF dans
   `arr/profiles/custom-formats.json` (insensible à la casse ; reprendre le
   nom tel que parsé, avec `-?` si des variantes avec et sans tiret
   existent). Pour une exclusion, ajouter aussi l'entrée dans
   `EXCLUDED_GROUPS`, avec date, effectif et exemple.
2. `make arr-overrides` (≥ 2 min, normal : `settle()` des tailles).
3. Vérifier que Sonarr/Radarr calculent le CF comme prévu :
   ```bash
   python3 .claude/skills/multi-groupes/multi_groupes.py --verifier
   ```
   Exigé : **0 désaccord** (.NET contre Python, sur des titres réels de
   chaque groupe). Un désaccord veut presque toujours dire un groupe parsé
   autrement que dans la regex.
4. Mettre à jour la liste des groupes dans `docs/telechargement.md`
   (encadré sous « Règles de sélection ») et la date/les effectifs dans
   `.claude/docs/arr-config.md` (entrée « Profils de qualité »).
5. Commit seulement si l'utilisateur le demande.

## Pièges

- **Un « sans FR » peut être une piste FR sans tag de langue** :
  `audioLanguages` ne voit que les pistes taguées (*The Simpsons*
  S37E11/E13 ont une piste « French » non taguée). Avant d'exclure un
  groupe, confirmer par `ffprobe` (titre de piste, langue `und`).
- **`Multi-Subs` / `MSubs` n'est pas un MULTi** (la regex l'écarte) : un
  fichier de ce type sans audio FR ne dit rien sur la liste blanche
  (cas ToonsHub *One-Punch Man* S03E07, audio JPN, hors périmètre).
- **Ne pas déduire une exclusion de l'absence de preuve** : un groupe
  inconnu n'est pas mauvais, il est non vérifié — il reste rejeté, c'est
  tout.
- **Les MULTi d'un tracker français ne sont pas un cas à part** : la
  spec ne voit pas l'indexeur (aucune spec de custom format ne le permet),
  seul le groupe compte.
- Un titre qui annonce aussi la VF explicitement (`MULTi.VFF`) est déjà
  compté par `Langue : VF (hors MULTi)` : le CF est nié dans ce cas pour ne
  pas compter double. Ces titres ne dépendent donc pas de la liste blanche.
