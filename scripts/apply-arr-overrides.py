#!/usr/bin/env python3
# Applique la configuration arr versionnée dans le dépôt, de façon déclarative :
# ce script FAIT AUTORITÉ, une modification faite à la main dans l'UI sur son
# périmètre est annulée au run suivant (cron de minuit, scripts/crontab).
#
# Profils de qualité (arr/profiles/) : custom formats (custom-formats.json,
# communs aux deux arr), profils, tailles de palier, délai de grab et profils
# enfants (sonarr.json, radarr.json). Depuis le 2026-10-07, plus de recyclarr :
# les profils des guides TRaSH ont été remplacés par des profils écrits à partir
# des règles de docs/telechargement.md (« Règles de sélection ») et d'une étude
# des releases réellement disponibles — voir .claude/docs/arr-config.md.
#
# Provisionne AUSSI la connexion Emby/Jellyfin de Sonarr/Radarr — création
# incluse — à partir des constantes JELLYFIN_* plus bas et de JELLYFIN_API_KEY
# (arr/.env, seule valeur secrète du lot) : ces réglages ne vivaient nulle part
# dans le repo, donc rien ne les recréait sur une installation neuve ni ne
# rattrapait une modification par mégarde dans l'UI.
#
# Provisionne AUSSI la limite de ratio des indexeurs publics (voir
# PUBLIC_INDEXER_SEED_RATIO) : même motif que la connexion Jellyfin, ce réglage
# ne vivait que dans la base Sonarr et en avait silencieusement disparu.
import copy
import datetime
import json
import os
import shlex
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(REPO_ROOT, "arr", "profiles")
CUSTOM_FORMATS_FILE = os.path.join(PROFILES_DIR, "custom-formats.json")

SONARR_CONTAINER = "arr-sonarr-1"
SONARR_URL = "http://localhost:8989/api/v3"
RADARR_CONTAINER = "arr-radarr-1"
RADARR_URL = "http://localhost:7878/api/v3"
PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1"

# --- relecture : les écritures de config atterrissent APRÈS la réponse -------
#
# `PUT /api/v3/qualitydefinition/...` peut répondre 202 Accepted : Sonarr/Radarr
# mettent la mise à jour en file et l'appliquent après avoir répondu (0,5 à 53 s
# mesurés le 2026-08-07, à l'époque où recyclarr réécrivait ces paliers juste
# avant ce script). Une lecture enchaînée derrière voit encore l'ancienne valeur
# et se déclare satisfaite. D'où settle() : on ne conclut qu'après
# SETTLE_STABLE_SECONDS de calme continu (latence max mesurée + marge), relu
# toutes les SETTLE_DELAY_SECONDS. Borné, pour qu'une dérive qui se rétablirait
# en boucle ne fasse pas tourner le cron sans fin : deux fenêtres complètes + 2
# passes au pire. Coût : au moins une minute par étape, accepté (cron de minuit).
# NE PAS remplacer par un `sleep` : la latence n'est pas bornée côté Servarr,
# seule la relecture prouve la stabilité.
SETTLE_STABLE_SECONDS = 60
SETTLE_DELAY_SECONDS = 10
SETTLE_CLEAN_PASSES = SETTLE_STABLE_SECONDS // SETTLE_DELAY_SECONDS + 1
SETTLE_ATTEMPTS = 2 * SETTLE_CLEAN_PASSES + 2

# --- limite de ratio sur les indexeurs publics -------------------------------
#
# Un tracker public ne tient aucun compte de ratio : seeder au-delà de ce qu'il
# faut pour rendre la release disponible n'apporte rien et immobilise la copie
# Transmission (le fichier library/ n'en est qu'un hardlink) indéfiniment. Sur
# un tracker privé au contraire, le ratio EST la monnaie du compte — d'où une
# limite posée sur les seuls indexeurs que Prowlarr marque `privacy: public`,
# jamais sur les autres, qui restent en seed sans limite propre.
#
# Sonarr/Radarr poussent cette valeur au client au moment du grab
# (seedCriteria.seedRatio), puis retirent le torrent du client une fois le seuil
# atteint (removeCompletedDownloads) — le fichier de la bibliothèque survit,
# c'est le hardlink qui le protège.
#
# Valeur portée ici et non dans l'UI parce que c'est exactement le réglage qui
# avait disparu : posé à la main sur Nyaa.si le 2026-07-28, il était revenu à
# None au 2026-08-06 (resynchronisation Prowlarr -> Sonarr), sans que rien ne le
# signale — trois épisodes seedaient donc sans limite, dont un à 13,25 de ratio.
# 1,5 (au lieu des 2 d'origine) : choix de l'utilisateur le 2026-08-06.
#
# La valeur est posée sur l'indexeur PROWLARR (torrentBaseSettings.seedRatio),
# pas seulement sur les indexeurs synchronisés côté Sonarr/Radarr : les deux
# applications Prowlarr sont en `syncLevel: fullSync`, et sa tâche
# ApplicationIndexerSync (toutes les 6 h) réécrit alors l'indexeur dans chaque
# arr en repartant de SA définition — ce qui efface tout `seedCriteria` que
# l'arr portait. Mesuré le 2026-08-07 sur les logs Sonarr : écriture du script à
# 12:17:14 (`PUT /api/v3/indexer/4?forceSave=true`), remise à None par Prowlarr
# à 12:20:41 (`PUT /api/v3/indexer/4`, déclenché par un ApplicationIndexerSync).
# C'est LA cause de la disparition constatée le 2026-08-06 et attribuée alors à
# une hypothèse ("probablement une resynchronisation") : elle est confirmée, et
# poser la valeur uniquement côté arr ne pouvait pas tenir plus de 6 h.
# Corollaire : Prowlarr fait autorité, donc c'est chez lui que le réglage doit
# vivre — la passe côté arr est conservée dessous comme filet (effet immédiat
# sans attendre un sync, et seul recours si `syncLevel` passait un jour à
# addOnly/disabled, cas où Prowlarr ne pousserait plus rien).
PUBLIC_INDEXER_SEED_RATIO = 1.5
PROWLARR_PUBLIC_PRIVACY = "public"
PROWLARR_SEED_RATIO_FIELD = "torrentBaseSettings.seedRatio"

# --- rejet des téléchargements dont le contenu n'est pas un média ---
#
# Chaque indexeur Torznab/Newznab porte un champ `failDownloads` (options 0 =
# Executables, 1 = Potentially Dangerous) : au traitement d'un téléchargement
# terminé, l'arr marque le download comme ÉCHOUÉ au lieu de le laisser en
# attente indéfinie dans la file. Combiné à `autoRedownloadFailed` (déjà à true
# sur les deux arr), la chaîne complète devient automatique : échec -> torrent
# blocklisté par hash -> retiré du client -> nouvelle recherche excluant cette
# release.
#
# Déclencheur : deux releases d'archive/exécutable grabées sur Nyaa.si (un .exe,
# puis `Ted Lasso S04E06 …Atmos.zipx` le 2026-09-07 — 1,1 Go de vrai ZIP, magic
# PK\x03\x04). La garde d'import de Sonarr les détectait bien (« Caution: Found
# potentially dangerous file with extension: .zipx ») mais s'arrêtait là :
# l'entrée restait en `importPending`, sans que rien ne la purge ni ne relance
# la recherche.
#
# Pourquoi PAS un custom format, contrairement au CF `Pack NN of NN` qui traite
# une autre release mal formée : l'extension n'est PAS dans le titre de la
# release (`Ted Lasso S04E06 1080p ATVP WEB-DL DDP5 1 Atmos` en base, vérifié
# dans l'historique de grab et la blocklist), seulement dans le nom du fichier
# du torrent. Un `ReleaseTitleSpecification` ne peut donc rien matcher au grab —
# l'information n'existe qu'après téléchargement, ce qui est précisément le
# moment où `failDownloads` agit.
# La blocklist seule ne suffisait pas non plus : la même release, déjà
# blocklistée le 2026-09-06, a été regrabée le 2026-09-07 sous un autre
# infoHash (deux uploads distincts du même titre), Sonarr matchant titre +
# indexeur.
#
# Vérifié le 2026-09-07 : ce champ SURVIT à l'ApplicationIndexerSync de Prowlarr
# (relecture après un sync déclenché à la main), contrairement à
# `seedCriteria.seedRatio` et à `categories` — Prowlarr ne l'expose pas sur son
# propre objet indexeur, il ne peut donc pas l'écraser. C'est pour ça que ce
# réglage vit côté arr et n'a pas de pendant Prowlarr ici.
INDEXER_FAIL_DOWNLOADS_FIELD = "failDownloads"
INDEXER_FAIL_DOWNLOADS = [0, 1]

# --- Nyaa.si restreint à sa catégorie Anime ---
#
# `cat-id` est un champ de la définition Cardigann `nyaasi` : il fixe la
# catégorie interrogée côté site. À 0 (« All categories », le défaut) Nyaa
# renvoie aussi ses catégories Live Action, Audio et Software — c'est par là
# qu'une release live-action a atterri dans Sonarr, et c'est la source possible
# d'un exécutable.
#
# Mesuré le 2026-09-07 : sur 578 grabs Sonarr, Nyaa.si n'avait servi que 2 fois
# pour une série non-anime, et ce sont exactement les deux grabs de l'archive
# Ted Lasso ; les 197 grabs de séries standard viennent de TR4KER/YggReborn/
# C411/V3X. Côté Radarr, 0 grab Nyaa.si sur les 34 derniers. La restriction ne
# coûte donc aucune source réellement utilisée.
# Effet vérifié par A/B sur la même requête (« Nogizaka46 ») : 75 résultats dont
# 63 en Live Action à cat-id 0, contre 1 résultat (Anime Music Video) et 0 Live
# Action à cat-id 1 — les recherches d'anime, elles, sont inchangées (« One
# Piece » : 75 résultats dans les deux cas).
#
# Posé côté PROWLARR et pas via le champ `categories` des indexeurs
# synchronisés : ce dernier est réécrit par l'ApplicationIndexerSync (mesuré le
# 2026-09-07, `categories` remis de [] à [5000] après un sync), même piège que
# seedRatio. Rattachement par `definitionFile`, pas par id : les ids d'indexeur
# Prowlarr ne sont pas stables (voir CLAUDE.md, section cross-seed).
NYAA_DEFINITION_FILE = "nyaasi"
NYAA_CATEGORY_FIELD = "cat-id"
NYAA_ANIME_CATEGORY = 1

# --- connexion "Emby/Jellyfin" de Sonarr/Radarr (refresh ciblé de Jellyfin) ---
#
# Entièrement déclarée ici : nom, cible réseau, mapping de chemins et
# déclencheurs. Rien de tout ça n'est propre au déploiement — `jellyfin:8096`
# est le nom de service Docker (jellyfin/docker-compose.yml) et le mapping
# découle des montages du repo (Sonarr/Radarr voient /data_root/library via leur
# mount unique, Jellyfin voit /library via le sien, voir docs/medias.md). Seule
# la clé API est un secret, donc la seule valeur en .env (JELLYFIN_API_KEY).
#
# Déclencheurs : onDownload/onUpgrade/onRename couvrent l'arrivée d'un fichier
# (le rôle d'origine de ces connexions, 2026-07-24). Les deux déclencheurs de
# suppression ont été ajoutés le 2026-08-05 : sans eux Jellyfin ne découvre la
# disparition d'un titre que par son watcher de bibliothèque, dont le
# LibraryMonitorDelay vaut 60 s — un titre supprimé restait affiché jusqu'à une
# minute dans Jellyfin, et autant dans Kodi qui réplique la bibliothèque Jellyfin
# via jellyfin-kodi (dont dépend l'addon kodi/context.clearr).
#
# Les variantes *ForUpgrade sont volontairement absentes : un remplacement est
# déjà annoncé par onUpgrade, les activer n'ajouterait qu'un aller-retour
# "retiré puis rajouté" côté Jellyfin à chaque upgrade. Comme pour les profils
# anime, cette liste FAIT AUTORITÉ : tout autre déclencheur est remis à False.
JELLYFIN_IMPLEMENTATION = "MediaBrowser"
JELLYFIN_CONFIG_CONTRACT = "MediaBrowserSettings"
JELLYFIN_CONNECTION_NAME = "Jellyfin"
JELLYFIN_FIELDS = {
    "host": "jellyfin",
    "port": 8096,
    "useSsl": False,
    "urlBase": "",
    # notify=False : pas de notification à l'écran des clients Jellyfin, on ne
    # veut que le rafraîchissement de bibliothèque.
    "notify": False,
    "updateLibrary": True,
    "mapFrom": "/data_root/library",
    "mapTo": "/library",
}
JELLYFIN_COMMON_TRIGGERS = ("onDownload", "onUpgrade", "onRename")
SONARR_JELLYFIN_TRIGGERS = JELLYFIN_COMMON_TRIGGERS + ("onSeriesDelete", "onEpisodeFileDelete")
RADARR_JELLYFIN_TRIGGERS = JELLYFIN_COMMON_TRIGGERS + ("onMovieDelete", "onMovieFileDelete")

# Metadata writer "Kodi (XBMC) / Emby" (XbmcMetadata) activé sur les deux arr,
# ajouté le 2026-08-06 : Jellyfin n'apprend JAMAIS les ids externes de
# Sonarr/Radarr (la connexion ci-dessus ne signale qu'un dossier à rescanner),
# il réidentifie chaque titre lui-même à partir du nom de dossier + année via
# une recherche TMDB — et se trompe quand un autre titre de la même année est
# plus populaire. Deux cas réels constatés ce jour-là : "Dead Man (1995)"
# (Jarmusch) identifié comme "Dead Man Walking" (Tim Robbins, même année), et
# "One Piece" (dossier sans année) comme la série live-action Netflix de 2023 au
# lieu de l'anime de 1999. Le .nfo écrit à côté du fichier porte les uniqueid
# imdb/tmdb/tvdb, et les bibliothèques Jellyfin ont "Nfo" en tête de leur
# LocalMetadataReaderOrder : l'identification devient déterministe.
# Conséquence collatérale, c'est aussi ce qui rendait un tel titre
# insupprimable depuis Kodi (kodi/context.clearr envoie les ids externes vus
# par Jellyfin — un id faux ne matche aucun titre arr, et le repli par chemin
# s'interdit library/, voir CLAUDE.md).
# Contrepartie du .nfo, à connaître : il fait autorité sur le TITRE AFFICHÉ par
# Jellyfin, qui n'utilise donc plus la traduction TMDB (constaté le 2026-08-06,
# "Les Simpson" devenu "The Simpsons" au premier rafraîchissement). D'où
# movieMetadataLanguage=2 (French) sur Radarr — bibliothèque regardée en
# français. Sonarr n'a AUCUN champ équivalent : ses tvshow.nfo restent au titre
# TVDB, donc les séries s'affichent en anglais, écart accepté explicitement (le
# seul autre moyen serait de renoncer aux .nfo, donc au correctif
# d'identification ci-dessus — Jellyfin n'offre pas d'ignorer le seul champ
# <title> d'un .nfo).
# Images volontairement désactivées (jaquettes/fanarts déjà téléchargés par
# Jellyfin dans son propre cache) : on ne veut que les identifiants dans
# library/, pas des fichiers image dupliqués à côté de chaque vidéo.
# Déclaratif comme les profils anime : le writer est activé et ses champs
# alignés, tout champ image est remis à False.
# Les .nfo ne sont écrits qu'à l'import (ou sur rescan) — après un premier
# passage, rattraper la bibliothèque existante avec les commandes
# `RescanMovie`/`RescanSeries` (sans argument = tous les titres).
XBMC_METADATA_IMPLEMENTATION = "XbmcMetadata"
XBMC_METADATA_IMAGE_FIELDS = ("movieImages", "seriesImages", "seasonImages",
                              "episodeImages", "episodeImageThumb")
SONARR_XBMC_METADATA_FIELDS = {"seriesMetadata": True, "episodeMetadata": True}
RADARR_XBMC_METADATA_FIELDS = {"movieMetadata": True, "movieMetadataLanguage": 2}

class MissingIntegration(Exception):
    """Intégration documentée comme optionnelle et absente de ce déploiement :
    ni une correction à signaler, ni une erreur à faire échouer le script."""


def load_env_file(path):
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


# curl -s sort 0 quel que soit le code HTTP : sans -w, une réponse 401/500 est
# indiscernable d'un succès. On demande donc le code sur une dernière ligne et
# on le sépare du corps — même mécanisme que scripts/provision.py, dont
# l'asymétrie avec ce fichier était le vrai défaut (là-bas le code était lu,
# ici non).
CURL_WRITE_OUT = "\n%{http_code}"


def _curl(container, api_key, args, stdin=None):
    """Renvoie (code_http, corps). Lève si docker exec lui-même échoue.

    La clé API passe par STDIN, jamais en argument : l'argv d'un `docker exec`
    est lisible dans `ps` par n'importe quel processus local, et /proc n'est pas
    monté avec hidepid ici. Ces scripts tournent par cron toutes les 5 min pour
    certains, la fenêtre d'exposition n'était donc pas théorique.

    Le shell lit la clé sur la première ligne (`read k`), curl consomme le reste
    du flux pour le corps via `--data @-` — un seul canal pour les deux, sans
    fichier temporaire. `IFS= read -r` pour ne rien réinterpréter dans la clé.
    """
    quoted = " ".join(shlex.quote(a) for a in args)
    script = f'IFS= read -r k; exec curl -s -w {shlex.quote(CURL_WRITE_OUT)} -H "X-Api-Key: $k" {quoted}'
    payload = api_key.encode() + b"\n" + (stdin or b"")
    res = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                         input=payload, capture_output=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")
    body, _, code = res.stdout.decode().rpartition("\n")
    return code.strip(), body.strip()


def api_get(container, base_url, api_key, path):
    code, body = _curl(container, api_key, [f"{base_url}{path}"])
    if not code.startswith("2"):
        # Sans ce test, une réponse d'erreur Servarr — un JSON valide du type
        # {"message": "Unauthorized"} — était rendue telle quelle aux appelants,
        # qui attendent une liste : on itérait alors sur les CLÉS du dict et le
        # diagnostic remonté à l'utilisateur était un « string indices must be
        # integers » au lieu de « clé API refusée ».
        raise RuntimeError(f"GET {path} : HTTP {code} — {body[:200] or 'réponse vide'}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"GET {path} : réponse illisible — {body[:200]!r}")


def api_write(container, base_url, api_key, method, path, obj):
    code, body = _curl(container, api_key,
                       ["-X", method, "-H", "Content-Type: application/json",
                        "--data", "@-", f"{base_url}{path}"],
                       stdin=json.dumps(obj).encode())
    # Le code HTTP fait foi. L'ancien critère — « un objet JSON avec un id » —
    # traitait tout corps VIDE comme un succès, et avalait donc le 202 Accepted
    # que renvoie justement /qualitydefinition/update, plus les 204 et tout
    # 4xx/5xx sans corps. Le script se déclarait alors satisfait d'une écriture
    # qui n'avait pas eu lieu : un faux négatif silencieux, exactement ce que
    # settle() avait été écrit pour fermer une couche plus haut.
    if not code.startswith("2"):
        raise RuntimeError(f"{method} {path} : HTTP {code} — {body[:300] or 'réponse vide'}")
    if not body:
        return None
    parsed = json.loads(body)
    if isinstance(parsed, dict) and "id" in parsed:
        return parsed
    # 2xx mais corps inattendu : Sonarr répond parfois 200 avec une liste
    # d'erreurs de validation, qu'on remonte telle quelle.
    raise RuntimeError(f"{method} {path} refusé par l'API : {body[:300]}")


def api_put(container, base_url, api_key, path, obj):
    api_write(container, base_url, api_key, "PUT", path, obj)


class StepFailed(RuntimeError):
    """Échec d'une étape, porteur des corrections qu'elle a DÉJÀ écrites.

    Une exception ordinaire ne transporte que le message : tout `changed +=
    f(...)` dont `f` lève perd les lignes que `f` avait accumulées avant de
    lever. Constaté sur deux formes — une boucle d'indexeurs dont le 1er PUT
    passe et le 2e échoue (le 1er n'était jamais rapporté, les suivants jamais
    traités), et l'étape Radarr qui enchaîne tailles puis langue (tailles
    écrites, langue en erreur, tailles absentes du rapport). L'exploitant lisait
    alors « rien corrigé » sur un état qui avait bel et bien changé.
    """

    def __init__(self, message, changed):
        super().__init__(message)
        self.changed = changed


class SettleFailed(StepFailed):
    """Échec d'une étape sous settle() — mêmes garanties que StepFailed, pour
    les corrections cumulées sur toutes les passes."""


def step_result(changed, errors):
    """Fin commune des étapes qui traitent plusieurs éléments indépendants
    (indexeurs, paliers, profils…) : un élément en échec est noté et la boucle
    continue, puis l'étape lève à la fin si besoin — avec ce qu'elle a écrit.
    Un tracker en 520 au moment du PUT d'un indexeur ne doit pas laisser les
    indexeurs suivants en dérive."""
    if errors:
        raise StepFailed(" ; ".join(errors), changed)
    return changed


def run_all(*steps):
    """Enchaîne des étapes indépendantes en cumulant leurs corrections, même
    quand l'une d'elles échoue : sert à regrouper sous un même settle() des
    réglages qui dérivent ensemble (Radarr : tailles de palier + langue)."""
    changed, errors = [], []
    for step in steps:
        try:
            changed += step()
        except StepFailed as e:
            changed += e.changed
            errors.append(str(e))
        except Exception as e:
            errors.append(str(e))
    return step_result(changed, errors)


def _dedupe(lines):
    """Ordre de première apparition conservé — settle() rejoue les mêmes
    corrections quand ça ne converge pas, et six lignes identiques dans le
    rapport disent moins que la même ligne suivie de l'erreur de non-convergence.
    """
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def settle(step):
    """Rejoue `step` jusqu'à SETTLE_CLEAN_PASSES passes consécutives sans rien à
    corriger — la seule façon de distinguer « rien ne dérive » d'une lecture
    faite trop tôt, avant qu'une écriture asynchrone n'atterrisse (voir le
    commentaire de SETTLE_CLEAN_PASSES).

    Une passe qui corrige quelque chose remet le compteur à zéro : si une
    écriture atterrit en deux temps, on repart pour un tour au lieu de conclure
    sur la première accalmie. Les corrections de toutes les passes sont cumulées, donc
    une valeur rattrapée au 3e tour apparaît bien dans le rapport."""
    changed = []
    clean = 0
    for attempt in range(SETTLE_ATTEMPTS):
        if attempt:
            time.sleep(SETTLE_DELAY_SECONDS)
        try:
            applied = step()
        except Exception as e:
            # Les passes précédentes ont déjà ÉCRIT côté arr : les perdre avec
            # l'exception ferait rapporter « rien corrigé » alors que des valeurs
            # ont bel et bien changé, et l'exploitant croirait l'état intact.
            # Idem pour ce que CETTE passe a écrit avant de lever (StepFailed).
            partial = e.changed if isinstance(e, StepFailed) else []
            raise SettleFailed(str(e), _dedupe(changed + partial)) from e
        if applied:
            changed += applied
            clean = 0
        else:
            clean += 1
            if clean >= SETTLE_CLEAN_PASSES:
                return _dedupe(changed)
    # Épuisement des tentatives sans jamais obtenir SETTLE_CLEAN_PASSES passes
    # propres : quelque chose réécrit en continu, ou nos écritures sont refusées
    # sans qu'on s'en aperçoive. L'ancienne version sortait ici par le bas, sans
    # rien distinguer d'une convergence réussie — six lignes « corrigé »
    # identiques, exit 0, marqueur cron écrit, carte du dashboard au vert, et la
    # dérive installée pour 24 h. C'est le faux négatif que ce mécanisme devait
    # justement supprimer, reproduit un cran plus haut.
    raise SettleFailed(
        f"non stabilisé après {SETTLE_ATTEMPTS} passes : les mêmes corrections sont "
        "rejouées en boucle — écritures refusées, ou un tiers réécrit en continu",
        _dedupe(changed))


def apply_quality_sizes(label, container, base_url, api_key, overrides):
    changed, errors = [], []
    for definition in api_get(container, base_url, api_key, "/qualitydefinition"):
        name = definition["quality"]["name"]
        if name not in overrides:
            continue
        wanted = overrides[name]
        current = {k: definition.get(k) for k in wanted}
        if current == wanted:
            continue
        definition.update(wanted)
        try:
            api_put(container, base_url, api_key, f"/qualitydefinition/{definition['id']}", definition)
        except Exception as e:
            errors.append(f"palier {name} : {e}")
            continue
        changed.append(f"{label} {name}: {current} -> {wanted}")
    return step_result(changed, errors)


# Réglages de /config/mediamanagement à maintenir sur les deux arr.
#
# copyUsingHardlinks est la valeur par défaut du produit, donc une installation
# neuve la retrouve — mais RIEN ne la protégeait d'un basculement dans l'UI ni
# d'un changement de défaut en amont. La repasser à false rejouerait
# silencieusement le bug de juillet : chaque import redevient une copie
# complète au lieu d'un hardlink, ~185 Go regagnés à l'époque en le corrigeant,
# et aucune erreur nulle part — la seule manifestation est le disque qui se
# remplit deux fois plus vite. C'est exactement le genre de réglage qu'un
# script déclaratif doit tenir.
#
# downloadPropersAndRepacks: doNotPrefer (2026-10-01, recommandation TRaSH).
# Le défaut `preferAndUpgrade` fait passer un repack AVANT le score de custom
# format : Sonarr a grabé `Tomb Raider King S01E12 REPACK … H.264` (CF 5) sur un
# fichier x265 déjà en place (CF 10), puis en a refusé l'import (« Not a Custom
# Format upgrade ») — téléchargement pour rien, bloqué dans la file sans limite.
# Depuis le 2026-10-07 (upgradeAllowed: false partout), un repack ne remplace
# de toute façon jamais un fichier en place ; le réglage reste comme garde.
MEDIA_MANAGEMENT_OVERRIDES = {"copyUsingHardlinks": True,
                              "downloadPropersAndRepacks": "doNotPrefer"}

# --- renommage des fichiers à l'import --------------------------------------
# Activé le 2026-09-07, les deux arr laissaient jusque-là le nom brut de la
# release. Jellyfin résout la saison d'un épisode par le SxxExx du NOM DE
# FICHIER, qui prime sur le dossier `Season NN` : une release nommée par son
# groupe en saison 1 + numérotation absolue (« One Piece S01E1172 … », pratique
# de Tsundere-Raws) atterrissait dans une saison 1 fantôme côté Jellyfin, donc
# côté Kodi, alors que Sonarr l'avait correctement rangée en S23E17 et que son
# `.nfo` disait bien `<season>23</season>` — un NFO ne peut plus déplacer un
# item entre saisons, celle-ci est figée à la résolution du chemin.
#
# **Ces deux champs n'agissent qu'à l'import.** Les fichiers déjà en place ne
# bougent pas, et c'est voulu : le rattrapage rétroactif (`RenameFiles`) a été
# écarté après mesure sur les 390 episodefiles du 2026-09-07 —
#   - Jellyfin identifie ses items par chemin, donc un renommage = ancien item
#     supprimé + nouveau créé, le `UserData` restant attaché à l'ancien id :
#     62 épisodes marqués vus et 5 positions de reprise seraient perdus, Kodi
#     compris (sa table vient de jellyfin-kodi).
#   - 49 des 390 fichiers n'ont pas de `sceneName` (imports manuels). Or un CF
#     `ReleaseTitleSpecification` est réévalué APRÈS import sur `sceneName`
#     s'il existe, sinon sur le nom de fichier : les renommer au format en
#     place, qui ne porte aucun token de langue, leur retirerait
#     `FRENCH`/`VOSTFR`/`MULTi`. 16 passaient alors sous le `cutoffFormatScore`
#     de leur profil, donc remis en recherche au prochain RSS sync (sans
#     upgrade depuis le 2026-10-07, plus de regrab, mais le score affiché
#     resterait faux). Le cas One Piece a donc été
#     corrigé à la main, en gardant `VOSTFR` dans le nouveau nom (score
#     inchangé, 50).
#
# Corollaire : NE PAS ajouter le format de nommage à ces overrides sans y
# reporter d'abord le token de langue (`{MediaInfo AudioLanguages}` ou
# `{Custom Formats}`). Le format en place ne porte ni langue ni groupe de
# release ; le versionner tel quel inviterait au rattrapage écarté ci-dessus.
# Pour les imports à venir la question ne se pose pas : une release grabée
# depuis un indexeur a toujours son `sceneName`, le renommage est donc neutre
# sur son score.
SONARR_NAMING_OVERRIDES = {"renameEpisodes": True}
RADARR_NAMING_OVERRIDES = {"renameMovies": True}

# Section /config/host des trois arr : de quoi rendre à
# `authenticationRequired: disabledForLocalAddresses` le comportement que son
# nom annonce quand on est derrière un reverse proxy (constaté le 2026-09-17,
# les trois arr demandaient un login depuis le LAN).
#
# `trustedNetworks` n'est PAS « les adresses tenues pour locales », malgré ce
# que le nom suggère : c'est la liste des PROXIES dont l'en-tête
# `X-Forwarded-For` est cru. Il faut donc y mettre le réseau Docker d'où
# Traefik parle, surtout pas le LAN. Mesuré sur Sonarr en injectant l'en-tête
# à la main :
#
#   trustedNetworks vide, ou = LAN     XFF 192.168.0.50 -> 302 /login
#   trustedNetworks = 172.16.0.0/12    XFF 192.168.0.50 -> 200
#                                      XFF 10.9.9.9     -> 200
#                                      XFF 203.0.113.7  -> 302 /login
#
# Tant que le proxy n'est pas reconnu, la seule présence d'un `X-Forwarded-For`
# suffit à faire exiger l'authentification — garde délibérée, une requête
# relayée par un inconnu ne peut pas être dite locale. Une fois le proxy
# reconnu, c'est l'IP transmise qui décide, et `disabledForLocalAddresses`
# accepte toute adresse RFC1918, pas seulement le LAN : y mettre `LAN_CIDR` ne
# restreint donc rien et ne ferait que laisser croire le contraire.
#
# Deux propriétés vérifiées qui font tenir l'ensemble :
#   - la restriction au LAN reste assurée par l'`ipAllowList` de Traefik, pas
#     ici. Pendant une fenêtre `make switch-lan-only-middleware`, un visiteur
#     WAN arrive avec une IP publique et se voit donc demander un mot de passe.
#   - un `X-Forwarded-For` forgé par le client ne contourne rien : Traefik
#     ajoute l'IP réelle en dernier et c'est celle-là qui est retenue (testé
#     depuis le LAN avec `XFF: 203.0.113.7`, en-tête forgé sans effet).
#
# Le subnet est relu à chaque exécution (voir trusted_proxy_networks) plutôt
# qu'écrit en dur : Docker le réattribue à chaque recréation du réseau, et une
# valeur figée ferait silencieusement revenir la panne ci-dessus — le cron
# quotidien rattrape le changement tout seul.
TRAEFIK_NETWORK = "traefik-public"
# Repli si le réseau est introuvable : la plage où Docker pioche par défaut.
# Ni une valeur vide (le PUT partirait en 400) ni un subnet deviné ne seraient
# préférables — celle-ci est juste dans le cas courant, et fausse exactement
# dans le cas qu'un `default-address-pools` maison aurait déjà rendu visible.
TRUSTED_PROXY_FALLBACK = "172.16.0.0/12"


def trusted_proxy_networks():
    """Subnets du réseau par lequel Traefik joint les arr, pour `trustedNetworks`.

    IPv6 compris s'il y en a un : `IPAM.Config` porte les deux familles, et en
    omettre une reviendrait à ne pas reconnaître le proxy sur cette
    famille-là — soit le login qui revient, pour un client v6 seulement.

    Renvoie (subnets, résolu) : le drapeau distingue le repli d'un réseau
    réellement inspecté, qu'une simple comparaison à TRUSTED_PROXY_FALLBACK
    confondrait le jour où les deux coïncident.
    """
    res = subprocess.run(["docker", "network", "inspect", TRAEFIK_NETWORK,
                          "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}"],
                         capture_output=True, text=True, timeout=15)
    subnets = res.stdout.split() if res.returncode == 0 else []
    if not subnets:
        return TRUSTED_PROXY_FALLBACK, False
    return ",".join(subnets), True


# `authenticationRequired: enabled` (2026-09-29) : login exigé MÊME depuis une
# adresse locale. `disabledForLocalAddresses` tenait toute IP RFC1918 pour
# locale, y compris celle d'un conteneur Docker : jellyfin, seerr et
# nextcloud-web — les services exposés au WAN — partagent traefik-public avec
# les arr et lisaient sans authentification /initialize.json, qui renvoie la
# clé API en clair (200 mesuré depuis jellyfin). Contrepartie acceptée : un
# login par navigateur sur le LAN. Les appelants programmatiques (Seerr,
# cross-seed, Prowlarr -> applications, clearr, ce script) passent par la clé
# API et ne sont pas concernés. `trustedNetworks` reste utile : il fait
# journaliser l'IP réelle du client plutôt que celle de Traefik.
#
# `allowedHosts` vient avec : les Servarr REFUSENT le PUT (400, "Allowed Hosts
# is required when 'Authentication Required' is not 'Enabled'") tant que ce
# champ est vide — c'est leur garde anti-DNS-rebinding, qui devient obligatoire
# dès qu'on rouvre l'accès sans mot de passe. Les noms ne sont pas décoratifs,
# chacun correspond à un appelant réel :
#   <service>.<domaine>  le navigateur, via Traefik (seul chemin exposé — les
#                        ports des arr ne sont publiés sur aucune interface) ;
#   <service>            Prowlarr -> applications, cross-seed et Seerr, qui se
#                        parlent par le nom de service Docker ;
#   localhost/127.0.0.1  le healthcheck du compose file et les `docker exec` de
#                        ce script comme de provision.py.
# En oublier un ne casse pas l'arr mais l'un de ces appelants, silencieusement
# et seulement au prochain sync — d'où la liste explicite plutôt qu'un `*`.
def host_overrides(networks, domain, service):
    return {
        "authenticationRequired": "enabled",
        "trustedNetworks": networks,
        "allowedHosts": f"{service}.{domain},{service},localhost,127.0.0.1",
    }


def apply_config_overrides(label, container, base_url, api_key, section, overrides):
    """Aligne quelques champs d'une section /config/<section> d'un arr.

    Un seul gabarit pour toutes ces sections (`mediamanagement`, `naming`) :
    elles ont la même forme — un objet unique porteur d'un `id`, qu'on relit,
    compare champ à champ et repasse entier au PUT. Une fonction par section
    aurait recopié ces six lignes à l'identique.
    """
    path = f"/config/{section}"
    config = api_get(container, base_url, api_key, path)
    # Même leçon que _arr_covered_paths dans clearr : une réponse d'erreur
    # Servarr est un JSON valide ({"message": ...}), donc isinstance ne suffit
    # pas — on exige le champ qu'on va réellement utiliser.
    if not isinstance(config, dict) or "id" not in config:
        raise RuntimeError(f"{label}: {path} n'a pas renvoyé un objet exploitable")
    current = {k: config.get(k) for k in overrides}
    if current == overrides:
        return []
    config.update(overrides)
    api_put(container, base_url, api_key, f"{path}/{config['id']}", config)
    return [f"{label} {section}: {current} -> {overrides}"]


def set_field(body, name, value):
    for field in body["fields"]:
        if field["name"] == name:
            field["value"] = value
            return
    body["fields"].append({"name": name, "value": value})


def jellyfin_body(skeleton, jellyfin_key, triggers):
    """Applique la config Jellyfin voulue sur un squelette — la connexion
    existante, ou /notification/schema pour une création (même principe que
    build_profile_body pour les profils qualité).

    `jellyfin_key` à None laisse le champ apiKey tel quel : l'API renvoie les
    champs secrets masqués en "********" et les préserve à l'écriture quand on
    les repasse ainsi (vérifié le 2026-08-05 via POST /notification/testall, les
    connexions restant valides après un PUT fait depuis un GET) — c'est ce qui
    permet de corriger des déclencheurs sans connaître la clé."""
    body = copy.deepcopy(skeleton)
    body["name"] = JELLYFIN_CONNECTION_NAME
    body["implementation"] = JELLYFIN_IMPLEMENTATION
    body["configContract"] = JELLYFIN_CONFIG_CONTRACT
    body["includeHealthWarnings"] = False
    body["tags"] = []
    for name, value in JELLYFIN_FIELDS.items():
        set_field(body, name, value)
    if jellyfin_key:
        set_field(body, "apiKey", jellyfin_key)
    # Déclaratif : la liste des déclencheurs voulus fait autorité, tout autre
    # onX booléen est explicitement remis à False (les supportsOnX, en lecture
    # seule côté API, ne commencent pas par "on" et ne sont donc pas touchés).
    for key, value in body.items():
        if key.startswith("on") and isinstance(value, bool):
            body[key] = key in triggers
    return body


def jellyfin_signature(notification):
    """apiKey exclue de la comparaison : l'API ne la révèle jamais (masquée en
    "********"), donc une clé qui aurait dérivé côté Sonarr/Radarr est
    indétectable d'ici — elle n'est réécrite qu'à la création, ou à l'occasion
    d'une écriture déclenchée par un autre champ. `POST /notification/testall`
    reste le seul moyen de vérifier qu'elle fonctionne encore."""
    fields = {f["name"]: f.get("value") for f in notification["fields"] if f["name"] != "apiKey"}
    triggers = {k: v for k, v in notification.items()
                if k.startswith("on") and isinstance(v, bool)}
    # tags et includeHealthWarnings : jellyfin_body les force, donc ils doivent
    # entrer dans la comparaison — sinon une dérive de ces deux champs (un tag
    # posé dans l'UI restreint la connexion aux seuls titres tagués) n'était
    # jamais corrigée, la signature restant identique. Normalisés (liste triée,
    # booléen) pour ne pas voir une différence de forme là où la valeur relue
    # est celle qu'on a écrite : absent et False valent la même chose.
    return (notification["name"], fields, triggers,
            sorted(notification.get("tags") or []),
            bool(notification.get("includeHealthWarnings")))


def apply_jellyfin_connection(label, container, base_url, api_key, jellyfin_key, triggers):
    """Provisionne la connexion Emby/Jellyfin de cet arr (création incluse).

    Deux niveaux de service selon que JELLYFIN_API_KEY (arr/.env) est renseignée :
    avec la clé, la connexion est créée si absente et entièrement réalignée ;
    sans la clé, une connexion existante voit quand même ses champs non secrets
    et ses déclencheurs corrigés, mais une connexion absente ne peut pas être
    créée — MissingIntegration, donc une note et pas une erreur (la connexion
    est documentée comme optionnelle dans docs/installation.md : un déploiement sans
    Jellyfin ne doit pas voir le cron quotidien sortir en échec)."""
    notifications = api_get(container, base_url, api_key, "/notification")
    target = next((n for n in notifications
                   if n["implementation"] == JELLYFIN_IMPLEMENTATION), None)
    if target is None:
        if not jellyfin_key:
            raise MissingIntegration(
                f"{label} : aucune connexion Jellyfin ({JELLYFIN_IMPLEMENTATION}) et "
                "JELLYFIN_API_KEY absente de arr/.env — connexion non gérée "
                "(voir arr/.env.example et docs/installation.md, étape 14)")
        schema = api_get(container, base_url, api_key, "/notification/schema")
        skeleton = next((s for s in schema
                         if s["implementation"] == JELLYFIN_IMPLEMENTATION), None)
        if skeleton is None:
            raise RuntimeError(
                f"{label} : implémentation {JELLYFIN_IMPLEMENTATION} absente de "
                "/notification/schema — nom changé côté Servarr ?")
        api_write(container, base_url, api_key, "POST", "/notification",
                  jellyfin_body(skeleton, jellyfin_key, triggers))
        return [f"{label} connexion Jellyfin créée"]

    body = jellyfin_body(target, jellyfin_key, triggers)
    if jellyfin_signature(target) == jellyfin_signature(body):
        return []
    api_put(container, base_url, api_key, f"/notification/{target['id']}", body)
    return [f"{label} connexion {target['name']!r} réalignée"]


def metadata_signature(metadata):
    return (metadata["enable"],
            {f["name"]: f.get("value") for f in metadata["fields"]})


def apply_xbmc_metadata(label, container, base_url, api_key, wanted_fields):
    """Active le metadata writer Kodi/Emby et aligne ses champs (voir
    XBMC_METADATA_IMPLEMENTATION). Le writer est toujours présent dans la liste
    des metadata consumers d'un Servarr — pas besoin de passer par un schéma de
    création, contrairement à la connexion Jellyfin."""
    consumers = api_get(container, base_url, api_key, "/metadata")
    target = next((m for m in consumers
                   if m["implementation"] == XBMC_METADATA_IMPLEMENTATION), None)
    if target is None:
        raise RuntimeError(
            f"{label} : implémentation {XBMC_METADATA_IMPLEMENTATION} absente de "
            "/metadata — nom changé côté Servarr ?")
    body = copy.deepcopy(target)
    body["enable"] = True
    for name, value in wanted_fields.items():
        set_field(body, name, value)
    for name in XBMC_METADATA_IMAGE_FIELDS:
        if any(f["name"] == name for f in body["fields"]):
            set_field(body, name, False)
    if metadata_signature(target) == metadata_signature(body):
        return []
    api_put(container, base_url, api_key, f"/metadata/{target['id']}", body)
    return [f"{label} metadata {target['name']!r} réaligné"]


def prowlarr_public_ids(prowlarr_api_key):
    """Ids Prowlarr des indexeurs marqués publics. C'est Prowlarr qui porte
    l'information (`privacy`), pas Sonarr/Radarr : leurs indexeurs synchronisés
    n'en gardent que l'URL Torznab, dont le dernier segment est justement cet
    id (voir sonarr_indexer_prowlarr_id)."""
    if not prowlarr_api_key:
        raise MissingIntegration(
            "PROWLARR_API_KEY absente de arr/.env — impossible de savoir quels indexeurs "
            "sont publics, limite de ratio non gérée (voir arr/.env.example)")
    indexers = api_get(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key, "/indexer")
    return {i["id"] for i in indexers if i.get("privacy") == PROWLARR_PUBLIC_PRIVACY}


def apply_prowlarr_seed_ratio(prowlarr_api_key, public_ids):
    """Pose PUBLIC_INDEXER_SEED_RATIO sur chaque indexeur public de Prowlarr
    lui-même. C'est l'écriture qui TIENT : en `syncLevel: fullSync`, Prowlarr
    réécrit périodiquement l'indexeur de chaque arr depuis sa propre définition,
    donc une valeur posée seulement côté arr est effacée au sync suivant (voir le
    commentaire de PUBLIC_INDEXER_SEED_RATIO).

    `forceSave=true` pour la même raison que côté arr : Prowlarr teste l'indexeur
    au moment du PUT et ces trackers répondent régulièrement 520/530."""
    changed, errors = [], []
    for indexer in api_get(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key, "/indexer"):
        if indexer["id"] not in public_ids:
            continue
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == PROWLARR_SEED_RATIO_FIELD), None)
        if current == PUBLIC_INDEXER_SEED_RATIO:
            continue
        set_field(indexer, PROWLARR_SEED_RATIO_FIELD, PUBLIC_INDEXER_SEED_RATIO)
        try:
            api_put(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key,
                    f"/indexer/{indexer['id']}?forceSave=true", indexer)
        except Exception as e:
            errors.append(f"indexeur {indexer['name']!r} : {e}")
            continue
        changed.append(f"Prowlarr indexeur public {indexer['name']!r}: "
                       f"seedRatio {current} -> {PUBLIC_INDEXER_SEED_RATIO}")
    return step_result(changed, errors)


def indexer_prowlarr_id(indexer):
    """Id Prowlarr derrière un indexeur synchronisé côté Sonarr/Radarr, lu dans
    son baseUrl (`http://prowlarr:9696/<id>/`). None pour un indexeur ajouté
    directement dans l'arr, sans Prowlarr derrière : on n'y touche pas, faute de
    savoir s'il est public."""
    base_url = next((f.get("value") for f in indexer["fields"] if f["name"] == "baseUrl"), None)
    if not base_url:
        return None
    segments = [s for s in str(base_url).split("/") if s]
    return int(segments[-1]) if segments and segments[-1].isdigit() else None


def apply_public_indexer_seed_ratio(label, container, base_url, api_key, public_ids):
    """Pose PUBLIC_INDEXER_SEED_RATIO sur chaque indexeur adossé à un indexeur
    Prowlarr public. Ne touche à rien d'autre : un indexeur privé garde ses
    critères de seed tels quels (généralement aucun, donc seed sans fin, ce qui
    est le comportement voulu là où le ratio compte).

    `forceSave=true` : Sonarr/Radarr testent la connexion à l'indexeur au moment
    du PUT, et ces trackers répondent régulièrement 520/530 (voir les rafales
    d'échecs Cloudflare dans les logs) — sans ce paramètre, une panne passagère
    du tracker suffirait à faire échouer un réalignement qui ne touche pourtant
    qu'un champ local. Le champ apiKey revient masqué en "********" du GET et est
    préservé tel quel à l'écriture, même mécanique que la connexion Jellyfin."""
    changed, errors = [], []
    for indexer in api_get(container, base_url, api_key, "/indexer"):
        if indexer_prowlarr_id(indexer) not in public_ids:
            continue
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == "seedCriteria.seedRatio"), None)
        if current == PUBLIC_INDEXER_SEED_RATIO:
            continue
        set_field(indexer, "seedCriteria.seedRatio", PUBLIC_INDEXER_SEED_RATIO)
        try:
            api_put(container, base_url, api_key,
                    f"/indexer/{indexer['id']}?forceSave=true", indexer)
        except Exception as e:
            errors.append(f"indexeur {indexer['name']!r} : {e}")
            continue
        changed.append(f"{label} indexeur public {indexer['name']!r}: "
                       f"seedRatio {current} -> {PUBLIC_INDEXER_SEED_RATIO}")
    return step_result(changed, errors)


def apply_indexer_fail_downloads(label, container, base_url, api_key):
    """Pose INDEXER_FAIL_DOWNLOADS sur chaque indexeur qui expose le champ, pour
    que l'arr traite un téléchargement contenant un exécutable ou un fichier
    « potentiellement dangereux » comme un échec plutôt que de le laisser en
    attente dans la file (voir le commentaire de INDEXER_FAIL_DOWNLOADS).

    Le champ n'existe que sur les implémentations Torznab/Newznab : un indexeur
    qui ne le déclare pas est sauté, jamais complété — `set_field` ajouterait
    sinon une clé inconnue au corps envoyé à l'API.

    `forceSave=true` pour la même raison que les passes seedRatio : l'arr teste
    la connexion à l'indexeur au moment du PUT et ces trackers répondent
    régulièrement 520/530."""
    changed, errors = [], []
    for indexer in api_get(container, base_url, api_key, "/indexer"):
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == INDEXER_FAIL_DOWNLOADS_FIELD), None)
        if current is None:
            continue                  # implémentation sans ce champ
        if sorted(current) == INDEXER_FAIL_DOWNLOADS:
            continue
        set_field(indexer, INDEXER_FAIL_DOWNLOADS_FIELD, INDEXER_FAIL_DOWNLOADS)
        try:
            api_put(container, base_url, api_key,
                    f"/indexer/{indexer['id']}?forceSave=true", indexer)
        except Exception as e:
            errors.append(f"indexeur {indexer['name']!r} : {e}")
            continue
        changed.append(f"{label} indexeur {indexer['name']!r}: "
                       f"failDownloads {current} -> {INDEXER_FAIL_DOWNLOADS}")
    return step_result(changed, errors)


def apply_prowlarr_nyaa_category(prowlarr_api_key):
    """Restreint l'indexeur Nyaa.si de Prowlarr à sa catégorie Anime. Rattaché
    par `definitionFile`, donc silencieux (et sans erreur) sur un déploiement
    qui n'a pas cet indexeur — c'est un réglage propre à cette définition
    Cardigann, pas une règle générale sur les indexeurs."""
    changed, errors = [], []
    for indexer in api_get(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key, "/indexer"):
        definition = next((f.get("value") for f in indexer["fields"]
                           if f["name"] == "definitionFile"), None)
        if definition != NYAA_DEFINITION_FILE:
            continue
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == NYAA_CATEGORY_FIELD), None)
        if current == NYAA_ANIME_CATEGORY:
            continue
        set_field(indexer, NYAA_CATEGORY_FIELD, NYAA_ANIME_CATEGORY)
        try:
            api_put(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key,
                    f"/indexer/{indexer['id']}?forceSave=true", indexer)
        except Exception as e:
            errors.append(f"indexeur {indexer['name']!r} : {e}")
            continue
        changed.append(f"Prowlarr indexeur {indexer['name']!r}: "
                       f"{NYAA_CATEGORY_FIELD} {current} -> {NYAA_ANIME_CATEGORY} (Anime)")
    return step_result(changed, errors)


def spec_body(spec):
    """Un champ `fields` complet est inutile à l'écriture : l'arr ne lit que
    `name`/`value`, et tout stocker (label/helpText traduits par l'UI, ordre,
    privacy) ferait diverger le JSON versionné à chaque changement de langue
    de l'instance."""
    return {
        "name": spec["name"], "implementation": spec["implementation"],
        "negate": spec["negate"], "required": spec["required"],
        "fields": [{"name": "value", "value": spec["value"]}],
    }


def spec_signature(spec):
    """Réduit une spec (côté API ou côté JSON versionné) à ce qui nous
    intéresse pour comparer — permet une idempotence qui ne dépend pas des
    champs décoratifs renvoyés par l'API."""
    value = spec.get("value")
    if value is None:
        value = next(f["value"] for f in spec["fields"] if f["name"] == "value")
    return (spec["name"], spec["implementation"], spec["negate"], spec["required"], value)


def apply_custom_formats(label, container, base_url, api_key, wanted_formats):
    changed, errors = [], []
    existing = {cf["name"]: cf for cf in api_get(container, base_url, api_key, "/customformat")}
    for wanted in wanted_formats:
        name = wanted["name"]
        body = {"name": name, "includeCustomFormatWhenRenaming": False,
                "specifications": [spec_body(s) for s in wanted["specifications"]]}
        current = existing.get(name)
        try:
            if current is None:
                api_write(container, base_url, api_key, "POST", "/customformat", body)
                changed.append(f"{label} custom format {name!r} créé")
                continue
            if [spec_signature(s) for s in current["specifications"]] == \
               [spec_signature(s) for s in wanted["specifications"]]:
                continue
            body["id"] = current["id"]
            api_put(container, base_url, api_key, f"/customformat/{current['id']}", body)
        except Exception as e:
            errors.append(f"custom format {name!r} : {e}")
            continue
        changed.append(f"{label} custom format {name!r} mis à jour")
    return step_result(changed, errors)


def item_name(item):
    return item["name"] if item.get("quality") is None else item["quality"]["name"]


def flatten_qualities(items):
    """{nom: objet quality} de toutes les qualités d'un profil, groupes dépliés,
    dans l'ordre du profil (du moins bon au meilleur)."""
    out = {}
    for item in items:
        if item.get("quality") is not None:
            out[item["quality"]["name"]] = item["quality"]
        out.update(flatten_qualities(item.get("items") or []))
    return out


# Id du groupe de qualités : Sonarr/Radarr réservent les ids ≥ 1000 aux groupes.
QUALITY_GROUP_ID = 1000
# Langues de profil Radarr connues (Sonarr v4 n'a plus ce champ).
PROFILE_LANGUAGES = {"Any": -1, "Original": -2}


def build_profile_body(skeleton, wanted, format_ids):
    """Applique la config voulue sur un squelette — le profil existant, ou
    /qualityprofile/schema pour une création. Qualités et custom formats sont
    désignés par NOM dans le JSON versionné : leurs ids sont propres à chaque
    instance.

    Toutes les qualités autorisées vont dans UN SEUL groupe. Sonarr/Radarr
    classent les releases par qualité AVANT le score de custom format : avec
    des paliers séparés, une VOSTFR 2160p battrait toujours une MULTi 1080p.
    Dans un groupe, les qualités sont à égalité et c'est le score qui
    départage — d'où l'ordre langue > résolution > codec > HDR, porté par les
    ordres de grandeur des scores (milliers, centaines, dizaines, unités).

    Jamais d'upgrade (règle « regrab minimum », cutoff au minimum toléré) : le
    premier grab, fait après le délai du profil de délai, est définitif.
    """
    body = copy.deepcopy(skeleton)
    body["name"] = wanted["name"]
    body["upgradeAllowed"] = False
    body["minFormatScore"] = wanted["minFormatScore"]
    body["cutoffFormatScore"] = wanted["minFormatScore"]
    if "minUpgradeFormatScore" in body:
        body["minUpgradeFormatScore"] = 1

    qualities = flatten_qualities(body["items"])
    group_names = wanted["group"]["qualities"]
    unknown = [n for n in group_names if n not in qualities]
    if unknown:
        raise RuntimeError(f"profil {wanted['name']!r} : qualité(s) inconnue(s) {unknown}")
    body["items"] = [{"quality": q, "items": [], "allowed": False}
                     for n, q in qualities.items() if n not in group_names]
    body["items"].append({
        "id": QUALITY_GROUP_ID, "name": wanted["group"]["name"], "allowed": True,
        "items": [{"quality": qualities[n], "items": [], "allowed": True} for n in group_names],
    })
    body["cutoff"] = QUALITY_GROUP_ID

    if "language" in wanted:
        body["language"] = {"id": PROFILE_LANGUAGES[wanted["language"]], "name": wanted["language"]}

    unknown = set(wanted["scores"]) - set(format_ids)
    if unknown:
        raise RuntimeError(f"profil {wanted['name']!r} : custom format(s) absent(s) : {sorted(unknown)}")
    body["formatItems"] = [
        {"format": fid, "name": name, "score": wanted["scores"].get(name, 0)}
        for name, fid in format_ids.items()
    ]
    return body


def profile_signature(profile):
    # L'état `allowed` des qualités À L'INTÉRIEUR d'un groupe est comparé aussi :
    # une qualité décochée dans un groupe autorisé ne matche plus rien, et sans
    # elle dans la signature elle n'était jamais rattrapée.
    return (
        profile["upgradeAllowed"], profile["cutoff"], profile["minFormatScore"],
        profile["cutoffFormatScore"], (profile.get("language") or {}).get("name"),
        [(item_name(i), i["allowed"],
          [(item_name(c), c["allowed"]) for c in i.get("items") or []])
         for i in profile["items"]],
        sorted((f["name"], f["score"]) for f in profile["formatItems"]),
    )


def apply_quality_profiles(label, container, base_url, api_key, wanted_profiles):
    changed = []
    format_ids = {cf["name"]: cf["id"]
                  for cf in api_get(container, base_url, api_key, "/customformat")}
    existing = {p["name"]: p for p in api_get(container, base_url, api_key, "/qualityprofile")}
    schema = None
    errors = []
    for wanted in wanted_profiles:
        current = existing.get(wanted["name"])
        try:
            if current is None:
                if schema is None:
                    schema = api_get(container, base_url, api_key, "/qualityprofile/schema")
                api_write(container, base_url, api_key, "POST", "/qualityprofile",
                          build_profile_body(schema, wanted, format_ids))
                changed.append(f"{label} profil {wanted['name']!r} créé")
                continue
            body = build_profile_body(current, wanted, format_ids)
            if profile_signature(current) == profile_signature(body):
                continue
            api_put(container, base_url, api_key, f"/qualityprofile/{current['id']}", body)
        except Exception as e:
            errors.append(f"profil {wanted['name']!r} : {e}")
            continue
        changed.append(f"{label} profil {wanted['name']!r} réaligné sur arr/profiles/")
    return step_result(changed, errors)


def api_delete(container, base_url, api_key, path):
    code, body = _curl(container, api_key, ["-X", "DELETE", f"{base_url}{path}"])
    if not code.startswith("2"):
        raise RuntimeError(f"DELETE {path} : HTTP {code} — {body[:300] or 'réponse vide'}")


def apply_delay_profile(label, container, base_url, api_key, wanted):
    """Un seul délai pour tout (règle du 2026-10-07 : 24 h) : le profil de délai
    par défaut (sans tag) est réaligné, et tout profil de délai à tag est
    supprimé — un tag plus court le court-circuiterait pour les séries visées.

    `bypassIfHighestQuality` doit rester à false : avec un seul groupe de
    qualités, TOUTE release est « de la meilleure qualité » et partirait sans
    attendre."""
    changed, errors = [], []
    for delay in api_get(container, base_url, api_key, "/delayprofile"):
        try:
            if delay["tags"]:
                api_delete(container, base_url, api_key, f"/delayprofile/{delay['id']}")
                changed.append(f"{label} profil de délai à tag {delay['tags']} supprimé")
            elif any(delay.get(k) != v for k, v in wanted.items()):
                api_put(container, base_url, api_key, f"/delayprofile/{delay['id']}", {**delay, **wanted})
                changed.append(f"{label} profil de délai par défaut réaligné "
                               f"({wanted['torrentDelay']} min)")
        except Exception as e:
            errors.append(f"profil de délai {delay['id']} : {e}")
    return step_result(changed, errors)


def apply_kids_profiles(label, container, base_url, api_key, config):
    """Le tag `pour-les-enfants` (posé depuis Seerr à la requête) fait passer
    la série ou le film sur la variante VF de son profil : VF obligatoire pour
    les enfants. Sens unique : retirer le tag ne repasse pas en VOSTFR, un
    profil VF choisi à la main reste."""
    tag_id = next((t["id"] for t in api_get(container, base_url, api_key, "/tag")
                   if t["label"] == config["kids_tag"]), None)
    if tag_id is None:
        return []
    by_name = {p["name"]: p["id"] for p in api_get(container, base_url, api_key, "/qualityprofile")}
    mapping = {by_name[src]: by_name[dst] for src, dst in config["kids_profiles"].items()
               if src in by_name and dst in by_name}
    kind, ids_field = ("series", "seriesIds") if label == "Sonarr" else ("movie", "movieIds")
    moves = {}
    for item in api_get(container, base_url, api_key, f"/{kind}"):
        if tag_id in item["tags"] and item["qualityProfileId"] in mapping:
            moves.setdefault(mapping[item["qualityProfileId"]], []).append(item)
    changed, errors = [], []
    names = {v: k for k, v in by_name.items()}
    for dst, items in moves.items():
        # /editor plutôt qu'un PUT par élément : on n'envoie que le profil, sans
        # renvoyer toute la série (monitoring, chemins...). Il répond par une
        # liste que api_write refuserait, d'où l'appel direct.
        try:
            code, body = _curl(container, api_key,
                               ["-X", "PUT", "-H", "Content-Type: application/json",
                                "--data", "@-", f"{base_url}/{kind}/editor"],
                               stdin=json.dumps({ids_field: [i["id"] for i in items],
                                                 "qualityProfileId": dst}).encode())
        except Exception as e:
            errors.append(f"PUT /{kind}/editor : {e}")
            continue
        if not code.startswith("2"):
            errors.append(f"PUT /{kind}/editor : HTTP {code} — {body[:300] or 'réponse vide'}")
            continue
        changed.append(f"{label} profil {names[dst]!r} (tag {config['kids_tag']}) : "
                       + ", ".join(i["title"] for i in items))
    return step_result(changed, errors)


def load_profiles_config(app):
    with open(os.path.join(PROFILES_DIR, f"{app}.json")) as f:
        config = json.load(f)
    with open(CUSTOM_FORMATS_FILE) as f:
        config["custom_formats"] = json.load(f)
    return config


def apply_profiles_config(label, container, base_url, api_key, config):
    # Les custom formats d'abord : les profils les référencent par nom et
    # échouent tant qu'ils n'existent pas. run_all et pas une séquence qui
    # s'arrête au premier échec : un CF en erreur n'empêche pas de réaligner
    # ce qui n'en dépend pas (les profils qui en dépendent lèvent une erreur
    # explicite dans build_profile_body).
    return run_all(
        lambda: apply_custom_formats(label, container, base_url, api_key, config["custom_formats"]),
        lambda: apply_quality_profiles(label, container, base_url, api_key, config["quality_profiles"]),
        lambda: apply_delay_profile(label, container, base_url, api_key, config["delay_profile"]),
        lambda: apply_kids_profiles(label, container, base_url, api_key, config))


def main():
    # Séparateur horodaté en tête de chaque exécution. La sortie est appendée
    # dans arr/apply-overrides.log par cron : sans lui, impossible d'attribuer
    # une ligne « corrigé » à un run, donc impossible de distinguer « corrigé
    # une fois puis stable » de « rejoué en boucle chaque nuit ». C'est ce qui
    # avait empêché de confirmer la non-convergence par la seule lecture du log.
    print(f"=== {datetime.datetime.now().isoformat(timespec='seconds')} ===")
    arr_env = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    sonarr_api_key = arr_env.get("SONARR_API_KEY")
    radarr_api_key = arr_env.get("RADARR_API_KEY")
    # Facultative (voir arr/.env.example) : sans elle la connexion Jellyfin
    # existante est quand même maintenue, mais pas créée si elle manque.
    jellyfin_api_key = arr_env.get("JELLYFIN_API_KEY")
    prowlarr_api_key = arr_env.get("PROWLARR_API_KEY")
    # `.env.shared` et pas arr/.env : DOMAIN y est déjà, c'est lui qui nomme les
    # routeurs Traefik des arr et donc leur `allowedHosts` (voir host_overrides).
    # Absent = étape sautée avec une note, jamais un nom d'hôte deviné : une
    # liste fausse ne casserait pas l'arr mais ses appelants, silencieusement.
    try:
        shared_env = load_env_file(os.path.join(REPO_ROOT, ".env.shared"))
    except OSError:
        shared_env = {}
    domain = shared_env.get("DOMAIN")

    changed = []
    notes = []
    errors = []

    def run(label, step):
        """Best-effort par domaine : une étape en échec n'emporte pas les
        suivantes. Les corrections qu'elle a écrites AVANT d'échouer sont
        rapportées quand même (StepFailed, SettleFailed compris) — sans ça un
        `changed += f()` qui lève faisait disparaître du rapport des écritures
        bien réelles."""
        try:
            changed.extend(step())
        except MissingIntegration as e:
            notes.append(str(e))
        except StepFailed as e:
            changed.extend(e.changed)
            errors.append(f"{label}: {e}")
        except Exception as e:
            errors.append(f"{label}: {e}")

    # Résolu une seule fois pour les deux arr : c'est la même liste d'indexeurs
    # Prowlarr derrière l'un comme l'autre. public_ids à None = liste inconnue
    # (Prowlarr injoignable ou clé absente), les deux passes sont alors sautées
    # plutôt que d'agir sur une liste vide, ce qui ne ferait rien mais laisserait
    # croire que tout est en ordre.
    public_ids = None
    try:
        public_ids = prowlarr_public_ids(prowlarr_api_key)
    except MissingIntegration as e:
        notes.append(str(e))
    except Exception as e:
        errors.append(f"Prowlarr: {e}")

    # Profils de qualité (arr/profiles/). Tailles de palier sous `settle` : leur
    # écriture est asynchrone (voir SETTLE_CLEAN_PASSES). Étapes séparées : une
    # erreur sur les profils ne doit pas empêcher les tailles d'être corrigées,
    # et inversement (best-effort par domaine, même principe que Sonarr vs Radarr).
    for label, app, container, url, key in (("Sonarr", "sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key),
                                            ("Radarr", "radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key)):
        try:
            config = load_profiles_config(app)
        except Exception as e:
            errors.append(f"{label} (profils) : arr/profiles/{app}.json illisible — {e}")
            continue
        run(label, lambda: settle(
            lambda: apply_quality_sizes(label, container, url, key, config["quality_definitions"])))
        run(f"{label} (profils)", lambda: apply_profiles_config(label, container, url, key, config))
    run("Sonarr (Jellyfin)", lambda: apply_jellyfin_connection(
        "Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key, jellyfin_api_key,
        SONARR_JELLYFIN_TRIGGERS))
    # Étape à part pour la même raison que les deux précédentes. Ne dépend
    # d'aucun service tiers : le writer est local à l'arr, contrairement à la
    # connexion Jellyfin qui a besoin d'une clé.
    run("Sonarr (metadata)", lambda: apply_xbmc_metadata(
        "Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key, SONARR_XBMC_METADATA_FIELDS))
    # Hors `settle` : seul un changement manuel dans l'UI peut faire dériver
    # /config/mediamanagement, il n'y a pas d'écriture concurrente à attendre.
    run("Sonarr (mediamanagement)", lambda: apply_config_overrides(
        "Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key, "mediamanagement",
        MEDIA_MANAGEMENT_OVERRIDES))
    # Étape à part de la précédente : deux sections de config distinctes, une
    # erreur sur l'une ne doit pas laisser l'autre en dérive.
    run("Sonarr (naming)", lambda: apply_config_overrides(
        "Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key, "naming",
        SONARR_NAMING_OVERRIDES))
    run("Radarr (Jellyfin)", lambda: apply_jellyfin_connection(
        "Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key, jellyfin_api_key,
        RADARR_JELLYFIN_TRIGGERS))
    run("Radarr (metadata)", lambda: apply_xbmc_metadata(
        "Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key, RADARR_XBMC_METADATA_FIELDS))
    run("Radarr (mediamanagement)", lambda: apply_config_overrides(
        "Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key, "mediamanagement",
        MEDIA_MANAGEMENT_OVERRIDES))
    run("Radarr (naming)", lambda: apply_config_overrides(
        "Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key, "naming",
        RADARR_NAMING_OVERRIDES))
    # Ne dépend pas de Prowlarr, contrairement aux deux passes seedRatio qui
    # suivent : le champ n'existe que côté arr et Prowlarr ne l'écrase pas
    # (voir le commentaire de INDEXER_FAIL_DOWNLOADS), donc cette passe tourne
    # même si Prowlarr est injoignable ou sa clé absente.
    for label, container, url, key in (("Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key),
                                       ("Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key)):
        run(f"{label} (failDownloads)",
            lambda: apply_indexer_fail_downloads(label, container, url, key))
    # Les trois arr ensemble, Prowlarr compris : contrairement au reste du
    # script, ce réglage n'a rien de propre au rôle de chaque instance — elles
    # sont derrière le même proxy et servies au même LAN.
    if domain:
        proxy_networks, resolved = trusted_proxy_networks()
        if not resolved:
            notes.append(f"réseau {TRAEFIK_NETWORK} introuvable : "
                         f"trustedNetworks posé sur le repli {TRUSTED_PROXY_FALLBACK}")
        for label, container, url, key in (("Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key),
                                           ("Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key),
                                           ("Prowlarr", PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key)):
            if not key:
                continue
            run(f"{label} (host)", lambda: apply_config_overrides(
                label, container, url, key, "host",
                host_overrides(proxy_networks, domain, label.lower())))
    else:
        notes.append("DOMAIN absent de .env.shared : section host laissée telle quelle")

    # Étape à part des deux passes seedRatio : elles ont besoin de la liste des
    # indexeurs publics, celle-ci non — elle se rattache par definitionFile.
    if prowlarr_api_key:
        run("Prowlarr (catégorie Nyaa.si)", lambda: apply_prowlarr_nyaa_category(prowlarr_api_key))

    # Étape à part, et par arr : le PUT d'un indexeur est le seul de ce script à
    # dépendre d'un service tiers joignable (le tracker lui-même, testé par
    # Sonarr/Radarr au moment de l'écriture même avec forceSave).
    #
    # Prowlarr AVANT les deux arr, pas après : c'est lui qui fait autorité, et
    # sauver un indexeur chez lui déclenche déjà un sync vers les applications —
    # la passe côté arr qui suit n'a alors plus rien à corriger, ce qui est le
    # signe que le réglage tient de lui-même.
    if public_ids:
        run("Prowlarr (indexeurs publics)",
            lambda: apply_prowlarr_seed_ratio(prowlarr_api_key, public_ids))
        for label, container, url, key in (("Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key),
                                           ("Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key)):
            run(f"{label} (indexeurs publics)",
                lambda: apply_public_indexer_seed_ratio(label, container, url, key, public_ids))

    for line in changed:
        print(f"corrigé: {line}")
    if not changed and not errors:
        print("déjà à jour, rien à faire")
    for note in notes:
        print(f"note: {note}")
    for err in errors:
        print(f"erreur: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
