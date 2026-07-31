# Point d'entrée unique de l'image clearr — trois sous-commandes partageant
# le même core.py (voir core.py pour le pourquoi) :
#   serve            service web (uvicorn), lancé en continu par
#                     `make up STACK=arr` (arr/docker-compose.yml)
#   tui              TUI interactive, `make clearr`
#   delete-by-inode  mode non-interactif, utilisé par le skill anime-vf
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(prog="clearr")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="service web (uvicorn)")
    sub.add_parser("tui", help="TUI interactive")

    p_inode = sub.add_parser("delete-by-inode", help="supprime un torrent par (dev, inode) — skill anime-vf")
    p_inode.add_argument("dev", type=int)
    p_inode.add_argument("ino", type=int)
    p_inode.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn
        uvicorn.run("app.webapp:app", host="0.0.0.0", port=8000)
    elif args.command == "tui":
        from . import tui
        tui.run()
    elif args.command == "delete-by-inode":
        from . import cli, core
        try:
            cli.delete_by_inode(args.dev, args.ino, args.dry_run)
        except RuntimeError as e:
            core.logger.error("delete-by-inode: %s", e)
            print(f"Erreur : {e} (voir {core.LOG_PATH})", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
