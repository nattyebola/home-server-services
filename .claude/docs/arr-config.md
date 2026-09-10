# Config arr : chaîne vers Jellyfin/Kodi + scripts de provisioning

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à
`scripts/provision.py`, `scripts/apply-arr-overrides.py`,
`scripts/search-missing.py`, `arr/profiles/`, `arr/recyclarr/`,
ou aux connexions arr → Jellyfin.

## Chaîne arr → Jellyfin → Kodi

- **Metadata writer « Kodi (XBMC) / Emby » activé sur Sonarr et Radarr**,
  porté par `scripts/apply-arr-overrides.py` (`XBMC_METADATA_*`, 2026-08-06) :
  **Jellyfin n'apprend jamais les ids externes des arr** — la connexion
  Emby/Jellyfin ne signale qu'un dossier à rescanner, aucun identifiant ne
  circule, donc Jellyfin réidentifie chaque titre par une recherche TMDB sur
  le nom de dossier + année. Deux erreurs réelles constatées, toutes deux dues
  à un homonyme plus populaire (`Dead Man (1995)` pris pour *Dead Man
  Walking*, `One Piece` sans année pris pour la série live-action de 2023).
  Les `.nfo` écrits à côté des fichiers portent les `uniqueid`
  imdb/tmdb/tvdb, et les bibliothèques Jellyfin ont `Nfo` en tête de leur
  `LocalMetadataReaderOrder` : l'identification devient déterministe.
  **Ce n'est pas qu'un problème d'affichage** : un id faux rendait le titre
  insupprimable depuis Kodi (l'addon envoie les ids vus par Jellyfin, et le
  repli par chemin s'interdit `library/`).
  Images désactivées volontairement (`movieImages`/`seriesImages`/
  `seasonImages`/`episodeImages`/`episodeImageThumb`) : Jellyfin télécharge
  déjà ses jaquettes dans son cache, on ne veut que les identifiants dans
  `library/`.
  **Contrepartie : le `.nfo` fait autorité sur le titre affiché**, Jellyfin
  n'utilise donc plus la traduction TMDB. D'où `movieMetadataLanguage: 2`
  (French) côté Radarr. **Sonarr n'a aucun champ équivalent** (vérifié champ
  par champ ; son `uiLanguage` ne concerne que son interface), donc **les
  titres de séries s'affichent en anglais/original** — écart **accepté
  explicitement** après avoir écarté les deux parades : renommer et
  verrouiller le titre dans Jellyfin (manuel), ou reprendre le titre français
  depuis les `alternateTitles` de Sonarr (qui les connaît, mais sans étiquette
  de langue — deviner lequel est le français est exactement le genre
  d'heuristique qui a produit le bug corrigé ici). Ne pas reproposer sans
  redemande.
  Jellyfin **relit un `.nfo` modifié tout seul** (il en suit la date à son
  scan) ; un `/Items/{id}/Refresh` explicite ne sert qu'à ne pas attendre.
  Les `.nfo` ne sont écrits qu'à l'import ou sur rescan — rattraper une
  bibliothèque existante avec `RescanMovie`/`RescanSeries` sans argument.
  Pour **réidentifier** un titre déjà faux : `POST
  /Items/RemoteSearch/{Movie,Series}` puis `POST /Items/RemoteSearch/Apply/{id}`
  (équivalent API du bouton « Identifier ») — un rescan arr seul ne suffit
  pas, Jellyfin ne relit un `.nfo` qu'à un rafraîchissement de métadonnées.
  Audit à refaire de la même façon en cas de doute : comparer
  `imdbId`/`tmdbId`/`tvdbId` des objets arr aux `ProviderIds` des items
  Jellyfin **appariés par chemin** (`/library/...` côté Jellyfin ↔
  `/data_root/library/...` côté arr).
- **Renommage des fichiers à l'import activé sur les deux arr**
  (`renameEpisodes`/`renameMovies`, 2026-09-07, portés par
  `scripts/apply-arr-overrides.py`) : **Jellyfin résout la saison d'un épisode
  par le `SxxExx` du NOM DE FICHIER, qui prime sur le dossier `Season NN`** —
  et un `.nfo` ne peut pas rattraper ça, la saison étant figée à la résolution
  du chemin (le NFO n'a corrigé que le titre et le numéro d'épisode).
  Déclencheur : `One Piece S01E1172 …-Tsundere-Raws.mkv`, nommé par son groupe
  en saison 1 + numérotation absolue, rangé en **S1E17** par Jellyfin donc par
  Kodi, alors que Sonarr l'avait correctement importé en S23E17 dans
  `Season 23` et que son `.nfo` disait `<season>23</season>`.
  **Le toggle n'agit qu'à l'import : aucun rattrapage rétroactif n'a été fait,
  et il ne faut pas en lancer un** (`RenameFiles`/`RenameSeries` renommerait
  les 390 fichiers en place). Deux coûts mesurés le 2026-09-07 :
  - Jellyfin identifie ses items **par chemin** : renommer = ancien item
    supprimé + nouveau créé, et le `UserData` reste sur l'ancien id. **62
    épisodes marqués vus et 5 positions de reprise** seraient perdus, Kodi
    compris (sa table vient de jellyfin-kodi).
  - **49 des 390 fichiers n'ont pas de `sceneName`** (imports manuels). Un CF
    `ReleaseTitleSpecification` est réévalué **après** import sur `sceneName`
    s'il existe, **sinon sur le nom de fichier** — c'est pour ça qu'un
    renommage peut changer un score alors que les profils ne servent qu'au
    choix d'une release. Le format en place ne portant aucun token de langue,
    ces fichiers perdraient `FRENCH`/`VOSTFR`/`MULTi` : **16 passeraient sous
    le `cutoffFormatScore`** de leur profil, donc remis en recherche au
    prochain RSS sync — du quota indexeur brûlé pour des fichiers inchangés.
    Corollaire : ne pas versionner le format de nommage dans les overrides sans
    y reporter d'abord la langue (`{MediaInfo AudioLanguages}` ou
    `{Custom Formats}`). Pour les imports à venir la question ne se pose pas,
    une release grabée depuis un indexeur a toujours son `sceneName`.
  Le cas One Piece a donc été corrigé **à la main et à l'unité**, en gardant
  `VOSTFR` dans le nouveau nom (`One Piece S23E17 VOSTFR …`) puis
  `RescanSeries` : `cfScore` inchangé à 50, aucun regrab, hardlink et seed
  intacts (l'inode ne bouge pas, le nom sous `.transmission/data` non plus).
  Seule perte, annoncée : la position de reprise de cet épisode.
  **Piège de diagnostic** : `GET /api/v3/rename?seriesId=…` renvoie `[]` tant
  que `renameEpisodes` est à `false` — Sonarr construit le nom cible avec le
  même code que l'import, qui retourne le nom d'origine dans ce cas. Donc ni
  preview ni `RenameFiles` ne fonctionnent avant d'avoir activé le toggle, et
  un `[]` ne veut pas dire « tout est déjà conforme ».
- **Connexion Sonarr/Radarr → Jellyfin entièrement provisionnée**
  (`JELLYFIN_FIELDS`/`JELLYFIN_TRIGGERS` dans
  `scripts/apply-arr-overrides.py`), création incluse.
  `mapFrom=/data_root/library` / `mapTo=/library` est **obligatoire** : les
  arr voient la bibliothèque sous `/data_root/library/...` (mount unique du
  fix hardlink), Jellyfin sous `/library/...` — sans ça le refresh ciblé ne
  trouve pas le bon dossier. Cible `jellyfin:8096` en direct (réseau
  `traefik-public` partagé, pas de passage par Traefik).
  Déclencheurs voulus : `onSeriesDelete`+`onEpisodeFileDelete` côté Sonarr,
  `onMovieDelete`+`onMovieFileDelete` côté Radarr. Ils étaient **tous à
  `False`** depuis la création des connexions : Jellyfin ne découvrait une
  suppression que par son propre watcher. Les `*ForUpgrade` restent
  volontairement à `False` — le remplacement est déjà annoncé par `onUpgrade`,
  les activer ajouterait un « retiré puis rajouté » à chaque upgrade.
  **Déclaratif et faisant autorité** : tout déclencheur `on*` hors de la liste
  voulue est remis à `False`.
  `JELLYFIN_API_KEY` dans `arr/.env` — même clé que celle de Seerr, réutilisée
  plutôt qu'une clé dédiée. Le reste (`JELLYFIN_FIELDS`, triggers) est déclaré
  **dans le script** et non en `.env` : `jellyfin:8096` est un nom de service
  Docker et `mapFrom`/`mapTo` découlent des montages du repo, rien là-dedans
  n'identifie ce déploiement.
  **`MissingIntegration` → `note:` + exit 0**, pas une erreur : `README.md`
  donne cette connexion pour optionnelle, un déploiement sans Jellyfin verrait
  sinon le cron quotidien sortir en échec. D'où un 3e canal de sortie dans
  `main()` (`notes`, à côté de `changed`/`errors`) — à réutiliser pour toute
  future intégration optionnelle plutôt que de choisir entre « silencieux » et
  « échec ». Sans clé mais avec une connexion existante, les champs non
  secrets et les déclencheurs sont quand même maintenus.
  **Piège central : l'API Servarr renvoie les champs secrets masqués en
  `********`** et les préserve quand on les repasse tels quels. C'est ce qui
  rend possible le mode « sans clé », mais ça veut aussi dire qu'une clé qui
  aurait dérivé est **indétectable** par comparaison (elle est donc exclue de
  `jellyfin_signature()`, sinon chaque passage réécrirait pour rien).
  `POST /api/v3/notification/testall` est le seul moyen de vérifier qu'une clé
  stockée fonctionne encore.
  Ce n'est pas recyclarr qui fait dériver ces champs (il ne touche pas aux
  notifications) : le réalignement quotidien ne sert qu'à rattraper une
  modification par mégarde dans l'UI, la reproductibilité sur une installation
  neuve étant l'objectif principal.
  **Ces déclencheurs ne raccourcissent PAS le délai de Jellyfin** (affirmé à
  tort puis démenti par la mesure) : `POST /Library/Media/Updated` émis à la
  main déclenche le rafraîchissement **60 s plus tard** — Jellyfin fait passer
  les mises à jour *signalées* par le même temporisateur `LibraryMonitorDelay`
  (60 s, `${DATA_ROOT}/.jellyfin/config/config/system.xml`) que les événements
  inotify. Ce qu'ils apportent : le bon dossier est signalé explicitement,
  sans dépendre d'inotify. Réduire le délai demanderait de baisser
  `LibraryMonitorDelay` lui-même — **non fait**, réglage global qui protège
  aussi les imports d'un scan lancé sur un fichier encore en écriture.
  Trou connu : une suppression qui ne retire pas le titre côté arr (vue
  Torrents, où Sonarr se contente d'un `unmonitor`) ne notifie rien et reste
  tributaire du watcher.
  Bruit connu : Sonarr logue deux `[Warn] MediaBrowserProxy: Unable to send
  notification to Emby` par suppression alors que `notify` est à `false` —
  c'est la notification *à l'écran* des clients, pas le rafraîchissement de
  bibliothèque. Sans effet, non diagnostiqué plus loin.
- **Tag `pour-les-enfants` sur les deux arr**, créé par `scripts/provision.py`
  (`ARR_TAGS`) : posé depuis Seerr au moment de la requête (override `tags`
  par requête), il ressort dans le `<tag>` du `.nfo`, donc dans les `Tags` de
  l'item Jellyfin, donc dans la table `tag` de Kodi, où il sert de filtre.
  Dans `provision.py` et pas `apply-arr-overrides.py` : un tag est un objet
  que l'utilisateur peut légitimement renommer, donc créé-si-absent et jamais
  réécrit. Rattachement **par libellé**, les ids diffèrent d'un arr à l'autre.
  **Libellé en tirets, pas en underscores** : Radarr valide `^[a-z0-9-]+$` et
  refuse `pour_les_enfants`, là où Sonarr l'accepte — divergence de validation
  entre les deux, à ne pas re-découvrir. Le même libellé des deux côtés est ce
  qui permet de n'écrire qu'un seul filtre en aval.
  Trou connu, non traité : le tag **`fr-priority`** (Sonarr) est la cible d'un
  **delay profile** qui ne vit que dans la base Sonarr — ni le tag ni le profil
  ne sont reproductibles depuis le repo.
- **Seerr parle à Jellyfin en direct (`jellyfin:8096`)**, pas par le domaine
  public (2026-08-24) : il était réglé sur `https://jellyfin.${DOMAIN}:443`,
  donc chaque requête traversait Traefik et son middleware `rate-limit` — que
  son propre `AvailabilitySync` fait sauter (13 × `429` pendant le sync de
  03:00, dont 12 titres non retrouvés). Comme ce job retire la disponibilité
  d'un média introuvable, **un 429 y est indiscernable d'une suppression
  réelle**, et le job se déclare quand même « complete ».
  **Piège : renseigner `externalHostname` en même temps.** Les liens « Lire
  sur Jellyfin » sont construits dans `server/entity/Media.ts`, qui retombe
  sur `getHostname()` (= l'adresse *interne*) quand il est vide — tous les
  liens montrés aux utilisateurs deviendraient `http://jellyfin:8096/...`. Il
  porte donc l'URL publique complète, schéma inclus et **sans slash final**
  (`Media.ts` concatène `${jellyfinHost}/web/...`). `mediaUrl` est recalculé
  en `@AfterLoad()`, les entrées déjà en base reprennent la bonne URL seules.
  **Éditer `settings.json` conteneur arrêté** (`docker stop`/`start`, pas de
  recréation) : Seerr le réécrit lui-même, une édition à chaud est perdue.
  `provision.py` écrit déjà l'adresse interne **et** `externalHostname` dérivé
  de `DOMAIN` — la dérive venait de l'assistant web, pas du script.

## Scripts de provisioning arr

- **`scripts/provision.py` (`make api-keys` / `make provision`)** automatise
  la configuration d'installation qui se faisait à la main dans les UI :
  bibliothèques Jellyfin, clés API arr + cross-seed + Jellyfin, applications
  Prowlarr, client de téléchargement, root folders, remote path mapping,
  Connection cross-seed, tags, config Seerr complète.
  **Deux targets et pas un** parce que l'ordre est contraint dans les deux
  sens : `api-keys` doit précéder `recyclarr-sync`/`arr-overrides` (qui ont
  besoin des clés), et la config Seerr de `provision` doit les suivre (elle
  désigne les profils qualité **par nom**).
  **Créé-si-absent, jamais réécrit** — l'inverse d'`apply-arr-overrides.py`.
  Volontaire : ce sont des objets d'infrastructure que l'utilisateur peut
  légitimement ajuster ensuite dans les UI, et ce script n'est donc **pas**
  dans `scripts/crontab`. Piège évité de peu : un premier jet POSTait
  `/settings/jellyfin` de Seerr sans garde, ce qui aurait remplacé l'adresse
  réglée à la main sur une installation en service.
  **Best-effort par objet (`run_step`)**, imposé par des validations
  synchrones côté serveur : Sonarr valide le client de téléchargement en s'y
  connectant au moment du POST (un `transmission-vpn` arrêté faisait échouer
  tout le provisioning) ; la Connection Custom Script exige que le fichier
  existe côté conteneur ; Prowlarr vérifie que Sonarr/Radarr sait le joindre
  en retour sur `PROWLARR_INTERNAL_URL`.
  `jellyfin/.env` porte `JELLYFIN_ADMIN_USER`/`PASSWORD`, nécessaires aux deux
  seules opérations que Jellyfin refuse à une clé API : créer la première clé
  (bootstrap) et créer le compte propriétaire de Seerr. **Une clé API suffit
  en revanche pour créer une bibliothèque** — ne pas réintroduire de
  dépendance aux identifiants dans `make provision`.
  **`Authorization: MediaBrowser Token="<jeton>"`** (`jellyfin_auth()`) est la
  seule forme d'authentification que Jellyfin 12.0.0 accepte **par défaut**,
  pour une clé API comme pour un token de session : il a désactivé les deux
  autres, historiquement équivalentes — le paramètre d'URL `?api_key=` **et**
  l'en-tête `X-Emby-Token` répondaient **401** (les deux vérifiés le
  2026-09-08, quelques heures après la mise à niveau automatique — le volet
  Jellyfin de `make api-keys`/`make provision` était cassé sans que rien ne le
  signale, le cron n'appelant pas ce script). La clé stockée, elle, reste
  valide : un 401 ici veut dire « mauvaise forme d'en-tête », pas « clé à
  regénérer » — et la relire par `?api_key=` pour « vérifier » ne prouverait
  que ça. **`scripts/provision.py` reste sur la forme `MediaBrowser Token`** :
  c'est la seule qui ne dépend d'aucun réglage serveur.
  **En revanche les connexions Sonarr/Radarr → Jellyfin, elles, ont bien
  cassé — affirmé le contraire ici, à tort.** Leur implémentation .NET
  (`MediaBrowserProxy`) envoie `X-Emby-Token` : depuis la mise à niveau, tout
  rafraîchissement ciblé de bibliothèque échouait en `401 Unauthorized` sur
  `GET /Items` (`Unable to process notification queue for Jellyfin`, 4 fois
  côté Sonarr et 1 côté Radarr entre le 2026-09-08 09:28 et le 2026-09-09
  03:19). Dégradation silencieuse et pas panne : Jellyfin retombait sur son
  propre watcher.
  **`POST /api/v3/notification/testall` ne détecte PAS ce cas** : il répond
  `isValid: true` alors que le chemin réel est en 401 — il ne sonde qu'un
  endpoint public. Ne jamais s'en servir seul pour conclure qu'une connexion
  Jellyfin fonctionne ; reproduire la vraie requête
  (`docker exec arr-sonarr-1 curl -H "X-Emby-Token: …" http://jellyfin:8096/Items?…`).
  Réparé le 2026-09-09 en repassant **`EnableLegacyAuthorization` à `true`**
  côté Jellyfin, ce qui réautorise `X-Emby-Token` et `?api_key=` sans rien
  ouvrir en anonyme (sans jeton c'est toujours 401). Écrit **par l'API**
  (`GET` puis `POST /System/Configuration`, objet complet), pas en éditant
  `config/config/system.xml` : Jellyfin réécrit ce fichier lui-même, une
  édition à chaud serait perdue — même piège que le `settings.json` de Seerr.
  Le réglage est pris en compte **sans redémarrage**.
  À surveiller : c'est un sursis, pas une cible. Le jour où Jellyfin retirera
  le flag, seul un correctif upstream côté Servarr débloquera ces connexions.
  Détail sans conséquence : après suppression d'une bibliothèque Jellyfin,
  celle-ci reste un moment listée par l'endpoint que Seerr interroge (cache
  côté Jellyfin) — elle arrive désactivée côté Seerr. Corollaire voulu : une
  bibliothèque personnelle (ex. « Kids ») n'est pas activée dans Seerr par le
  script, `JELLYFIN_LIBRARIES` seul l'est.
- **`scripts/apply-arr-overrides.py` (`make arr-overrides`)**, enchaîné par
  cron quotidien juste après `make recyclarr-sync` : **déclaratif et faisant
  autorité**, il réapplique tout ce que recyclarr écrase ou ne couvre pas.
  Périmètre : tailles de palier « Quality Definition » et champ `language` des
  deux profils principaux (Sonarr `WEB-2160p (Combined)`, Radarr `[SQP] SQP-1
  WEB (2160p)`), config anime (`arr/profiles/sonarr-anime.json`), connexions
  Jellyfin, metadata writer, ratio des indexeurs publics, renommage des
  fichiers à l'import (`renameEpisodes`/`renameMovies`), rejet des
  téléchargements non-média (`failDownloads`), catégorie Anime de Nyaa.si.
  Résout les profils **par nom, jamais par id** (propres à chaque instance —
  c'est précisément pourquoi un dump d'API brut ne serait pas reproductible).
  Idempotent et best-effort par arr.
  `arr/profiles/sonarr-anime.json` couvre les 3 custom formats qui nous
  appartiennent (`FRENCH`, `VOSTFR (hors suffixe)`, `Pack NN of NN`) et les profils
  `Anime (Fansub)*` : aucun `trash_id` ne les couvrait, donc rien ne les
  recréait sur une installation neuve et rien ne rattrapait leur dérive.
  **Le JSON fait autorité** : tout custom format absent de `scores` est remis
  à 0 sur le profil concerné. Un profil absent est créé depuis
  `/api/v3/qualityprofile/schema` plutôt qu'en versionnant tout l'arbre des
  paliers.
  **Ordre imposé** : custom formats d'abord, profils ensuite (qui les
  référencent par nom) — et surtout `make recyclarr-sync` AVANT tout le
  script, sinon les CF du guide scorés par les profils anime (`MULTi`, `LQ`,
  `Upscaled`...) n'existent pas encore ; ce cas lève une erreur explicite
  plutôt que de créer un profil silencieusement dépourvu de la moitié de ses
  scores.
  `api_put` passe par un `api_write` commun qui **vérifie la réponse** :
  `curl -s` sort 0 même sur un 400, sans ça une écriture refusée par la
  validation Sonarr était comptée comme réussie.
  **`settle()` : relecture jusqu'à 2 passes consécutives sans rien à
  corriger** (bornées à 6 tentatives × 5 s), appliqué aux seules étapes que
  recyclarr fait dériver. Nécessaire à cause des écritures Servarr asynchrones
  — voir le piège des écritures Servarr asynchrones dans `CLAUDE.md`.
  Une passe qui corrige remet le compteur à
  zéro, donc une écriture en deux temps ne conclut pas sur la première
  accalmie.
- **`scripts/search-missing.py` (`make search-missing`)**, cron hebdomadaire
  le lundi 5h : relance une recherche sur les épisodes/films manquants
  **déjà sortis**. Comble un trou structurel — **ni Sonarr ni Radarr n'a de
  tâche planifiée de recherche des manquants** (leur `/api/v3/system/task` ne
  liste que RSS Sync / Refresh / Import List Sync), donc une release absente ou
  rejetée au moment où l'item passe dans le flux RSS n'est **plus jamais
  retentée**. Constaté le 2026-08-29 : `The Simpsons` S37E13/E15 manquants
  depuis janvier/février alors qu'une release approuvée était disponible chez
  les indexeurs à l'instant du diagnostic.
  **Le quota indexeur est la contrainte dominante**, pas la couverture : une
  passe « tous les manquants, chaque nuit » est exactement la rafale qui a fait
  tomber C411 le 2026-07-28. Trois garde-fous, dans cet ordre — cadence
  hebdomadaire ; plafond dur par exécution (`MAX_SEARCHES_PER_RUN`) ; **mémoire
  de la dernière recherche par item** (`${DATA_ROOT}/.search-missing-state.json`,
  `MIN_RESEARCH_INTERVAL_DAYS`), qui sert d'abord les plus anciennement
  cherchés. Sans cette rotation le plafond écarterait toujours les mêmes items ;
  avec elle, tout finit couvert en étalant la charge.
  N'est **jamais** recherché : un item **déjà dans la file** (y compris
  `importBlocked` — la release est trouvée, la rechercher ne ferait que
  consommer du quota et risquer un doublon), et un film à
  `isAvailable == False` (pas encore sorti selon sa `minimumAvailability` — le
  catalogue en contient en permanence une dizaine). Sonarr n'a pas besoin de
  l'équivalent : son `wanted/missing` ne renvoie que des épisodes déjà diffusés.
  `--dry-run` liste la sélection sans rien envoyer aux indexeurs — le réflexe
  avant de toucher aux plafonds.
- **Le scheduler interne de recyclarr est désactivé**, le service passe en
  mode manuel pur (`arr/docker-compose.yml` : plus de `restart:`/healthcheck,
  `profiles: [manual]` pour rester absent de `make up STACK=arr`), déclenché
  uniquement par `make recyclarr-sync` (`docker compose run --rm recyclarr
  sync` — passer un argument à l'entrypoint bascule du mode cron au mode CLI
  one-shot). Aucune valeur de `CRON_SCHEDULE` ne le désactive proprement, et
  son `@daily` créait une fenêtre pendant laquelle les overrides étaient dans
  l'état par défaut du guide TRaSH. `scripts/crontab` enchaîne donc
  `recyclarr-sync && apply-arr-overrides.py` sur une seule ligne.
  Recyclarr est gardé pour sa vraie valeur — les MAJ communautaires des ~120
  regex, listes LQ, groupes de release, tags de plateformes — mais **toute la
  config custom doit vivre dans le repo**.
- **Limite de ratio 1.5 sur les indexeurs marqués publics**
  (`PUBLIC_INDEXER_SEED_RATIO`) : `seedCriteria.seedRatio` posé sur tout
  indexeur dont
  l'indexeur Prowlarr correspondant a `privacy: public`, jamais sur les
  autres — un tracker privé compte le ratio comme une monnaie, un public n'en
  tient aucun compte, donc y seeder au-delà du nécessaire n'immobilise que la
  copie Transmission. Rattachement par le dernier segment du `baseUrl` de
  l'indexeur synchronisé (`http://prowlarr:9696/<id>/`) : seul Prowlarr porte
  l'information `privacy`, les arr n'en gardent que l'URL.
  **La valeur fait autorité sur l'indexeur PROWLARR**
  (`torrentBaseSettings.seedRatio`), pas seulement côté arr : les deux
  applications Prowlarr sont en `syncLevel: fullSync` et sa tâche
  `ApplicationIndexerSync` tourne **toutes les 6 h**, réécrivant l'indexeur de
  chaque arr depuis la définition de Prowlarr — ce qui efface tout
  `seedCriteria` que l'arr portait. Une valeur posée seulement côté arr ne
  peut donc pas tenir plus de 6 h. La passe côté arr est **conservée** comme
  filet : effet immédiat sans attendre un sync, et seul recours si `syncLevel`
  passait à `addOnly`/`disabled`.
  Prowlarr expose bien `torrentBaseSettings.seedRatio`, `seedTime`,
  `packSeedTime` et `appMinimumSeeders` sur son propre objet indexeur (une
  version antérieure de ce fichier affirmait le contraire, à tort).
  `forceSave=true` sur le PUT côté arr : Sonarr/Radarr testent la connexion à
  l'indexeur au moment de l'écriture, et ces trackers répondent régulièrement
  520/530 — sans ça une panne passagère ferait échouer un réalignement
  purement local.
  Ne pas confondre avec la limite globale de Transmission
  (`ratio-limit-enabled: false`, laissée telle quelle) : c'est bien une limite
  **par torrent** (`seedRatioMode=1`) que les arr poussent au grab.
  **Non rétroactif** : `seedRatio` ne s'applique qu'au moment du grab. Les
  torrents déjà présents ont été repris une fois à la main (`torrent-set` en
  masse). Refaire ce rattrapage si un indexeur public est ajouté avec des
  torrents déjà en place. Un torrent annonçant **aussi** à un tracker privé
  est exclu : lui couper le seed coûterait du ratio là où il compte.
- **Deux profils anime plutôt qu'un scoring global** : `Anime (Fansub) VF`
  (audio français préféré) et `Anime (Fansub) VOSTFR` (japonais + sous-titres).
  La bascule par série fait elle-même office de sélection explicite — un
  premier essai avait modifié un profil partagé en place, corrigé après retour
  de l'utilisateur. Custom Format `FRENCH` (`\b(TRUEFRENCH|FRENCH|VFF|VFQ)\b`,
  résoudre son id **par nom**, ne pas le supposer fixe) scoré 200 sur le profil
  VF. `VOSTFR` veut dire japonais + sous-titres français, **pas** de l'audio
  français — à ne pas confondre.
  Profils Sonarr existants : les 6 par défaut (aucun utilisé),
  `WEB-2160p (Combined)`, `Anime (Fansub) VF`, `Anime (Fansub) VOSTFR`.
  Skill `.claude/skills/anime-vf/SKILL.md` : bascule la série sur le profil
  VF, relance `SeriesSearch`, rapporte ce qui a été grabé — le remplacement
  du fichier reste automatique côté Sonarr (`upgradeAllowed: true`). Le skill
  capture le `(dev, ino)` de chaque fichier **avant** la recherche, attend
  l'import, puis supprime l'ancien torrent via `delete-by-inode`. Ne marche
  que si une release FRENCH/VFF/VFQ/TRUEFRENCH existe réellement chez les
  indexeurs au moment de la recherche.
- **`scripts/vpn-bench.py` (skill `vpn-bench`)** compare latence/débit entre
  le serveur AirVPN configuré (`vpn/custom/default.ovpn`) et d'autres pays.
  Marche parce que le certificat client AirVPN est lié au **compte**, pas au
  serveur : seule la ligne `remote [pays].vpn.airdns.org <port>` change, rien
  à regénérer depuis le site. **Restaure systématiquement la config d'origine
  à la fin** (backup sur disque en plus de la copie en mémoire, pour survivre
  à un kill dur) — jamais de changement permanent sans le redemander.
  Latence mesurée vers un tracker tiré au hasard parmi les torrents actifs
  (accepté explicitement : moins reproductible, plus simple).

