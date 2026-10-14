#!/usr/bin/env python3
# Inventaire, en LECTURE SEULE, des séries/animés (Sonarr) et films (Radarr)
# qui n'ont AUCUN fichier en bibliothèque, débarrassé de ce qui n'est pas
# encore sorti ou sorti depuis moins de GRACE_DAYS. Le rapport ne garde que
# les titres suivis (ceux que rien ne suit sont un choix, hors sujet) ; --json
# garde toutes les catégories. N'envoie aucune recherche et ne change aucun
# suivi : les actions restent à proposer (voir SKILL.md).
#
#   python3 .claude/skills/sans-fichier/sans_fichier.py [--json] [--grace N]
import argparse
import datetime
import importlib.util
import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Délai laissé aux groupes pour sortir une version FR après la diffusion ou la
# sortie numérique : en deçà, un titre sans fichier n'est pas encore anormal
# (le délai de grab de 24 h et l'attente d'une VF suffisent à l'expliquer).
GRACE_DAYS = 14

# Mêmes accès API que search-missing.py (clé par STDIN, docker exec), et son
# fichier d'état donne la date de dernière recherche automatique par item.
spec = importlib.util.spec_from_file_location(
    "sm", os.path.join(REPO_ROOT, "scripts", "search-missing.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)


def parse_date(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def day(value):
    return value.date().isoformat() if value else ""


def last_search(state, ids):
    dates = [state[str(i)] for i in ids if str(i) in state]
    return max(dates) if dates else ""


def series_items(api, state, now):
    in_queue = {q.get("seriesId") for q in
                sm.api_get(*api, "/queue?pageSize=1000&includeUnknownSeriesItems=true")["records"]}
    items = []
    for s in sm.api_get(*api, "/series"):
        if s.get("statistics", {}).get("episodeFileCount", 0) > 0:
            continue
        # Les spéciaux (saison 0) ne disent rien de la sortie d'une série.
        episodes = [e for e in sm.api_get(*api, f"/episode?seriesId={s['id']}")
                    if e["seasonNumber"] > 0]
        aired = [e for e in episodes if e.get("airDateUtc") and parse_date(e["airDateUtc"]) <= now]
        future = [e for e in episodes if e not in aired]
        monitored_seasons = {x["seasonNumber"] for x in s["seasons"]
                             if x["monitored"] and x["seasonNumber"] > 0}
        wanted = [e for e in aired if e["monitored"] and s["monitored"]]
        next_air = min((parse_date(e["airDateUtc"]) for e in future if e.get("airDateUtc")), default=None)
        item = {
            "kind": "anime" if s["seriesType"] == "anime" else "série",
            "title": s["title"], "year": s.get("year"),
            "aired": len(aired), "wanted": len(wanted),
            "last_search": last_search(state, [e["id"] for e in wanted]),
        }
        if s["id"] in in_queue:
            item.update(category="en file", date="")
        elif wanted:
            # Date de référence : le plus ancien épisode voulu. Un seul épisode
            # sorti depuis plus de GRACE_DAYS suffit à rendre le manque anormal.
            item.update(category="cherché", date=day(min(parse_date(e["airDateUtc"]) for e in wanted)))
        elif s["monitored"] and any(e["seasonNumber"] in monitored_seasons for e in future):
            # Une saison suivie attend sa diffusion : la suite n'est pas sortie.
            item.update(category="pas sorti", date=day(next_air) or "date inconnue")
        elif aired:
            item.update(category="rien de suivi", date=day(min(parse_date(e["airDateUtc"]) for e in aired)))
        else:
            item.update(category="pas sorti", date=day(next_air) or "date inconnue")
        items.append(item)
    return items


def movie_release(movie):
    """Date de sortie utile = première sortie numérique ou physique. Sans elle,
    on s'en remet au statut Radarr (« released » sans date = sortie ancienne,
    ex. un documentaire de 1997). Une sortie en salle seule ne compte pas :
    il n'en existe que des CAM, que les profils refusent."""
    dates = [parse_date(movie.get(k)) for k in ("digitalRelease", "physicalRelease")]
    dates = [d for d in dates if d]
    if dates:
        return min(dates)
    if movie.get("status") == "released":
        return parse_date(movie.get("inCinemas")) or datetime.datetime(
            movie.get("year") or 1900, 1, 1, tzinfo=datetime.timezone.utc)
    return None


def movie_items(api, state, now):
    in_queue = {q.get("movieId") for q in
                sm.api_get(*api, "/queue?pageSize=1000&includeUnknownMovieItems=true")["records"]}
    items = []
    for m in sm.api_get(*api, "/movie"):
        if m.get("hasFile"):
            continue
        release = movie_release(m)
        item = {"kind": "film", "title": m["title"], "year": m.get("year"),
                "last_search": last_search(state, [m["id"]])}
        if m["id"] in in_queue:
            item.update(category="en file", date="")
        elif not release or release > now:
            item.update(category="pas sorti",
                        date=day(release) or (f"en salle {day(parse_date(m.get('inCinemas')))}"
                                              if m.get("inCinemas") else "date inconnue"))
        elif not m["monitored"]:
            item.update(category="rien de suivi", date=day(release))
        else:
            item.update(category="cherché", date=day(release))
        items.append(item)
    return items


def main():
    parser = argparse.ArgumentParser(description="Séries, animés et films sans aucun fichier, déjà sortis.")
    parser.add_argument("--json", action="store_true", help="sortie brute, toutes catégories")
    parser.add_argument("--grace", type=int, default=GRACE_DAYS,
                        help=f"jours depuis la sortie en deçà desquels un titre est écarté (défaut : {GRACE_DAYS})")
    args = parser.parse_args()

    arr_env = sm.load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    state = sm.load_state(sm.load_env_file(os.path.join(REPO_ROOT, ".env.shared")).get("DATA_ROOT"))
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=args.grace)).date().isoformat()

    items, errors = [], []
    for name, container, url, key, collect in (
        ("sonarr", sm.SONARR_CONTAINER, sm.SONARR_URL, "SONARR_API_KEY", series_items),
        ("radarr", sm.RADARR_CONTAINER, sm.RADARR_URL, "RADARR_API_KEY", movie_items),
    ):
        try:
            items += collect((container, url, arr_env[key]), state.get(name, {}), now)
        except (KeyError, RuntimeError) as e:
            errors.append(f"{name} : {e}")

    # Un titre sorti depuis moins de --grace jours est écarté comme un titre à venir.
    for item in items:
        if item["category"] in ("cherché", "rien de suivi") and item["date"] > cutoff:
            item["category"] = "trop récent"

    if args.json:
        print(json.dumps({"grace_days": args.grace, "items": items, "errors": errors},
                         ensure_ascii=False, indent=1))
        return 1 if errors else 0

    sections = (
        ("cherché", "Suivis, sortis, aucun fichier trouvé"),
        ("en file", "Dans la file de téléchargement (voir skill manual-import)"),
    )
    for category, heading in sections:
        rows = [i for i in items if i["category"] == category]
        if not rows:
            continue
        print(f"## {heading} ({len(rows)})")
        for i in sorted(rows, key=lambda i: (i["kind"], i["date"])):
            extra = f", {i['wanted']}/{i['aired']} ép. voulus" if i["kind"] != "film" else ""
            searched = f", dernière recherche auto {i['last_search']}" if i["last_search"] else ""
            title = i["title"] if i["title"].endswith(f"({i['year']})") else f"{i['title']} ({i['year']})"
            print(f"  [{i['kind']}] {title} — sorti {i['date']}{extra}{searched}")
        print()
    for category, label in (("pas sorti", "pas encore sortis"),
                            ("trop récent", f"sortis depuis moins de {args.grace} j")):
        rows = [i for i in items if i["category"] == category]
        if rows:
            print(f"Écartés, {label} ({len(rows)}) : "
                  + ", ".join(f"{i['title']} ({i['date']})" for i in rows))
    for e in errors:
        print(f"ERREUR {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
