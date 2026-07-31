# Mode non-interactif utilisé par le skill anime-vf
# (.claude/skills/anime-vf/SKILL.md) : après qu'une nouvelle release ait
# remplacé un fichier library/ existant, retrouve et supprime le torrent de
# l'ANCIENNE version par inode. L'appelant doit avoir capturé ce (dev, inode)
# AVANT l'import — une fois la nouvelle release importée, Sonarr/Radarr a pu
# déjà supprimer ce chemin côté library/, un stat a posteriori échouerait.
# Ne touche PAS au monitoring Sonarr/Radarr (contrairement à une suppression
# dans la TUI/le web, voir core.plan_sonarr_unmonitor) : l'épisode reste
# surveillé, on vient de le remplacer par une meilleure release, pas de le
# retirer. Affiche un JSON sur stdout pour que l'appelant (Claude) parse le
# résultat sans dépendre du log.
import json

from . import core


def delete_by_inode(dev, ino, dry_run):
    client = core.TransmissionClient()
    torrent = core.find_torrent_by_inode(client, dev, ino)
    if not torrent:
        print(json.dumps({"found": False, "deleted": False, "torrent": None}))
        return
    if dry_run:
        print(json.dumps({"found": True, "deleted": False, "torrent": torrent["name"]}))
        return
    client.remove_torrent(torrent["id"])
    core.logger.info("delete-by-inode: torrent %r (id=%s) supprimé (remplacé par une nouvelle release)",
                      torrent["name"], torrent["id"])
    print(json.dumps({"found": True, "deleted": True, "torrent": torrent["name"]}))
