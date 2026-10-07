#!/usr/bin/env python3
# Collecte, en LECTURE SEULE, les preuves pour tenir la liste blanche du custom
# format « Langue : MULTi (groupe vérifié) » (arr/profiles/custom-formats.json) :
#   - preuve forte : les fichiers MULTi déjà en bibliothèque (Sonarr + Radarr),
#     dont l'arr a lu les pistes audio (mediaInfo.audioLanguages) ;
#   - indice seulement : les releases MULTi récentes de Nyaa (une recherche
#     Prowlarr) et, avec --pages, la description de leur page Nyaa.
# N'écrit rien nulle part : la décision et l'édition du JSON restent à faire
# après accord de l'utilisateur (voir SKILL.md).
#
#   python3 .claude/skills/multi-groupes/multi_groupes.py [--pages] [--json]
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
CF_NAME = "Langue : MULTi (groupe vérifié)"

# Seuil pour proposer un AJOUT : assez de fichiers, sur plusieurs œuvres (un
# groupe peut sortir une série avec VF et une autre sans), et aucun sans FR.
MIN_FILES = 5
MIN_TITLES = 2
FR_AUDIO = {"fre", "fra", "fr", "french"}
# Catégorie Newznab « TV/Anime », côté Prowlarr.
TV_ANIME_CATEGORY = 5070

# Groupes exclus, avec leur mesure. Mémoire OBLIGATOIRE et pas une déduction :
# un fichier sans FR finit remplacé, et sa preuve disparaît alors de la
# bibliothèque (VARYG ressortait « 4 FR / 0 sans FR » le soir même). Y ajouter
# tout groupe dont un MULTi sans FR est constaté, AVANT de remplacer le fichier.
EXCLUDED_GROUPS = {
    "VARYG": "2026-10-07 : 9 MULTi sur 13 sans audio FR (Smoking Behind the "
             "Supermarket with You, jpn/eng/hin/tha), fichiers remplacés depuis",
}

spec = importlib.util.spec_from_file_location(
    "aao", os.path.join(REPO_ROOT, "scripts", "apply-arr-overrides.py"))
aao = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aao)


def load_cf():
    cfs = json.load(open(os.path.join(REPO_ROOT, "arr", "profiles", "custom-formats.json")))
    cf = next(c for c in cfs if c["name"] == CF_NAME)
    by_impl = {}
    for s in cf["specifications"]:
        by_impl.setdefault((s["implementation"], s["negate"]), s["value"])
    return (regex.compile(by_impl[("ReleaseTitleSpecification", False)], regex.I),
            regex.compile(by_impl[("ReleaseGroupSpecification", False)], regex.I),
            regex.compile(by_impl[("ReleaseTitleSpecification", True)], regex.I))


def has_fr_audio(media_info):
    langs = re.split(r"[/,\s]+", ((media_info or {}).get("audioLanguages") or "").lower())
    return bool(FR_AUDIO & set(langs))


def library_evidence(env, multi):
    """{groupe: {"fr": n, "sans_fr": n, "titres": set, "exemples_sans_fr": [...]}}"""
    out = collections.defaultdict(lambda: {"fr": 0, "sans_fr": 0, "titres": set(), "exemples_sans_fr": [],
                                           "noms": []})
    sources = (("Sonarr", aao.SONARR_CONTAINER, aao.SONARR_URL, env.get("SONARR_API_KEY"), "/series", "seriesId"),
               ("Radarr", aao.RADARR_CONTAINER, aao.RADARR_URL, env.get("RADARR_API_KEY"), "/movie", "movieId"))
    for label, ct, url, key, items_path, id_param in sources:
        if not key:
            print(f"note: clé {label} absente, fichiers {label} ignorés", file=sys.stderr)
            continue
        items = {i["id"]: i["title"] for i in aao.api_get(ct, url, key, items_path)}
        file_path = "/episodefile" if label == "Sonarr" else "/moviefile"
        for item_id, title in items.items():
            for f in aao.api_get(ct, url, key, f"{file_path}?{id_param}={item_id}"):
                name = f.get("sceneName") or os.path.basename(f.get("relativePath") or "")
                group = f.get("releaseGroup")
                if not group or not multi.search(name) or not (f.get("mediaInfo") or {}).get("audioLanguages"):
                    continue
                g = out[group]
                g["titres"].add(title)
                g["noms"].append(name)
                if has_fr_audio(f.get("mediaInfo")):
                    g["fr"] += 1
                else:
                    g["sans_fr"] += 1
                    g["exemples_sans_fr"].append(f"{name} (audio {f['mediaInfo']['audioLanguages']})")
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


def verify(env, lib, multi, whitelist, explicit_vf):
    """Après `make arr-overrides` : le CF tel que l'arr le calcule (.NET) doit
    coïncider avec la prédiction Python, sur des titres réels de chaque groupe
    connu. Un désaccord = groupe parsé autrement qu'on ne le croit, ou regex
    que les deux moteurs lisent différemment."""
    titles = [n for g in lib.values() for n in g["noms"][:8]]
    with ThreadPoolExecutor(6) as ex:
        parsed = list(ex.map(lambda t: (t, parse_any(env, t)), titles))
    n = hits = 0
    dis = []
    for t, p in parsed:
        if p is None:
            continue
        group, net_cfs = p
        n += 1
        py = bool(multi.search(t)) and bool(group and whitelist.search(group)) and not explicit_vf.search(t)
        net = CF_NAME in net_cfs
        hits += net
        if py != net:
            dis.append(f"{'.NET' if net else 'Python'} seul : {t} (groupe parsé {group!r})")
    print(f"titres rattachés : {n} | matchs .NET : {hits} | désaccords : {len(dis)}")
    for d in dis[:20]:
        print(f"  {d}")
    return 1 if dis else 0


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


def nyaa_candidates(env, multi, explicit_vf, pages):
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
        # Deux pages par groupe hors liste blanche suffisent pour un indice.
        _, whitelist, _ = load_cf()
        for g, items in out.items():
            if not whitelist.search(g) and g not in EXCLUDED_GROUPS:
                for c in items[:2]:
                    c["declaration"] = nyaa_declaration(c["page"])
    return out


def verdict(group, in_list, lib):
    if group in EXCLUDED_GROUPS:
        return "À RETIRER (exclu)" if in_list else "exclu"
    fr, sans = (lib["fr"], lib["sans_fr"]) if lib else (0, 0)
    titres = len(lib["titres"]) if lib else 0
    if in_list:
        if sans:
            return "À RETIRER"
        if fr < MIN_FILES or titres < MIN_TITLES:
            return "garder (preuve faible)"
        return "garder"
    if sans:
        return "exclu (MULTi sans FR constatée)"
    if fr >= MIN_FILES and titres >= MIN_TITLES:
        return "À AJOUTER"
    if fr:
        return "preuve insuffisante"
    return "candidat (aucun fichier)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", action="store_true", help="lit aussi la description des pages Nyaa (indice)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verifier", action="store_true",
                    help="compare le CF calculé par l'arr (.NET) à la prédiction Python, puis s'arrête")
    args = ap.parse_args()
    env = aao.load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    multi, whitelist, explicit_vf = load_cf()
    lib = library_evidence(env, multi)
    if args.verifier:
        return verify(env, lib, multi, whitelist, explicit_vf)
    cand = nyaa_candidates(env, multi, explicit_vf, args.pages)
    rows = []
    for g in sorted(set(lib) | set(cand), key=str.lower):
        in_list = bool(whitelist.search(g))
        l = lib.get(g)
        rows.append({
            "groupe": g, "liste_blanche": in_list, "verdict": verdict(g, in_list, l),
            "fichiers_fr": l["fr"] if l else 0, "fichiers_sans_fr": l["sans_fr"] if l else 0,
            "oeuvres": sorted(l["titres"]) if l else [],
            "exemples_sans_fr": l["exemples_sans_fr"][:3] if l else [],
            "nyaa_recent": len(cand.get(g, [])),
            "nyaa_declarations": dict(collections.Counter(c["declaration"] for c in cand.get(g, []) if c["declaration"])),
            "nyaa_exemple": (cand.get(g) or [{}])[0].get("titre"),
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print(f"liste blanche actuelle : {whitelist.pattern}")
    print(f"seuil d'ajout : ≥ {MIN_FILES} fichiers MULTi avec audio FR, sur ≥ {MIN_TITLES} œuvres, 0 sans FR\n")
    for r in rows:
        print(f"[{r['verdict']}] {r['groupe']}{' (liste blanche)' if r['liste_blanche'] else ''} — "
              f"fichiers {r['fichiers_fr']} FR / {r['fichiers_sans_fr']} sans FR sur {len(r['oeuvres'])} œuvre(s) ; "
              f"Nyaa récent : {r['nyaa_recent']} {r['nyaa_declarations'] or ''}")
        if r["groupe"] in EXCLUDED_GROUPS:
            print(f"    exclusion : {EXCLUDED_GROUPS[r['groupe']]}")
        for e in r["exemples_sans_fr"]:
            print(f"    sans FR : {e}")
        if r["nyaa_exemple"] and not r["liste_blanche"]:
            print(f"    ex. Nyaa : {r['nyaa_exemple']}")


if __name__ == "__main__":
    sys.exit(main())
