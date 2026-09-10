# Pièges Sonarr / Radarr / Prowlarr / cross-seed / Seerr

Chargé à la demande depuis `CLAUDE.md`. À lire avant de diagnostiquer
un grab, un import bloqué, un regrab en boucle, un custom format, un
score de profil, cross-seed ou Seerr.

## Sonarr / Radarr / Prowlarr

- **Une release peut être une archive ou un exécutable, et le titre ne le dit
  pas.** Deux cas rencontrés, un `.exe` puis `Ted Lasso S04E06 …Atmos.zipx` le
  2026-09-07 (1,1 Go, vrai ZIP — magic `PK\x03\x04`), tous deux grabés sur
  Nyaa.si. La garde d'import de Sonarr les détecte (« Caution: Found potentially
  dangerous file with extension ») mais **s'arrête là** : l'entrée reste en
  `importPending` sans limite, rien ne la purge et l'épisode reste manquant.
  **Un custom format ne peut PAS traiter ce cas** : l'extension n'est pas dans
  le titre de la release (`Ted Lasso S04E06 1080p ATVP WEB-DL DDP5 1 Atmos` en
  base, vérifié dans l'historique de grab et la blocklist), seulement dans le
  nom du fichier du torrent — un `ReleaseTitleSpecification` n'a donc rien à
  matcher au grab. Ne pas repartir sur cette piste.
  La blocklist ne suffit pas non plus : la même release, blocklistée le 09-06,
  a été regrabée le 09-07 sous un autre infoHash (deux uploads du même titre).
  Fix, deux couches, toutes deux portées par `scripts/apply-arr-overrides.py` :
  - **`failDownloads: [0, 1]`** (Executables + Potentially Dangerous) sur tous
    les indexeurs des deux arr : l'arr marque le download **échoué** au lieu de
    l'attendre, et `autoRedownloadFailed` (déjà à `true`) enchaîne blocklist par
    hash → retrait du client → nouvelle recherche excluant la release. Validé en
    conditions réelles le 2026-09-07 : `downloadFailed / "Failed download
    detected"` 3 min après activation, torrent retiré, 1,1 Go libéré, sans
    intervention.
  - **`cat-id: 1` (Anime) sur l'indexeur Nyaa.si de Prowlarr** : à `0`
    (« All categories », le défaut) Nyaa renvoie aussi ses catégories Live
    Action, Audio et Software. A/B sur la même requête (« Nogizaka46 ») : 75
    résultats dont 63 Live Action à `0`, contre 1 résultat et 0 Live Action à
    `1` ; les recherches d'anime sont inchangées (« One Piece » : 75 dans les
    deux cas). Coût nul mesuré : sur 578 grabs Sonarr, Nyaa.si n'avait servi que
    **2 fois** pour une série non-anime — les deux grabs de l'archive Ted Lasso
    — les 197 grabs de séries standard venant de TR4KER/YggReborn/C411/V3X, et
    0 grab Nyaa.si sur les 34 derniers de Radarr.
  **Piège de placement, mesuré le 2026-09-07** : le champ `categories` d'un
  indexeur synchronisé est **réécrit par l'`ApplicationIndexerSync`** de
  Prowlarr (vidé à `[]` côté Sonarr, remis à `[5000]` après un sync) — restreindre
  Nyaa.si par là ne pouvait pas tenir, d'où le passage par `cat-id` chez
  Prowlarr, qui fait autorité. Même piège que `seedCriteria.seedRatio`.
  **`failDownloads`, lui, SURVIT au sync** : Prowlarr ne l'expose pas sur son
  propre objet indexeur, il ne peut donc pas l'écraser — c'est pourquoi ce
  réglage vit côté arr et n'a pas de pendant Prowlarr.

- **Un compte d'épisodes dans le titre (`[09 of 12]`) est lu par Sonarr comme
  un numéro d'épisode absolu.** Diagnostiqué le 2026-09-04 sur des packs
  d'uploaders russes de Nyaa.si (`… [2026] [09 of 12] [WEBRip] [1080p] [RUS +
  JAP]`, où `09 of 12` veut dire « 9 épisodes sur 12 sortis ») :
  `/api/v3/parse` renvoie `absoluteEpisodeNumbers: [12]`, donc Sonarr grabe le
  pack en croyant obtenir l'épisode 12 — justement manquant — puis refuse
  d'importer son contenu réel (E01→E09, déjà présents en mieux). 14,25 Go
  immobilisés en `importPending`, sur 2 séries.
  **Ce n'est PAS la boucle de regrab par `cutoffFormatScore`** (documentée plus
  bas) : aucun upgrade n'est en jeu, les fichiers en place satisfont le cutoff.
  Toucher à un score de cutoff n'y changerait rien.
  Fix : custom format **`Pack NN of NN`** (`ReleaseTitleSpecification`,
  `\[\s*\d{1,3}\s+(of|из)\s+\d{1,3}\s*\]`) scoré **-10000** sur les deux
  profils anime — leur `minFormatScore: 0` transforme ça en rejet au grab, même
  mécanique que `LQ`/`BR-DISK`/`Upscaled`.
  **Cible le schéma de titre, pas la langue**, et c'est délibéré : le défaut est
  que le parseur ne sait pas lire ce format, donc une release ainsi nommée n'est
  de toute façon jamais grabable correctement, quelle que soit son origine. Un
  ciblage par langue a été **écarté après mesure** — le CF du guide
  `Language: Not Original` matche 6 fichiers VOSTFR parfaitement légitimes
  (tout *Tomb Raider King*, en japonais, parce que sa langue d'origine est le
  coréen), donc le scorer négativement bloquerait tous les anime non japonais
  d'origine. Ne pas reproposer cette voie.
  Validé selon la méthode de la section regex plus bas : sur un corpus de 1099
  titres réels (historique + blocklist + `sceneName`/`relativePath` des
  episodefiles), **3 matchs, tous voulus, 0 faux positif**, et **0 désaccord**
  entre .NET (`/api/v3/parse` + CF jetable) et Python `re` sur les 411 titres à
  risque (ceux contenant `of` ou `[…chiffre…]`).
  Portée volontairement limitée aux profils anime : ce schéma de nommage vient
  de Nyaa.si, qui n'est interrogé que pour les anime. À étendre à
  `WEB-2160p (Combined)` (donc via `recyclarr.yml`, pas ce JSON) s'il y
  apparaissait.
  La blocklist ne suffisait pas comme parade : la même release y était **déjà**
  depuis le 2026-08-29 et a quand même été regrabée le 03-09 — Sonarr y matche
  le titre de release + l'indexeur, un renommage ou un autre indexeur passe à
  travers.

- **Ne PAS bloquer le groupe ToonsHub** (demandé le 2026-09-10, écarté après
  mesure) : le postulat « ils ne font pas de VOSTFR » est faux. Sur 39 grabs,
  **26 sont tagués `Multi-Subs`/`MULTi`**, et **8 des 10 fichiers ToonsHub en
  place portent réellement une piste `fre`** (lue dans `mediaInfo.subtitles`,
  pas déduite du titre). Ils sont la source principale de 8 séries suivies —
  un `ReleaseGroupSpecification` aurait coupé l'approvisionnement de la moitié
  du catalogue anime pour traiter une minorité de releases.
  Ce qui pollue, c'est leur variante **sans français**, et elle s'annonce dans
  le titre : `(… Japanese Sub …)`, `(English-Sub)`, et le token `ESub` du nom
  de fichier (`MSubs` = multi, à ne pas confondre — `\b[EJ]-?Subs?\b` ne le
  matche pas, il n'y a pas de frontière de mot dans `MSubs`).
  D'où le custom format **`Subs non-FR (JSub/ESub)`** dans
  `arr/profiles/sonarr-anime.json`, scoré **-10000** sur les deux profils anime
  (`minFormatScore: 0` ⇒ rejet au grab, même mécanique que `Pack NN of NN`).
  **Deux specs `required: true`, pas une** : le marqueur de sous-titres, ET une
  garde `negate: true` sur les marqueurs FR — sans elle un hypothétique
  `(Multi-Subs, English-Sub)` serait rejeté à tort.
  **Ciblé sur le schéma de nommage, pas sur le groupe**, délibérément : la même
  annonce « sous-titres dans une seule langue non française » mérite le même
  traitement de n'importe quel groupe.
  Validé selon la méthode de la section regex plus bas : corpus de 833 titres
  réels (historique + blocklist + `sceneName`/`relativePath`), **10 matchs, tous
  voulus, 0 faux positif**, et **0 désaccord** .NET (`/api/v3/parse` + CF
  jetable supprimé après) / Python `re` sur les 273 titres contenant « sub ».
  **Contrairement au suffixe entre parenthèses du piège de la boucle de regrab,
  ce CF est volontairement symétrique** : il matche aussi bien le titre de
  release (`(English-Sub)`) que le nom de fichier (`ESub`), donc un fichier déjà
  importé score -10000 comme la release — pas d'asymétrie grab/fichier, et les
  fichiers sans FR déjà en place deviennent éligibles à un vrai remplacement.
  **Trou connu, non traité** : `Multi-Subs` est une promesse du titre, pas une
  garantie. `One.Punch.Man.S03E07 …BILI…MSubs-ToonsHub` n'a que `eng/ind/tha`
  en sous-titres. Rien dans le titre ne permet de le voir — seul un contrôle
  post-import du `mediaInfo` le détecterait, non fait.
  Ne pas confondre avec les releases **`RUS + JAP`** (titres cyrilliques
  d'uploaders russes de Nyaa.si) : elles ne viennent pas de ToonsHub et sont
  déjà couvertes par `Pack NN of NN`.

- **Ni Sonarr ni Radarr ne re-cherche un manquant tout seul** : aucune tâche
  planifiée de recherche des manquants depuis Sonarr v3 (leur
  `/api/v3/system/task` ne liste que RSS Sync / Refresh / Import List Sync).
  Ce que le flux RSS a raté à la sortie reste manquant indéfiniment, **sans
  erreur ni signal** — d'où `scripts/search-missing.py`. Ne pas chercher un
  réglage d'UI pour ça, il n'y en a pas.
- **Un titre « manquant » est le plus souvent déjà téléchargé** : avant de
  conclure « aucune release trouvée », croiser `wanted/missing` avec
  `/api/v3/queue`. Le 2026-08-29, 5 des 7 épisodes/films manquants étaient en
  fait `importBlocked`/`importPending` — deux causes récurrentes, toutes deux
  muettes côté UI hors de la file :
  *« matched to series/movie by ID, automatic import is not possible »*
  (release dont le titre ne se parse pas ; le grab a été rattaché via
  l'historique, pas via le nom) et *« Invalid season or episode »* (numérotation
  absolue d'anime que Sonarr ne remappe pas — `One Piece S01E1172` pour
  S23E17). Les deux se débloquent par `GET /api/v3/manualimport?downloadId=…`
  puis `POST /api/v3/command {"name":"ManualImport", …}`, en réinjectant
  `episodeIds`/`movieId` à la main pour le second cas (le candidat revient avec
  `episodes: []`). Vérifier la langue détectée au passage : sur une release
  MULTi/VFF, Radarr a proposé « Vietnamese » — elle part dans le nom du fichier
  renommé et dans le `.nfo` lu par Jellyfin.
  Outillé par `scripts/manual-import.py` (`list`/`apply`/`assign`) et le skill
  `.claude/skills/manual-import/SKILL.md`. Le script **sépare quatre familles
  et n'en importe qu'une** — celle sans rejet ni ambiguïté : une cible devinée
  écraserait le fichier d'un autre épisode, et un pack « Not a Custom Format
  upgrade » ne doit jamais être importé, seulement purgé. Un 4e cas ne se
  rattache pas du tout : titre absent du catalogue (« Unknown Movie ») — c'est
  une question pour l'utilisateur, pas un ajout d'office, le téléchargement
  pouvant être volontairement hors arr.
- **Sonarr/Radarr n'importent pas les fichiers vidéo posés en vrac à la racine
  d'un dossier scanné** — ils ne reconnaissent que la convention
  un-film/une-série par sous-dossier, sans erreur ni log pour les fichiers
  ignorés. Utiliser **Manual Import**, pas le scan automatique.
- **Supprimer un episodefile via l'API déclenche quasi instantanément la
  recherche automatique interne de Sonarr** pour l'épisode redevenu
  « manquant » : dans la fenêtre de quelques secondes entre la suppression et
  un grab manuel visant une release précise, Sonarr a grabé de lui-même autre
  chose (mieux scorée sur le profil de la série). Supprimer un fichier pour
  forcer un remplacement laisse donc une fenêtre de course. Pas de parade
  fiable identifiée.
- **Le cache de `GET /api/v3/release?episodeId=...` expire vite** : un `guid`
  récupéré par un GET précédent peut déjà être invalide au `POST` (grab), avec
  `"Couldn't find requested release in cache, try searching again"` — a
  fortiori si une recherche automatique s'est intercalée. Le grab manuel doit
  suivre la recherche dans la foulée, sans appel intermédiaire.
- **Le paramètre `seriesId` de `GET /api/v3/history` n'est pas fiable** : la
  réponse contient des entrées d'autres séries malgré le filtre. Toujours
  filtrer côté client sur `record["seriesId"]`. Voir aussi `eventType=1`
  (entier) plutôt que la chaîne `"grabbed"`, qui renvoie une réponse vide.
- **`GET /api/v3/wanted/cutoff` ne reflète que le cutoff *qualité*, pas
  `cutoffFormatScore`** — un épisode à la bonne qualité mais sous le score n'y
  apparaît pas, alors qu'il reste éligible à l'upgrade. Ne pas s'en servir
  pour estimer l'ampleur d'une vague de re-recherche : recalculer les scores
  fichier par fichier (`GET /api/v3/episodefile`, champ `customFormats`,
  croisé avec les `formatItems` du profil cible), ou vérifier au cas par cas
  avec `GET /api/v3/release`, dont les `rejections` mentionnent explicitement
  `Existing file meets cutoff`.
- **Avant tout changement d'un custom format par l'API, vérifier qu'il n'est
  pas géré par recyclarr** : recyclarr resynchronise la **définition** du CF,
  pas seulement son score, donc un `PUT` est appliqué puis réécrit au sync
  suivant. Repéré uniquement parce que `recyclarr sync --preview` a été
  relancé *après* le PUT. Réflexe : `grep` le `trash_id` dans
  `recyclarr.yml`, et relancer `--preview` après coup.
  Solution retenue dans ce cas plutôt que de sortir le CF de `recyclarr.yml`
  (ce qui aurait fait perdre sa création automatique sur un déploiement neuf,
  donc un recul de reproductibilité) : **un CF distinct que nous possédons**
  (`VOSTFR (hors suffixe)`), le CF du guide restant intact et scoré **0** sur
  les profils concernés, le nôtre reprenant son score.

## La boucle de regrab infini (`cutoffFormatScore` + regex)

Le piège le plus coûteux du repo, diagnostiqué en trois passes. À lire en
entier avant de toucher à un score ou à une regex de custom format.

- **Mécanisme** : les CF `VOSTFR`/`SUBFRENCH`/`FRENCH`
  (`ReleaseTitleSpecification`) matchaient le suffixe entre parenthèses que
  certains groupes ajoutent au **titre du post** (`(VF, FRENCH, SUBFRENCH,
  VOSTFR, ...)`) — suffixe **absent du nom réel du fichier `.mkv`**. Au grab
  la release score haut, après import le fichier réévalué score plus bas → la
  release déjà possédée a l'air d'être un upgrade → regrab → réimport → même
  écart → boucle. Confirmé via l'historique : littéralement le même
  magnet/infoHash grabé 3 à 5 fois.
  **Le moteur de la boucle est l'asymétrie grab/fichier**, pas le plafond.
  Abaisser `cutoffFormatScore` seul ne suffit pas : tant que le cutoff reste
  au-dessus du maximum qu'un *fichier* peut atteindre, la porte de l'upgrade
  ne se referme jamais.
- **Un `cutoffFormatScore` n'a de sens que s'il est (a) atteignable et (b)
  posé là où l'objectif du profil est rempli.** Le défaut des guides TRaSH
  (10000) ne l'est jamais, et un calcul naïf par somme des CF positifs ne
  l'est pas non plus : beaucoup sont **mutuellement exclusifs** (un seul tier,
  une seule plateforme de streaming, un seul codec, un seul format audio, une
  seule résolution, un seul niveau de repack — les specs `negate` du guide les
  enchaînent). Pour un profil à CF déterminant, caler sur ce CF ; pour un
  profil généraliste, sur le haut de la distribution **réellement observée**.
  Ces distributions sont **bimodales** selon qu'une release porte ou non un
  tag de groupe « Tier » (+1600 à 1700), largement absent des trackers FR :
  viser le maximum théorique maintiendrait des recherches perpétuelles (donc
  du quota indexeur) pour la majorité des titres.
  Pour un profil géré par recyclarr, passer par `upgrade.until_score` dans
  `recyclarr.yml`, pas par l'API — un appel API serait écrasé au sync.
  **Piège** : dès qu'un profil fournit une liste `qualities:` explicite,
  recyclarr **exige** `until_quality` en plus de `until_score`, sinon le sync
  échoue en validation.
- **Exclure un terme situé dans un groupe parenthésé demande DEUX assertions**,
  et c'est le cœur du correctif dans `arr/profiles/sonarr-anime.json` :
  - lookahead `(?![^()]*\))` — attrape le terme placé **après** un groupe
    imbriqué ;
  - lookbehind `(?<!\([^)]*)` — attrape le terme placé **avant**, le cas
    réellement rencontré.
  Le lookahead seul est contourné par des parenthèses imbriquées : dans
  `(VF, FRENCH, VOSTFR, Koukaku Kidoutai (2026), ...)`, le `(` de `(2026)`
  arrive avant la première `)`, `[^()]*\)` échoue, l'assertion négative
  réussit — et le terme matche alors qu'il est bien dans le suffixe.
  **.NET accepte un lookbehind de longueur variable, Python `re` non** —
  utiliser le module `regex` pour tout test hors Sonarr.
  Méthode de validation à reprendre : calculer la vérité terrain par
  **comptage réel de profondeur de parenthèses**, pas par une autre regex, sur
  les titres réels de `/api/v3/history` + `/api/v3/episodefile` ; puis croiser
  .NET (`/api/v3/parse`, via des CF jetables supprimés après) et Python
  `regex` en exigeant 0 désaccord.
  Deux limites connues laissées en l'état : un terme entouré de groupes fermés
  des **deux** côtés dans les mêmes parenthèses passe encore (aucun schéma de
  nommage réel ne fait ça), et un titre à parenthèses non appariées ne matche
  pas (déjà vrai avant).
- Trou connu, délibéré : `WEB-2160p (Combined)` a `VOSTFR` à +100 et est géré
  par recyclarr, mais le suffixe est une pratique de groupes d'anime 1080p qui
  ne croise pas ce profil 2160p — à revoir si un regrab en boucle y apparaît.

## cross-seed

- **Connect « Custom Script », pas « Webhook »** — le type Webhook générique
  de Sonarr/Radarr envoie un payload de test factice (pas de vrai hash) au
  moment d'enregistrer la connexion ; cross-seed le rejette (`A valid
  infoHash or an accessible path must be provided`), ce qui **empêche
  l'enregistrement** de la connexion (échec bloquant, pas un warning). La
  méthode documentée est un Custom Script
  (`arr/scripts/cross-seed-notify.sh`) qui lit
  `$sonarr_download_id`/`$radarr_download_id` et appelle l'API lui-même.
- **`useClientTorrents: true` requis dans `arr/cross-seed/config.js`** (faux
  par défaut) — sans ça le webhook ne consulte jamais le client réel pour
  matcher l'infoHash reçu et échoue systématiquement (`Torrent client does
  not have any torrent with criteria`), même quand le torrent y est bien
  présent. Le job périodique « inject » ne rattrape pas ces échecs non plus.
- **Les URLs Torznab Prowlarr sont par ID d'indexeur
  (`http://prowlarr:9696/<id>/api`), et ces IDs ne sont pas stables.** La
  config a pointé 4 jours sur un indexeur supprimé : chaque webhook
  déclenchait bien une recherche, mais contre un indexeur mort (`410 Gone`),
  donc 0 injection — jamais d'erreur bloquante, juste un `[webhook] Found 0
  torrents` systématique. Si un indexeur est ajouté/supprimé/recréé dans
  Prowlarr, vérifier les IDs actuels et les refléter dans
  `CROSS_SEED_INDEXER_IDS` (`arr/.env`, pas en dur dans `config.js` : quels
  indexeurs existent et dans quel ordre est propre à ce déploiement). **Rien
  ne prévient d'un ID devenu obsolète autrement qu'en lisant les logs
  cross-seed.**
  Nyaa.si est exclu de cette liste : cross-seeder un torrent déjà obtenu d'un
  tracker à ratio vers un tracker public n'apporte aucun bénéfice (seul
  l'inverse en a un), et le retrait ne coupe que cette direction — un fichier
  d'origine Nyaa.si continue d'être cross-seedé vers les indexeurs à ratio.
- **`searchCadence` impose deux contraintes de validation** non documentées
  ailleurs que dans l'erreur elle-même, et une config invalide fait boucler le
  conteneur en crash (`restart: unless-stopped`) : `excludeRecentSearch` doit
  valoir au moins 3× `searchCadence`, et `excludeOlder` de 2 à 5×
  `excludeRecentSearch`. Valeurs retenues : 3 j / 9 j / 30 j — la recherche
  périodique ne couvre donc **que les torrents vus il y a moins de 30 jours**.
  Le rattrapage complet se fait à la demande via `docker exec <container>
  cross-seed search --exclude-older 999999999 --exclude-recent-search 0`,
  **jamais automatisé** (potentiellement des centaines de requêtes d'un coup).

## Seerr

- **`seerr` ne chown pas lui-même son volume `/app/config`** — contrairement
  aux images linuxserver.io, il tourne nativement en UID 1000 sans étape
  root-puis-drop, donc si `${DATA_ROOT}/.seerr/config` n'existe pas encore,
  Docker le crée en `root:root` et le container crash en boucle (`EACCES`).
  Avant le premier `make up STACK=seerr` :
  `sudo chown -R 1000:1000 ${DATA_ROOT}/.seerr`.
- **Seerr ne détecte les films/séries déjà téléchargés qu'en scannant les
  bibliothèques Jellyfin**, pas en interrogeant Sonarr/Radarr pour l'existant.
  Sans bibliothèque Jellyfin pointant sur `${DATA_ROOT}/library`, tout
  apparaît comme non disponible et Seerr propose de re-demander du contenu
  déjà présent. Après ajout, lancer manuellement « Jellyfin Full Library
  Scan » au lieu d'attendre le cron.

