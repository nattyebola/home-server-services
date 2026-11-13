#!/usr/bin/env python3
# Tient la LISTE NOIRE du custom format « Langue : MULTi (groupe non exclu) »
# (arr/profiles/custom-formats.json), qui fait compter un MULTi anime comme de
# la VF sauf si son groupe est exclu. Liste noire depuis le 2026-11-13 (liste
# blanche avant : aucun nouveau groupe ne pouvait plus prouver qu'il était bon,
# ses releases n'étant jamais téléchargées). Tout est en LECTURE SEULE :
#   - défaut : bilan par groupe, d'après les fichiers MULTi en bibliothèque
#     (pistes audio lues par l'arr, mediaInfo.audioLanguages) et une recherche
#     MULTi sur Nyaa (indice seulement, --pages lit les descriptions) ;
#   - --controle : le filet de détection, appelé par /server-report : tout
#     fichier MULTi en bibliothèque SANS audio FR, à remplacer, et dont le
#     groupe est à exclure ;
#   - --verifier : après `make arr-overrides`, CF .NET contre prédiction Python.
# La décision et l'édition du JSON restent à faire après accord (SKILL.md).
#
#   python3 .claude/skills/multi-groupes/multi_groupes.py [--pages] [--json]
#   python3 .claude/skills/multi-groupes/multi_groupes.py --controle [--tout] [--json]
#   python3 .claude/skills/multi-groupes/multi_groupes.py --verifier
import argparse
import collections
import html
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import regex  # même moteur que la validation .NET / Python des custom formats

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CF_NAME = "Langue : MULTi (groupe non exclu)"
NO_GROUP = "(sans groupe)"

FR_AUDIO = {"fre", "fra", "fr", "french"}
# Catégorie Newznab « TV/Anime », côté Prowlarr.
TV_ANIME_CATEGORY = 5070

# Pourquoi chaque groupe de la regex d'exclusion y est. Mémoire OBLIGATOIRE :
# un fichier sans FR finit remplacé et sa preuve disparaît de la bibliothèque
# (VARYG ressortait « 4 FR / 0 sans FR » le soir même). Le script signale
# toute divergence entre ce dict et la regex du CF.
EXCLUSION_NOTES = {
    "VARYG": "2026-10-07 : 9 MULTi sur 13 sans audio FR (Smoking Behind the "
             "Supermarket with You, jpn/eng/hin/tha), fichiers remplacés depuis",
    "Anime-DL": "2026-11-13 : 8 MULTi sur 8 sans audio FR, sur 3 œuvres (titres "
                "`MULTI.NO-VF`, grabés parce que `NO-VF` comptait comme VF ; "
                "ex. TOUGEN.ANKI.S02E04, audio jpn/spa)",
}

spec = importlib.util.spec_from_file_location(
    "aao", os.path.join(REPO_ROOT, "scripts", "apply-arr-overrides.py"))
aao = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aao)


def load_cf():
    """(multi, groupes_exclus, vf_explicite) : les trois specs du CF, compilées."""
    cfs = json.load(open(os.path.join(REPO_ROOT, "arr", "profiles", "custom-formats.json")))
    cf = next(c for c in cfs if c["name"] == CF_NAME)
    by_impl = {(s["implementation"], s["negate"]): s["value"] for s in cf["specifications"]}
    return (regex.compile(by_impl[("ReleaseTitleSpecification", False)], regex.I),
            regex.compile(by_impl[("ReleaseGroupSpecification", True)], regex.I),
            regex.compile(by_impl[("ReleaseTitleSpecification", True)], regex.I))


def excluded_in_regex(excluded):
    """Les noms de groupe littéraux de ^(A|B)$, pour le contrôle de cohérence."""
    m = re.fullmatch(r"\^\((.*)\)\$", excluded.pattern)
    return set(m.group(1).split("|")) if m else set()


def audio_status(media_info):
    """'fr', 'sans_fr' ou 'non_tagué' : une piste sans tag de langue peut être
    du FR (cas The Simpsons S37E11/E13), à confirmer par ffprobe."""
    mi = media_info or {}
    langs = [l for l in re.split(r"[/,\s]+", (mi.get("audioLanguages") or "").lower()) if l]
    if FR_AUDIO & set(langs):
        return "fr"
    if "und" in langs or (mi.get("audioStreamCount") or 0) > len(langs):
        return "non_tagué"
    return "sans_fr"


def library_files(env, multi):
    """Tous les fichiers en bibliothèque dont le nom porte MULTi (hors
    Multi-Subs) et dont l'arr a lu les pistes audio."""
    out = []
    sources = (("Sonarr", aao.SONARR_CONTAINER, aao.SONARR_URL, env.get("SONARR_API_KEY"), "/series", "seriesId"),
               ("Radarr", aao.RADARR_CONTAINER, aao.RADARR_URL, env.get("RADARR_API_KEY"), "/movie", "movieId"))
    for label, ct, url, key, items_path, id_param in sources:
        if not key:
            print(f"ERREUR {label} : clé d'API absente, fichiers ignorés", file=sys.stderr)
            continue
        try:
            profiles = {p["id"]: p["name"] for p in aao.api_get(ct, url, key, "/qualityprofile")}
            items = aao.api_get(ct, url, key, items_path)
        except Exception as e:
            print(f"ERREUR {label} : {e}", file=sys.stderr)
            continue
        file_path = "/episodefile" if label == "Sonarr" else "/moviefile"
        for item in items:
            for f in aao.api_get(ct, url, key, f"{file_path}?{id_param}={item['id']}"):
                name = f.get("sceneName") or os.path.basename(f.get("relativePath") or "")
                if not multi.search(name) or not (f.get("mediaInfo") or {}).get("audioLanguages"):
                    continue
                out.append({"arr": label, "oeuvre": item["title"], "annee": item.get("year"),
                            "profil": profiles.get(item.get("qualityProfileId"), "?"),
                            "groupe": f.get("releaseGroup") or NO_GROUP, "nom": name,
                            "fichier": f.get("relativePath"), "audio": f["mediaInfo"]["audioLanguages"],
                            "statut": audio_status(f.get("mediaInfo")),
                            # sous-titres FR : le fichier vaut au moins une VOSTFR (minimum du profil Anime)
                            "st_fr": bool(FR_AUDIO & set(re.split(r"[/,\s]+", (f["mediaInfo"].get("subtitles") or "").lower())))})
    return out


def by_group(files, explicit_vf):
    """{groupe: {"fr", "sans_fr", "non_tagué", "vf_explicite", "titres", "exemples_sans_fr", "noms"}}

    Un MULTi qui annonce aussi la VF (`MULTi.VFF`, `MULTi.VF2`…) ne dit rien de
    la pratique MULTi du groupe : le CF est nié sur ces titres. Avec FR, il
    n'entre pas dans « fr » (faux « À AJOUTER » de WHY7726 le 2026-11-13, du
    temps de la liste blanche). Sans FR, il compte en « sans_fr » : le groupe a
    menti sur la VF."""
    out = collections.defaultdict(lambda: {"fr": 0, "sans_fr": 0, "non_tagué": 0, "vf_explicite": 0,
                                           "titres": set(), "exemples_sans_fr": [], "noms": []})
    for f in files:
        g = out[f["groupe"]]
        # noms garde aussi les VF explicites : --verifier y teste la négation du CF
        g["noms"].append(f["nom"])
        if f["statut"] == "fr" and explicit_vf.search(f["nom"]):
            g["vf_explicite"] += 1
            continue
        g["titres"].add(f["oeuvre"])
        g[f["statut"]] += 1
        if f["statut"] != "fr":
            g["exemples_sans_fr"].append(f"{f['nom']} (audio {f['audio']})")
    return out


def sonarr_group(env, title):
    """Le groupe tel que Sonarr le parse : c'est CE nom que voit la spec
    ReleaseGroupSpecification, pas celui qu'on lirait à l'œil dans le titre."""
    q = urllib.parse.urlencode({"title": title})
    try:
        p = aao.api_get(aao.SONARR_CONTAINER, aao.SONARR_URL, env["SONARR_API_KEY"], f"/parse?{q}")
    except Exception:
        return None
    return (p.get("parsedEpisodeInfo") or {}).get("releaseGroup")


def parse_any(env, title):
    """/parse côté Sonarr puis Radarr : les custom formats ne sont calculés que
    si le titre est rattaché à une série ou un film de la bibliothèque."""
    q = urllib.parse.urlencode({"title": title})
    for ct, url, key, attached in ((aao.SONARR_CONTAINER, aao.SONARR_URL, "SONARR_API_KEY", "series"),
                                   (aao.RADARR_CONTAINER, aao.RADARR_URL, "RADARR_API_KEY", "movie")):
        try:
            p = aao.api_get(ct, url, env[key], f"/parse?{q}")
        except Exception:
            continue
        if p.get(attached):
            info = p.get("parsedEpisodeInfo") or p.get("parsedMovieInfo") or {}
            return info.get("releaseGroup"), {c["name"] for c in p.get("customFormats", [])}
    return None


def verify(env, groups, multi, excluded, explicit_vf):
    """Après `make arr-overrides` : le CF tel que l'arr le calcule (.NET) doit
    coïncider avec la prédiction Python, sur des titres réels de chaque groupe
    connu (sans groupe compris : la spec niée laisse alors passer). Un
    désaccord = groupe parsé autrement qu'on ne le croit, ou regex que les
    deux moteurs lisent différemment."""
    titles = [n for g in groups.values() for n in g["noms"][:8]]
    with ThreadPoolExecutor(6) as ex:
        parsed = list(ex.map(lambda t: (t, parse_any(env, t)), titles))
    n = hits = 0
    dis = []
    for t, p in parsed:
        if p is None:
            continue
        group, net_cfs = p
        n += 1
        py = bool(multi.search(t)) and not (group and excluded.search(group)) and not explicit_vf.search(t)
        net = CF_NAME in net_cfs
        hits += net
        if py != net:
            dis.append(f"{'.NET' if net else 'Python'} seul : {t} (groupe parsé {group!r})")
    print(f"titres rattachés : {n} | matchs .NET : {hits} | désaccords : {len(dis)}")
    for d in dis[:20]:
        print(f"  {d}")
    return 1 if dis else 0


def controle(files, excluded, as_json, tout):
    """Le filet : fichiers MULTi sans audio FR encore en bibliothèque, regroupés
    par œuvre et groupe, avec l'action qu'ils appellent :
      - « exclure le groupe » si le groupe n'est pas encore dans la regex ;
      - « remplacer » si le fichier ne vaut même pas une VOSTFR (pas de
        sous-titres FR) ou si le profil exige la VF (`… VF`).
    Un fichier sans audio FR mais sous-titré FR, d'un groupe déjà exclu, sur un
    profil qui accepte la VOSTFR, n'appelle rien (il vaut le minimum du
    profil, et sans upgrade rien ne le remplacera) : masqué sauf --tout.
    Sortie vide = rien à signaler."""
    rows = collections.OrderedDict()
    for f in sorted(files, key=lambda f: (f["oeuvre"].lower(), f["nom"])):
        if f["statut"] == "fr":
            continue
        k = (f["arr"], f["oeuvre"], f["groupe"])
        r = rows.setdefault(k, {"arr": f["arr"], "oeuvre": f["oeuvre"], "annee": f["annee"],
                                "profil": f["profil"], "groupe": f["groupe"],
                                "groupe_exclu": f["groupe"] != NO_GROUP and bool(excluded.search(f["groupe"])),
                                "sans_fr": 0, "non_tagué": 0, "sans_st_fr": 0, "audio": set(), "exemples": []})
        r[f["statut"]] += 1
        r["sans_st_fr"] += not f["st_fr"]
        r["audio"].add(f["audio"])
        r["exemples"].append(f["nom"])
    out = []
    for r in rows.values():
        actions = []
        if not r["groupe_exclu"] and r["groupe"] != NO_GROUP and r["sans_fr"]:
            actions.append("exclure le groupe")
        if r["sans_st_fr"] or r["profil"].endswith("VF"):
            actions.append("remplacer")
        if r["non_tagué"]:
            actions.append("confirmer par ffprobe (piste non taguée)")
        if actions or tout:
            out.append(dict(r, audio=sorted(r["audio"]), exemples=r["exemples"][:3], actions=actions))
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    for r in out:
        etat = "groupe déjà exclu" if r["groupe_exclu"] else "groupe NON exclu"
        detail = (f"{r['sans_fr']} sans FR" + (f", {r['non_tagué']} piste non taguée" if r["non_tagué"] else "")
                  + (f", dont {r['sans_st_fr']} sans sous-titres FR non plus" if r["sans_st_fr"] else ", sous-titres FR présents"))
        print(f"[{r['arr']}/{r['profil']}] {r['oeuvre']} ({r['annee']}) — {r['groupe']} ({etat}) : "
              f"{detail} ; audio {', '.join(r['audio'])} ; action : {', '.join(r['actions']) or 'aucune'} ; "
              f"ex. {r['exemples'][0]}")
    return 0


def nyaa_declaration(info_url):
    """Ce que l'uploader déclare dans sa description — déclaratif, jamais une
    preuve. 'FR déclaré' si une ligne audio cite le français."""
    # Séquentiel et espacé : Nyaa répond 429 dès quelques requêtes parallèles.
    for attempt in range(3):
        time.sleep(1 + 4 * attempt)
        try:
            with urllib.request.urlopen(info_url, timeout=15) as r:
                page = r.read().decode("utf-8", "replace")
            break
        except Exception as e:
            err = e
    else:
        return f"page illisible ({getattr(err, 'code', err.__class__.__name__)})"
    m = re.search(r'id="torrent-description"[^>]*>(.*?)</div>', page, re.S)
    desc = html.unescape(m.group(1)) if m else ""
    if not desc.strip() or "No description" in desc:
        return "pas de description"
    for line in desc.splitlines():
        if re.search(r"audio|🔊|langu|piste", line, re.I):
            if re.search(r"\b(FR|FRE|FRA|French|Français|VF|VFF)\b", line, re.I):
                return "FR déclaré"
            return "audio sans FR déclaré"
    return "audio non précisé"


def nyaa_candidates(env, multi, explicit_vf, excluded, pages):
    pk = env.get("PROWLARR_API_KEY")
    if not pk:
        print("note: clé Prowlarr absente, pas de candidats Nyaa", file=sys.stderr)
        return {}
    nyaa = [i for i in aao.api_get(aao.PROWLARR_CONTAINER, aao.PROWLARR_URL, pk, "/indexer")
            if i.get("definitionName") == aao.NYAA_DEFINITION_FILE]
    if not nyaa:
        return {}
    q = urllib.parse.urlencode({"query": "MULTi", "indexerIds": nyaa[0]["id"],
                                "categories": TV_ANIME_CATEGORY, "type": "search", "limit": 100})
    results = [r for r in aao.api_get(aao.PROWLARR_CONTAINER, aao.PROWLARR_URL, pk, f"/search?{q}")
               if multi.search(r["title"]) and not explicit_vf.search(r["title"])]
    with ThreadPoolExecutor(6) as ex:
        groups = list(ex.map(lambda r: sonarr_group(env, r["title"]), results))
    out = collections.defaultdict(list)
    for r, g in zip(results, groups):
        if g:
            out[g].append({"titre": r["title"], "page": r.get("infoUrl"), "declaration": None})
    if pages:
        # Deux pages par groupe non exclu suffisent pour un indice : une
        # déclaration « audio sans FR » désigne un groupe à surveiller.
        for g, items in out.items():
            if not excluded.search(g):
                for c in items[:2]:
                    c["declaration"] = nyaa_declaration(c["page"])
    return out


def verdict(group, is_excluded, lib):
    if is_excluded:
        return "exclu"
    if group == NO_GROUP:
        return "sans groupe (non excluable)" if lib["sans_fr"] else "sans groupe"
    if lib and lib["sans_fr"]:
        return "À EXCLURE"
    if lib and lib["non_tagué"]:
        return "à confirmer (piste non taguée)"
    if lib and lib["fr"]:
        return "accepté (FR constaté)"
    return "accepté (non vérifié)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", action="store_true", help="lit aussi la description des pages Nyaa (indice)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--controle", action="store_true",
                    help="filet de détection : fichiers MULTi sans audio FR en bibliothèque, puis s'arrête")
    ap.add_argument("--tout", action="store_true",
                    help="avec --controle : liste aussi les fichiers qui n'appellent aucune action")
    ap.add_argument("--verifier", action="store_true",
                    help="compare le CF calculé par l'arr (.NET) à la prédiction Python, puis s'arrête")
    args = ap.parse_args()
    env = aao.load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    multi, excluded, explicit_vf = load_cf()
    in_regex = excluded_in_regex(excluded)
    if in_regex != set(EXCLUSION_NOTES):
        print(f"ERREUR cohérence : regex d'exclusion {sorted(in_regex)} ≠ EXCLUSION_NOTES "
              f"{sorted(EXCLUSION_NOTES)}", file=sys.stderr)
    files = library_files(env, multi)
    if args.controle:
        return controle(files, excluded, args.json, args.tout)
    groups = by_group(files, explicit_vf)
    if args.verifier:
        return verify(env, groups, multi, excluded, explicit_vf)
    cand = nyaa_candidates(env, multi, explicit_vf, excluded, args.pages)
    rows = []
    for g in sorted(set(groups) | set(cand), key=str.lower):
        is_excl = g != NO_GROUP and bool(excluded.search(g))
        l = groups.get(g)
        rows.append({
            "groupe": g, "exclu": is_excl, "verdict": verdict(g, is_excl, l),
            "fichiers_fr": l["fr"] if l else 0, "fichiers_sans_fr": l["sans_fr"] if l else 0,
            "fichiers_non_tagués": l["non_tagué"] if l else 0,
            "fichiers_vf_explicite": l["vf_explicite"] if l else 0,
            "oeuvres": sorted(l["titres"]) if l else [],
            "exemples_sans_fr": l["exemples_sans_fr"][:3] if l else [],
            "nyaa_recent": len(cand.get(g, [])),
            "nyaa_declarations": dict(collections.Counter(c["declaration"] for c in cand.get(g, []) if c["declaration"])),
            "nyaa_exemple": (cand.get(g) or [{}])[0].get("titre"),
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"groupes exclus : {excluded.pattern}\n")
    for r in rows:
        print(f"[{r['verdict']}] {r['groupe']} — "
              f"fichiers {r['fichiers_fr']} FR / {r['fichiers_sans_fr']} sans FR"
              + (f" / {r['fichiers_non_tagués']} non tagués" if r["fichiers_non_tagués"] else "")
              + f" sur {len(r['oeuvres'])} œuvre(s) ; Nyaa récent : {r['nyaa_recent']} {r['nyaa_declarations'] or ''}"
              + (f" ; {r['fichiers_vf_explicite']} MULTi+VF explicite non comptés" if r["fichiers_vf_explicite"] else ""))
        if r["groupe"] in EXCLUSION_NOTES:
            print(f"    exclusion : {EXCLUSION_NOTES[r['groupe']]}")
        for e in r["exemples_sans_fr"]:
            print(f"    sans FR : {e}")


if __name__ == "__main__":
    sys.exit(main())
