#!/usr/bin/env python3
# Réapplique les réglages que recyclarr ne peut pas exprimer en YAML sur les
# deux profils principaux (voir arr/recyclarr/recyclarr.yml) : tailles de
# palier de "Quality Definition" (le guide TRaSH "series" côté Sonarr ne
# fixe aucun maxSize sur les paliers 2160p ; le guide "sqp-streaming" côté
# Radarr cale preferredSize quasi au max sur tous les paliers) et le champ
# `language` du profil Radarr (forcé à "Original" par le JSON du guide à
# chaque sync). recyclarr resynchronise ces valeurs à leurs défauts à
# CHAQUE `recyclarr sync` (cron interne @daily du conteneur recyclarr) —
# ce script doit donc être relancé juste après (voir scripts/crontab) pour
# que la dérive ne s'installe pas silencieusement entre deux exécutions
# manuelles.
#
# Provisionne AUSSI la connexion Emby/Jellyfin de Sonarr/Radarr — création
# incluse — à partir des constantes JELLYFIN_* plus bas et de JELLYFIN_API_KEY
# (arr/.env, seule valeur secrète du lot). Contrairement au reste, ce n'est pas
# recyclarr qui fait dériver ces réglages (il ne touche pas aux notifications) :
# ils ne vivaient nulle part dans le repo, donc rien ne les recréait sur une
# installation neuve ni ne rattrapait une modification par mégarde dans l'UI.
#
# Provisionne AUSSI la limite de ratio des indexeurs publics (voir
# PUBLIC_INDEXER_SEED_RATIO) : même motif que la connexion Jellyfin, ce réglage
# ne vivait que dans la base Sonarr et en avait silencieusement disparu.
#
# Provisionne AUSSI la config anime (arr/profiles/sonarr-anime.json) : les
# custom formats qui nous appartiennent et les 3 profils Anime (Fansub)*.
# recyclarr ne les gère pas (aucun trash_id ne les couvre), donc rien ne les
# recréerait sur une installation neuve et rien ne les rattraperait s'ils
# dérivaient — ils ne vivaient jusqu'au 2026-08-02 que dans la base Sonarr,
# récupérables par la sauvegarde restic mais pas reproductibles depuis le
# repo. Le JSON est déclaratif et fait autorité : tout custom format absent
# de `scores` est remis à 0 sur le profil concerné.
import copy
import datetime
import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANIME_CONFIG = os.path.join(REPO_ROOT, "arr", "profiles", "sonarr-anime.json")

SONARR_CONTAINER = "arr-sonarr-1"
SONARR_URL = "http://localhost:8989/api/v3"
RADARR_CONTAINER = "arr-radarr-1"
RADARR_URL = "http://localhost:7878/api/v3"
PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1"

RADARR_PROFILE_NAME = "[SQP] SQP-1 WEB (2160p)"

# --- relecture : les écritures de recyclarr atterrissent APRÈS sa sortie ------
#
# `PUT /api/v3/qualitydefinition/update` répond **202 Accepted** : Sonarr/Radarr
# mettent la mise à jour en file et l'appliquent après avoir répondu. recyclarr a
# donc déjà rendu la main quand les valeurs du guide atterrissent — et ce script,
# enchaîné juste derrière par `&&` (scripts/crontab), lisait des valeurs encore
# correctes, concluait « déjà à jour, rien à faire », et laissait la dérive
# s'installer pour 24 h, jusqu'au sync suivant qui reperdait la même course.
#
# Mesuré le 2026-08-07 en rejouant la chaîne cron à la main : PUT de recyclarr à
# 12:26:17.2 (202 Accepted, 5 ms), lecture du script à 12:26:17.4 encore aux
# bonnes valeurs, « déjà à jour » à 12:26:17.9 — et valeurs du guide bien en
# place à 12:27:10. Même comportement côté Radarr (202 à 12:26:17.0).
#
# C'est ce qui rendait le correctif du 2026-07-30 (enchaîner recyclarr et ce
# script par `&&` au lieu de deux horaires séparés) inopérant sur ces champs : la
# fenêtre n'est pas un problème d'ordonnancement mais d'écriture asynchrone,
# qu'aucun `&&` ne peut refermer.
#
# D'où une relecture des seules étapes que recyclarr fait dériver : on ne
# s'arrête qu'après SETTLE_CLEAN_PASSES passes consécutives sans rien à
# corriger, ce qui prouve qu'aucune écriture n'est encore en vol. Bornée, pour
# qu'une dérive qui se rétablirait en boucle ne fasse pas tourner le cron sans
# fin — au pire SETTLE_ATTEMPTS x SETTLE_DELAY_SECONDS d'attente.
SETTLE_CLEAN_PASSES = 2
SETTLE_ATTEMPTS = 6
SETTLE_DELAY_SECONDS = 5

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

# --- connexion "Emby/Jellyfin" de Sonarr/Radarr (refresh ciblé de Jellyfin) ---
#
# Entièrement déclarée ici : nom, cible réseau, mapping de chemins et
# déclencheurs. Rien de tout ça n'est propre au déploiement — `jellyfin:8096`
# est le nom de service Docker (jellyfin/docker-compose.yml) et le mapping
# découle des montages du repo (Sonarr/Radarr voient /data_root/library via leur
# mount unique, Jellyfin voit /library via le sien, voir ARCHITECTURE.md). Seule
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

SONARR_SIZE_OVERRIDES = {
    "WEBRip-2160p": {"maxSize": 100, "preferredSize": 85},
    "WEBDL-2160p": {"maxSize": 100, "preferredSize": 85},
}

RADARR_SIZE_OVERRIDES = {
    # maxSize WEBDL/WEBRip-1080p remonté de 45 à 50 Mo/min le 2026-08-01
    # (demandé explicitement) : seule release WEB-DL trouvée pour "Maradona
    # par Kusturica" (documentaire Kusturica, 90 min) pesait 4174 Mio =
    # 46,4 Mo/min, rejetée de peu par l'ancien plafond (45). Marge donnée
    # au-dessus de ce cas réel plutôt que collée dessus.
    "WEBDL-1080p": {"minSize": 10, "preferredSize": 30, "maxSize": 50},
    "WEBRip-1080p": {"minSize": 10, "preferredSize": 30, "maxSize": 50},
    "Bluray-1080p": {"minSize": 18, "preferredSize": 40, "maxSize": 60},
    "WEBDL-2160p": {"minSize": 25, "preferredSize": 65, "maxSize": 100},
    "WEBRip-2160p": {"minSize": 25, "preferredSize": 65, "maxSize": 100},
    "Bluray-2160p": {"minSize": 40, "preferredSize": 75, "maxSize": 120},
}


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
    """Renvoie (code_http, corps). Lève si docker exec lui-même échoue."""
    cmd = ["docker", "exec"] + (["-i"] if stdin is not None else []) + [
        container, "curl", "-s", "-w", CURL_WRITE_OUT,
        "-H", f"X-Api-Key: {api_key}"] + args
    res = subprocess.run(cmd, input=stdin, capture_output=True, timeout=15)
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


class SettleFailed(RuntimeError):
    """Échec d'une étape sous settle(), porteur des corrections DÉJÀ appliquées.

    Sans ça, une exception à la 3e passe faisait perdre ce que les deux
    premières avaient réellement écrit côté Sonarr/Radarr : le rapport final
    n'affichait aucune ligne « corrigé » alors que des paliers avaient changé.
    """

    def __init__(self, message, changed):
        super().__init__(message)
        self.changed = changed


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
    faite trop tôt, avant qu'une écriture asynchrone de recyclarr n'atterrisse
    (voir le commentaire de SETTLE_CLEAN_PASSES).

    Une passe qui corrige quelque chose remet le compteur à zéro : si recyclarr
    écrit en deux temps, on repart pour un tour au lieu de conclure sur la
    première accalmie. Les corrections de toutes les passes sont cumulées, donc
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
            raise SettleFailed(str(e), _dedupe(changed)) from e
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
    changed = []
    for definition in api_get(container, base_url, api_key, "/qualitydefinition"):
        name = definition["quality"]["name"]
        if name not in overrides:
            continue
        wanted = overrides[name]
        current = {k: definition.get(k) for k in wanted}
        if current == wanted:
            continue
        definition.update(wanted)
        api_put(container, base_url, api_key, f"/qualitydefinition/{definition['id']}", definition)
        changed.append(f"{label} {name}: {current} -> {wanted}")
    return changed


def apply_radarr_language(container, base_url, api_key, profile_name):
    profiles = api_get(container, base_url, api_key, "/qualityprofile")
    profile = next((p for p in profiles if p["name"] == profile_name), None)
    if profile is None:
        raise RuntimeError(f"profil Radarr {profile_name!r} introuvable")
    if profile["language"]["name"] == "Any":
        return []
    before = profile["language"]["name"]
    profile["language"] = {"id": -1, "name": "Any"}
    api_put(container, base_url, api_key, f"/qualityprofile/{profile['id']}", profile)
    return [f"Radarr {profile_name}: language {before} -> Any"]


# Réglages de /config/mediamanagement à maintenir sur les deux arr. Un seul
# aujourd'hui, mais celui-là porte à lui seul le fix du 2026-07-23.
#
# copyUsingHardlinks est la valeur par défaut du produit, donc une installation
# neuve la retrouve — mais RIEN ne la protégeait d'un basculement dans l'UI ni
# d'un changement de défaut en amont. La repasser à false rejouerait
# silencieusement le bug de juillet : chaque import redevient une copie
# complète au lieu d'un hardlink, ~185 Go regagnés à l'époque en le corrigeant,
# et aucune erreur nulle part — la seule manifestation est le disque qui se
# remplit deux fois plus vite. C'est exactement le genre de réglage qu'un
# script déclaratif doit tenir.
MEDIA_MANAGEMENT_OVERRIDES = {"copyUsingHardlinks": True}


def apply_media_management(label, container, base_url, api_key, overrides):
    config = api_get(container, base_url, api_key, "/config/mediamanagement")
    # Même leçon que _arr_covered_paths dans clearr : une réponse d'erreur
    # Servarr est un JSON valide ({"message": ...}), donc isinstance ne suffit
    # pas — on exige le champ qu'on va réellement utiliser.
    if not isinstance(config, dict) or "id" not in config:
        raise RuntimeError(f"{label}: /config/mediamanagement n'a pas renvoyé un objet exploitable")
    current = {k: config.get(k) for k in overrides}
    if current == overrides:
        return []
    config.update(overrides)
    api_put(container, base_url, api_key, f"/config/mediamanagement/{config['id']}", config)
    return [f"{label} mediamanagement: {current} -> {overrides}"]


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
    return (notification["name"], fields, triggers)


def apply_jellyfin_connection(label, container, base_url, api_key, jellyfin_key, triggers):
    """Provisionne la connexion Emby/Jellyfin de cet arr (création incluse).

    Deux niveaux de service selon que JELLYFIN_API_KEY (arr/.env) est renseignée :
    avec la clé, la connexion est créée si absente et entièrement réalignée ;
    sans la clé, une connexion existante voit quand même ses champs non secrets
    et ses déclencheurs corrigés, mais une connexion absente ne peut pas être
    créée — MissingIntegration, donc une note et pas une erreur (la connexion
    est documentée comme optionnelle dans README.md : un déploiement sans
    Jellyfin ne doit pas voir le cron quotidien sortir en échec)."""
    notifications = api_get(container, base_url, api_key, "/notification")
    target = next((n for n in notifications
                   if n["implementation"] == JELLYFIN_IMPLEMENTATION), None)
    if target is None:
        if not jellyfin_key:
            raise MissingIntegration(
                f"{label} : aucune connexion Jellyfin ({JELLYFIN_IMPLEMENTATION}) et "
                "JELLYFIN_API_KEY absente de arr/.env — connexion non gérée "
                "(voir arr/.env.example et README.md, étape de configuration de arr)")
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
    changed = []
    for indexer in api_get(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key, "/indexer"):
        if indexer["id"] not in public_ids:
            continue
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == PROWLARR_SEED_RATIO_FIELD), None)
        if current == PUBLIC_INDEXER_SEED_RATIO:
            continue
        set_field(indexer, PROWLARR_SEED_RATIO_FIELD, PUBLIC_INDEXER_SEED_RATIO)
        api_put(PROWLARR_CONTAINER, PROWLARR_URL, prowlarr_api_key,
                f"/indexer/{indexer['id']}?forceSave=true", indexer)
        changed.append(f"Prowlarr indexeur public {indexer['name']!r}: "
                       f"seedRatio {current} -> {PUBLIC_INDEXER_SEED_RATIO}")
    return changed


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
    changed = []
    for indexer in api_get(container, base_url, api_key, "/indexer"):
        if indexer_prowlarr_id(indexer) not in public_ids:
            continue
        current = next((f.get("value") for f in indexer["fields"]
                        if f["name"] == "seedCriteria.seedRatio"), None)
        if current == PUBLIC_INDEXER_SEED_RATIO:
            continue
        set_field(indexer, "seedCriteria.seedRatio", PUBLIC_INDEXER_SEED_RATIO)
        api_put(container, base_url, api_key,
                f"/indexer/{indexer['id']}?forceSave=true", indexer)
        changed.append(f"{label} indexeur public {indexer['name']!r}: "
                       f"seedRatio {current} -> {PUBLIC_INDEXER_SEED_RATIO}")
    return changed


def spec_body(spec):
    """Un champ `fields` complet est inutile à l'écriture : Sonarr ne lit que
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


def apply_custom_formats(container, base_url, api_key, wanted_formats):
    changed = []
    existing = {cf["name"]: cf for cf in api_get(container, base_url, api_key, "/customformat")}
    for wanted in wanted_formats:
        name = wanted["name"]
        body = {"name": name, "includeCustomFormatWhenRenaming": True,
                "specifications": [spec_body(s) for s in wanted["specifications"]]}
        current = existing.get(name)
        if current is None:
            api_write(container, base_url, api_key, "POST", "/customformat", body)
            changed.append(f"Sonarr custom format {name!r} créé")
            continue
        if [spec_signature(s) for s in current["specifications"]] == \
           [spec_signature(s) for s in wanted["specifications"]]:
            continue
        body["id"] = current["id"]
        api_put(container, base_url, api_key, f"/customformat/{current['id']}", body)
        changed.append(f"Sonarr custom format {name!r} mis à jour")
    return changed


def item_name(item):
    return item["name"] if item.get("quality") is None else item["quality"]["name"]


def build_profile_body(skeleton, wanted, format_ids):
    """Applique la config voulue sur un squelette — le profil existant, ou
    /qualityprofile/schema pour une création. Les qualités et les custom
    formats sont désignés par NOM dans le JSON versionné : leurs ids sont
    propres à chaque instance (un déploiement neuf n'aura pas les mêmes),
    c'est tout l'intérêt de ne pas figer un dump d'API brut."""
    body = copy.deepcopy(skeleton)
    body["name"] = wanted["name"]
    body["upgradeAllowed"] = wanted["upgradeAllowed"]
    body["minFormatScore"] = wanted["minFormatScore"]
    body["cutoffFormatScore"] = wanted["cutoffFormatScore"]

    allowed = set(wanted["allowed"])
    cutoff_id = None
    for item in body["items"]:
        name = item_name(item)
        item["allowed"] = name in allowed
        # Les qualités d'un groupe suivent l'état du groupe (convention
        # Sonarr : un groupe autorisé dont les enfants ne le sont pas est
        # accepté par l'API mais ne matche rien).
        for child in item.get("items") or []:
            child["allowed"] = item["allowed"]
        if name == wanted["cutoff"]:
            cutoff_id = item["id"] if item.get("quality") is None else item["quality"]["id"]
    if cutoff_id is None:
        raise RuntimeError(f"profil {wanted['name']!r} : cutoff {wanted['cutoff']!r} introuvable")
    body["cutoff"] = cutoff_id

    unknown = set(wanted["scores"]) - set(format_ids)
    if unknown:
        raise RuntimeError(
            f"profil {wanted['name']!r} : custom format(s) absent(s) de Sonarr : {sorted(unknown)} "
            "— `make recyclarr-sync` doit tourner avant ce script (voir scripts/crontab)")
    body["formatItems"] = [
        {"format": fid, "name": name, "score": wanted["scores"].get(name, 0)}
        for name, fid in format_ids.items()
    ]
    return body


def profile_signature(profile):
    return (
        profile["upgradeAllowed"], profile["cutoff"], profile["minFormatScore"],
        profile["cutoffFormatScore"],
        [(item_name(i), i["allowed"]) for i in profile["items"]],
        sorted((f["name"], f["score"]) for f in profile["formatItems"]),
    )


def apply_quality_profiles(container, base_url, api_key, wanted_profiles):
    changed = []
    format_ids = {cf["name"]: cf["id"]
                  for cf in api_get(container, base_url, api_key, "/customformat")}
    existing = {p["name"]: p for p in api_get(container, base_url, api_key, "/qualityprofile")}
    schema = None
    for wanted in wanted_profiles:
        current = existing.get(wanted["name"])
        if current is None:
            if schema is None:
                schema = api_get(container, base_url, api_key, "/qualityprofile/schema")
            api_write(container, base_url, api_key, "POST", "/qualityprofile",
                      build_profile_body(schema, wanted, format_ids))
            changed.append(f"Sonarr profil {wanted['name']!r} créé")
            continue
        body = build_profile_body(current, wanted, format_ids)
        if profile_signature(current) == profile_signature(body):
            continue
        api_put(container, base_url, api_key, f"/qualityprofile/{current['id']}", body)
        changed.append(f"Sonarr profil {wanted['name']!r} réaligné sur {os.path.basename(ANIME_CONFIG)}")
    return changed


def apply_anime_config(container, base_url, api_key):
    with open(ANIME_CONFIG) as f:
        config = json.load(f)
    # Les custom formats d'abord : les profils ci-dessous les référencent par
    # nom et échouent tant qu'ils n'existent pas.
    changed = apply_custom_formats(container, base_url, api_key, config["custom_formats"])
    changed += apply_quality_profiles(container, base_url, api_key, config["quality_profiles"])
    return changed


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

    changed = []
    notes = []
    errors = []
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

    # Sous `settle` : c'est l'étape que recyclarr fait dériver, et son écriture
    # est asynchrone (voir SETTLE_CLEAN_PASSES).
    try:
        changed += settle(
            lambda: apply_quality_sizes("Sonarr", SONARR_CONTAINER, SONARR_URL,
                                        sonarr_api_key, SONARR_SIZE_OVERRIDES))
    except SettleFailed as e:
        changed += e.changed          # ces corrections-là ont bien été écrites
        errors.append(f"Sonarr: {e}")
    except Exception as e:
        errors.append(f"Sonarr: {e}")
    # Bloc à part : une erreur sur la config anime ne doit pas empêcher les
    # tailles de palier ci-dessus d'être corrigées, et inversement
    # (best-effort par domaine, même principe que Sonarr vs Radarr).
    try:
        changed += apply_anime_config(SONARR_CONTAINER, SONARR_URL, sonarr_api_key)
    except Exception as e:
        errors.append(f"Sonarr (anime): {e}")
    try:
        changed += apply_jellyfin_connection("Sonarr", SONARR_CONTAINER, SONARR_URL,
                                             sonarr_api_key, jellyfin_api_key,
                                             SONARR_JELLYFIN_TRIGGERS)
    except MissingIntegration as e:
        notes.append(str(e))
    except Exception as e:
        errors.append(f"Sonarr (Jellyfin): {e}")
    # Bloc à part pour la même raison que les deux précédents. Ne dépend
    # d'aucun service tiers : le writer est local à l'arr, contrairement à la
    # connexion Jellyfin qui a besoin d'une clé.
    try:
        changed += apply_xbmc_metadata("Sonarr", SONARR_CONTAINER, SONARR_URL,
                                       sonarr_api_key, SONARR_XBMC_METADATA_FIELDS)
    except Exception as e:
        errors.append(f"Sonarr (metadata): {e}")
    # Hors `settle` : recyclarr ne touche pas à /config/mediamanagement, il n'y
    # a donc pas de course à perdre ici — seul un changement manuel dans l'UI
    # peut faire dériver ce réglage.
    try:
        changed += apply_media_management("Sonarr", SONARR_CONTAINER, SONARR_URL,
                                          sonarr_api_key, MEDIA_MANAGEMENT_OVERRIDES)
    except Exception as e:
        errors.append(f"Sonarr (mediamanagement): {e}")
    # Les deux réglages Radarr que recyclarr fait dériver, donc sous `settle`
    # pour la même raison que les tailles Sonarr — le champ `language` vit sur le
    # profil qualité, que recyclarr réécrit aussi.
    try:
        changed += settle(
            lambda: apply_quality_sizes("Radarr", RADARR_CONTAINER, RADARR_URL,
                                        radarr_api_key, RADARR_SIZE_OVERRIDES)
            + apply_radarr_language(RADARR_CONTAINER, RADARR_URL, radarr_api_key,
                                    RADARR_PROFILE_NAME))
    except SettleFailed as e:
        changed += e.changed
        errors.append(f"Radarr: {e}")
    except Exception as e:
        errors.append(f"Radarr: {e}")
    # Bloc à part de celui ci-dessus pour la même raison que la config anime :
    # une connexion Jellyfin absente ou en erreur ne doit pas emporter les
    # tailles de palier et le champ language de Radarr.
    try:
        changed += apply_jellyfin_connection("Radarr", RADARR_CONTAINER, RADARR_URL,
                                             radarr_api_key, jellyfin_api_key,
                                             RADARR_JELLYFIN_TRIGGERS)
    except MissingIntegration as e:
        notes.append(str(e))
    except Exception as e:
        errors.append(f"Radarr (Jellyfin): {e}")
    try:
        changed += apply_xbmc_metadata("Radarr", RADARR_CONTAINER, RADARR_URL,
                                       radarr_api_key, RADARR_XBMC_METADATA_FIELDS)
    except Exception as e:
        errors.append(f"Radarr (metadata): {e}")
    try:
        changed += apply_media_management("Radarr", RADARR_CONTAINER, RADARR_URL,
                                          radarr_api_key, MEDIA_MANAGEMENT_OVERRIDES)
    except Exception as e:
        errors.append(f"Radarr (mediamanagement): {e}")
    # Bloc à part, et par arr : le PUT d'un indexeur est le seul de ce script à
    # dépendre d'un service tiers joignable (le tracker lui-même, testé par
    # Sonarr/Radarr au moment de l'écriture même avec forceSave).
    #
    # Prowlarr AVANT les deux arr, pas après : c'est lui qui fait autorité, et
    # sauver un indexeur chez lui déclenche déjà un sync vers les applications —
    # la passe côté arr qui suit n'a alors plus rien à corriger, ce qui est le
    # signe que le réglage tient de lui-même.
    if public_ids:
        try:
            changed += apply_prowlarr_seed_ratio(prowlarr_api_key, public_ids)
        except Exception as e:
            errors.append(f"Prowlarr (indexeurs publics): {e}")
        for label, container, url, key in (("Sonarr", SONARR_CONTAINER, SONARR_URL, sonarr_api_key),
                                           ("Radarr", RADARR_CONTAINER, RADARR_URL, radarr_api_key)):
            try:
                changed += apply_public_indexer_seed_ratio(label, container, url, key, public_ids)
            except Exception as e:
                errors.append(f"{label} (indexeurs publics): {e}")

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
