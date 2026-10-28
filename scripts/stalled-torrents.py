#!/usr/bin/env python3
# Repère les torrents grabés par Sonarr/Radarr qui ne progressent plus, et
# (avec --fix) les fait retirer par leur arr pour qu'il cherche une autre
# release.
#
# Pourquoi ce script existe : un torrent mort ne passe JAMAIS en échec côté
# arr. Sonarr/Radarr ne regardent que l'état que Transmission leur rapporte
# (« downloading ») : un tracker qui répond 403, une release sans seeder ou
# des métadonnées jamais reçues laissent l'entrée « en téléchargement »
# indéfiniment, et l'épisode n'est jamais recherché ailleurs (search-missing
# saute ce qui est déjà en file). `minimumSeeders: 1` n'y change rien : il
# n'est vérifié qu'au grab. Vécu le 2026-10-28 sur une autre instance : 3
# releases C411 en 403 (0 à 6 jours), une release Astra sans seeder bloquée
# 18 jours.
#
# Bloqué = percentDone < 1, ajouté il y a plus de STALL_HOURS ET aucune
# activité depuis STALL_HOURS. Le motif affiché (erreur tracker, 0 seeder,
# métadonnées absentes) est informatif : le critère est l'inactivité seule.
#
# --fix ne retire QUE ce qui est sûr (voir action_for()) :
#   - jamais un torrent hors file arr : on ne sait pas qui l'a ajouté ;
#   - jamais un torrent qui a déjà des données, il peut reprendre ;
#   - jamais un torrent en pause ou en attente dans la file Transmission :
#     l'inactivité y est voulue, pas subie.
#   - jamais pendant une coupure réseau, ni dans les 24 h qui suivent son
#     retour (voir network_health()).
# Le retrait passe par l'arr (DELETE /queue/bulk, removeFromClient +
# blocklist + nouvelle recherche), jamais par Transmission directement : sinon
# l'arr verrait juste son téléchargement disparaître, sans blocklist ni
# recherche.
#
# Sortie : une ligne par torrent bloqué, et l'état complet dans
# DATA_ROOT/.stalled-torrents.json (pour une éventuelle carte du dashboard).
# Code de retour non nul sur toute erreur (RPC, arr, retrait), pour que la
# chaîne `&&` de cron n'écrive pas son marqueur de succès. Des torrents
# bloqués sans erreur ne sont PAS un échec de la tâche.
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TRANSMISSION_CONTAINER = "vpn-transmission-vpn-1"
RPC_URL = "http://localhost:9091/transmission/rpc"

STALL_HOURS = 24
STATE_FILE_NAME = ".stalled-torrents.json"

# Réseau jugé vivant si au moins un tracker a répondu à une annonce depuis ce
# délai. Avec ~300 torrents en seed, la plus récente date de moins d'une
# minute (relevé du 2026-10-28) : 2 h ne laisse aucun doute.
NETWORK_PROBE_SECONDS = 2 * 3600

# (nom, conteneur, URL, clé dans arr/.env, paramètre « entrées orphelines ») —
# ce dernier n'a pas le même nom d'un arr à l'autre, et sans lui une entrée
# dont le titre a quitté le catalogue est invisible (voir
# .claude/docs/traefik-dashboard.md, colonne Imports).
ARRS = (
    ("sonarr", "arr-sonarr-1", "http://localhost:8989/api/v3", "SONARR_API_KEY",
     "includeUnknownSeriesItems=true"),
    ("radarr", "arr-radarr-1", "http://localhost:7878/api/v3", "RADARR_API_KEY",
     "includeUnknownMovieItems=true"),
)

# Spec RPC Transmission : status 0 = en pause, 3 = en attente de
# téléchargement (file de Transmission pleine) ; error 1/2 = avertissement /
# erreur du tracker, 3 = erreur locale.
STATUS_STOPPED = 0
STATUS_DOWNLOAD_WAIT = 3
ERROR_LOCAL = 3

TORRENT_FIELDS = ["hashString", "name", "status", "percentDone", "haveUnchecked",
                  "addedDate", "activityDate", "metadataPercentComplete",
                  "peersSendingToUs", "trackerStats", "error", "errorString"]


def load_env_file(path):
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key] = value
    except FileNotFoundError:
        pass
    return values


# --- Transmission : même mécanisme que scripts/transmission-stats.py,
# dupliqué (chaque script du repo est autonome). Tout correctif vaut pour les
# deux.

class RPCError(RuntimeError):
    pass


def _rpc_once(method, arguments, session_id):
    cmd = ["docker", "exec", "-i", TRANSMISSION_CONTAINER, "curl", "-s", "-i",
           "-X", "POST", "-d", "@-", RPC_URL]
    if session_id:
        cmd[6:6] = ["-H", f"X-Transmission-Session-Id: {session_id}"]
    payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
    try:
        res = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RPCError(f"Transmission RPC timeout sur {method}")
    if res.returncode != 0:
        raise RPCError(f"docker exec {TRANSMISSION_CONTAINER} a échoué — container arrêté ?")
    text = res.stdout.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    return head.split("\r\n", 1)[0], head, body


def rpc(method, arguments=None):
    """CSRF Transmission : premier appel sans session-id -> 409 avec le vrai
    id dans l'en-tête, on rejoue une fois avec."""
    status_line, head, body = _rpc_once(method, arguments, None)
    if " 409 " in status_line:
        session_id = None
        for line in head.split("\r\n"):
            if line.lower().startswith("x-transmission-session-id:"):
                session_id = line.split(":", 1)[1].strip()
        status_line, head, body = _rpc_once(method, arguments, session_id)
    if not body.strip():
        raise RPCError(f"réponse Transmission vide (statut: {status_line})")
    data = json.loads(body)
    if data.get("result") != "success":
        raise RPCError(f"Transmission RPC error: {data}")
    return data["arguments"]


# --- arr : même mécanisme que scripts/search-missing.py (clé API par STDIN,
# jamais en argument : l'argv d'un `docker exec` est lisible dans `ps`).

CURL_WRITE_OUT = "\n%{http_code}"


def _curl(container, api_key, args, stdin=None):
    quoted = " ".join(shlex.quote(a) for a in args)
    script = f'IFS= read -r k; exec curl -s -w {shlex.quote(CURL_WRITE_OUT)} -H "X-Api-Key: $k" {quoted}'
    payload = api_key.encode() + b"\n" + (stdin or b"")
    res = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                         input=payload, capture_output=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")
    body, _, code = res.stdout.decode().rpartition("\n")
    return code.strip(), body.strip()


def api_get(container, base_url, api_key, path):
    code, body = _curl(container, api_key, [f"{base_url}{path}"])
    if not code.startswith("2"):
        raise RuntimeError(f"GET {path} : HTTP {code} — {body[:200] or 'réponse vide'}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"GET {path} : réponse illisible — {body[:200]!r}")


def remove_from_queue(container, base_url, api_key, queue_ids):
    """Retire de la file, du client, blocklist la release et relance la
    recherche (skipRedownload=false) — tout en un appel, côté arr."""
    path = "/queue/bulk?removeFromClient=true&blocklist=true&skipRedownload=false"
    code, body = _curl(container, api_key,
                       ["-X", "DELETE", "-H", "Content-Type: application/json",
                        "--data", "@-", f"{base_url}{path}"],
                       stdin=json.dumps({"ids": sorted(queue_ids)}).encode())
    if not code.startswith("2"):
        raise RuntimeError(f"DELETE /queue/bulk : HTTP {code} — {body[:200] or 'réponse vide'}")


def arr_queues(arr_env):
    """Renvoie (hash en majuscules -> (arr, [ids de file], titre), erreurs).
    Un pack de saison a une entrée de file par épisode, d'où la liste d'ids.
    Une entrée sans downloadId (en attente du délai de 24 h) n'est pas
    encore chez Transmission : ignorée."""
    by_hash, errors = {}, []
    for name, container, base_url, key_name, unknown_param in ARRS:
        api_key = arr_env.get(key_name)
        if not api_key:
            errors.append(f"{name} : {key_name} absent de arr/.env (voir `make api-keys`)")
            continue
        try:
            queue = api_get(container, base_url, api_key,
                            f"/queue?page=1&pageSize=1000&{unknown_param}")
        except Exception as e:
            errors.append(f"{name} : {e}")
            continue
        for record in queue.get("records", []):
            download_id = (record.get("downloadId") or "").upper()
            if not download_id:
                continue
            entry = by_hash.setdefault(download_id, (name, [], record.get("title")))
            entry[1].append(record["id"])
    return by_hash, errors


def is_stalled(torrent, now):
    if torrent.get("percentDone", 0) >= 1:
        return False
    added = torrent.get("addedDate") or now
    # activityDate = 0 : aucune activité depuis l'ajout.
    last_activity = torrent.get("activityDate") or added
    threshold = STALL_HOURS * 3600
    return now - added >= threshold and now - last_activity >= threshold


def stall_reasons(torrent):
    reasons = []
    if torrent.get("status") == STATUS_STOPPED:
        reasons.append("en pause")
    if torrent.get("metadataPercentComplete", 1) < 1:
        reasons.append("métadonnées absentes")
    stats = torrent.get("trackerStats") or []
    tracker_errors = sorted({s.get("lastAnnounceResult") for s in stats
                             if s.get("hasAnnounced") and not s.get("lastAnnounceSucceeded")
                             and s.get("lastAnnounceResult")})
    reasons += [f"tracker : {e}" for e in tracker_errors]
    # seederCount vaut -1 quand le tracker ne l'a pas donné : on ne conclut
    # « 0 seeder » que sur des annonces réussies.
    seeders = [s.get("seederCount", -1) for s in stats if s.get("lastAnnounceSucceeded")]
    if seeders and max(seeders) == 0:
        reasons.append("0 seeder")
    if torrent.get("error") == ERROR_LOCAL:
        reasons.append(f"erreur locale : {torrent.get('errorString')}")
    if not reasons and not torrent.get("peersSendingToUs"):
        reasons.append("aucun pair ne l'envoie")
    return reasons or ["aucune activité"]


def network_health(torrents, seconds_active, previous_down_at, now):
    """Renvoie (réseau vivant, dernière coupure observée, réseau sain depuis).

    Pourquoi : une coupure (VPN tombé, box hors ligne) fige TOUS les torrents
    à 0 % au même titre qu'un tracker mort. Sans ce garde-fou, une coupure de
    plus de 24 h faisait blocklister des releases saines ; et si seul le VPN
    tombe, Sonarr/Radarr (qui sortent en direct, pas par le VPN) regrabent
    aussitôt une release elle aussi figée, retirée 24 h plus tard : une
    release saine brûlée par titre et par jour de panne.

    D'où « réseau sain depuis » = la plus tardive de la dernière coupure vue
    par ce cron (mémorisée dans le fichier d'état, le cron étant horaire) et
    du démarrage de la session Transmission (un serveur éteint 2 jours ne
    voit aucune coupure, mais ses torrents n'ont rien pu recevoir). Un retrait
    exige STALL_HOURS de réseau sain : tester seulement « le réseau marche
    maintenant » retirerait un torrent juste après une coupure de 23 h."""
    alive = any(s.get("lastAnnounceSucceeded") and now - s.get("lastAnnounceTime", 0) <= NETWORK_PROBE_SECONDS
                for t in torrents for s in t.get("trackerStats") or [])
    down_at = now if not alive else previous_down_at
    healthy_since = max(x for x in (down_at, now - seconds_active) if x is not None)
    return alive, down_at, healthy_since


def has_data(torrent):
    # haveUnchecked : octets reçus pas encore vérifiés, absents de percentDone.
    return torrent.get("percentDone", 0) > 0 or torrent.get("haveUnchecked", 0) > 0


def action_for(torrent, queued, arr_errors, healthy_for):
    """Renvoie (retirable, libellé). L'ordre compte : hors arr d'abord, c'est
    la seule règle qui protège un torrent ajouté à la main. `healthy_for` :
    secondes de réseau sain (voir network_health())."""
    if not queued:
        if arr_errors:
            return False, "signalé (arr injoignable, rattachement inconnu)"
        return False, "signalé (hors file arr)"
    if has_data(torrent):
        return False, "conservé (données partielles, peut reprendre)"
    if torrent.get("status") == STATUS_STOPPED:
        return False, "conservé (en pause dans Transmission)"
    if torrent.get("status") == STATUS_DOWNLOAD_WAIT:
        return False, "conservé (en attente dans la file Transmission)"
    if healthy_for < STALL_HOURS * 3600:
        return False, (f"conservé (réseau sain depuis {human_duration(healthy_for)} seulement, "
                       f"{STALL_HOURS} h requises)")
    return True, "à retirer (--fix)"


def human_duration(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = [p for p in (f"{days}j" if days else "", f"{hours}h" if hours else "",
                          f"{minutes}min" if not days and minutes else "") if p]
    return " ".join(parts) if parts else "< 1min"


def load_previous_state(data_root):
    """L'état précédent ne sert qu'à se souvenir de la dernière coupure : un
    fichier absent ou illisible repart de rien, la session Transmission borne
    alors seule (voir network_health())."""
    if not data_root:
        return {}
    try:
        with open(os.path.join(data_root, STATE_FILE_NAME)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(data_root, state):
    """Écriture atomique (mkstemp + os.replace) : le dashboard peut lire le
    fichier pendant qu'on l'écrit."""
    if not data_root:
        return
    dest = os.path.join(data_root, STATE_FILE_NAME)
    fd, tmp = tempfile.mkstemp(dir=data_root, prefix=f"{STATE_FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main():
    parser = argparse.ArgumentParser(description="Repère les torrents arr qui ne progressent plus.")
    parser.add_argument("--fix", action="store_true",
                        help="fait retirer par leur arr (blocklist + nouvelle recherche) les torrents "
                             "bloqués sans aucune donnée")
    args = parser.parse_args()

    now = time.time()
    print(f"=== {datetime.datetime.now().isoformat(timespec='seconds')} ===")
    arr_env = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    data_root = load_env_file(os.path.join(REPO_ROOT, ".env.shared")).get("DATA_ROOT")
    previous_down_at = load_previous_state(data_root).get("network_down_at")
    state = {"generated_at": int(now), "stall_hours": STALL_HOURS, "fix": args.fix,
             "network_down_at": previous_down_at, "torrents": [], "errors": []}

    try:
        torrents = rpc("torrent-get", {"fields": TORRENT_FIELDS})["torrents"]
        seconds_active = rpc("session-stats")["current-stats"]["secondsActive"]
    except Exception as e:
        state["errors"].append(f"transmission : {e}")
        torrents = []
    stalled = [t for t in torrents if is_stalled(t, now)]

    # Sans liste de torrents (RPC en échec, ou aucun torrent), rien n'est
    # bloqué : pas de sonde, la dernière coupure connue est conservée.
    healthy_for = 0
    if torrents:
        alive, down_at, healthy_since = network_health(torrents, seconds_active, previous_down_at, now)
        healthy_for = now - healthy_since
        state["network_down_at"] = int(down_at) if down_at is not None else None
        state["network_healthy_since"] = int(healthy_since)
        if not alive:
            # Erreur volontaire : le marqueur cron n'est pas écrit et la tâche
            # passe au rouge sur le dashboard tant que dure la coupure.
            state["errors"].append(f"réseau : aucune annonce tracker réussie depuis "
                                   f"{NETWORK_PROBE_SECONDS // 3600} h (VPN ou connexion coupé) — "
                                   f"aucun retrait jusqu'à {STALL_HOURS} h après son retour")

    by_hash, arr_errors = arr_queues(arr_env) if stalled else ({}, [])
    state["errors"] += arr_errors
    arr_by_name = {a[0]: a for a in ARRS}

    for t in sorted(stalled, key=lambda t: t.get("addedDate", 0)):
        queued = by_hash.get(t["hashString"].upper())
        arr_name = queued[0] if queued else None
        removable, action = action_for(t, queued, arr_errors, healthy_for)
        if removable and args.fix:
            _, container, base_url, key_name, _ = arr_by_name[arr_name]
            try:
                remove_from_queue(container, base_url, arr_env[key_name], queued[1])
                action = f"retiré ({len(queued[1])} entrée(s) de file), blocklisté, recherche relancée"
            except Exception as e:
                action = "échec du retrait"
                state["errors"].append(f"{arr_name} : {t['name']} : {e}")
        last_activity = t.get("activityDate") or t.get("addedDate") or now
        entry = {
            "hash": t["hashString"],
            "name": t["name"],
            "arr": arr_name,
            "age_seconds": int(now - t.get("addedDate", now)),
            "idle_seconds": int(now - last_activity),
            "percent_done": t.get("percentDone", 0),
            "reasons": stall_reasons(t),
            "removable": removable,
            "action": action,
        }
        state["torrents"].append(entry)
        print(f"[{arr_name or '—'}] {entry['name']} — ajouté il y a {human_duration(entry['age_seconds'])}, "
              f"inactif depuis {human_duration(entry['idle_seconds'])}, {entry['percent_done']:.1%} — "
              f"{' ; '.join(entry['reasons'])} — {action}")

    if not stalled and not state["errors"]:
        print(f"aucun torrent bloqué ({len(torrents)} torrent(s), seuil {STALL_HOURS} h)")

    try:
        write_state(data_root, state)
    except OSError as e:
        state["errors"].append(f"écriture de {STATE_FILE_NAME} : {e}")

    for error in state["errors"]:
        print(f"ERREUR {error}", file=sys.stderr)
    return 1 if state["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
