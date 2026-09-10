# clearr + addon Kodi — détail

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à
`arr/clearr/` ou `kodi/context.clearr`.

## clearr (`arr/clearr/`) — nettoyage torrents/bibliothèque

- **Remplace l'ancien `scripts/torrent-cleanup.py` / `make cleanup`**
  (2026-07-31) : web LAN-only (`clearr.${DOMAIN}`, middleware
  `arr-lan-only`) **et** TUI d'origine (`make clearr`), toutes deux
  appuyées sur le même `arr/clearr/app/core.py`. `webapp.py` (FastAPI +
  Jinja2 + Bootstrap) et `tui.py`/`cli.py` (curses / `delete-by-inode`)
  importent `core.py`, **jamais l'inverse**.
  Tourne en conteneur et rejoint `vpn-internal` (externe,
  `vpn_vpn-internal`) + le réseau `default` d'arr : HTTP direct vers
  `transmission-vpn:9091`, `sonarr:8989`, `radarr:7878`, `prowlarr:9696`.
  L'ancienne TUI passait par `docker exec <container> curl` depuis l'hôte —
  plus besoin, et surtout pas de socket Docker (voir Socle).
  Monte `${DATA_ROOT}:/data_root` en lecture-écriture, exactement comme
  sonarr/radarr (même mount unique, même raison hardlink) : core.py et arr
  partagent donc le même référentiel de chemins, **il n'y a plus de
  traduction de chemin à faire** (les `host_to_arr_path()`/
  `arr_path_to_host()` de l'ancien script ont disparu avec).
  1er service du repo avec des dépendances Python tierces
  (`arr/clearr/requirements.txt`) et 2e avec un build custom après
  `nextcloud/app`/`web` (`build: ./clearr`, `python:3-slim`, tag flottant) —
  `make update STACK=arr` doit donc aussi rebuilder.
  Chaque requête recalcule tout (`core.load_full_state()`), **jamais de
  cache ni d'état en mémoire entre deux requêtes** — contrairement à la TUI
  qui ne recharge qu'au démarrage. Mesuré acceptable à l'échelle de cette
  bibliothèque, à revoir si ça dérive.
- **Pas de HTMX ni de Popper**, malgré la validation initiale de
  « FastAPI + HTMX » : `static/clearr.js` est un petit JS maison
  (`data-get`/`data-post`/`data-live`). Bootstrap est vendoré en
  `bootstrap.min.css`/`.min.js` **sans le bundle** — donc pas de Popper,
  donc **aucun tooltip / dropdown / popover Bootstrap n'est disponible**
  (les infobulles passent par un `title=` natif).
  Pour vendorer un asset : `curl`, **jamais `WebFetch`** (qui peut reformater
  du contenu non-HTML — `curl` donne les octets exacts), et vérifier
  l'intégrité par un second téléchargement + comparaison sha256 avant de
  committer.
- **Zéro appel WAN côté serveur**, contrainte demandée explicitement. Les
  jaquettes viennent du cache disque des arr
  (`${DATA_ROOT}/.arr/{sonarr,radarr}/config/MediaCover/<id>/poster-250.jpg`)
  servi par `/poster/{kind}/{arr_id}` — `arr_id` passé par `int()`, c'est
  **la** garantie anti-traversal. Les ids externes
  (`imdbId`/`tvdbId`/`tmdbId`) sont déjà dans les objets arr. Seuls les
  liens sortent, suivis par le navigateur. TVDB adressé par
  `thetvdb.com/dereferrer/series/<tvdbId>` (Sonarr n'expose pas le slug) ;
  Radarr par `<tmdbId>` (son `titleSlug` EST le tmdbId).
  Seule exception au « zéro WAN » : `core.quality_profile_names()`, sur le
  réseau interne.
  `DOMAIN` (liens `sonarr.${DOMAIN}`/`radarr.${DOMAIN}`) vient de
  `.env.shared`, hors du `env_file: .env` du service : injecté par un bloc
  `environment:` dans `arr/docker-compose.yml`. Absent = pas de lien arr,
  le reste fonctionne.
- **Rattachement torrent → titre arr** (`core.build_arr_meta_index()`/
  `torrent_meta()`) : films par chemin exact de `movieFile.path`, séries
  par préfixe de `series.path` — mêmes critères que
  `plan_radarr_deletion`/`plan_sonarr_unmonitor`, en repartant des inodes
  déjà calculés par `analyze_torrent_files()` (donc zéro `stat`
  supplémentaire, symlinks cross-seed déjà résolus). Best-effort : un
  torrent jamais importé n'a ni jaquette ni lien, un arr injoignable dégrade
  la vue sans la casser.
- **Un seul gabarit par comportement partagé entre les vues** :
  `templates/_meta.html` (cellule titre : jaquette + liens),
  `templates/details.html` (fiche « toutes les informations connues », en
  modale, purement descriptive), `render_arr_tab(tab, ...)` + `ARR_TABS`
  pour Séries/Animés/Films. Sans ça le même bloc serait écrit deux ou trois fois.
  `core.find_series_by_id`/`find_movie_by_id` plutôt que des `next((...))`
  recopiés dans les routes.
- **4 onglets web : Torrents / Séries / Animés / Films** (2026-09-09).
  Séries et Animés sont **deux vues filtrées d'une seule liste Sonarr** :
  même `fetch`, même gabarit `series_tab.html`, mêmes routes de suppression
  `/series/{id}/...` — seuls le prédicat `select` de la spec `ARR_TABS` et les
  libellés (`count_label`/`empty_label`, portés par la spec pour qu'elle reste
  la seule source) changent. Pas d'`animes_tab.html` : il aurait été la copie
  mot pour mot du premier.
  **Le critère est `seriesType == "anime"` (`core.is_anime`), pas le root
  folder** : les deux concordent parfaitement ici (21 anime / 13 standard le
  2026-09-09), mais `library/anime` est un chemin propre à cette installation
  (cf. `ARR_ROOT_FOLDERS` de `provision.py`) là où `seriesType` est la notion
  par laquelle Sonarr lui-même distingue un anime — c'est ce champ qui active
  sa numérotation absolue. Une série mal typée dans Sonarr sort dans le mauvais
  onglet, volontairement : la correction se fait dans Sonarr, pas par une
  heuristique de chemin.
  **L'onglet d'origine voyage jusqu'à la modale** (`&tab=` sur le lien confirm,
  champ caché `tab` dans `confirm_series.html`) : les deux onglets postent sur
  la même route, et sans ça une suppression depuis Animés rerendait la liste
  des séries standard — donc une vue où le titre supprimé n'était de toute
  façon pas. Ce nom vient du client, d'où `arr_tab_name()` qui le ramène à une
  valeur de `ARR_TABS` (un `ARR_TABS[tab]` nu lèverait un KeyError, donc un 500
  nu, sur une valeur inventée).
  **La TUI n'a pas bougé** (`core.VIEWS` reste à 3), comme les autres ajouts
  récents.
- **Erreur réseau rendue en bandeau lisible**
  (`@app.exception_handler(RuntimeError)`) plutôt qu'un 500 brut — la TUI
  avait déjà ce filet dans `run()`, le web non (chaque route peut lever
  indépendamment). Le handler teste le préfixe `/api/` et répond en **JSON**
  dans ce cas : sinon l'addon Kodi recevait le fragment Bootstrap destiné au
  navigateur.
- **Mise en page réglée au détail près, demandée ainsi — ne pas
  « nettoyer » le CSS de `clearr.css` sans redemande** : table resserrée
  (`table-sm`), suppression via une **croix ✕ (U+2715)** et pas un emoji
  (rendu par la police couleur du système, donc ni monochrome ni cohérent
  d'un appareil à l'autre, là où un glyphe texte hérite du rouge de
  `.btn-outline-danger`), colonne d'actions figée à droite dans la vue
  Torrents (`.table-sticky-actions`) sans bordure ni ombre sur son bord
  gauche, tri au clic sur l'en-tête de colonne (reclic = sens inverse).
  Trois pièges CSS à ne pas re-découvrir :
  - `tr.collapsing { transition: none }` est **obligatoire** : les groupes
    cross-seed sont des `<tr class="collapse">` (un `<details>` ne peut pas
    entourer des `<tr>`), et l'animation de hauteur de Bootstrap est pensée
    pour des blocs — sans ça l'ouverture clignote. Comportement documenté
    de Bootstrap sur les tableaux, pas un bug.
  - `background-color` explicite sur la cellule figée : `--bs-table-bg` est
    transparent, les cellules qui défilent dessous resteraient visibles au
    travers. Le surlignage de survol de Bootstrap passe par un `box-shadow`
    inset, donc peint par-dessus et continue de marcher.
  - `.title-link.has-poster` redéclaré explicitement : `.title-link` étant
    déclaré après `.has-poster`, son `text-decoration: none` effaçait le
    souligné pointillé qui signale une jaquette au survol.
  Jaquette chargée seulement au premier survol dans les tableaux
  (`data-poster` porte l'URL, pas un `<img>` — sinon 45 images par rendu
  alors qu'on en regarde une), dans un conteneur flottant attaché au
  `<body>` (les lignes vivent dans `.table-responsive`, dont l'`overflow`
  découperait une vignette). Dans la fiche détail c'est un vrai `<img>` :
  une seule fiche ouverte à la fois, rien à économiser.
  Bootstrap 5.3 ne suit pas `prefers-color-scheme` tout seul — un script en
  tête de `page.html` pose `data-bs-theme` avant le rendu du `<body>` pour
  éviter un flash clair→sombre.
- **Titres sans fichier masqués par défaut dans Séries/Animés/Films**
  (2026-09-09), révélés par un switch « Afficher les titres sans fichier ».
  Demandé : la vocation de clearr est de montrer ce qui occupe de la place, et
  le nombre de titres suivis en attente de diffusion ne peut que croître (7/16
  séries et 14/42 films au moment de l'ajout, soit 21 lignes de bruit).
  Critère `core.series_without_files`/`movie_without_files` : ce que l'objet arr
  porte déjà (`statistics.episodeFileCount`, `hasFile`), **jamais un appel
  supplémentaire** — au rendu d'un onglet c'est la seule contrainte qui compte.
  **Ce n'est PAS « rien à supprimer »** : une série sans fichier peut encore
  porter des torrents grabés jamais importés (`series_grabbed_torrents`), que
  seule la purge emporte. C'est pour ça que c'est un **switch et pas un filtre
  en dur**, que le compte des masqués reste affiché (« 9/16 série(s) · 7 sans
  fichier masqué(s) »), et que le `title=` du switch renvoie vers l'onglet
  Torrents. Les rattacher au rendu coûterait un appel history par série.
  **Masquage 100 % CSS depuis `data-clearr-empty` sur `<html>`**, posé par le
  script de tête de `page.html` à côté de `data-bs-theme` : `#tab-content` est
  remplacé en entier à chaque clic d'onglet, tri et frappe dans le filtre, donc
  une classe portée par le fragment aurait dû être rejouée en JS après chaque
  swap — avec un flash des lignes masquées entre les deux. Le switch ne coûte
  aucun aller-retour serveur, et l'état survit aux swaps.
  Écrit en `:not([data-clearr-empty="show"])` et non `[…="hide"]` : sans
  attribut (JS coupé, `localStorage` qui lève en navigation privée) le défaut
  reste le masquage, celui qui est demandé.
  Les **deux comptes sont rendus ensemble**, le CSS choisit lequel s'affiche —
  réécrire le texte en JS après chaque swap aurait fait clignoter le mauvais
  chiffre. Seul l'état *coché* de la case ne peut pas venir du CSS, d'où
  `syncEmptySwitches()` appelé aux 3 points d'entrée (les 2 chemins de swap +
  le rendu initial, qui vient de `page.html` et ne passe pas par `swapInto`).
  Le switch vit **hors du `<form>` de filtre** : dedans, il serait sérialisé
  dans les paramètres du filtre en direct.
  Sa présence tient à `empty_total` (tout l'onglet) et non `empty_count` (la
  sélection filtrée) : sinon un filtre textuel ne ramenant aucun titre sans
  fichier ferait disparaître le switch, sans plus aucun moyen de le rebasculer.
  Un onglet sans aucun titre masquable ne l'affiche pas du tout (Animés, 0/21).
  Gabarit unique `templates/_rowcount.html` (macro `count_and_switch`), comme
  `title_cell` de `_meta.html` — sinon le bloc serait recopié dans
  `series_tab.html` et `films_tab.html`.
- **Suppression toujours confirmée par une modale**, composant Modal natif
  de Bootstrap plutôt qu'un `<dialog>` fait main : focus trap / Échap /
  clic-sur-le-fond déjà corrects, les réimplémenter aurait été strictement
  moins bien.
- **Les écrans de confirmation annoncent aussi les fichiers sans torrent**
  et marquent les torrents encore en seed (🌱) ou arrêtés (⏸,
  `SEEDING_STATUSES`) : `execute_delete_series` supprime les torrents
  **puis** balaie le dossier (`cleanup_orphan_files`), la modale promettait
  donc moins que ce qu'elle faisait. Déclencheur : un fichier `library/`
  dont Sonarr avait retiré le torrent du client après le ratio atteint — le
  hardlink Transmission disparu, le fichier restant.
  `core.orphan_files_under()` factorise le calcul, `series_orphan_files()`
  l'applique au dossier d'une série. **Piège : les deux appelants ne
  raisonnent pas dans le même espace de chemins** — un titre hors arr a ses
  *données* sous le dossier visé (les couverts sont les `host_files` du
  torrent), une série n'y a que des *hardlinks* (les couverts sont les
  `lib_matches`, les données vivant sous `.transmission/data`). Comparer les
  mauvais chemins ferait passer tous les fichiers pour orphelins.
  Les films n'ont volontairement pas d'orphelins : `_delete_movie` ne balaie
  pas le dossier du film (soit un torrent le couvre, soit Radarr supprime son
  propre fichier), donc en annoncer serait promettre plus que ce qui est fait.
- **Bouton « Orphelins library/ »**, pas une 4e vue (demandé) :
  `core.library_orphan_files()`/`delete_library_orphans()`. Comble un trou
  structurel — les 3 vues partent des torrents ou des objets arr, donc un
  fichier ni lié à un torrent ni connu d'un arr n'apparaissait nulle part.
  Le calcul coûte `2 + N` appels arr (~170 ms contre ~50 ms pour l'onglet
  Torrents) : à la demande, jamais au rendu d'un onglet — d'où l'absence de
  compteur ou de bouton grisé.
  Couverture d'un fichier : un **torrent** le couvre s'il partage son inode ;
  un **arr** le couvre si c'est un de ses `episodefile`/`movieFile` **ou** un
  sidecar (`SIDECAR_EXTENSIONS`) sous le dossier d'un titre qu'il connaît
  encore. Ce 2e volet est indispensable depuis le metadata writer (241
  `.nfo`), sans lui ils ressortaient tous orphelins. Corollaire voulu : une
  vidéo posée dans le dossier d'une série suivie mais jamais importée **est**
  un orphelin (c'est le cas qui a motivé la demande), là où un `.nfo` au même
  endroit ne l'est pas. Couverture par **chemin de fichier exact**, pas par
  préfixe de dossier de titre — un préfixe aurait rendu invisible exactement
  le cas cherché.
  **`_arr_covered_paths()` n'est PAS best-effort**, seule exception du
  module : elle lève `RuntimeError` si un appel arr échoue au lieu de
  dégrader. Sans la liste des fichiers d'un arr, tout ce qu'il gère passerait
  pour orphelin — proposer de supprimer la moitié de `library/` sur un
  timeout serait le pire échec possible de cette fonction.
  Le POST **recalcule** la liste au lieu de reprendre les chemins de la
  modale : aucun chemin à supprimer ne vient du client.
- **Fiche détail : agrégé pour une série, complet pour un film.** Série →
  `core.fetch_episode_files()` puis valeurs *distinctes* de qualité /
  groupe / langue / codec / résolution (empiler le `mediaInfo` de 12
  épisodes noierait la fiche ; ce qu'on veut savoir c'est s'il y a un
  mélange). `_distinct()` préserve l'ordre de première apparition, pas
  l'ordre alphabétique. Fichiers triés par `relativePath` (= ordre
  saison/épisode) et non par l'ordre de l'API, qui est celui des ids donc
  des imports. Film → un seul fichier, détail complet, **sans custom
  formats** : le `movieFile` imbriqué dans l'objet film de Radarr n'en porte
  pas (contrairement à l'`episodefile` de Sonarr), les afficher n'aurait
  donné que des tirets.
  `seedRatioLimit` **et** `seedRatioMode` demandés ensemble à `torrent-get`
  (`_seed_limit_label()`) : afficher la limite sans le mode induit en
  erreur, un torrent en mode 0 traînant souvent un `seedRatioLimit` résiduel
  qui ne s'applique pas.
  Une section sans lignes n'est pas rendue — un film sans fichier n'affiche
  simplement ni Fichier(s) ni Média, sans cas particulier dans le code.
- **Mode CLI `delete-by-inode <dev> <ino> [--dry-run]`**, réutilisé par le
  skill `anime-vf` : retrouve et supprime un torrent par `(dev, ino)` déjà
  connu — capturé par l'appelant *avant* qu'un import ne remplace le fichier
  `library/`, un stat a posteriori échouerait. Ne déclenche délibérément
  **pas** `plan_sonarr_unmonitor`/`plan_radarr_deletion` : l'épisode reste
  monitored, il vient d'être remplacé par une meilleure release, pas retiré.
  Sortie JSON sur stdout pour un appelant scripté.
- **Synchronisation arr à la suppression**, pour éviter qu'un titre encore
  monitored soit re-téléchargé : film Radarr → retiré complètement (+ import
  exclusion) ; épisode/saison Sonarr → saison désactivée seulement si elle
  est *terminée* (`totalEpisodeCount == episodeCount`) et entièrement
  supprimée, sinon seuls les épisodes concernés — pour ne jamais couper le
  monitoring d'une saison en cours de diffusion. Depuis les vues
  Séries/Films, retrait complet du titre (`DELETE /api/v3/series/{id}`,
  `addImportListExclusion=true`), sans la condition « saison terminée » :
  ici on supprime tout. Matching par chemin, pas par nom — fiable même si le
  titre affiché diffère (VO/VF, ponctuation).
  Best-effort : un arr injoignable ou un fichier jamais importé ne bloque
  jamais la suppression des fichiers eux-mêmes.
- **Suppression d'une série SAISON PAR SAISON** (2026-08-30, web + addon
  Kodi). Deux modes, deux boutons distincts dans la modale — jamais une case
  à cocher, l'intention doit se lire dans le bouton cliqué :
  - **normal** (`purge=False`, le défaut) : les saisons choisies partent, la
    série **reste dans Sonarr**, ses saisons supprimées passent en
    `monitored: false` et `monitorNewItems` est **forcé à `"all"`** — c'est
    tout l'intérêt : une saison future arrive quand même. Pas d'exclusion de
    liste, donc une saison redemandée depuis Seerr revient : différence
    voulue entre les deux boutons, pas un oubli.
  - **purge** (`execute_delete_series`, inchangé) : tout part, série retirée
    + exclusion. **La sélection de saisons est alors ignorée** — purger une
    partie laisserait les saisons gardées dans `library/` sans plus aucun arr
    pour les revendiquer, donc invisibles des trois vues.
  Les deux chemins sont **deux fonctions séparées** (`execute_delete_seasons`
  vs `execute_delete_series`) et non un paramètre : ils ne partagent ni
  l'ordre des écritures Sonarr, ni ce qu'ils balaient, ni ce qu'ils
  promettent. La TUI n'appelle que le second, donc n'a pas bougé.
  **Le plan part des `episodefile`, jamais des dossiers `Season XX` ni des
  torrents.** Pas des dossiers : format configurable, `Specials` pour la
  saison 0, et une série peut n'en avoir aucun. Pas des torrents : mesuré le
  2026-08-30, **164 des 269 `episodefile` (61 %) n'ont plus aucun torrent**
  (Sonarr les retire du client au `seedRatio` 1.5 des indexeurs publics), des
  saisons entières n'existant que comme fichiers `library/`. `core.
  series_episode_files()` **lève** au lieu de dégrader, comme
  `_arr_covered_paths()` : sans elle on ne supprimerait presque rien tout en
  s'annonçant réussi.
  **Ordre imposé dans `execute_delete_seasons()`** : `unmonitor` **avant**
  `DELETE /api/v3/episodefile/bulk`. Supprimer un episodefile d'une saison
  encore suivie déclenche la recherche automatique interne de Sonarr (piège
  déjà documenté dans `.claude/docs/arr-pieges.md`) — les deux répondent
  200, rien ne le signalerait
  à l'exécution, d'où un test dédié qui verrouille l'ordre. C'est **Sonarr**
  qui retire les hardlinks `library/` (et notifie Jellyfin via
  `onEpisodeFileDelete`, seul déclencheur actif ici — pas de `onSeriesDelete`
  sans purge) ; les torrents ne sont supprimés qu'ensuite. Corollaire :
  `do_delete()` traite désormais `FileNotFoundError` en `debug` et non en
  `warning`, le fichier déjà parti étant le cas NORMAL sur ce chemin.
  **Torrent à cheval sur une saison gardée** (pack `S01-S03`) : **conservé**,
  seuls les hardlinks `library/` de la saison choisie partent, donc **aucun
  espace libéré** — d'où `freed_bytes` distinct de `size_bytes` dans le plan
  et dans le résumé Kodi. Rare (0 sur 87 torrents le 2026-08-30) mais pas
  théorique. Un torrent qu'on n'arrive pas à rattacher à une saison connue
  n'est jamais supprimé au jugé.
  **Balayage des fichiers annexes borné au dossier de saison**, déduit des
  `episodefile` — et **rien n'est balayé** si `dirname == series.path` (série
  sans dossiers de saison) : sinon on emporterait toutes les autres saisons.
  Les sidecars (`SIDECAR_EXTENSIONS`) sont comptés à part **pour l'affichage
  seulement** : depuis le metadata writer une saison porte un `.nfo` par
  épisode (21 pour One Piece S23), les lister noierait le seul cas qui mérite
  d'être lu — une vidéo que Sonarr ne revendique pas.
  **Rien de ce qui est supprimé ne vient du client** : le POST ne reçoit que
  des numéros de saison (entiers), le plan est recalculé côté serveur, et une
  saison inconnue de Sonarr est **refusée** (`ValueError` → 400 JSON sur
  `/api/`, via un handler dédié : sans lui l'addon Kodi recevait un 500
  `text/plain` sans champ `message`).
  Les saisons **sans aucun fichier** ne sont pas proposées dans l'UI (One
  Piece en aligne 22) mais restent acceptées par l'API.
  `purge` est un **paramètre d'URL** et non un champ : les deux boutons
  soumettent le même formulaire, et un champ caché aurait imposé un second
  `<form>` imbriqué dans le premier — invalide en HTML. C'est ce qui a motivé
  l'ajout de **`data-form`** à `clearr.js` (le pied d'une modale Bootstrap est
  un frère de son corps, `closest("form")` n'y trouve rien).
- **Torrents grabés pour une série mais JAMAIS IMPORTÉS**
  (`core.series_grabbed_torrents()`, 2026-09-09) : la purge d'une série les
  emporte désormais aussi. `find_series_torrents()` ne rattache que par hardlink
  sous le dossier de la série — un grab que Sonarr a refusé d'importer
  (« Series title mismatch », numérotation absolue…) n'a aucun fichier
  `library/`, donc aucun `lib_matches`, donc était **structurellement invisible**
  de la vue Séries. Déclencheur : une série purgée depuis clearr, bien retirée de
  Sonarr, laissant **5 torrents et 21,97 Go** derrière elle, visibles de la seule
  vue Torrents. Mesuré dans la foulée sur le catalogue : **14 torrents / 18,4 Go
  sur 6 séries** étaient dans ce cas. Couvre du même coup le torrent dont Sonarr
  a supprimé le fichier après un upgrade en le laissant en seed.
  **Le rattachement vient de Sonarr, jamais du nom** : `GET
  /api/v3/history/series?seriesId=…&eventType=1` (liste plate, non paginée) porte
  `seriesId` **et** `downloadId` = l'infoHash, croisé avec le `hashString` des
  torrents — d'où l'ajout de ce champ aux `fields` de `list_torrents()`.
  Rapprocher des titres serait strictement moins sûr sur un chemin qui supprime
  des fichiers : deux séries homonymes suffiraient à effacer l'une pour l'autre.
  Le filtre `seriesId` est **refait côté client** — celui de `/api/v3/history`
  laisse passer d'autres séries (piège documenté dans
  `.claude/docs/arr-pieges.md`), on ne parie pas sur
  le fait que `/history/series` soit mieux tenu.
  **ORDRE IMPOSÉ : appeler AVANT `DELETE /api/v3/series/{id}`.** Sonarr purge
  l'historique d'une série avec elle ; après le retrait le rattachement n'existe
  plus et la liste revient simplement **vide, sans erreur**. Les appelants
  (`_delete_series` purge, les 2 sites de `tui.py`) le calculent donc avant et
  concatènent à `matched` — `execute_delete_series` ne les distingue pas, son
  `still_covered` ne lisant que des `lib_matches` (vides ici, volontairement).
  **Best-effort**, contrairement à `_arr_covered_paths()`/`series_episode_files()`
  qui lèvent : un échec fait *rater* des torrents, il n'en fait jamais supprimer
  à tort — bloquer toute la purge pour un complément coûterait plus qu'il ne
  protège.
  **Volontairement absent du chemin saison par saison** : ces torrents
  n'appartiennent à aucune saison connue (aucun `episodefile`), donc
  `execute_delete_seasons` ne peut pas les rattacher — et la preview partielle ne
  les compte pas, sinon elle annoncerait une taille jamais libérée. L'écran de
  confirmation web les liste dans une section à part disant explicitement
  « emporté(s) uniquement par Purger », même règle que les orphelins : ne jamais
  promettre pour un bouton ce que seul l'autre fait.
- Écrit sur mesure plutôt que d'ajouter un service tiers (Decluttarr,
  Removarr...) : aucun ne couvre « suppression Sonarr/Radarr → nettoyage
  automatique du client torrent », trou connu et non résolu de l'écosystème
  *arr (cf. issue ManiMatter/decluttarr#292).
- TUI seulement : marqueur `'M'` pour un torrent dont le fichier a disparu
  (cas Transmission « No data found! », jamais nettoyé tout seul) +
  `Maj+P` pour les purger en masse, et un écran d'aide (`?`) plutôt qu'un
  footer surchargé. Pas de jaquette (curses ne fait que du texte ; une vraie
  image demanderait un protocole terminal ou `chafa`). Les ajouts récents
  sont **web seulement**.

## Addon Kodi « Supprimer avec clearr » (`kodi/context.clearr`)

- `make kodi-install`, ajouté le 2026-08-05 — détail humain dans
  `kodi/README.md`. Envoie les ids externes + le chemin du titre sélectionné
  à `POST /api/{preview,delete}/{film,series}` de clearr, qui réutilisent les
  mêmes helpers `_delete_series`/`_delete_movie` que les routes web.
- **Transport HTTP, pas la CLI** : `python -m app delete-…` aurait été aussi
  court, mais clearr tourne en conteneur — l'atteindre depuis un client media
  supposerait SSH ou l'utilisateur Kodi dans le groupe `docker`
  (root-équivalent, cf. Socle). Le conteneur écoute déjà et est joignable en
  LAN, l'addon n'a besoin que d'`urllib`.
- **Résolution par id externe puis par chemin** (`_resolve_target`) : Kodi ne
  connaît pas les ids Sonarr/Radarr, seulement les `ProviderIds` que
  jellyfin-kodi recopie depuis Jellyfin. IMDb d'abord, puis TVDB/TMDB. **Un
  id qui matche plusieurs titres renvoie None (404) plutôt que d'en deviner
  un** — c'est une suppression de fichiers.
  Le repli par chemin sert les titres hors arr (Jellyfin sert aussi
  `completed/` comme bibliothèque), et il est **plus discriminant** que les
  ids : deux dossiers peuvent porter le même `tvdbId` (2 saisons téléchargées
  séparément = 2 séries pour Jellyfin) là où la résolution par id refuse — à
  raison — de trancher.
  `core.resolve_media_path()` ne suppose **aucun préfixe commun** (Kodi voit
  `/grosDur/...`, clearr `/data_root/...`, un client distant verrait un
  partage réseau) : il cherche le plus long suffixe de composants qui existe
  réellement sous `completed/` ou `library/`, **2 composants minimum** — sans
  ce plancher, un chemin finissant par `film` résoudrait sur toute la
  catégorie. Introuvable ou ambigu = rien de supprimé.
- **Le repli par chemin s'interdit `library/`** (`is_arr_managed_path`) : un
  titre qui y vit est presque toujours suivi par un arr, supprimer ses
  fichiers sans retirer son entrée le ferait re-télécharger — et ça referme
  du même coup le cas « id ambigu », dont les fichiers sont justement là.
  Message renvoyant vers l'UI web dans ce cas.
- Supprime les torrents **et** les fichiers du dossier qu'aucun torrent ne
  couvre (choix explicite) : sans ça un titre récupéré à la main, sans
  torrent, serait insupprimable depuis Kodi. Aucun appel arr dans ce mode,
  par construction.
- **Routes `/api/preview/...` appelées avant la confirmation** : la boîte
  annonce « 3 torrents — 15,6 Go » et pas seulement le titre, et un titre non
  résolu est signalé *avant* la confirmation. Elles couvrent les deux modes,
  pas seulement le repli — deux comportements selon l'origine du titre
  auraient été plus de code pour moins de cohérence. `orphan_lines()` plafonne
  à 5 noms : la boîte `yesno` de Kodi ne défile pas, au-delà le texte passe
  sous les boutons.
- **Menu contextuel aussi sur une saison** (`DBType=season`, addon `1.1.0`) :
  Kodi n'expose aucun `uniqueid` sur une saison, `season_target()` fait donc
  **deux appels JSON-RPC** (`GetSeasonDetails` → numéro + `tvshowid`, puis
  `GetTVShowDetails` pour les ids de la série). Depuis une **série**, les
  saisons se choisissent dans un `multiselect` alimenté par la **preview
  clearr**, jamais par `VideoLibrary.GetSeasons` : la base Kodi reflète
  Jellyfin, qui peut connaître des saisons que Sonarr n'a pas.
  Confirmation en `yesnocustom` à trois boutons (Annuler / Supprimer /
  Purger) ; **« Purger » n'apparaît que sur une série dont toutes les saisons
  sont sélectionnées** — jamais depuis une saison, jamais sur un film.
  Deuxième preview seulement si la sélection diffère du tout : sinon le
  résumé annoncerait une taille et un nombre de torrents faux.
  `DeleteTarget` gagne `seasons`/`purge`, **`purge=False` par défaut** : c'est
  un changement de comportement pour un appelant qui n'enverrait pas le champ,
  d'où le bump d'`addon.xml` — un addon non réinstallé cesserait silencieusement
  de purger.
- **Dépend de jellyfin-kodi en chemins directs** (`useDirectPaths=1`) : en
  mode addon, Kodi ne connaît qu'une URL `plugin://`, inexploitable — seuls
  les titres suivis par arr resteraient supprimables.
- **Ids lus par JSON-RPC** (`VideoLibrary.Get*Details`, propriété `uniqueid`)
  et non par les InfoLabels `ListItem.UniqueID(imdb)`/`IMDBNumber`, qui ne
  remontent que l'id désigné par défaut alors qu'on veut pouvoir retomber sur
  les autres.
- **Le retrait de la ligne dans Kodi n'est pas fait par l'addon** (option
  choisie par l'utilisateur après comparatif) : Kodi ne réplique pas le disque
  mais la bibliothèque Jellyfin, donc la chaîne est `clearr supprime` →
  `Jellyfin rafraîchit` → `KodiSyncQueue` → `jellyfin-kodi retire`. Un
  `VideoLibrary.RemoveMovie` local aurait fait disparaître la ligne
  instantanément mais désynchronisé la table de correspondance de
  jellyfin-kodi.
  **Aucun `Container.Refresh` à la fin** : la propagation prend **1 min 38 s**
  (chronométré — <1 s côté serveur, puis 65 s de `LibraryMonitorDelay`, 5 s de
  KodiSyncQueue, 25 s avant `LibraryChanged`, 3 s de retrait). Un refresh
  différé de 6 s rerendait strictement la même liste.
- Durées de notification dans `NOTIFICATION_MS`/`NOTIFICATION_ERROR_MS`
  (2,5 s / 5 s) : la suppression est déjà terminée quand elles s'affichent,
  ce n'est qu'un accusé de réception ; l'erreur reste plus longue, elle porte
  une information à lire. À ne pas confondre avec les toasts de jellyfin-kodi
  (`newvideotime`), qui ne viennent pas de cet addon.
- **L'URL de clearr est un réglage d'addon** (`resources/settings.xml`), pas
  une constante : elle contient `${DOMAIN}`, hors de question dans un fichier
  versionné. `make kodi-install` la pré-remplit depuis `.env.shared` et
  **n'écrase jamais un `settings.xml` existant**, Kodi réécrivant lui-même ce
  fichier. Addon **copié** et non symlinké (Kodi refuse un addon dont le
  dossier est un lien sortant de son `addons/`).
- **Pas de jeton d'authentification sur ces routes**, décidé explicitement :
  le service est LAN-only et son UI web expose déjà les mêmes suppressions en
  POST sans jeton — à revoir pour les deux ensemble, jamais pour l'API seule.

