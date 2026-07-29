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
# manuelles. Ne touche qu'aux profils "film"/"série" — les profils
# Anime (Fansub)* sont hors périmètre (usage personnel, voir CLAUDE.md).
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SONARR_CONTAINER = "arr-sonarr-1"
SONARR_URL = "http://localhost:8989/api/v3"
RADARR_CONTAINER = "arr-radarr-1"
RADARR_URL = "http://localhost:7878/api/v3"

RADARR_PROFILE_NAME = "[SQP] SQP-1 WEB (2160p)"

SONARR_SIZE_OVERRIDES = {
    "WEBRip-2160p": {"maxSize": 100, "preferredSize": 85},
    "WEBDL-2160p": {"maxSize": 100, "preferredSize": 85},
}

RADARR_SIZE_OVERRIDES = {
    "WEBDL-1080p": {"minSize": 10, "preferredSize": 30, "maxSize": 45},
    "WEBRip-1080p": {"minSize": 10, "preferredSize": 30, "maxSize": 45},
    "Bluray-1080p": {"minSize": 18, "preferredSize": 40, "maxSize": 60},
    "WEBDL-2160p": {"minSize": 25, "preferredSize": 65, "maxSize": 100},
    "WEBRip-2160p": {"minSize": 25, "preferredSize": 65, "maxSize": 100},
    "Bluray-2160p": {"minSize": 40, "preferredSize": 75, "maxSize": 120},
}


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


def api_get(container, base_url, api_key, path):
    cmd = ["docker", "exec", container, "curl", "-s",
           "-H", f"X-Api-Key: {api_key}", f"{base_url}{path}"]
    res = subprocess.run(cmd, capture_output=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")
    return json.loads(res.stdout)


def api_put(container, base_url, api_key, path, obj):
    cmd = ["docker", "exec", "-i", container, "curl", "-s", "-X", "PUT",
           "-H", f"X-Api-Key: {api_key}", "-H", "Content-Type: application/json",
           "--data", "@-", f"{base_url}{path}"]
    res = subprocess.run(cmd, input=json.dumps(obj).encode(), capture_output=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")


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


def main():
    arr_env = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    sonarr_api_key = arr_env.get("SONARR_API_KEY")
    radarr_api_key = arr_env.get("RADARR_API_KEY")

    changed = []
    errors = []
    try:
        changed += apply_quality_sizes("Sonarr", SONARR_CONTAINER, SONARR_URL,
                                        sonarr_api_key, SONARR_SIZE_OVERRIDES)
    except Exception as e:
        errors.append(f"Sonarr: {e}")
    try:
        changed += apply_quality_sizes("Radarr", RADARR_CONTAINER, RADARR_URL,
                                        radarr_api_key, RADARR_SIZE_OVERRIDES)
        changed += apply_radarr_language(RADARR_CONTAINER, RADARR_URL, radarr_api_key, RADARR_PROFILE_NAME)
    except Exception as e:
        errors.append(f"Radarr: {e}")

    for line in changed:
        print(f"corrigé: {line}")
    if not changed and not errors:
        print("déjà à jour, rien à faire")
    for err in errors:
        print(f"erreur: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
